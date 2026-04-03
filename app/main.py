import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import random
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlencode, urljoin
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, text
from sqlalchemy.orm import Session, aliased
from starlette.requests import Request as StarletteRequest

from app.config import settings
from app.db import SessionLocal, get_db
from app.models import (
    AppSettings,
    PairwiseVote,
    RatingScore,
    RatingScoreSnapshot,
    Song,
    User,
    UserIdentity,
    UserSession,
    UserSkippedSong,
    YouTubeLookupCache,
)

DEFAULT_RATING = 1000
ELO_K = 24
DEFAULT_POPULARITY_WEIGHT = 0.35
POPULARITY_RATING_COUNT_CAP = 500
POPULARITY_USER_RATING_MAX = 10.0
POPULARITY_COUNT_WEIGHT = 0.75
POPULARITY_USER_RATING_WEIGHT = 0.25
SESSION_COOKIE_NAME = "songranker_session"
OIDC_LOGIN_COOKIE_NAME = "songranker_oidc_login"
OIDC_LOGIN_TTL_SECONDS = 600
LOG_FILE_PATH = Path("/log.log")
logger = logging.getLogger(__name__)
YOUTUBE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
_youtube_lookup_cache_lock = threading.Lock()
_youtube_lookup_cache: dict[tuple[str, str], dict[str, Any]] = {}
ALLOWED_AVATAR_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
AVATAR_EXTENSION_BY_MIME = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
MAX_AVATAR_SIZE_BYTES = 2 * 1024 * 1024
AVATAR_UPLOAD_DIR = Path("app/static/uploads/avatars")
DEFAULT_AVATAR_URL = "/static/default-avatar.svg"

app = FastAPI(title="SongRanker")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def _configure_logging() -> None:
    LOG_FILE_PATH.write_text("", encoding="utf-8")

    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    file_handler = TimedRotatingFileHandler(
        filename=str(LOG_FILE_PATH),
        when="W0",
        interval=1,
        backupCount=8,
        encoding="utf-8",
        utc=True,
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True


class NextPairResponse(BaseModel):
    filters: dict[str, Any]
    pair: list[dict[str, Any]]


class VoteRequest(BaseModel):
    winner_song_id: int
    loser_song_id: int
    filters: dict[str, Any] = Field(default_factory=dict)


class VoteResponse(BaseModel):
    winner: dict[str, Any]
    loser: dict[str, Any]


class SkipSongsRequest(BaseModel):
    song_ids: list[int] = Field(default_factory=list)


class SkipArtistRequest(BaseModel):
    artist: str


class UnskipSelectedRequest(BaseModel):
    song_ids: list[int] = Field(default_factory=list)


class RankingsResponse(BaseModel):
    filters: dict[str, Any]
    sort_by: str
    sort_dir: str
    total: int
    rows: list[dict[str, Any]]


class VoteHistoryResponse(BaseModel):
    filters: dict[str, Any]
    page: int
    page_size: int
    total: int
    rows: list[dict[str, Any]]


class SongHistoryResponse(BaseModel):
    song: dict[str, Any]
    current_score: int
    recent_matchups: list[dict[str, Any]]


class PlexSettingsUpdateRequest(BaseModel):
    plex_url: str
    plex_token: str


class PlexLibraryUpdateRequest(BaseModel):
    plex_music_section_id: str


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 600_000).hex()
    return f"{salt}:{digest}"


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, expected = stored_hash.split(":", 1)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 600_000).hex()
    return hmac.compare_digest(expected, actual)


def _get_or_create_settings(db: Session) -> AppSettings:
    app_settings = db.query(AppSettings).filter(AppSettings.id == 1).first()
    if app_settings is None:
        app_settings = AppSettings(id=1, is_initialized=False)
        db.add(app_settings)
        db.commit()
        db.refresh(app_settings)
    return app_settings


def _clamp_popularity_weight(raw_weight: float | None) -> float:
    if raw_weight is None:
        return DEFAULT_POPULARITY_WEIGHT
    return max(0.0, min(1.0, raw_weight))


def _current_session(db: Session, request: StarletteRequest) -> UserSession | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None

    now = datetime.now(timezone.utc)
    session = db.query(UserSession).filter(UserSession.session_token == token).first()
    if session is None:
        return None
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        db.delete(session)
        db.commit()
        return None
    return session


def _require_current_user(request: StarletteRequest, db: Session = Depends(get_db)) -> User:
    session = _current_session(db, request)
    if session is None:
        raise HTTPException(status_code=401, detail="Sign in required")

    user = db.query(User).filter(User.id == session.user_id).first()
    if user is None:
        raise HTTPException(status_code=401, detail="Session user does not exist")
    return user


def _create_session_for_user(db: Session, user_id: int) -> str:
    return _create_session_for_user_with_rotation(db=db, user_id=user_id, previous_token=None)


def _create_session_for_user_with_rotation(db: Session, user_id: int, previous_token: str | None) -> str:
    if previous_token:
        db.query(UserSession).filter(UserSession.session_token == previous_token).delete(synchronize_session=False)

    token = secrets.token_urlsafe(48)
    now = datetime.now(timezone.utc)
    db.add(
        UserSession(
            session_token=token,
            user_id=user_id,
            expires_at=now + timedelta(days=max(1, settings.session_ttl_days)),
        )
    )
    db.commit()
    return token


def _log_auth_event(
    event: str,
    *,
    outcome: str,
    user_id: int | None = None,
    username: str | None = None,
    provider: str | None = None,
    reason: str | None = None,
) -> None:
    level = logging.INFO if outcome == "success" else logging.WARNING
    logger.log(
        level,
        "auth_event=%s outcome=%s user_id=%s username=%s provider=%s reason=%s",
        event,
        outcome,
        user_id,
        username,
        provider,
        reason,
    )


def _oidc_signing_key() -> bytes:
    return settings.google_client_secret.encode("utf-8")


def _urlsafe_b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _urlsafe_b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _encode_signed_cookie(payload: dict[str, Any]) -> str:
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_encoded = _urlsafe_b64encode(payload_bytes)
    signature = hmac.new(_oidc_signing_key(), payload_encoded.encode("utf-8"), hashlib.sha256).digest()
    return f"{payload_encoded}.{_urlsafe_b64encode(signature)}"


def _decode_signed_cookie(raw_value: str | None) -> dict[str, Any] | None:
    if not raw_value or "." not in raw_value:
        return None

    payload_encoded, signature_encoded = raw_value.split(".", 1)
    expected_signature = hmac.new(_oidc_signing_key(), payload_encoded.encode("utf-8"), hashlib.sha256).digest()
    try:
        received_signature = _urlsafe_b64decode(signature_encoded)
    except Exception:
        return None
    if not hmac.compare_digest(expected_signature, received_signature):
        return None

    try:
        decoded = json.loads(_urlsafe_b64decode(payload_encoded))
    except Exception:
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def _fetch_json(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = urlencode(payload).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = Request(url, method=method, headers=headers, data=data)
    with urlopen(req, timeout=10) as resp:
        body = resp.read().decode("utf-8")
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("Expected JSON object response")
    return parsed


def _fetch_google_oidc_metadata() -> dict[str, Any]:
    return _fetch_json(settings.google_oidc_discovery_url)


def _decode_jwt_payload(jwt_token: str) -> dict[str, Any]:
    parts = jwt_token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT format")
    payload_raw = _urlsafe_b64decode(parts[1])
    payload = json.loads(payload_raw)
    if not isinstance(payload, dict):
        raise ValueError("Invalid JWT payload")
    return payload


def _normalize_email_verified(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    return False


def _derive_username_from_email(db: Session, email: str) -> str:
    local_part = email.split("@", 1)[0]
    normalized = re.sub(r"[^a-z0-9_]", "_", local_part.lower()).strip("_")
    base = normalized or "google_user"
    candidate = base[:64]
    suffix = 1
    while db.query(User.id).filter(User.username == candidate).first() is not None:
        suffix_text = f"_{suffix}"
        candidate = f"{base[: max(1, 64 - len(suffix_text))]}{suffix_text}"
        suffix += 1
    return candidate


def _build_avatar_url(current_user: User | None) -> str:
    if current_user is None or not current_user.profile_image_path:
        return DEFAULT_AVATAR_URL
    return f"/static/{current_user.profile_image_path}"


def _profile_photo_redirect(message: str, status: str = "error") -> RedirectResponse:
    encoded_message = quote(message)
    return RedirectResponse(url=f"/?avatar_status={status}&avatar_message={encoded_message}", status_code=303)


def _sniff_image_mime(data: bytes) -> str | None:
    if len(data) >= 8 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 3 and data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _session_redirect(url: str, token: str) -> RedirectResponse:
    response = RedirectResponse(url=url, status_code=303)
    cookie_kwargs: dict[str, Any] = {
        "key": SESSION_COOKIE_NAME,
        "value": token,
        "httponly": True,
        "samesite": settings.normalized_session_cookie_samesite,
        "secure": settings.session_cookie_secure,
        "max_age": max(1, settings.session_ttl_days) * 24 * 60 * 60,
    }
    if settings.normalized_session_cookie_domain:
        cookie_kwargs["domain"] = settings.normalized_session_cookie_domain
    response.set_cookie(**cookie_kwargs)
    return response


def _clear_session_cookie(response: RedirectResponse):
    if settings.normalized_session_cookie_domain:
        response.delete_cookie(SESSION_COOKIE_NAME, domain=settings.normalized_session_cookie_domain)
        return
    response.delete_cookie(SESSION_COOKIE_NAME)


def _plex_get_xml(plex_url: str, plex_token: str, path: str) -> ElementTree.Element:
    query = urlencode({"X-Plex-Token": plex_token})
    full_url = urljoin(plex_url.rstrip("/") + "/", path.lstrip("/"))
    if "?" in full_url:
        full_url = f"{full_url}&{query}"
    else:
        full_url = f"{full_url}?{query}"

    request = Request(full_url, headers={"Accept": "application/xml"})
    with urlopen(request, timeout=20) as response:
        payload = response.read()
    return ElementTree.fromstring(payload)


def _decade_for_year(year: int | None) -> str | None:
    if not year:
        return None
    return f"{(year // 10) * 10}s"


def _decade_bounds(decade: str | None) -> tuple[int, int] | None:
    cleaned = _normalize_decade(decade)
    if not cleaned:
        return None
    if len(cleaned) != 5 or not cleaned.endswith("s"):
        return None
    decade_start_raw = cleaned[:4]
    if not decade_start_raw.isdigit():
        return None
    decade_start = int(decade_start_raw)
    return decade_start, decade_start + 9


def _normalize_decade(decade: str | None) -> str | None:
    if not decade:
        return None
    cleaned = str(decade).strip().lower()
    if len(cleaned) == 4 and cleaned.isdigit():
        cleaned = f"{cleaned}s"
    if len(cleaned) == 5 and cleaned.endswith("s") and cleaned[:4].isdigit():
        return cleaned

    return None


def _sync_tracks_from_plex(db: Session, app_settings: AppSettings) -> dict[str, int]:
    if not app_settings.plex_url or not app_settings.plex_token or not app_settings.plex_music_section_id:
        raise HTTPException(status_code=400, detail="Plex settings are incomplete")

    logger.info("Starting Plex sync for section %s", app_settings.plex_music_section_id)
    imported = 0
    updated = 0
    page_size = 200
    start = 0
    album_year_cache: dict[str, int | None] = {}

    while True:
        root = _plex_get_xml(
            app_settings.plex_url,
            app_settings.plex_token,
            f"/library/sections/{app_settings.plex_music_section_id}/all?type=10&X-Plex-Container-Start={start}&X-Plex-Container-Size={page_size}",
        )
        tracks = root.findall("Track")
        if not tracks:
            break

        for track in tracks:
            rating_key = track.attrib.get("ratingKey")
            if not rating_key:
                continue

            title = track.attrib.get("title", "Unknown")
            artist = track.attrib.get("grandparentTitle") or track.attrib.get("originalTitle") or "Unknown"
            album = track.attrib.get("parentTitle")
            year_raw = track.attrib.get("year")
            year = int(year_raw) if year_raw and year_raw.isdigit() else None
            if year is None:
                parent_rating_key = track.attrib.get("parentRatingKey")
                if parent_rating_key:
                    if parent_rating_key in album_year_cache:
                        year = album_year_cache[parent_rating_key]
                    else:
                        album_year: int | None = None
                        try:
                            album_root = _plex_get_xml(
                                app_settings.plex_url,
                                app_settings.plex_token,
                                f"/library/metadata/{parent_rating_key}",
                            )
                            album_entry = album_root.find("Directory") or album_root.find("Video")
                            if album_entry is not None:
                                album_year_raw = album_entry.attrib.get("year")
                                album_year = int(album_year_raw) if album_year_raw and album_year_raw.isdigit() else None
                        except Exception:
                            logger.warning(
                                "Unable to resolve album metadata for parentRatingKey=%s during Plex sync",
                                parent_rating_key,
                            )
                        album_year_cache[parent_rating_key] = album_year
                        year = album_year
            source_uri = track.attrib.get("key")
            plex_user_rating = _parse_plex_user_rating(track.attrib.get("userRating"))
            plex_rating_count = _parse_plex_rating_count(track.attrib.get("ratingCount"))

            existing = db.query(Song).filter(Song.plex_rating_key == rating_key).first()
            if existing:
                existing.title = title
                existing.artist = artist
                existing.album = album
                existing.year = year
                existing.decade = _decade_for_year(year)
                existing.source_uri = source_uri
                existing.plex_user_rating = plex_user_rating
                existing.plex_rating_count = plex_rating_count
                existing.updated_at = datetime.now(timezone.utc)
                updated += 1
            else:
                db.add(
                    Song(
                        title=title,
                        artist=artist,
                        album=album,
                        year=year,
                        decade=_decade_for_year(year),
                        plex_rating_key=rating_key,
                        plex_user_rating=plex_user_rating,
                        plex_rating_count=plex_rating_count,
                        source_uri=source_uri,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                imported += 1

        if len(tracks) < page_size:
            break
        start += page_size

    app_settings.last_sync_at = datetime.now(timezone.utc)
    db.commit()
    logger.info(
        "Plex sync completed successfully for section %s (imported=%d, updated=%d)",
        app_settings.plex_music_section_id,
        imported,
        updated,
    )

    return {"imported": imported, "updated": updated}


def get_plex_connection_snapshot(app_settings: AppSettings) -> dict[str, Any]:
    libraries: list[dict[str, str | None]] = []
    if not app_settings.plex_url or not app_settings.plex_token:
        return {
            "connection_state": "failure",
            "connection_error": "Plex URL and token are required.",
            "libraries": libraries,
        }

    try:
        sections_root = _plex_get_xml(app_settings.plex_url, app_settings.plex_token, "/library/sections")
        for directory in sections_root.findall("Directory"):
            if directory.attrib.get("type") == "artist":
                libraries.append({"key": directory.attrib.get("key"), "title": directory.attrib.get("title")})
        return {
            "connection_state": "success",
            "connection_error": None,
            "libraries": libraries,
        }
    except Exception as exc:
        return {
            "connection_state": "failure",
            "connection_error": str(exc),
            "libraries": libraries,
        }


def _plex_connection_payload(app_settings: AppSettings) -> dict[str, Any]:
    snapshot = get_plex_connection_snapshot(app_settings)
    libraries = snapshot["libraries"]
    error = snapshot["connection_error"]
    has_connection_config = bool(app_settings.plex_url and app_settings.plex_token)
    if snapshot["connection_state"] == "failure":
        connection_status = "error"
    elif has_connection_config:
        connection_status = "connected"
    else:
        connection_status = "not_configured"

    return {
        "plex_url": app_settings.plex_url or "",
        "plex_token_set": bool(app_settings.plex_token),
        "plex_music_section_id": app_settings.plex_music_section_id or "",
        "libraries": libraries,
        "connection_status": connection_status,
        "connection_error": error,
    }


def _normalize_pair(song_a: int, song_b: int) -> tuple[int, int]:
    return (song_a, song_b) if song_a < song_b else (song_b, song_a)


def _parse_plex_user_rating(raw_value: str | None) -> float | None:
    if raw_value is None:
        return None
    try:
        parsed = float(raw_value)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def _parse_plex_rating_count(raw_value: str | None) -> int | None:
    if raw_value is None:
        return None
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def _song_selection_weight(song: Song, popularity_weight: float) -> float:
    rating_count = song.plex_rating_count
    user_rating = song.plex_user_rating

    count_component = 0.0
    if rating_count is not None:
        normalized_count = min(rating_count, POPULARITY_RATING_COUNT_CAP) / POPULARITY_RATING_COUNT_CAP
        count_component = max(0.0, min(1.0, normalized_count))

    rating_component = 0.0
    if user_rating is not None and POPULARITY_USER_RATING_MAX > 0:
        normalized_rating = user_rating / POPULARITY_USER_RATING_MAX
        rating_component = max(0.0, min(1.0, normalized_rating))

    blended_signal = (count_component * POPULARITY_COUNT_WEIGHT) + (rating_component * POPULARITY_USER_RATING_WEIGHT)
    effective_popularity_weight = _clamp_popularity_weight(popularity_weight)
    return max(0.0001, 1.0 + (effective_popularity_weight * blended_signal))


def _weighted_song_choice(
    song_rows: list[tuple[int, float]],
    excluded_song_ids: set[int] | None = None,
) -> int | None:
    excluded_song_ids = excluded_song_ids or set()
    available_rows = [(song_id, weight) for song_id, weight in song_rows if song_id not in excluded_song_ids]
    if not available_rows:
        return None
    song_ids = [song_id for song_id, _ in available_rows]
    weights = [weight for _, weight in available_rows]
    return random.choices(song_ids, weights=weights, k=1)[0]


def _expected_score(rating_a: int, rating_b: int) -> float:
    return 1.0 / (1.0 + math.pow(10, (rating_b - rating_a) / 400.0))


def _ranking_score_from_matchups(winner_count: int, vote_count: int) -> float:
    if vote_count <= 0:
        return 0.0
    return round(winner_count / vote_count, 4)


def _apply_filters(query, filters: dict[str, Any]):
    if artist := filters.get("artist"):
        query = query.filter(Song.artist == artist)

    if album := filters.get("album"):
        query = query.filter(Song.album == album)

    if decade := _normalize_decade(filters.get("decade")):
        if bounds := _decade_bounds(decade):
            decade_start, decade_end = bounds
            query = query.filter(
                or_(
                    Song.year.between(decade_start, decade_end),
                    func.lower(Song.decade) == decade,
                )
            )
        else:
            query = query.filter(func.lower(Song.decade) == decade)

    if title_query := filters.get("title_query"):
        query = query.filter(Song.title.ilike(f"%{title_query}%"))

    if song_ids := filters.get("song_ids"):
        query = query.filter(Song.id.in_(song_ids))

    return query


def _serialize_song(song: Song, score: int) -> dict[str, Any]:
    return {
        "song_id": song.id,
        "title": song.title,
        "artist": song.artist,
        "album": song.album,
        "year": song.year,
        "decade": song.decade,
        "score": score,
    }


def _serialize_song_for_pair(song: Song, score: int, app_settings: AppSettings) -> dict[str, Any]:
    payload = _serialize_song(song, score)
    popularity_weight = _clamp_popularity_weight(app_settings.popularity_weight)
    album_art_url = None
    if app_settings.plex_url and app_settings.plex_token and song.plex_rating_key:
        album_art_url = f"/api/plex/album-art/{song.plex_rating_key}"
    payload["album_art_url"] = album_art_url
    payload["plex_user_rating"] = song.plex_user_rating
    payload["plex_rating_count"] = song.plex_rating_count
    payload["selection_weight"] = _song_selection_weight(song, popularity_weight)
    return payload


def _candidate_pair_for_user(
    db: Session,
    user_id: int,
    filters: dict[str, Any],
    popularity_weight: float,
) -> tuple[Song, Song] | None:
    started_at = datetime.now(timezone.utc)
    skipped_song_ids = {
        row.song_id
        for row in db.query(UserSkippedSong.song_id).filter(UserSkippedSong.user_id == user_id).all()
    }
    songs_query = _apply_filters(db.query(Song), filters)
    if skipped_song_ids:
        songs_query = songs_query.filter(~Song.id.in_(skipped_song_ids))
    candidates = songs_query.order_by(Song.id.asc()).all()
    effective_popularity_weight = _clamp_popularity_weight(popularity_weight)
    song_rows = [(song.id, _song_selection_weight(song, effective_popularity_weight)) for song in candidates]
    song_ids = [song_id for song_id, _ in song_rows]

    if len(song_ids) < 2:
        elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        logger.info(
            "Pair selection aborted due to small pool | user_id=%s filters=%s song_count=%s elapsed_ms=%s",
            user_id,
            filters,
            len(song_ids),
            elapsed_ms,
        )
        return None

    last_vote = (
        db.query(PairwiseVote)
        .filter(PairwiseVote.user_id == user_id)
        .order_by(PairwiseVote.created_at.desc(), PairwiseVote.id.desc())
        .first()
    )
    last_pair = _normalize_pair(last_vote.winner_song_id, last_vote.loser_song_id) if last_vote else None

    selected_pair: tuple[int, int] | None = None
    max_attempts = min(50, len(song_ids) * 2)
    for _ in range(max_attempts):
        song_a_id = _weighted_song_choice(song_rows)
        if song_a_id is None:
            break
        song_b_id = _weighted_song_choice(song_rows, excluded_song_ids={song_a_id})
        if song_b_id is None:
            break
        candidate = _normalize_pair(song_a_id, song_b_id)
        if candidate == last_pair:
            continue
        selected_pair = candidate
        break

    if selected_pair is None:
        for song_a_id in song_ids:
            for song_b_id in song_ids:
                if song_a_id == song_b_id:
                    continue
                candidate = _normalize_pair(song_a_id, song_b_id)
                if candidate == last_pair:
                    continue
                selected_pair = candidate
                break
            if selected_pair is not None:
                break

    if selected_pair is None:
        return None

    selected_songs = db.query(Song).filter(Song.id.in_([selected_pair[0], selected_pair[1]])).all()
    songs_by_id = {song.id: song for song in selected_songs}
    if selected_pair[0] not in songs_by_id or selected_pair[1] not in songs_by_id:
        return None

    elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
    weight_by_song_id = dict(song_rows)
    selected_weights = {
        selected_pair[0]: weight_by_song_id.get(selected_pair[0]),
        selected_pair[1]: weight_by_song_id.get(selected_pair[1]),
    }
    logger.info(
        "Pair selected from weighted filtered pool | user_id=%s filters=%s song_count=%s pair=%s pair_weights=%s popularity_weight=%s elapsed_ms=%s",
        user_id,
        filters,
        len(song_ids),
        [selected_pair[0], selected_pair[1]],
        selected_weights,
        effective_popularity_weight,
        elapsed_ms,
    )
    return songs_by_id[selected_pair[0]], songs_by_id[selected_pair[1]]


def _add_skipped_songs(db: Session, user_id: int, song_ids: list[int]) -> int:
    normalized_song_ids = sorted(set(song_ids))
    if not normalized_song_ids:
        return 0

    existing_song_ids = {
        row.song_id
        for row in db.query(UserSkippedSong.song_id)
        .filter(and_(UserSkippedSong.user_id == user_id, UserSkippedSong.song_id.in_(normalized_song_ids)))
        .all()
    }
    song_ids_to_insert = [song_id for song_id in normalized_song_ids if song_id not in existing_song_ids]
    for song_id in song_ids_to_insert:
        db.add(UserSkippedSong(user_id=user_id, song_id=song_id))
    return len(song_ids_to_insert)


def _extract_first_youtube_video_id(search_html: str) -> str | None:
    patterns = [
        r'"videoId":"([A-Za-z0-9_-]{11})"',
        r"watch\?v=([A-Za-z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, search_html)
        if match:
            return match.group(1)
    return None


class YouTubeLookupError(Exception):
    def __init__(self, code: str, message: str, source: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.source = source


def _log_youtube_lookup(event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    logger.info("youtube_lookup %s", json.dumps(payload, sort_keys=True, default=str))


def _is_valid_youtube_video_id(video_id: str | None) -> bool:
    return bool(video_id and YOUTUBE_ID_PATTERN.match(video_id))


def _read_json_from_url(full_url: str, *, timeout: int = 15) -> dict[str, Any]:
    request = Request(
        full_url,
        headers={
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": "SongRanker/1.0",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8", errors="ignore")
    return json.loads(payload or "{}")


YOUTUBE_CONFIDENCE_VERIFIED = "verified_embeddable"
YOUTUBE_CONFIDENCE_UNVERIFIED = "unverified"
YOUTUBE_CONFIDENCE_MANUAL_OVERRIDE = "manual_override"


class YouTubeSearchProvider:
    name = "unknown"

    def search(self, query: str, *, embeddable_only: bool) -> list[dict[str, str]]:
        raise NotImplementedError


class YouTubeDataApiSearchProvider(YouTubeSearchProvider):
    name = "youtube_data_api"

    def __init__(self, api_key: str):
        self.api_key = (api_key or "").strip()

    def _extract_video_ids(self, payload: dict[str, Any]) -> list[str]:
        items = payload.get("items") or []
        video_ids: list[str] = []
        for item in items:
            item_id = item.get("id") if isinstance(item, dict) else None
            video_id = item_id.get("videoId") if isinstance(item_id, dict) else None
            if _is_valid_youtube_video_id(video_id):
                video_ids.append(video_id)
        return video_ids

    def _fetch_embeddable_video_ids(self, video_ids: list[str]) -> set[str]:
        if not video_ids:
            return set()
        status_params: dict[str, Any] = {
            "part": "status",
            "id": ",".join(video_ids),
            "key": self.api_key,
            "maxResults": len(video_ids),
        }
        status_url = f"https://www.googleapis.com/youtube/v3/videos?{urlencode(status_params)}"
        payload = _read_json_from_url(status_url, timeout=15)
        embeddable_ids: set[str] = set()
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            video_id = item.get("id")
            status = item.get("status")
            embeddable = status.get("embeddable") if isinstance(status, dict) else None
            if _is_valid_youtube_video_id(video_id) and embeddable is True:
                embeddable_ids.add(video_id)
        return embeddable_ids

    def search(self, query: str, *, embeddable_only: bool) -> list[dict[str, str]]:
        if not self.api_key:
            raise YouTubeLookupError(
                code="youtube_provider_unavailable",
                message="YouTube Data API key is not configured.",
                source=self.name,
            )
        params: dict[str, Any] = {
            "part": "snippet",
            "type": "video",
            "maxResults": 3,
            "q": query,
            "key": self.api_key,
        }
        if embeddable_only:
            params["videoEmbeddable"] = "true"
        api_url = f"https://www.googleapis.com/youtube/v3/search?{urlencode(params)}"
        try:
            payload = _read_json_from_url(api_url, timeout=15)
            candidate_video_ids = self._extract_video_ids(payload)
            embeddable_ids = self._fetch_embeddable_video_ids(candidate_video_ids)
        except Exception as exc:
            raise YouTubeLookupError(
                code="youtube_network_failure",
                message="Unable to contact YouTube provider.",
                source=self.name,
            ) from exc
        candidates: list[dict[str, str]] = []
        for video_id in candidate_video_ids:
            if video_id in embeddable_ids:
                candidates.append(
                    {
                        "video_id": video_id,
                        "embeddability_confidence": YOUTUBE_CONFIDENCE_VERIFIED,
                        "provider": self.name,
                    }
                )
            elif not embeddable_only:
                candidates.append(
                    {
                        "video_id": video_id,
                        "embeddability_confidence": YOUTUBE_CONFIDENCE_UNVERIFIED,
                        "provider": self.name,
                    }
                )
        return candidates


class YouTubeHtmlSearchProvider(YouTubeSearchProvider):
    name = "youtube_html_scrape"

    def search(self, query: str, *, embeddable_only: bool) -> list[dict[str, str]]:
        if embeddable_only:
            return []
        search_url = f"https://www.youtube.com/results?search_query={quote(query)}"
        request = Request(
            search_url,
            headers={
                "Accept": "text/html",
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
            },
        )
        try:
            with urlopen(request, timeout=15) as response:
                search_html = response.read().decode("utf-8", errors="ignore")
        except Exception as exc:
            raise YouTubeLookupError(
                code="youtube_network_failure",
                message="Unable to contact YouTube provider.",
                source=self.name,
            ) from exc
        candidate = _extract_first_youtube_video_id(search_html)
        if not _is_valid_youtube_video_id(candidate):
            return []
        return [
            {
                "video_id": candidate,
                "embeddability_confidence": YOUTUBE_CONFIDENCE_UNVERIFIED,
                "provider": self.name,
            }
        ]


def _cache_key_for_youtube_lookup(title: str, artist: str) -> tuple[str, str, str]:
    title_norm = (title or "").strip().lower()
    artist_norm = (artist or "").strip().lower()
    return title_norm, artist_norm, f"{title_norm}::{artist_norm}"


def _get_cached_youtube_lookup(title: str, artist: str) -> dict[str, Any] | None:
    title_norm, artist_norm, query_key = _cache_key_for_youtube_lookup(title, artist)
    now = datetime.now(timezone.utc)
    with _youtube_lookup_cache_lock:
        cached = _youtube_lookup_cache.get((title_norm, artist_norm))
        if not cached:
            return None
        if cached["expires_at"] <= now:
            _youtube_lookup_cache.pop((title_norm, artist_norm), None)
            return None
        return cached


def _set_cached_youtube_lookup(title: str, artist: str, cached_result: dict[str, Any]) -> None:
    ttl_seconds = max(30, settings.youtube_lookup_cache_ttl_seconds)
    title_norm, artist_norm, query_key = _cache_key_for_youtube_lookup(title, artist)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    checked_at = datetime.now(timezone.utc)
    l1_payload = {
        **cached_result,
        "title_norm": title_norm,
        "artist_norm": artist_norm,
        "query_key": query_key,
        "expires_at": expires_at,
        "checked_at": checked_at,
    }
    with _youtube_lookup_cache_lock:
        _youtube_lookup_cache[(title_norm, artist_norm)] = l1_payload


def _set_persistent_youtube_lookup_cache(db: Session, title: str, artist: str, cached_result: dict[str, Any]) -> None:
    title_norm, artist_norm, query_key = _cache_key_for_youtube_lookup(title, artist)
    ttl_seconds = max(30, settings.youtube_lookup_cache_ttl_seconds)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=ttl_seconds)
    cache_row = db.query(YouTubeLookupCache).filter(YouTubeLookupCache.query_key == query_key).first()
    if cache_row is None:
        cache_row = YouTubeLookupCache(
            query_key=query_key,
            title_norm=title_norm,
            artist_norm=artist_norm,
            result=str(cached_result.get("result") or "error"),
            video_id=cached_result.get("video_id"),
            code=cached_result.get("code"),
            message=str(cached_result.get("message") or "No YouTube video found for the requested song."),
            source=str(cached_result.get("source") or "provider_chain"),
            confidence=str(cached_result.get("embeddability_confidence") or YOUTUBE_CONFIDENCE_UNVERIFIED),
            expires_at=expires_at,
            checked_at=now,
        )
        db.add(cache_row)
    else:
        cache_row.title_norm = title_norm
        cache_row.artist_norm = artist_norm
        cache_row.result = str(cached_result.get("result") or "error")
        cache_row.video_id = cached_result.get("video_id")
        cache_row.code = cached_result.get("code")
        cache_row.message = str(cached_result.get("message") or "No YouTube video found for the requested song.")
        cache_row.source = str(cached_result.get("source") or "provider_chain")
        cache_row.confidence = str(cached_result.get("embeddability_confidence") or YOUTUBE_CONFIDENCE_UNVERIFIED)
        cache_row.expires_at = expires_at
        cache_row.checked_at = now
    db.commit()


def _get_persistent_youtube_lookup_cache(db: Session, title: str, artist: str) -> dict[str, Any] | None:
    title_norm, artist_norm, query_key = _cache_key_for_youtube_lookup(title, artist)
    now = datetime.now(timezone.utc)
    row = (
        db.query(YouTubeLookupCache)
        .filter(
            and_(
                YouTubeLookupCache.query_key == query_key,
                YouTubeLookupCache.expires_at > now,
            )
        )
        .first()
    )
    if row is None:
        return None
    payload = {
        "result": row.result,
        "video_id": row.video_id,
        "code": row.code,
        "message": row.message,
        "source": row.source,
        "provider": row.source,
        "embeddability_confidence": row.confidence,
        "title_norm": row.title_norm,
        "artist_norm": row.artist_norm,
        "query_key": row.query_key,
        "expires_at": row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc),
        "checked_at": row.checked_at if row.checked_at.tzinfo else row.checked_at.replace(tzinfo=timezone.utc),
    }
    with _youtube_lookup_cache_lock:
        _youtube_lookup_cache[(title_norm, artist_norm)] = payload
    return payload


def _prune_expired_youtube_lookup_cache(db: Session) -> int:
    now = datetime.now(timezone.utc)
    deleted = db.query(YouTubeLookupCache).filter(YouTubeLookupCache.expires_at <= now).delete(synchronize_session=False)
    if deleted:
        db.commit()
    return deleted


def _make_youtube_provider_chain() -> list[YouTubeSearchProvider]:
    providers: list[YouTubeSearchProvider] = [YouTubeDataApiSearchProvider(settings.youtube_data_api_key)]
    fallback = settings.youtube_search_fallback_provider.strip().lower()
    if fallback == "youtube_html_scrape":
        providers.append(YouTubeHtmlSearchProvider())
    return providers


def _fetch_first_youtube_video(db: Session, title: str, artist: str) -> dict[str, Any]:
    query = f"{(title or '').strip()} {(artist or '').strip()}".strip()
    if not query:
        raise YouTubeLookupError(
            code="video_not_found",
            message="No YouTube video found for the requested song.",
            source="input",
        )

    if cached := _get_cached_youtube_lookup(title, artist):
        _log_youtube_lookup(
            "cache_hit",
            title=title,
            artist=artist,
            result=cached.get("result"),
            source=cached.get("source"),
            provider=cached.get("provider"),
            embeddability_confidence=cached.get("embeddability_confidence"),
        )
        if cached.get("result") == "ok":
            return cached
        if cached.get("result") == "error":
            raise YouTubeLookupError(
                code=str(cached.get("code") or "video_not_found"),
                message=str(cached.get("message") or "No YouTube video found for the requested song."),
                source=str(cached.get("source") or "cache"),
            )
    elif persisted := _get_persistent_youtube_lookup_cache(db, title, artist):
        _log_youtube_lookup(
            "l2_cache_hit",
            title=title,
            artist=artist,
            result=persisted.get("result"),
            source=persisted.get("source"),
            provider=persisted.get("provider"),
            embeddability_confidence=persisted.get("embeddability_confidence"),
        )
        if persisted.get("result") == "ok":
            return persisted
        if persisted.get("result") == "error":
            raise YouTubeLookupError(
                code=str(persisted.get("code") or "video_not_found"),
                message=str(persisted.get("message") or "No YouTube video found for the requested song."),
                source=str(persisted.get("source") or "cache"),
            )

    providers = _make_youtube_provider_chain()
    last_network_error: YouTubeLookupError | None = None
    saw_known_non_embeddable = False
    first_unknown_candidate: dict[str, str] | None = None
    for provider in providers:
        try:
            embeddable_matches = provider.search(query, embeddable_only=True)
            if embeddable_matches:
                best_match = embeddable_matches[0]
                video_id = best_match.get("video_id")
                result = {
                    "result": "ok",
                    "video_id": video_id,
                    "source": provider.name,
                    "provider": best_match.get("provider", provider.name),
                    "embeddability_confidence": best_match.get(
                        "embeddability_confidence",
                        YOUTUBE_CONFIDENCE_VERIFIED,
                    ),
                    "message": "Lookup succeeded.",
                }
                _set_cached_youtube_lookup(title, artist, result)
                _set_persistent_youtube_lookup_cache(db, title, artist, result)
                _log_youtube_lookup(
                    "lookup_success",
                    title=title,
                    artist=artist,
                    source=provider.name,
                    provider=result["provider"],
                    query=query,
                    video_id=video_id,
                    embeddability_confidence=result["embeddability_confidence"],
                )
                return result

            any_matches = provider.search(query, embeddable_only=False)
            if any_matches:
                for candidate in any_matches:
                    confidence = candidate.get("embeddability_confidence", YOUTUBE_CONFIDENCE_UNVERIFIED)
                    if confidence == YOUTUBE_CONFIDENCE_VERIFIED:
                        video_id = candidate.get("video_id")
                        result = {
                            "result": "ok",
                            "video_id": video_id,
                            "source": provider.name,
                            "provider": candidate.get("provider", provider.name),
                            "embeddability_confidence": YOUTUBE_CONFIDENCE_VERIFIED,
                            "message": "Lookup succeeded.",
                        }
                        _set_cached_youtube_lookup(title, artist, result)
                        _set_persistent_youtube_lookup_cache(db, title, artist, result)
                        _log_youtube_lookup(
                            "lookup_success_from_broad_search",
                            title=title,
                            artist=artist,
                            source=provider.name,
                            provider=result["provider"],
                            query=query,
                            video_id=video_id,
                            embeddability_confidence=result["embeddability_confidence"],
                        )
                        return result
                    if confidence == YOUTUBE_CONFIDENCE_UNVERIFIED:
                        saw_known_non_embeddable = True
                    if confidence == YOUTUBE_CONFIDENCE_UNVERIFIED and first_unknown_candidate is None:
                        first_unknown_candidate = candidate
                _log_youtube_lookup(
                    "lookup_candidate_scan",
                    title=title,
                    artist=artist,
                    source=provider.name,
                    provider=provider.name,
                    query=query,
                    candidate_count=len(any_matches),
                    saw_known_non_embeddable=saw_known_non_embeddable,
                    saw_unknown_embeddability=first_unknown_candidate is not None,
                )
        except YouTubeLookupError as exc:
            if exc.code == "youtube_network_failure":
                last_network_error = exc
                _log_youtube_lookup(
                    "provider_network_failure",
                    title=title,
                    artist=artist,
                    source=provider.name,
                    provider=provider.name,
                    query=query,
                    code=exc.code,
                )
                continue
            _log_youtube_lookup(
                "provider_error",
                title=title,
                artist=artist,
                source=provider.name,
                provider=provider.name,
                query=query,
                code=exc.code,
            )
            continue

    if first_unknown_candidate:
        result = {
            "result": "ok",
            "video_id": first_unknown_candidate.get("video_id"),
            "source": str(first_unknown_candidate.get("provider") or "provider_chain"),
            "provider": str(first_unknown_candidate.get("provider") or "provider_chain"),
            "embeddability_confidence": YOUTUBE_CONFIDENCE_UNVERIFIED,
            "message": "Lookup succeeded, embeddability could not be verified.",
        }
        _set_cached_youtube_lookup(title, artist, result)
        _set_persistent_youtube_lookup_cache(db, title, artist, result)
        _log_youtube_lookup(
            "lookup_embeddability_unknown",
            title=title,
            artist=artist,
            source=result["source"],
            provider=result["provider"],
            query=query,
            video_id=result["video_id"],
            embeddability_confidence=result["embeddability_confidence"],
        )
        return result

    if saw_known_non_embeddable:
        failure = {
            "result": "error",
            "code": "video_not_embeddable",
            "message": "Only non-embeddable YouTube videos were found for the requested song.",
            "source": "provider_chain",
            "provider": "provider_chain",
            "embeddability_confidence": YOUTUBE_CONFIDENCE_UNVERIFIED,
        }
        _set_cached_youtube_lookup(title, artist, failure)
        _set_persistent_youtube_lookup_cache(db, title, artist, failure)
        raise YouTubeLookupError(
            code=failure["code"],
            message=failure["message"],
            source=failure["source"],
        )

    if last_network_error is not None:
        raise last_network_error

    failure = {
        "result": "error",
        "code": "video_not_found",
        "message": "No YouTube video found for the requested song.",
        "source": "provider_chain",
        "provider": "provider_chain",
        "embeddability_confidence": YOUTUBE_CONFIDENCE_UNVERIFIED,
    }
    _set_cached_youtube_lookup(title, artist, failure)
    _set_persistent_youtube_lookup_cache(db, title, artist, failure)
    raise YouTubeLookupError(code=failure["code"], message=failure["message"], source=failure["source"])


@app.middleware("http")
async def setup_guard(request: StarletteRequest, call_next):
    if request.url.path.startswith("/static") or request.url.path in {
        "/health",
        "/setup",
    } or request.url.path.startswith("/api/setup") or request.url.path.startswith("/api/auth"):
        return await call_next(request)

    db = SessionLocal()
    try:
        app_settings = _get_or_create_settings(db)
        if not app_settings.is_initialized:
            return RedirectResponse(url="/setup", status_code=307)
    finally:
        db.close()

    return await call_next(request)


async def _periodic_sync_loop():
    while True:
        await asyncio.sleep(3600)
        db = SessionLocal()
        try:
            app_settings = _get_or_create_settings(db)
            if app_settings.is_initialized:
                _sync_tracks_from_plex(db, app_settings)
            _prune_expired_youtube_lookup_cache(db)
        except Exception:
            db.rollback()
            logger.exception("Periodic Plex sync failed")
        finally:
            db.close()


@app.on_event("startup")
async def startup_event():
    _configure_logging()
    logger.info("Logging initialized at %s (weekly rotation enabled)", LOG_FILE_PATH)
    asyncio.create_task(_periodic_sync_loop())


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/signin", response_class=HTMLResponse)
def signin_page(request: StarletteRequest, db: Session = Depends(get_db)):
    return RedirectResponse(url="/", status_code=303)


def _friendly_auth_message(auth_error: str | None, detail: str | None) -> tuple[str, str]:
    if not auth_error:
        return ("Auth is intentionally lightweight and not secure yet.", "status-info")

    detail_text = unquote(detail).strip() if detail else ""
    messages: dict[str, str] = {
        "google_consent_denied": "Google sign-in was canceled. You can try again or use username/password.",
        "google_auth_error": "Google sign-in could not be completed. Please try again.",
        "google_state_missing": "Your Google sign-in session expired. Please start sign-in again.",
        "google_state_mismatch": "Google sign-in verification failed. Please retry from this device.",
        "google_missing_code": "Google sign-in response was incomplete. Please try again.",
        "google_nonce_mismatch": "Google sign-in could not be verified securely. Please try again.",
        "google_link_confirmation_required": "We found a matching email. Confirm linking by signing in again with Google.",
        "google_duplicate_link_conflict": "This Google account is already linked to another SongRanker user.",
        "google_invalid_token": "Google sign-in failed token validation. Please try again.",
    }
    base_message = messages.get(auth_error, "Authentication could not be completed. Please try again.")
    if detail_text:
        base_message = f"{base_message} ({detail_text})"
    return (base_message, "status-error")


@app.get("/", response_class=HTMLResponse)
def index(request: StarletteRequest, db: Session = Depends(get_db)):
    session = _current_session(db, request)
    current_user = None
    show_onboarding_helper = False
    if session is not None:
        current_user = db.query(User).filter(User.id == session.user_id).first()
    if current_user is not None:
        has_pairwise_votes = (
            db.query(PairwiseVote.id).filter(PairwiseVote.user_id == current_user.id).first() is not None
        )
        has_rating_scores = (
            db.query(RatingScore.id).filter(RatingScore.user_id == current_user.id).first() is not None
        )
        show_onboarding_helper = not (has_pairwise_votes or has_rating_scores)
    auth_error = request.query_params.get("auth_error")
    auth_detail = request.query_params.get("detail")
    auth_notice, auth_notice_class = _friendly_auth_message(auth_error, auth_detail)
    avatar_status = request.query_params.get("avatar_status", "").strip().lower()
    avatar_message_raw = request.query_params.get("avatar_message")
    avatar_message = unquote(avatar_message_raw).strip() if avatar_message_raw else ""
    avatar_notice_class = "status-error"
    if avatar_status == "success":
        avatar_notice_class = "status-success"
    google_identity_linked = False
    if current_user is not None:
        google_identity_linked = (
            db.query(UserIdentity.id)
            .filter(UserIdentity.user_id == current_user.id, UserIdentity.provider == "google")
            .first()
            is not None
        )
    users = db.query(User).order_by(User.username.asc()).all()

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_host": settings.app_host,
            "app_port": settings.app_port,
            "current_user": current_user,
            "is_authenticated": current_user is not None,
            "show_onboarding_helper": show_onboarding_helper,
            "google_identity_linked": google_identity_linked,
            "google_auth_available": bool(
                settings.google_client_id and settings.google_client_secret and settings.google_redirect_uri
            ),
            "local_signin_enabled": True,
            "users": users,
            "notice": auth_notice,
            "notice_class": auth_notice_class,
            "avatar_url": _build_avatar_url(current_user),
            "avatar_message": avatar_message,
            "avatar_notice_class": avatar_notice_class,
        },
    )


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: StarletteRequest, db: Session = Depends(get_db)):
    app_settings = _get_or_create_settings(db)
    users_count = db.query(func.count(User.id)).scalar() or 0

    snapshot = get_plex_connection_snapshot(app_settings)
    libraries = snapshot["libraries"]
    error = snapshot["connection_error"]

    return templates.TemplateResponse(
        "setup.html",
        {
            "request": request,
            "users_count": users_count,
            "settings": app_settings,
            "libraries": libraries,
            "error": error,
        },
    )


@app.post("/api/setup/user")
def setup_user(
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    existing = db.query(User).filter((User.username == username) | (User.email == email)).first()
    if existing:
        raise HTTPException(status_code=400, detail="User with username or email already exists")
    db.add(User(username=username, email=email, password_hash=_hash_password(password)))
    db.commit()
    return RedirectResponse(url="/setup", status_code=303)


@app.post("/api/auth/create-user")
def create_user(
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    existing = db.query(User).filter((User.username == username) | (User.email == email)).first()
    if existing:
        raise HTTPException(status_code=400, detail="User with username or email already exists")

    db.add(User(username=username, email=email, password_hash=_hash_password(password)))
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/api/auth/signin")
def signin(
    request: StarletteRequest,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username).first()
    if user is None or not _verify_password(password, user.password_hash):
        _log_auth_event("signin", outcome="failure", username=username, reason="invalid_credentials")
        raise HTTPException(status_code=401, detail="Invalid username/password")

    token = _create_session_for_user_with_rotation(
        db=db,
        user_id=user.id,
        previous_token=request.cookies.get(SESSION_COOKIE_NAME),
    )
    _log_auth_event("signin", outcome="success", user_id=user.id, username=user.username)
    return _session_redirect(url="/", token=token)


@app.get("/api/auth/google/start")
def google_auth_start(db: Session = Depends(get_db)):
    if not settings.google_client_id or not settings.google_client_secret or not settings.google_redirect_uri:
        raise HTTPException(status_code=503, detail="Google OIDC is not configured")

    metadata = _fetch_google_oidc_metadata()
    authorization_endpoint = metadata.get("authorization_endpoint")
    if not isinstance(authorization_endpoint, str) or not authorization_endpoint:
        raise HTTPException(status_code=502, detail="OIDC discovery missing authorization endpoint")

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    now = int(datetime.now(timezone.utc).timestamp())
    signed_state = _encode_signed_cookie({"state": state, "nonce": nonce, "exp": now + OIDC_LOGIN_TTL_SECONDS})

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "prompt": "select_account",
    }
    redirect_url = f"{authorization_endpoint}?{urlencode(params)}"
    response = RedirectResponse(url=redirect_url, status_code=303)
    response.set_cookie(
        key=OIDC_LOGIN_COOKIE_NAME,
        value=signed_state,
        max_age=OIDC_LOGIN_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@app.get("/api/auth/google/callback")
def google_auth_callback(
    request: StarletteRequest,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
    confirm_link: int = Query(default=0),
    db: Session = Depends(get_db),
):
    previous_token = request.cookies.get(SESSION_COOKIE_NAME)
    if error:
        detail = quote(error_description or error)
        _log_auth_event("google_callback", outcome="failure", provider="google", reason=error)
        if error == "access_denied":
            return RedirectResponse(url=f"/?auth_error=google_consent_denied&detail={detail}", status_code=303)
        return RedirectResponse(url=f"/?auth_error=google_auth_error&detail={detail}", status_code=303)

    signed = _decode_signed_cookie(request.cookies.get(OIDC_LOGIN_COOKIE_NAME))
    if signed is None:
        _log_auth_event("google_callback", outcome="failure", provider="google", reason="state_missing")
        response = RedirectResponse(url="/?auth_error=google_state_missing", status_code=303)
        response.delete_cookie(OIDC_LOGIN_COOKIE_NAME)
        return response

    expected_state = signed.get("state")
    expected_nonce = signed.get("nonce")
    expires_at = signed.get("exp")
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if (
        not isinstance(expected_state, str)
        or not isinstance(expected_nonce, str)
        or not isinstance(expires_at, int)
        or expires_at < now_ts
        or not state
        or state != expected_state
    ):
        _log_auth_event("google_callback", outcome="failure", provider="google", reason="state_mismatch")
        response = RedirectResponse(url="/?auth_error=google_state_mismatch", status_code=303)
        response.delete_cookie(OIDC_LOGIN_COOKIE_NAME)
        return response

    if not code:
        _log_auth_event("google_callback", outcome="failure", provider="google", reason="missing_code")
        response = RedirectResponse(url="/?auth_error=google_missing_code", status_code=303)
        response.delete_cookie(OIDC_LOGIN_COOKIE_NAME)
        return response

    try:
        linked_provider = False
        metadata = _fetch_google_oidc_metadata()
        token_endpoint = metadata.get("token_endpoint")
        issuer = metadata.get("issuer")
        if not isinstance(token_endpoint, str) or not token_endpoint:
            raise ValueError("OIDC discovery missing token endpoint")
        if not isinstance(issuer, str) or not issuer:
            raise ValueError("OIDC discovery missing issuer")

        token_response = _fetch_json(
            token_endpoint,
            method="POST",
            payload={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        id_token = token_response.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise ValueError("Token response missing id_token")

        token_info = _fetch_json(
            f"https://oauth2.googleapis.com/tokeninfo?id_token={quote(id_token, safe='')}",
            method="GET",
        )
        claims = _decode_jwt_payload(id_token)

        claim_iss = claims.get("iss")
        claim_aud = claims.get("aud")
        claim_exp = claims.get("exp")
        claim_nonce = claims.get("nonce")
        claim_sub = claims.get("sub")
        claim_email = claims.get("email")
        claim_email_verified = _normalize_email_verified(claims.get("email_verified"))

        if claim_iss != issuer or token_info.get("iss") != issuer:
            raise ValueError("Issuer mismatch")
        if claim_aud != settings.google_client_id or token_info.get("aud") != settings.google_client_id:
            raise ValueError("Audience mismatch")
        if not isinstance(claim_exp, int) or claim_exp <= now_ts:
            raise ValueError("ID token expired")
        if claim_nonce != expected_nonce:
            _log_auth_event("google_callback", outcome="failure", provider="google", reason="nonce_mismatch")
            response = RedirectResponse(url="/?auth_error=google_nonce_mismatch", status_code=303)
            response.delete_cookie(OIDC_LOGIN_COOKIE_NAME)
            return response
        if not isinstance(claim_sub, str) or not claim_sub:
            raise ValueError("Missing sub claim")
        if not isinstance(claim_email, str) or not claim_email:
            raise ValueError("Missing email claim")
        if not claim_email_verified:
            raise ValueError("Email is not verified")

        identity = (
            db.query(UserIdentity)
            .filter(UserIdentity.provider == "google", UserIdentity.provider_subject == claim_sub)
            .first()
        )
        user: User | None = None
        if identity is not None:
            user = db.query(User).filter(User.id == identity.user_id).first()
            if user is None:
                raise ValueError("Linked user no longer exists")
        else:
            existing_user = db.query(User).filter(func.lower(User.email) == claim_email.lower()).first()
            if existing_user is not None and confirm_link != 1:
                _log_auth_event("google_callback", outcome="failure", provider="google", reason="link_confirmation_required")
                response = RedirectResponse(url="/?auth_error=google_link_confirmation_required", status_code=303)
                response.delete_cookie(OIDC_LOGIN_COOKIE_NAME)
                return response

            if existing_user is None:
                user = User(
                    username=_derive_username_from_email(db, claim_email),
                    email=claim_email,
                    password_hash=_hash_password(secrets.token_urlsafe(48)),
                )
                db.add(user)
                db.flush()
            else:
                user = existing_user

            conflicting_identity = (
                db.query(UserIdentity)
                .filter(UserIdentity.provider == "google", UserIdentity.user_id == user.id)
                .first()
            )
            if conflicting_identity is not None and conflicting_identity.provider_subject != claim_sub:
                _log_auth_event("google_callback", outcome="failure", provider="google", reason="duplicate_link_conflict")
                response = RedirectResponse(url="/?auth_error=google_duplicate_link_conflict", status_code=303)
                response.delete_cookie(OIDC_LOGIN_COOKIE_NAME)
                return response

            db.add(
                UserIdentity(
                    user_id=user.id,
                    provider="google",
                    provider_subject=claim_sub,
                    email=claim_email,
                    email_verified=claim_email_verified,
                )
            )
            linked_provider = True
        db.commit()
        token = _create_session_for_user_with_rotation(db=db, user_id=user.id, previous_token=previous_token)
        if linked_provider:
            _log_auth_event("provider_link", outcome="success", user_id=user.id, username=user.username, provider="google")
        _log_auth_event("google_callback", outcome="success", user_id=user.id, username=user.username, provider="google")
    except Exception as exc:
        logger.exception("Google OIDC callback failed: %s", exc)
        _log_auth_event("google_callback", outcome="failure", provider="google", reason="callback_exception")
        db.rollback()
        response = RedirectResponse(url="/?auth_error=google_invalid_token", status_code=303)
        response.delete_cookie(OIDC_LOGIN_COOKIE_NAME)
        return response

    response = _session_redirect(url="/", token=token)
    response.delete_cookie(OIDC_LOGIN_COOKIE_NAME)
    return response


@app.post("/api/auth/switch-user")
def switch_user(
    request: StarletteRequest,
    user_id: int = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    if not settings.allow_auth_switch_user:
        _log_auth_event("switch_user", outcome="failure", user_id=user_id, reason="disabled_in_production")
        raise HTTPException(
            status_code=403,
            detail="Account switching is disabled. Re-authenticate with the target account or use admin controls.",
        )
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not _verify_password(password, user.password_hash):
        _log_auth_event("switch_user", outcome="failure", user_id=user_id, reason="invalid_credentials")
        raise HTTPException(status_code=401, detail="Invalid credentials for selected user")

    token = _create_session_for_user_with_rotation(
        db=db,
        user_id=user.id,
        previous_token=request.cookies.get(SESSION_COOKIE_NAME),
    )
    _log_auth_event("switch_user", outcome="success", user_id=user.id, username=user.username)
    return _session_redirect(url="/", token=token)


@app.post("/api/auth/signout")
def signout(request: StarletteRequest, db: Session = Depends(get_db)):
    session = _current_session(db, request)
    if session is not None:
        _log_auth_event("signout", outcome="success", user_id=session.user_id)
        db.delete(session)
        db.commit()
    else:
        _log_auth_event("signout", outcome="failure", reason="no_active_session")

    response = RedirectResponse(url="/", status_code=303)
    _clear_session_cookie(response)
    return response


@app.post("/api/auth/unlink-provider")
def unlink_provider(
    request: StarletteRequest,
    provider: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_current_user),
):
    normalized_provider = provider.strip().lower()
    if not normalized_provider:
        _log_auth_event("provider_unlink", outcome="failure", user_id=current_user.id, reason="provider_required")
        raise HTTPException(status_code=400, detail="provider is required")

    identity = (
        db.query(UserIdentity)
        .filter(UserIdentity.user_id == current_user.id, UserIdentity.provider == normalized_provider)
        .first()
    )
    if identity is None:
        _log_auth_event(
            "provider_unlink",
            outcome="failure",
            user_id=current_user.id,
            provider=normalized_provider,
            reason="identity_not_found",
        )
        raise HTTPException(status_code=404, detail="Provider identity was not found for current user")

    db.delete(identity)
    db.commit()

    token = _create_session_for_user_with_rotation(
        db=db,
        user_id=current_user.id,
        previous_token=request.cookies.get(SESSION_COOKIE_NAME),
    )
    _log_auth_event("provider_unlink", outcome="success", user_id=current_user.id, provider=normalized_provider)
    return _session_redirect(url="/", token=token)


@app.post("/api/profile/photo")
async def upload_profile_photo(
    profile_photo: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_current_user),
):
    content_type = (profile_photo.content_type or "").lower().strip()
    if content_type not in ALLOWED_AVATAR_MIME_TYPES:
        return _profile_photo_redirect("Invalid file type. Use PNG, JPEG, or WEBP.")

    try:
        raw = await profile_photo.read(MAX_AVATAR_SIZE_BYTES + 1)
    except Exception:
        return _profile_photo_redirect("Could not read uploaded file.")
    finally:
        await profile_photo.close()

    if not raw:
        return _profile_photo_redirect("Uploaded file is empty.")

    if len(raw) > MAX_AVATAR_SIZE_BYTES:
        return _profile_photo_redirect("Profile photo is too large. Max size is 2 MB.")

    sniffed_mime = _sniff_image_mime(raw)
    if sniffed_mime is None or sniffed_mime not in ALLOWED_AVATAR_MIME_TYPES:
        return _profile_photo_redirect("File does not appear to be a valid image.")

    if content_type != sniffed_mime:
        return _profile_photo_redirect("Declared file type does not match the image data.")

    file_hash = hashlib.sha256(raw).hexdigest()[:16]
    entropy = secrets.token_hex(6)
    extension = AVATAR_EXTENSION_BY_MIME[sniffed_mime]
    user_dir = AVATAR_UPLOAD_DIR / str(current_user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    output_name = f"{file_hash}-{entropy}{extension}"
    output_path = user_dir / output_name
    output_path.write_bytes(raw)

    previous_relative_path = current_user.profile_image_path
    current_user.profile_image_path = f"uploads/avatars/{current_user.id}/{output_name}"
    current_user.profile_image_mime = sniffed_mime
    current_user.profile_image_size_bytes = len(raw)
    db.commit()

    if previous_relative_path and previous_relative_path != current_user.profile_image_path:
        if previous_relative_path.startswith(f"uploads/avatars/{current_user.id}/"):
            previous_path = Path("app/static") / previous_relative_path
            try:
                previous_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Unable to delete old avatar file: %s", previous_path)

    return _profile_photo_redirect("Profile photo updated.", status="success")


@app.post("/api/setup/plex")
def setup_plex(plex_url: str = Form(...), plex_token: str = Form(...), db: Session = Depends(get_db)):
    app_settings = _get_or_create_settings(db)
    app_settings.plex_url = plex_url.strip().rstrip("/")
    app_settings.plex_token = plex_token.strip()
    db.commit()
    return RedirectResponse(url="/setup", status_code=303)


@app.get("/api/plex/settings")
def plex_settings(db: Session = Depends(get_db)):
    app_settings = _get_or_create_settings(db)
    snapshot = get_plex_connection_snapshot(app_settings)
    libraries = snapshot["libraries"]
    error = snapshot["connection_error"]
    if error:
        status = error
    elif app_settings.plex_url and app_settings.plex_token:
        status = "Connected"
    else:
        status = "Not configured"
    return {
        "plex_url": app_settings.plex_url or "",
        "plex_token": app_settings.plex_token or "",
        "plex_music_section_id": app_settings.plex_music_section_id or "",
        "popularity_weight": _clamp_popularity_weight(app_settings.popularity_weight),
        "libraries": libraries,
        "status": status,
        "status_ok": error is None and bool(app_settings.plex_url and app_settings.plex_token),
    }


@app.post("/api/plex/settings")
def save_plex_settings(
    plex_url: str = Form(...),
    plex_token: str = Form(...),
    popularity_weight: float = Form(DEFAULT_POPULARITY_WEIGHT),
    db: Session = Depends(get_db),
):
    app_settings = _get_or_create_settings(db)
    app_settings.plex_url = plex_url.strip().rstrip("/")
    app_settings.plex_token = plex_token.strip()
    app_settings.popularity_weight = _clamp_popularity_weight(popularity_weight)
    db.add(app_settings)
    db.commit()
    db.refresh(app_settings)

    snapshot = get_plex_connection_snapshot(app_settings)
    libraries = snapshot["libraries"]
    error = snapshot["connection_error"]
    return {
        "plex_url": app_settings.plex_url or "",
        "plex_token": app_settings.plex_token or "",
        "plex_music_section_id": app_settings.plex_music_section_id or "",
        "popularity_weight": _clamp_popularity_weight(app_settings.popularity_weight),
        "libraries": libraries,
        "status": error or "Connected",
        "status_ok": error is None,
    }


@app.post("/api/setup/library")
def setup_library(plex_music_section_id: str = Form(...), db: Session = Depends(get_db)):
    app_settings = _get_or_create_settings(db)
    app_settings.plex_music_section_id = plex_music_section_id
    db.commit()
    return RedirectResponse(url="/setup", status_code=303)


@app.post("/api/plex/library")
def save_plex_library(plex_music_section_id: str = Form(...), db: Session = Depends(get_db)):
    app_settings = _get_or_create_settings(db)
    app_settings.plex_music_section_id = plex_music_section_id
    db.add(app_settings)
    db.commit()

    snapshot = get_plex_connection_snapshot(app_settings)
    libraries = snapshot["libraries"]
    error = snapshot["connection_error"]
    return {
        "plex_url": app_settings.plex_url or "",
        "plex_token": app_settings.plex_token or "",
        "plex_music_section_id": app_settings.plex_music_section_id or "",
        "libraries": libraries,
        "status": error or "Library saved",
        "status_ok": error is None,
    }


@app.get("/api/settings/plex")
def get_settings_plex(
    db: Session = Depends(get_db),
    _: User = Depends(_require_current_user),
):
    app_settings = _get_or_create_settings(db)
    return _plex_connection_payload(app_settings)


@app.post("/api/settings/plex")
def update_settings_plex(
    payload: PlexSettingsUpdateRequest,
    db: Session = Depends(get_db),
    _: User = Depends(_require_current_user),
):
    app_settings = _get_or_create_settings(db)
    app_settings.plex_url = payload.plex_url.strip().rstrip("/")
    app_settings.plex_token = payload.plex_token.strip()
    db.add(app_settings)
    db.commit()
    db.refresh(app_settings)
    return _plex_connection_payload(app_settings)


@app.post("/api/settings/library")
def update_settings_library(
    payload: PlexLibraryUpdateRequest,
    db: Session = Depends(get_db),
    _: User = Depends(_require_current_user),
):
    app_settings = _get_or_create_settings(db)
    app_settings.plex_music_section_id = payload.plex_music_section_id.strip()
    db.add(app_settings)
    db.commit()
    db.refresh(app_settings)

    return {
        "saved": True,
        "plex_music_section_id": app_settings.plex_music_section_id or "",
        "message": "Library selection saved",
    }


@app.post("/api/setup/import")
def setup_import(db: Session = Depends(get_db)):
    app_settings = _get_or_create_settings(db)
    result = _sync_tracks_from_plex(db, app_settings)
    app_settings.is_initialized = True
    db.commit()
    return {"status": "ok", **result}


@app.post("/api/plex/resync")
def resync_plex(
    db: Session = Depends(get_db),
    _: User = Depends(_require_current_user),
):
    app_settings = _get_or_create_settings(db)
    try:
        result = _sync_tracks_from_plex(db, app_settings)
        return {"status": "ok", **result}
    except Exception:
        db.rollback()
        logger.exception("Manual Plex resync failed")
        raise


@app.get("/api/plex/album-art/{rating_key}")
def plex_album_art(
    rating_key: str,
    db: Session = Depends(get_db),
    _: User = Depends(_require_current_user),
):
    app_settings = _get_or_create_settings(db)
    if not app_settings.plex_url or not app_settings.plex_token:
        raise HTTPException(status_code=400, detail="Plex settings are incomplete")

    try:
        metadata_root = _plex_get_xml(app_settings.plex_url, app_settings.plex_token, f"/library/metadata/{rating_key}")
        track = metadata_root.find("Track")
        if track is None:
            raise HTTPException(status_code=404, detail="Track metadata was not found in Plex")

        thumb_path = (
            track.attrib.get("parentThumb")
            or track.attrib.get("thumb")
            or track.attrib.get("grandparentThumb")
        )
        if not thumb_path:
            raise HTTPException(status_code=404, detail="No album art is available for this track")

        request = Request(
            f"{app_settings.plex_url.rstrip('/')}{thumb_path}"
            f"?{urlencode({'X-Plex-Token': app_settings.plex_token})}"
        )
        with urlopen(request, timeout=20) as plex_response:
            payload = plex_response.read()
            media_type = plex_response.headers.get("Content-Type", "image/jpeg")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Album art request failed for rating_key=%s", rating_key)
        raise HTTPException(status_code=502, detail="Unable to fetch album art from Plex")

    return Response(content=payload, media_type=media_type)


@app.get("/api/youtube/first")
def get_first_youtube_video(
    title: str = Query(default=""),
    artist: str = Query(default=""),
    _: User = Depends(_require_current_user),
    db: Session = Depends(get_db),
):
    try:
        result = _fetch_first_youtube_video(db=db, title=title, artist=artist)
        video_id = result.get("video_id")
        if not _is_valid_youtube_video_id(video_id):
            raise HTTPException(
                status_code=502,
                detail={"code": "invalid_video_id", "message": "Provider returned an invalid YouTube video id."},
            )
        return {
            "video_id": video_id,
            "embeddability_confidence": result.get("embeddability_confidence", YOUTUBE_CONFIDENCE_UNVERIFIED),
            "provider": result.get("provider", result.get("source")),
        }
    except YouTubeLookupError as exc:
        status_code = 404
        if exc.code == "youtube_network_failure":
            status_code = 502
        elif exc.code == "video_not_embeddable":
            status_code = 422
        _log_youtube_lookup(
            "lookup_failed",
            title=title,
            artist=artist,
            code=exc.code,
            source=exc.source,
            provider=exc.source,
            status_code=status_code,
        )
        raise HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message})
    except HTTPException:
        raise
    except Exception:
        logger.exception("YouTube first-video lookup failed | title=%s artist=%s", title, artist)
        raise HTTPException(
            status_code=502,
            detail={"code": "youtube_fetch_failed", "message": "Unable to contact YouTube right now."},
        )


@app.get("/api/rate/next", response_model=NextPairResponse)
def get_next_pair(
    artist: str | None = Query(default=None),
    album: str | None = Query(default=None),
    decade: str | None = Query(default=None),
    title_query: str | None = Query(default=None),
    song_ids: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_current_user),
):
    started_at = datetime.now(timezone.utc)
    app_settings = _get_or_create_settings(db)
    filters: dict[str, Any] = {}
    if artist:
        filters["artist"] = artist
    if album:
        filters["album"] = album
    if normalized_decade := _normalize_decade(decade):
        filters["decade"] = normalized_decade
    if title_query:
        filters["title_query"] = title_query
    if song_ids:
        filters["song_ids"] = [int(song_id.strip()) for song_id in song_ids.split(",") if song_id.strip()]
    logger.info("Received next pair request | user_id=%s filters=%s", current_user.id, filters)

    popularity_weight = _clamp_popularity_weight(app_settings.popularity_weight)
    pair = _candidate_pair_for_user(
        db=db,
        user_id=current_user.id,
        filters=filters,
        popularity_weight=popularity_weight,
    )
    if pair is None:
        elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        logger.warning(
            "No pair available for request | user_id=%s filters=%s elapsed_ms=%s",
            current_user.id,
            filters,
            elapsed_ms,
        )
        raise HTTPException(status_code=404, detail="Not enough songs available for this filter context")

    song_ids_for_pair = [pair[0].id, pair[1].id]
    rating_rows = (
        db.query(RatingScore)
        .filter(and_(RatingScore.user_id == current_user.id, RatingScore.song_id.in_(song_ids_for_pair)))
        .all()
    )
    rating_lookup = {row.song_id: row.score for row in rating_rows}

    response = NextPairResponse(
        filters=filters,
        pair=[
            _serialize_song_for_pair(song, rating_lookup.get(song.id, DEFAULT_RATING), app_settings)
            for song in pair
        ],
    )
    elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
    logger.info(
        "Returning next pair response | user_id=%s filters=%s pair=%s elapsed_ms=%s",
        current_user.id,
        filters,
        song_ids_for_pair,
        elapsed_ms,
    )
    return response


@app.get("/api/pool/options")
def get_pool_options(
    filter_by: str = Query(default="none"),
    db: Session = Depends(get_db),
    _: User = Depends(_require_current_user),
):
    filter_by = (filter_by or "none").lower()
    if filter_by == "artist":
        rows = db.query(Song.artist).filter(Song.artist.isnot(None)).distinct().order_by(Song.artist.asc()).all()
        return {"filter_by": filter_by, "options": [row[0] for row in rows if row[0]]}
    if filter_by == "album":
        rows = db.query(Song.album).filter(Song.album.isnot(None)).distinct().order_by(Song.album.asc()).all()
        return {"filter_by": filter_by, "options": [row[0] for row in rows if row[0]]}
    return {"filter_by": "none", "options": []}


@app.get("/api/skips")
def get_skipped_songs(
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_current_user),
):
    rows = (
        db.query(UserSkippedSong, Song)
        .join(Song, Song.id == UserSkippedSong.song_id)
        .filter(UserSkippedSong.user_id == current_user.id)
        .order_by(UserSkippedSong.created_at.desc(), UserSkippedSong.id.desc())
        .all()
    )
    return {
        "total": len(rows),
        "rows": [
            {
                "song_id": song.id,
                "title": song.title,
                "artist": song.artist,
                "album": song.album,
                "year": song.year,
                "skipped_at": skipped.created_at.isoformat(),
            }
            for skipped, song in rows
        ],
    }


@app.post("/api/skips/songs")
def skip_songs(
    payload: SkipSongsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_current_user),
):
    song_ids = sorted(set(payload.song_ids))
    if not song_ids:
        raise HTTPException(status_code=400, detail="song_ids is required")
    existing_song_count = db.query(func.count(Song.id)).filter(Song.id.in_(song_ids)).scalar() or 0
    if existing_song_count != len(song_ids):
        raise HTTPException(status_code=404, detail="One or more songs were not found")
    inserted_count = _add_skipped_songs(db, current_user.id, song_ids)
    db.commit()
    return {"skipped": inserted_count, "requested": len(song_ids)}


@app.post("/api/skips/artist")
def skip_artist(
    payload: SkipArtistRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_current_user),
):
    artist = payload.artist.strip()
    if not artist:
        raise HTTPException(status_code=400, detail="artist is required")
    artist_song_ids = [song_id for (song_id,) in db.query(Song.id).filter(Song.artist == artist).all()]
    if not artist_song_ids:
        raise HTTPException(status_code=404, detail="No songs found for artist")
    inserted_count = _add_skipped_songs(db, current_user.id, artist_song_ids)
    db.commit()
    return {"artist": artist, "skipped": inserted_count, "requested": len(artist_song_ids)}


@app.post("/api/skips/unskip-selected")
def unskip_selected(
    payload: UnskipSelectedRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_current_user),
):
    song_ids = sorted(set(payload.song_ids))
    if not song_ids:
        raise HTTPException(status_code=400, detail="song_ids is required")
    removed_count = (
        db.query(UserSkippedSong)
        .filter(and_(UserSkippedSong.user_id == current_user.id, UserSkippedSong.song_id.in_(song_ids)))
        .delete(synchronize_session=False)
    )
    db.commit()
    return {"unskipped": removed_count, "requested": len(song_ids)}


@app.post("/api/skips/unskip-all")
def unskip_all(
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_current_user),
):
    removed_count = db.query(UserSkippedSong).filter(UserSkippedSong.user_id == current_user.id).delete()
    db.commit()
    return {"unskipped": removed_count}


@app.post("/api/rankings/reset/personal")
def reset_personal_rankings(
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_current_user),
):
    deleted_votes = db.query(PairwiseVote).filter(PairwiseVote.user_id == current_user.id).delete(synchronize_session=False)
    deleted_scores = db.query(RatingScore).filter(RatingScore.user_id == current_user.id).delete(synchronize_session=False)
    deleted_skips = db.query(UserSkippedSong).filter(UserSkippedSong.user_id == current_user.id).delete(synchronize_session=False)
    db.commit()
    return {
        "reset_scope": "personal",
        "deleted_votes": deleted_votes,
        "deleted_scores": deleted_scores,
        "deleted_skips": deleted_skips,
    }


@app.post("/api/rankings/reset/global")
def reset_global_rankings(
    db: Session = Depends(get_db),
    _current_user: User = Depends(_require_current_user),
):
    deleted_votes = db.query(PairwiseVote).delete(synchronize_session=False)
    deleted_scores = db.query(RatingScore).delete(synchronize_session=False)
    deleted_skips = db.query(UserSkippedSong).delete(synchronize_session=False)
    db.commit()
    return {
        "reset_scope": "global",
        "deleted_votes": deleted_votes,
        "deleted_scores": deleted_scores,
        "deleted_skips": deleted_skips,
    }


@app.get("/api/rankings", response_model=RankingsResponse)
def get_rankings(
    artist: str | None = Query(default=None),
    album: str | None = Query(default=None),
    decade: str | None = Query(default=None),
    scope: str = Query(default="personal"),
    sort_by: str = Query(default="rank"),
    sort_dir: str = Query(default="asc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_current_user),
):
    filters: dict[str, Any] = {}
    if artist:
        filters["artist"] = artist
    if album:
        filters["album"] = album
    if normalized_decade := _normalize_decade(decade):
        filters["decade"] = normalized_decade

    scope = (scope or "personal").lower()
    if scope not in {"personal", "global"}:
        raise HTTPException(status_code=400, detail="Invalid rankings scope; expected personal or global")

    songs = _apply_filters(db.query(Song), filters).order_by(Song.id.asc()).all()
    if not songs:
        return RankingsResponse(filters={**filters, "scope": scope}, sort_by=sort_by, sort_dir=sort_dir, total=0, rows=[])

    song_ids = [song.id for song in songs]
    if scope == "global":
        winner_counts = (
            db.query(PairwiseVote.winner_song_id, func.count(PairwiseVote.id))
            .filter(PairwiseVote.winner_song_id.in_(song_ids))
            .group_by(PairwiseVote.winner_song_id)
            .all()
        )
        loser_counts = (
            db.query(PairwiseVote.loser_song_id, func.count(PairwiseVote.id))
            .filter(PairwiseVote.loser_song_id.in_(song_ids))
            .group_by(PairwiseVote.loser_song_id)
            .all()
        )
    else:
        winner_counts = (
            db.query(PairwiseVote.winner_song_id, func.count(PairwiseVote.id))
            .filter(and_(PairwiseVote.user_id == current_user.id, PairwiseVote.winner_song_id.in_(song_ids)))
            .group_by(PairwiseVote.winner_song_id)
            .all()
        )
        loser_counts = (
            db.query(PairwiseVote.loser_song_id, func.count(PairwiseVote.id))
            .filter(and_(PairwiseVote.user_id == current_user.id, PairwiseVote.loser_song_id.in_(song_ids)))
            .group_by(PairwiseVote.loser_song_id)
            .all()
        )

    vote_counts: dict[int, int] = {}
    winner_lookup: dict[int, int] = {}
    loser_lookup: dict[int, int] = {}
    for song_id, count in winner_counts:
        winner_lookup[song_id] = count
        vote_counts[song_id] = vote_counts.get(song_id, 0) + count
    for song_id, count in loser_counts:
        loser_lookup[song_id] = count
        vote_counts[song_id] = vote_counts.get(song_id, 0) + count

    rows = []
    for song in songs:
        winner_count = winner_lookup.get(song.id, 0)
        loser_count = loser_lookup.get(song.id, 0)
        vote_count = vote_counts.get(song.id, 0)
        if vote_count <= 0:
            continue
        row = _serialize_song(song, _ranking_score_from_matchups(winner_count, vote_count))
        row["winner_count"] = winner_count
        row["loser_count"] = loser_count
        row["vote_count"] = vote_count
        rows.append(row)

    rows.sort(key=lambda row: (-row["score"], row["artist"], row["title"], row["song_id"]))
    for index, row in enumerate(rows, start=1):
        row["rank"] = index

    reverse = sort_dir.lower() == "desc"
    if sort_by == "score":
        rows.sort(key=lambda row: (row["score"], row["rank"]), reverse=reverse)
    elif sort_by == "vote_count":
        rows.sort(key=lambda row: (row["vote_count"], row["rank"]), reverse=reverse)
    elif sort_by == "artist":
        rows.sort(key=lambda row: (row["artist"] or "", row["title"], row["rank"]), reverse=reverse)
    elif sort_by == "album":
        rows.sort(key=lambda row: (row["album"] or "", row["title"], row["rank"]), reverse=reverse)
    elif sort_by == "year":
        rows.sort(key=lambda row: (row["year"] or 0, row["title"], row["rank"]), reverse=reverse)
    else:
        rows.sort(key=lambda row: row["rank"], reverse=reverse)
        sort_by = "rank"

    start = (page - 1) * page_size
    end = start + page_size
    return RankingsResponse(
        filters={**filters, "scope": scope},
        sort_by=sort_by,
        sort_dir="desc" if reverse else "asc",
        total=len(rows),
        rows=rows[start:end],
    )


@app.get("/api/history", response_model=VoteHistoryResponse)
def get_vote_history(
    artist: str | None = Query(default=None),
    album: str | None = Query(default=None),
    decade: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_current_user),
):
    winner_song = aliased(Song)
    loser_song = aliased(Song)
    query = (
        db.query(PairwiseVote, winner_song, loser_song)
        .join(winner_song, winner_song.id == PairwiseVote.winner_song_id)
        .join(loser_song, loser_song.id == PairwiseVote.loser_song_id)
        .filter(PairwiseVote.user_id == current_user.id)
    )

    filters: dict[str, Any] = {}
    if artist:
        query = query.filter((winner_song.artist == artist) | (loser_song.artist == artist))
        filters["artist"] = artist
    if album:
        query = query.filter((winner_song.album == album) | (loser_song.album == album))
        filters["album"] = album
    if normalized_decade := _normalize_decade(decade):
        if bounds := _decade_bounds(normalized_decade):
            decade_start, decade_end = bounds
            query = query.filter(
                or_(
                    winner_song.year.between(decade_start, decade_end),
                    loser_song.year.between(decade_start, decade_end),
                    func.lower(winner_song.decade) == normalized_decade,
                    func.lower(loser_song.decade) == normalized_decade,
                )
            )
        else:
            query = query.filter(
                (func.lower(winner_song.decade) == normalized_decade)
                | (func.lower(loser_song.decade) == normalized_decade)
            )
        filters["decade"] = normalized_decade
    if date_from:
        query = query.filter(PairwiseVote.created_at >= datetime.fromisoformat(date_from))
        filters["date_from"] = date_from
    if date_to:
        query = query.filter(PairwiseVote.created_at <= datetime.fromisoformat(date_to))
        filters["date_to"] = date_to

    total = query.count()
    rows = (
        query.order_by(PairwiseVote.created_at.desc(), PairwiseVote.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return VoteHistoryResponse(
        filters=filters,
        page=page,
        page_size=page_size,
        total=total,
        rows=[
            {
                "vote_id": vote.id,
                "winner": {"song_id": winner.id, "title": winner.title, "artist": winner.artist},
                "loser": {"song_id": loser.id, "title": loser.title, "artist": loser.artist},
                "timestamp": vote.created_at.isoformat(),
                "context": json.loads(vote.context_metadata) if vote.context_metadata else {},
            }
            for vote, winner, loser in rows
        ],
    )


@app.get("/api/history/song/{song_id}", response_model=SongHistoryResponse)
def get_song_history(
    song_id: int,
    recent_limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_current_user),
):
    song = db.query(Song).filter(Song.id == song_id).first()
    if song is None:
        raise HTTPException(status_code=404, detail="Song not found")

    rating_row = (
        db.query(RatingScore)
        .filter(and_(RatingScore.user_id == current_user.id, RatingScore.song_id == song_id))
        .first()
    )
    current_score = rating_row.score if rating_row else DEFAULT_RATING

    winner_song = aliased(Song)
    loser_song = aliased(Song)
    matchup_rows = (
        db.query(PairwiseVote, winner_song, loser_song)
        .join(winner_song, winner_song.id == PairwiseVote.winner_song_id)
        .join(loser_song, loser_song.id == PairwiseVote.loser_song_id)
        .filter(
            PairwiseVote.user_id == current_user.id,
            (PairwiseVote.winner_song_id == song_id) | (PairwiseVote.loser_song_id == song_id),
        )
        .order_by(PairwiseVote.created_at.desc(), PairwiseVote.id.desc())
        .limit(recent_limit)
        .all()
    )

    return SongHistoryResponse(
        song=_serialize_song(song, current_score),
        current_score=current_score,
        recent_matchups=[
            {
                "vote_id": vote.id,
                "result": "win" if vote.winner_song_id == song_id else "loss",
                "opponent": {
                    "song_id": loser.id if vote.winner_song_id == song_id else winner.id,
                    "title": loser.title if vote.winner_song_id == song_id else winner.title,
                    "artist": loser.artist if vote.winner_song_id == song_id else winner.artist,
                },
                "timestamp": vote.created_at.isoformat(),
                "context": json.loads(vote.context_metadata) if vote.context_metadata else {},
            }
            for vote, winner, loser in matchup_rows
        ],
    )


@app.post("/api/rate/vote", response_model=VoteResponse)
def cast_vote(
    payload: VoteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_current_user),
):
    if payload.winner_song_id == payload.loser_song_id:
        raise HTTPException(status_code=400, detail="winner_song_id and loser_song_id must differ")

    songs = (
        db.query(Song)
        .filter(Song.id.in_([payload.winner_song_id, payload.loser_song_id]))
        .all()
    )
    songs_by_id = {song.id: song for song in songs}

    if payload.winner_song_id not in songs_by_id or payload.loser_song_id not in songs_by_id:
        raise HTTPException(status_code=404, detail="One or both songs were not found")

    winner_score = (
        db.query(RatingScore)
        .filter(
            RatingScore.user_id == current_user.id,
            RatingScore.song_id == payload.winner_song_id,
        )
        .first()
    )
    if winner_score is None:
        winner_score = RatingScore(user_id=current_user.id, song_id=payload.winner_song_id, score=DEFAULT_RATING)
        db.add(winner_score)

    loser_score = (
        db.query(RatingScore)
        .filter(
            RatingScore.user_id == current_user.id,
            RatingScore.song_id == payload.loser_song_id,
        )
        .first()
    )
    if loser_score is None:
        loser_score = RatingScore(user_id=current_user.id, song_id=payload.loser_song_id, score=DEFAULT_RATING)
        db.add(loser_score)

    expected_winner = _expected_score(winner_score.score, loser_score.score)
    expected_loser = _expected_score(loser_score.score, winner_score.score)

    winner_score.score = round(winner_score.score + ELO_K * (1 - expected_winner))
    loser_score.score = round(loser_score.score + ELO_K * (0 - expected_loser))

    vote = PairwiseVote(
        user_id=current_user.id,
        winner_song_id=payload.winner_song_id,
        loser_song_id=payload.loser_song_id,
        context_metadata=json.dumps(payload.filters, sort_keys=True),
    )
    db.add(vote)
    db.flush()

    db.add(
        RatingScoreSnapshot(
            vote_id=vote.id,
            user_id=current_user.id,
            song_id=payload.winner_song_id,
            score=winner_score.score,
        )
    )
    db.add(
        RatingScoreSnapshot(
            vote_id=vote.id,
            user_id=current_user.id,
            song_id=payload.loser_song_id,
            score=loser_score.score,
        )
    )

    db.commit()

    return VoteResponse(
        winner={
            "song_id": payload.winner_song_id,
            "title": songs_by_id[payload.winner_song_id].title,
            "score": winner_score.score,
        },
        loser={
            "song_id": payload.loser_song_id,
            "title": songs_by_id[payload.loser_song_id].title,
            "score": loser_score.score,
        },
    )
