"""Add setup settings and plex metadata cache

Revision ID: 0002_setup_and_plex_cache
Revises: 0001_initial_schema
Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0002_setup_and_plex_cache"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("is_initialized", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("plex_url", sa.String(length=512), nullable=True),
        sa.Column("plex_token", sa.String(length=255), nullable=True),
        sa.Column("plex_music_section_id", sa.String(length=64), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.add_column("songs", sa.Column("album", sa.String(length=255), nullable=True))
    op.add_column("songs", sa.Column("year", sa.Integer(), nullable=True))
    op.add_column("songs", sa.Column("decade", sa.String(length=16), nullable=True))
    op.add_column("songs", sa.Column("plex_rating_key", sa.String(length=64), nullable=True))
    op.add_column("songs", sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("uq_songs_plex_rating_key", "songs", ["plex_rating_key"], unique=True)

    op.execute("INSERT INTO settings (id, is_initialized) VALUES (1, false)")


def downgrade() -> None:
    op.drop_index("uq_songs_plex_rating_key", table_name="songs")
    op.drop_column("songs", "updated_at")
    op.drop_column("songs", "plex_rating_key")
    op.drop_column("songs", "decade")
    op.drop_column("songs", "year")
    op.drop_column("songs", "album")
    op.drop_table("settings")
