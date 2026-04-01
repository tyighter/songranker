import asyncio
import hashlib
import hmac
import json
import logging
import math
import secrets
from datetime import datetime, timedelta, timezone
from itertools import combinations
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from fastapi import Depends, FastAPI, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, text
from sqlalchemy.orm import Session, aliased
from starlette.requests import Request as StarletteRequest

from app.config import settings
from app.db import SessionLocal, get_db
from app.models import AppSettings, PairwiseVote, RatingScore, RatingScoreSnapshot, Song, User, UserSession

DEFAULT_RATING = 1000
ELO_K = 24
SESSION_COOKIE_NAME = "songranker_session"
SESSION_TTL_DAYS = 30
LOG_FILE_PATH = Path("/log.log")

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


def _sync_tracks_from_plex(db: Session, app_settings: AppSettings) -> dict[str, int]:
    if not app_settings.plex_url or not app_settings.plex_token or not app_settings.plex_music_section_id:
        raise HTTPException(status_code=400, detail="Plex settings are incomplete")

    root = _plex_get_xml(
        app_settings.plex_url,
        app_settings.plex_token,
        f"/library/sections/{app_settings.plex_music_section_id}/all",
    )

    imported = 0
    updated = 0

    for track in root.findall("Track"):
        rating_key = track.attrib.get("ratingKey")
        if not rating_key:
            continue

        title = track.attrib.get("title", "Unknown")
        artist = track.attrib.get("grandparentTitle") or track.attrib.get("originalTitle") or "Unknown"
        album = track.attrib.get("parentTitle")
        year_raw = track.attrib.get("year")
        year = int(year_raw) if year_raw and year_raw.isdigit() else None
        source_uri = track.attrib.get("key")

        existing = db.query(Song).filter(Song.plex_rating_key == rating_key).first()
        if existing:
            existing.title = title
            existing.artist = artist
            existing.album = album
            existing.year = year
            existing.decade = _decade_for_year(year)
            existing.source_uri = source_uri
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
                    source_uri=source_uri,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            imported += 1

    app_settings.last_sync_at = datetime.now(timezone.utc)
    db.commit()

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


def _expected_score(rating_a: int, rating_b: int) -> float:
    return 1.0 / (1.0 + math.pow(10, (rating_b - rating_a) / 400.0))


def _apply_filters(query, filters: dict[str, Any]):
    if artist := filters.get("artist"):
        query = query.filter(Song.artist == artist)

    if album := filters.get("album"):
        query = query.filter(Song.album == album)

    if decade := filters.get("decade"):
        query = query.filter(Song.decade == decade)

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
        album_art_url = (
            f"{app_settings.plex_url.rstrip('/')}/library/metadata/{song.plex_rating_key}/thumb"
            f"?X-Plex-Token={app_settings.plex_token}"
        )
    payload["album_art_url"] = album_art_url
    return payload


def _candidate_pair_for_user(db: Session, user_id: int, filters: dict[str, Any]) -> tuple[Song, Song] | None:
    songs_query = db.query(Song)
    songs_query = _apply_filters(songs_query, filters)
    songs = songs_query.order_by(Song.id.asc()).all()

    if len(songs) < 2:
        return None

    song_ids = [song.id for song in songs]
    ratings = {
        row.song_id: row.score
        for row in db.query(RatingScore)
        .filter(and_(RatingScore.user_id == user_id, RatingScore.song_id.in_(song_ids)))
        .all()
    }

    vote_counts = {
        _normalize_pair(winner_id, loser_id): count
        for winner_id, loser_id, count in db.query(
            PairwiseVote.winner_song_id,
            PairwiseVote.loser_song_id,
            func.count(PairwiseVote.id),
        )
        .filter(
            PairwiseVote.user_id == user_id,
            PairwiseVote.winner_song_id.in_(song_ids),
            PairwiseVote.loser_song_id.in_(song_ids),
        )
        .group_by(PairwiseVote.winner_song_id, PairwiseVote.loser_song_id)
        .all()
    }

    last_vote = (
        db.query(PairwiseVote)
        .filter(PairwiseVote.user_id == user_id)
        .order_by(PairwiseVote.created_at.desc(), PairwiseVote.id.desc())
        .first()
    )
    last_pair = _normalize_pair(last_vote.winner_song_id, last_vote.loser_song_id) if last_vote else None

    best_pair: tuple[Song, Song] | None = None
    best_key: tuple[float, int, int] | None = None
    songs_by_id = {song.id: song for song in songs}

    for song_a_id, song_b_id in combinations(song_ids, 2):
        pair_key = _normalize_pair(song_a_id, song_b_id)
        if pair_key == last_pair:
            continue

        score_a = ratings.get(song_a_id, DEFAULT_RATING)
        score_b = ratings.get(song_b_id, DEFAULT_RATING)
        closeness = abs(score_a - score_b)
        prior_matches = vote_counts.get(pair_key, 0)

        rank_key = (closeness + (prior_matches * 25), pair_key[0], pair_key[1])

        if best_key is None or rank_key < best_key:
            best_key = rank_key
            best_pair = (songs_by_id[song_a_id], songs_by_id[song_b_id])

    if best_pair:
        return best_pair

    fallback_song_a_id, fallback_song_b_id = _normalize_pair(song_ids[0], song_ids[1])
    return songs_by_id[fallback_song_a_id], songs_by_id[fallback_song_b_id]


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
            logging.getLogger(__name__).exception("Periodic Plex sync failed")
        finally:
            db.close()


@app.on_event("startup")
async def startup_event():
    _configure_logging()
    logging.getLogger(__name__).info("Logging initialized at %s (weekly rotation enabled)", LOG_FILE_PATH)
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
    result = _sync_tracks_from_plex(db, app_settings)
    return {"status": "ok", **result}


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
    app_settings = _get_or_create_settings(db)
    filters: dict[str, Any] = {}
    if artist:
        filters["artist"] = artist
    if album:
        filters["album"] = album
    if decade:
        filters["decade"] = decade
    if title_query:
        filters["title_query"] = title_query
    if song_ids:
        filters["song_ids"] = [int(song_id.strip()) for song_id in song_ids.split(",") if song_id.strip()]

    pair = _candidate_pair_for_user(db=db, user_id=current_user.id, filters=filters)
    if pair is None:
        raise HTTPException(status_code=404, detail="Not enough songs available for this filter context")

    song_ids_for_pair = [pair[0].id, pair[1].id]
    rating_rows = (
        db.query(RatingScore)
        .filter(and_(RatingScore.user_id == current_user.id, RatingScore.song_id.in_(song_ids_for_pair)))
        .all()
    )
    rating_lookup = {row.song_id: row.score for row in rating_rows}

    return NextPairResponse(
        filters=filters,
        pair=[
            _serialize_song_for_pair(song, rating_lookup.get(song.id, DEFAULT_RATING), app_settings)
            for song in pair
        ],
    )


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


@app.get("/api/rankings", response_model=RankingsResponse)
def get_rankings(
    artist: str | None = Query(default=None),
    album: str | None = Query(default=None),
    decade: str | None = Query(default=None),
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
    if decade:
        filters["decade"] = decade

    songs = _apply_filters(db.query(Song), filters).order_by(Song.id.asc()).all()
    if not songs:
        return RankingsResponse(filters=filters, sort_by=sort_by, sort_dir=sort_dir, total=0, rows=[])

    song_ids = [song.id for song in songs]
    rating_rows = (
        db.query(RatingScore)
        .filter(and_(RatingScore.user_id == current_user.id, RatingScore.song_id.in_(song_ids)))
        .all()
    )
    rating_lookup = {row.song_id: row.score for row in rating_rows}
    rows = [_serialize_song(song, rating_lookup.get(song.id, DEFAULT_RATING)) for song in songs]

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
        filters=filters,
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
    if decade:
        query = query.filter((winner_song.decade == decade) | (loser_song.decade == decade))
        filters["decade"] = decade
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
