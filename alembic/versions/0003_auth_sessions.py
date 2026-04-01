"""Add lightweight auth password hashes and user sessions

Revision ID: 0003_auth_sessions
Revises: 0002_setup_and_plex_cache
Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0003_auth_sessions"
down_revision = "0002_setup_and_plex_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.String(length=255), nullable=True))
    op.execute("UPDATE users SET password_hash = 'bootstrapsalt:b0006bf9fa4356a0243999f5c1650eee65aacff1517198c9dcbbb4158cdf9bbf' WHERE password_hash IS NULL")
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("password_hash", existing_type=sa.String(length=255), nullable=False)

    op.create_table(
        "user_sessions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("session_token", sa.String(length=128), nullable=False, unique=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_sessions")
    op.drop_column("users", "password_hash")
