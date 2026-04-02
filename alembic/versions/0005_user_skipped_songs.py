"""Add per-user skipped songs

Revision ID: 0005_user_skipped_songs
Revises: 0004_song_popularity_columns
Create Date: 2026-04-02
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0005_user_skipped_songs"
down_revision = "0004_song_popularity_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_skipped_songs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("song_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["song_id"], ["songs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "song_id", name="uq_user_skipped_songs_user_song"),
    )


def downgrade() -> None:
    op.drop_table("user_skipped_songs")
