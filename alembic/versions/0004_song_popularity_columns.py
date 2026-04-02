"""Add optional Plex popularity columns on songs

Revision ID: 0004_song_popularity_columns
Revises: 0003_auth_sessions
Create Date: 2026-04-02
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0004_song_popularity_columns"
down_revision = "0003_auth_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("songs", sa.Column("plex_user_rating", sa.Float(), nullable=True))
    op.add_column("songs", sa.Column("plex_rating_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("songs", "plex_rating_count")
    op.drop_column("songs", "plex_user_rating")
