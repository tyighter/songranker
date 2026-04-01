from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import Depends, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlalchemy.orm import Session
from starlette.requests import Request as StarletteRequest

from app.config import settings
from app.db import get_db
from app.models import Settings, Song, User

app = FastAPI(title="SongRanker")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

RESYNC_INTERVAL = timedelta(minutes=30)


def get_or_create_settings(db: Session) -> Settings:
    record = db.query(Settings).filter(Settings.id == 1).one_or_none()
    if record is None:
        record = Settings(id=1, is_initialized=False)
        db.add(record)
        db.commit()
        db.refresh(record)
    return record


def plex_get(plex_url: str, plex_token: str, path: str, params: dict[str, str] | None = None) -> dict:
    query_params = params or {}
    query_params["X-Plex-Token"] = plex_token
    full_url = f"{plex_url.rstrip('/')}{path}?{urlencode(query_params)}"
    req = Request(full_url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_music_sections(plex_url: str, plex_token: str) -> list[dict[str, str]]:
    payload = plex_get(plex_url, plex_token, "/library/sections")
    directories = payload.get("MediaContainer", {}).get("Directory", [])
    sections: list[dict[str, str]] = []
    for directory in directories:
        if directory.get("type") == "artist":
            sections.append({"id": str(directory.get("key")), "title": directory.get("title", "Untitled")})
    return sections


def iter_tracks(plex_url: str, plex_token: str, section_id: str):
    payload = plex_get(
        plex_url,
        plex_token,
        f"/library/sections/{section_id}/all",
        {"type": "10", "includeGuids": "1"},
    )
    for item in payload.get("MediaContainer", {}).get("Metadata", []):
        yield item


def decade_from_year(year: int | None) -> str | None:
    if not year:
        return None
    return f"{(year // 10) * 10}s"


def resync_tracks(db: Session, app_settings: Settings) -> int:
    if not app_settings.plex_url or not app_settings.plex_token or not app_settings.plex_library_section_id:
        raise HTTPException(status_code=400, detail="Plex settings are incomplete")

    synced_at = datetime.now(UTC)
    imported = 0
    for track in iter_tracks(app_settings.plex_url, app_settings.plex_token, app_settings.plex_library_section_id):
        rating_key = str(track.get("ratingKey")) if track.get("ratingKey") else None
        year = track.get("year") if isinstance(track.get("year"), int) else None

        song = None
        if rating_key:
            song = db.query(Song).filter(Song.plex_rating_key == rating_key).one_or_none()

        if song is None:
            song = Song(plex_rating_key=rating_key)
            db.add(song)

        song.title = track.get("title") or "Unknown title"
        song.artist = track.get("grandparentTitle") or track.get("originalTitle") or "Unknown artist"
        song.album = track.get("parentTitle")
        song.year = year
        song.decade = decade_from_year(year)
        song.source_uri = track.get("guid")
        song.last_synced_at = synced_at
        imported += 1

    app_settings.last_resync_at = synced_at
    db.commit()
    return imported


@app.middleware("http")
def enforce_setup(request: StarletteRequest, call_next):
    if request.url.path.startswith("/static") or request.url.path in {"/health", "/setup", "/setup/sections", "/resync"}:
        return call_next(request)

    db = next(get_db())
    try:
        app_settings = get_or_create_settings(db)
        if not app_settings.is_initialized:
            return RedirectResponse(url="/setup", status_code=307)

        if app_settings.last_resync_at is None or datetime.now(UTC) - app_settings.last_resync_at > RESYNC_INTERVAL:
            try:
                resync_tracks(db, app_settings)
            except Exception:
                db.rollback()
        return call_next(request)
    finally:
        db.close()


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: StarletteRequest, db: Session = Depends(get_db)):
    song_count = db.query(func.count(Song.id)).scalar() or 0
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_host": settings.app_host,
            "app_port": settings.app_port,
            "song_count": song_count,
        },
    )


@app.get("/setup", response_class=HTMLResponse)
def setup(request: StarletteRequest, db: Session = Depends(get_db)):
    app_settings = get_or_create_settings(db)
    if app_settings.is_initialized:
        return RedirectResponse(url="/", status_code=303)

    return templates.TemplateResponse(
        "setup.html",
        {
            "request": request,
            "sections": None,
            "form_data": {},
            "error": None,
        },
    )


@app.post("/setup/sections", response_class=HTMLResponse)
def setup_sections(
    request: StarletteRequest,
    username: str = Form(...),
    email: str = Form(...),
    plex_url: str = Form(...),
    plex_token: str = Form(...),
):
    form_data = {
        "username": username.strip(),
        "email": email.strip(),
        "plex_url": plex_url.strip(),
        "plex_token": plex_token.strip(),
    }
    try:
        sections = fetch_music_sections(form_data["plex_url"], form_data["plex_token"])
    except Exception as exc:
        return templates.TemplateResponse(
            "setup.html",
            {"request": request, "sections": None, "form_data": form_data, "error": f"Could not connect to Plex: {exc}"},
            status_code=400,
        )

    return templates.TemplateResponse(
        "setup.html",
        {
            "request": request,
            "sections": sections,
            "form_data": form_data,
            "error": None,
        },
    )


@app.post("/setup/complete")
def setup_complete(
    username: str = Form(...),
    email: str = Form(...),
    plex_url: str = Form(...),
    plex_token: str = Form(...),
    plex_library_section_id: str = Form(...),
    db: Session = Depends(get_db),
):
    app_settings = get_or_create_settings(db)

    if app_settings.is_initialized:
        return RedirectResponse(url="/", status_code=303)

    user = User(username=username.strip(), email=email.strip())
    db.add(user)

    app_settings.plex_url = plex_url.strip()
    app_settings.plex_token = plex_token.strip()
    app_settings.plex_library_section_id = plex_library_section_id.strip()
    app_settings.is_initialized = True
    db.flush()

    try:
        resync_tracks(db, app_settings)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Unable to run initial Plex import: {exc}") from exc

    return RedirectResponse(url="/", status_code=303)


@app.post("/resync")
def manual_resync(db: Session = Depends(get_db)) -> dict[str, int | str]:
    app_settings = get_or_create_settings(db)
    if not app_settings.is_initialized:
        raise HTTPException(status_code=400, detail="App is not initialized")

    imported = resync_tracks(db, app_settings)
    return {"status": "ok", "imported_tracks": imported}
