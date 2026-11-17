"""Per-repo API tokens with read and write scopes

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-17 12:00:00.000000

``api_tokens`` holds the tokens ``siftwise token create`` makes: the SHA-256 of each token
(never the token), the repo it is for (a name or ``*``), its scope and when it was last used
or revoked. ``SIFTWISE_API_TOKEN`` stays an admin token outside this table. A new table: no
rewrite.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), server_default="", nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("repo", sa.String(length=255), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("scope IN ('read', 'write')", name=op.f("ck_api_tokens_scope")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_api_tokens_token_hash")),
    )


def downgrade() -> None:
    op.drop_table("api_tokens")
