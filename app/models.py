import secrets

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, event, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    profile_image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    profile_image_mime: Mapped[str | None] = mapped_column(String(64), nullable=True)
    profile_image_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class UserIdentity(Base):
    __tablename__ = "user_identities"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("provider", "provider_subject", name="uq_user_identities_provider_subject"),
        UniqueConstraint("user_id", "provider", name="uq_user_identities_user_provider"),
    )


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_token: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)


class Song(Base):
    __tablename__ = "songs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    artist: Mapped[str] = mapped_column(String(255), nullable=False)
    album: Mapped[str | None] = mapped_column(String(255), nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decade: Mapped[str | None] = mapped_column(String(16), nullable=True)
    plex_rating_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    plex_user_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    plex_rating_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AppSettings(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    is_initialized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    plex_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    plex_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    plex_music_section_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    popularity_weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.35)
    last_sync_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PairwiseVote(Base):
    __tablename__ = "pairwise_votes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    winner_song_id: Mapped[int] = mapped_column(ForeignKey("songs.id", ondelete="CASCADE"), nullable=False)
    loser_song_id: Mapped[int] = mapped_column(ForeignKey("songs.id", ondelete="CASCADE"), nullable=False)
    context_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RatingScore(Base):
    __tablename__ = "rating_scores"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    song_id: Mapped[int] = mapped_column(ForeignKey("songs.id", ondelete="CASCADE"), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RatingScoreSnapshot(Base):
    __tablename__ = "rating_score_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vote_id: Mapped[int] = mapped_column(ForeignKey("pairwise_votes.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    song_id: Mapped[int] = mapped_column(ForeignKey("songs.id", ondelete="CASCADE"), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("vote_id", "song_id", name="uq_rating_score_snapshots_vote_song"),
    )


class UserSkippedSong(Base):
    __tablename__ = "user_skipped_songs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    song_id: Mapped[int] = mapped_column(ForeignKey("songs.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "song_id", name="uq_user_skipped_songs_user_song"),
    )


class YouTubeLookupCache(Base):
    __tablename__ = "youtube_lookup_cache"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    query_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    title_norm: Mapped[str] = mapped_column(String(255), nullable=False)
    artist_norm: Mapped[str] = mapped_column(String(255), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    video_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    checked_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


def _assign_bigint_pk_for_sqlite(mapper, connection, target):
    """SQLite only auto-increments INTEGER PRIMARY KEY, not BIGINT."""
    if getattr(target, "id", None) is not None:
        return

    if connection.dialect.name != "sqlite":
        return

    # Avoid max(id)+1 allocation for SQLite: concurrent transactions can
    # compute the same value and collide on commit/flush.
    # Use a random signed 63-bit integer so IDs remain valid BIGINT values.
    target.id = secrets.randbits(63)


for model in (
    User,
    UserIdentity,
    UserSession,
    Song,
    PairwiseVote,
    RatingScore,
    RatingScoreSnapshot,
    UserSkippedSong,
    YouTubeLookupCache,
):
    event.listen(model, "before_insert", _assign_bigint_pk_for_sqlite)
