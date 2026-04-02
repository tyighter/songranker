"""Add popularity weight to app settings

Revision ID: 0006_settings_popularity_weight
Revises: 0005_user_skipped_songs
Create Date: 2026-04-02
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0006_settings_popularity_weight"
down_revision = "0005_user_skipped_songs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "settings",
        sa.Column("popularity_weight", sa.Float(), nullable=False, server_default=sa.text("0.35")),
    )
    op.alter_column("settings", "popularity_weight", server_default=None)


def downgrade() -> None:
    op.drop_column("settings", "popularity_weight")
