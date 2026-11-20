"""Dashboard sessions: one row per browser login at /ui

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-19 12:00:00.000000

The read-only dashboard (``SIFTWISE_DASHBOARD=1``) signs in with an API token and keeps a
server-side session: the cookie holds a random ID, this table its SHA-256, the token it was
made with (or, for the admin token, a salted digest), and an expiry. A new table: nothing is
rewritten. Rows go when they expire (deleted at the next login), at logout, or with their
token.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ui_sessions",
        sa.Column("id_hash", sa.String(64), primary_key=True),
        sa.Column(
            "api_token_id",
            sa.Integer(),
            sa.ForeignKey("api_tokens.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("admin_salt", sa.String(64), nullable=True),
        sa.Column("admin_digest", sa.String(64), nullable=True),
        sa.Column("repo", sa.String(255), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("csrf", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ui_sessions_api_token_id", "ui_sessions", ["api_token_id"])
    op.create_index("ix_ui_sessions_expires_at", "ui_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_table("ui_sessions")
