import asyncio
import hashlib
import hmac
import json
import logging
import math
import random
import secrets
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from fastapi import Depends, FastAPI, Form, HTTPException, Query
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
    UserSession,
    UserSkippedSong,
)

DEFAULT_RATING = 1000
ELO_K = 24
RANKING_VOTE_WEIGHT = 10
POPULARITY_MAX_BOOST = 0.35
POPULARITY_RATING_COUNT_CAP = 500
POPULARITY_USER_RATING_MAX = 10.0
POPULARITY_COUNT_WEIGHT = 0.6
POPULARITY_USER_RATING_WEIGHT = 0.4
SESSION_COOKIE_NAME = "songranker_session"
SESSION_TTL_DAYS = 30
LOG_FILE_PATH = Path("/log.log")
logger = logging.getLogger(__name__)

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
    token = secrets.token_urlsafe(48)
    now = datetime.now(timezone.utc)
    db.add(
        UserSession(
            session_token=token,
            user_id=user_id,
            expires_at=now + timedelta(days=SESSION_TTL_DAYS),
        )
    )
    db.commit()
    return token


def _session_redirect(url: str, token: str) -> RedirectResponse:
    response = RedirectResponse(url=url, status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=SESSION_TTL_DAYS * 24 * 60 * 60,
    )
    return response


def _clear_session_cookie(response: RedirectResponse):
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
    cleaned = str(decade).strip().lower().replace("'", "")
    return cleaned or None


def _sync_tracks_from_plex(db: Session, app_settings: AppSettings) -> dict[str, int]:
    if not app_settings.plex_url or not app_settings.plex_token or not app_settings.plex_music_section_id:
        raise HTTPException(status_code=400, detail="Plex settings are incomplete")

    logger.info("Starting Plex sync for section %s", app_settings.plex_music_section_id)
    imported = 0
    updated = 0
    page_size = 200
    start = 0

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


def _song_selection_weight(song: Song) -> float:
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
    return max(0.0001, 1.0 + (POPULARITY_MAX_BOOST * blended_signal))


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


def _ranking_score_with_vote_weight(score: int, vote_count: int) -> int:
    confidence = vote_count / (vote_count + RANKING_VOTE_WEIGHT)
    weighted_score = DEFAULT_RATING + ((score - DEFAULT_RATING) * confidence)
    return round(weighted_score)


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
    album_art_url = None
    if app_settings.plex_url and app_settings.plex_token and song.plex_rating_key:
        album_art_url = f"/api/plex/album-art/{song.plex_rating_key}"
    payload["album_art_url"] = album_art_url
    payload["plex_user_rating"] = song.plex_user_rating
    payload["plex_rating_count"] = song.plex_rating_count
    payload["selection_weight"] = _song_selection_weight(song)
    return payload


def _candidate_pair_for_user(db: Session, user_id: int, filters: dict[str, Any]) -> tuple[Song, Song] | None:
    started_at = datetime.now(timezone.utc)
    skipped_song_ids = {
        row.song_id
        for row in db.query(UserSkippedSong.song_id).filter(UserSkippedSong.user_id == user_id).all()
    }
    songs_query = _apply_filters(db.query(Song), filters)
    if skipped_song_ids:
        songs_query = songs_query.filter(~Song.id.in_(skipped_song_ids))
    candidates = songs_query.order_by(Song.id.asc()).all()
    song_rows = [(song.id, _song_selection_weight(song)) for song in candidates]
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
        "Pair selected from weighted filtered pool | user_id=%s filters=%s song_count=%s pair=%s pair_weights=%s popularity_max_boost=%s elapsed_ms=%s",
        user_id,
        filters,
        len(song_ids),
        [selected_pair[0], selected_pair[1]],
        selected_weights,
        POPULARITY_MAX_BOOST,
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


@app.get("/", response_class=HTMLResponse)
def index(request: StarletteRequest, db: Session = Depends(get_db)):
    session = _current_session(db, request)
    current_user = None
    if session is not None:
        current_user = db.query(User).filter(User.id == session.user_id).first()
    users = db.query(User).order_by(User.username.asc()).all()

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_host": settings.app_host,
            "app_port": settings.app_port,
            "current_user": current_user,
            "is_authenticated": current_user is not None,
            "users": users,
            "notice": "Auth is intentionally lightweight and not secure yet.",
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
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username).first()
    if user is None or not _verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username/password")

    token = _create_session_for_user(db, user.id)
    return _session_redirect(url="/", token=token)


@app.post("/api/auth/switch-user")
def switch_user(
    user_id: int = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not _verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials for selected user")

    token = _create_session_for_user(db, user.id)
    return _session_redirect(url="/", token=token)


@app.post("/api/auth/signout")
def signout(request: StarletteRequest, db: Session = Depends(get_db)):
    session = _current_session(db, request)
    if session is not None:
        db.delete(session)
        db.commit()

    response = RedirectResponse(url="/", status_code=303)
    _clear_session_cookie(response)
    return response


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
        "libraries": libraries,
        "status": status,
        "status_ok": error is None and bool(app_settings.plex_url and app_settings.plex_token),
    }


@app.post("/api/plex/settings")
def save_plex_settings(
    plex_url: str = Form(...),
    plex_token: str = Form(...),
    db: Session = Depends(get_db),
):
    app_settings = _get_or_create_settings(db)
    app_settings.plex_url = plex_url.strip().rstrip("/")
    app_settings.plex_token = plex_token.strip()
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

    pair = _candidate_pair_for_user(db=db, user_id=current_user.id, filters=filters)
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
        rating_rows = (
            db.query(
                RatingScore.song_id,
                func.round(func.avg(RatingScore.score)).label("avg_score"),
            )
            .filter(RatingScore.song_id.in_(song_ids))
            .group_by(RatingScore.song_id)
            .all()
        )
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
        rating_lookup = {row.song_id: int(row.avg_score or DEFAULT_RATING) for row in rating_rows}
    else:
        rating_rows = (
            db.query(RatingScore)
            .filter(and_(RatingScore.user_id == current_user.id, RatingScore.song_id.in_(song_ids)))
            .all()
        )
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
        rating_lookup = {row.song_id: row.score for row in rating_rows}

    vote_counts: dict[int, int] = {}
    for song_id, count in winner_counts:
        vote_counts[song_id] = vote_counts.get(song_id, 0) + count
    for song_id, count in loser_counts:
        vote_counts[song_id] = vote_counts.get(song_id, 0) + count

    rows = []
    for song in songs:
        raw_score = rating_lookup.get(song.id, DEFAULT_RATING)
        vote_count = vote_counts.get(song.id, 0)
        row = _serialize_song(song, _ranking_score_with_vote_weight(raw_score, vote_count))
        row["vote_count"] = vote_count
        row["raw_score"] = raw_score
        rows.append(row)

    rows.sort(key=lambda row: (-row["score"], row["artist"], row["title"], row["song_id"]))
    for index, row in enumerate(rows, start=1):
        row["rank"] = index

    reverse = sort_dir.lower() == "desc"
    if sort_by == "score":
        rows.sort(key=lambda row: (row["score"], row["rank"]), reverse=reverse)
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
