"""Add user identities for federated auth

Revision ID: 0008_user_identities
Revises: 0007_youtube_lookup_cache
Create Date: 2026-04-03
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0008_user_identities"
down_revision = "0007_youtube_lookup_cache"
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
    if not _has_table("user_identities"):
        op.create_table(
            "user_identities",
            sa.Column("id", sa.BigInteger(), nullable=False),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("provider_subject", sa.String(length=255), nullable=False),
            sa.Column("email", sa.String(length=255), nullable=True),
            sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("provider", "provider_subject", name="uq_user_identities_provider_subject"),
            sa.UniqueConstraint("user_id", "provider", name="uq_user_identities_user_provider"),
        )

    if not _has_index("user_identities", "ix_user_identities_user_id"):
        op.create_index("ix_user_identities_user_id", "user_identities", ["user_id"], unique=False)


def downgrade() -> None:
    if _has_index("user_identities", "ix_user_identities_user_id"):
        op.drop_index("ix_user_identities_user_id", table_name="user_identities")

    if _has_table("user_identities"):
        op.drop_table("user_identities")
