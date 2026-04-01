import json
import math
from itertools import combinations
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, text
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.config import settings
from app.db import get_db
from app.models import PairwiseVote, RatingScore, RatingScoreSnapshot, Song

DEFAULT_RATING = 1000
ELO_K = 24

app = FastAPI(title="SongRanker")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


class NextPairResponse(BaseModel):
    filters: dict[str, Any]
    pair: list[dict[str, Any]]


class VoteRequest(BaseModel):
    user_id: int
    winner_song_id: int
    loser_song_id: int
    filters: dict[str, Any] = Field(default_factory=dict)


class VoteResponse(BaseModel):
    winner: dict[str, Any]
    loser: dict[str, Any]


def _normalize_pair(song_a: int, song_b: int) -> tuple[int, int]:
    return (song_a, song_b) if song_a < song_b else (song_b, song_a)


def _expected_score(rating_a: int, rating_b: int) -> float:
    return 1.0 / (1.0 + math.pow(10, (rating_b - rating_a) / 400.0))


def _apply_filters(query, filters: dict[str, Any]):
    if artist := filters.get("artist"):
        query = query.filter(Song.artist == artist)

    if title_query := filters.get("title_query"):
        query = query.filter(Song.title.ilike(f"%{title_query}%"))

    if song_ids := filters.get("song_ids"):
        query = query.filter(Song.id.in_(song_ids))

    return query


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
    last_pair = (
        _normalize_pair(last_vote.winner_song_id, last_vote.loser_song_id) if last_vote else None
    )

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

        # lower key is better: close ratings and fewer prior comparisons are most uncertain.
        rank_key = (closeness + (prior_matches * 25), pair_key[0], pair_key[1])

        if best_key is None or rank_key < best_key:
            best_key = rank_key
            best_pair = (songs_by_id[song_a_id], songs_by_id[song_b_id])

    if best_pair:
        return best_pair

    # Fallback if only possible pair is the immediate repeat.
    fallback_song_a_id, fallback_song_b_id = _normalize_pair(song_ids[0], song_ids[1])
    return songs_by_id[fallback_song_a_id], songs_by_id[fallback_song_b_id]


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_host": settings.app_host,
            "app_port": settings.app_port,
        },
    )


@app.get("/api/rate/next", response_model=NextPairResponse)
def get_next_pair(
    user_id: int = Query(...),
    artist: str | None = Query(default=None),
    title_query: str | None = Query(default=None),
    song_ids: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    filters: dict[str, Any] = {}
    if artist:
        filters["artist"] = artist
    if title_query:
        filters["title_query"] = title_query
    if song_ids:
        filters["song_ids"] = [int(song_id.strip()) for song_id in song_ids.split(",") if song_id.strip()]

    pair = _candidate_pair_for_user(db=db, user_id=user_id, filters=filters)
    if pair is None:
        raise HTTPException(status_code=404, detail="Not enough songs available for this filter context")

    song_ids_for_pair = [pair[0].id, pair[1].id]
    rating_rows = (
        db.query(RatingScore)
        .filter(and_(RatingScore.user_id == user_id, RatingScore.song_id.in_(song_ids_for_pair)))
        .all()
    )
    rating_lookup = {row.song_id: row.score for row in rating_rows}

    return NextPairResponse(
        filters=filters,
        pair=[
            {
                "song_id": song.id,
                "title": song.title,
                "artist": song.artist,
                "score": rating_lookup.get(song.id, DEFAULT_RATING),
            }
            for song in pair
        ],
    )


@app.post("/api/rate/vote", response_model=VoteResponse)
def cast_vote(payload: VoteRequest, db: Session = Depends(get_db)):
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
            RatingScore.user_id == payload.user_id,
            RatingScore.song_id == payload.winner_song_id,
        )
        .first()
    )
    if winner_score is None:
        winner_score = RatingScore(user_id=payload.user_id, song_id=payload.winner_song_id, score=DEFAULT_RATING)
        db.add(winner_score)

    loser_score = (
        db.query(RatingScore)
        .filter(
            RatingScore.user_id == payload.user_id,
            RatingScore.song_id == payload.loser_song_id,
        )
        .first()
    )
    if loser_score is None:
        loser_score = RatingScore(user_id=payload.user_id, song_id=payload.loser_song_id, score=DEFAULT_RATING)
        db.add(loser_score)

    expected_winner = _expected_score(winner_score.score, loser_score.score)
    expected_loser = _expected_score(loser_score.score, winner_score.score)

    winner_score.score = round(winner_score.score + ELO_K * (1 - expected_winner))
    loser_score.score = round(loser_score.score + ELO_K * (0 - expected_loser))

    vote = PairwiseVote(
        user_id=payload.user_id,
        winner_song_id=payload.winner_song_id,
        loser_song_id=payload.loser_song_id,
        context_metadata=json.dumps(payload.filters, sort_keys=True),
    )
    db.add(vote)
    db.flush()

    db.add(
        RatingScoreSnapshot(
            vote_id=vote.id,
            user_id=payload.user_id,
            song_id=payload.winner_song_id,
            score=winner_score.score,
        )
    )
    db.add(
        RatingScoreSnapshot(
            vote_id=vote.id,
            user_id=payload.user_id,
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
