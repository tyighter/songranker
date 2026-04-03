"""Add profile photo fields to users

Revision ID: 0009_user_profile_photo_fields
Revises: 0008_user_identities
Create Date: 2026-04-03
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0009_user_profile_photo_fields"
down_revision = "0008_user_identities"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = inspector.get_columns(table_name)
    return any(column["name"] == column_name for column in columns)


def upgrade() -> None:
    if not _has_column("users", "profile_image_path"):
        op.add_column("users", sa.Column("profile_image_path", sa.String(length=512), nullable=True))
    if not _has_column("users", "profile_image_mime"):
        op.add_column("users", sa.Column("profile_image_mime", sa.String(length=64), nullable=True))
    if not _has_column("users", "profile_image_size_bytes"):
        op.add_column("users", sa.Column("profile_image_size_bytes", sa.Integer(), nullable=True))


def downgrade() -> None:
    if _has_column("users", "profile_image_size_bytes"):
        op.drop_column("users", "profile_image_size_bytes")
    if _has_column("users", "profile_image_mime"):
        op.drop_column("users", "profile_image_mime")
    if _has_column("users", "profile_image_path"):
        op.drop_column("users", "profile_image_path")
