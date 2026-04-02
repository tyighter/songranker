"""Add persistent YouTube lookup cache

Revision ID: 0007_youtube_lookup_cache
Revises: 0006_settings_popularity_weight
Create Date: 2026-04-02
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0007_youtube_lookup_cache"
down_revision = "0006_settings_popularity_weight"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = inspector.get_indexes(table_name)
    return any(index.get("name") == index_name for index in indexes)


def upgrade() -> None:
    if not _has_table("youtube_lookup_cache"):
        op.create_table(
            "youtube_lookup_cache",
            sa.Column("id", sa.BigInteger(), nullable=False),
            sa.Column("query_key", sa.String(length=512), nullable=False),
            sa.Column("title_norm", sa.String(length=255), nullable=False),
            sa.Column("artist_norm", sa.String(length=255), nullable=False),
            sa.Column("result", sa.String(length=16), nullable=False),
            sa.Column("video_id", sa.String(length=32), nullable=True),
            sa.Column("code", sa.String(length=64), nullable=True),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("confidence", sa.String(length=32), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _has_index("youtube_lookup_cache", "ix_youtube_lookup_cache_expires_at"):
        op.create_index("ix_youtube_lookup_cache_expires_at", "youtube_lookup_cache", ["expires_at"], unique=False)

    if not _has_index("youtube_lookup_cache", "ix_youtube_lookup_cache_query_key"):
        op.create_index("ix_youtube_lookup_cache_query_key", "youtube_lookup_cache", ["query_key"], unique=True)


def downgrade() -> None:
    if _has_index("youtube_lookup_cache", "ix_youtube_lookup_cache_query_key"):
        op.drop_index("ix_youtube_lookup_cache_query_key", table_name="youtube_lookup_cache")

    if _has_index("youtube_lookup_cache", "ix_youtube_lookup_cache_expires_at"):
        op.drop_index("ix_youtube_lookup_cache_expires_at", table_name="youtube_lookup_cache")

    if _has_table("youtube_lookup_cache"):
        op.drop_table("youtube_lookup_cache")
