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


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = inspector.get_columns(table_name)
    return any(column["name"] == column_name for column in columns)


def upgrade() -> None:
    if not _has_column("settings", "popularity_weight"):
        op.add_column(
            "settings",
            sa.Column("popularity_weight", sa.Float(), nullable=False, server_default=sa.text("0.35")),
        )

    # SQLite does not support ALTER COLUMN ... DROP DEFAULT.
    if op.get_bind().dialect.name != "sqlite":
        op.alter_column("settings", "popularity_weight", server_default=None)


def downgrade() -> None:
    if _has_column("settings", "popularity_weight"):
        op.drop_column("settings", "popularity_weight")
