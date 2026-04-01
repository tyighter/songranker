"""Initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("username", sa.String(length=64), nullable=False, unique=True),
        sa.Column("email", sa.String(length=255), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "songs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("artist", sa.String(length=255), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "pairwise_votes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("winner_song_id", sa.BigInteger(), sa.ForeignKey("songs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("loser_song_id", sa.BigInteger(), sa.ForeignKey("songs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("context_metadata", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "rating_scores",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("song_id", sa.BigInteger(), sa.ForeignKey("songs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False, server_default="1000"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_unique_constraint("uq_rating_scores_user_song", "rating_scores", ["user_id", "song_id"])


def downgrade() -> None:
    op.drop_constraint("uq_rating_scores_user_song", "rating_scores", type_="unique")
    op.drop_table("rating_scores")
    op.drop_table("pairwise_votes")
    op.drop_table("songs")
    op.drop_table("users")
