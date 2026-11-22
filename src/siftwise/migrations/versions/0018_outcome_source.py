"""Outcomes computed later: attempt_outcomes.source

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-22 12:00:00.000000

``attempt_outcomes.source``: ``ci`` (the CI run's own outcome call: every existing row) or
``backfill`` (``siftwise outcomes backfill``: verdicts computed later, for attempts uploaded
without one). Adding a column with a constant default is a catalog change (no table rewrite).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "attempt_outcomes",
        sa.Column("source", sa.String(16), server_default=sa.text("'ci'"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_attempt_outcomes_source"), "attempt_outcomes", "source IN ('ci', 'backfill')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_attempt_outcomes_source"), "attempt_outcomes", type_="check")
    op.drop_column("attempt_outcomes", "source")
