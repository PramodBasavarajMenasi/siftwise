"""triage: runs.affected (change links at record time), failed results by test

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-18 12:00:00.000000

``runs.affected`` keeps the import graphs and declared rules a run's change was recorded with
(``siftwise record`` / the GitHub Action), so triage can tell whether a past failure was
related to that run's change. Older runs have NULL: their relatedness is unknown, and triage
never counts them as flaky evidence.

The partial index serves triage's failure lookups by test. Building it reads ``test_results``
once (seconds on rdflib's 29M rows) and blocks writes to it meanwhile.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("affected", postgresql.JSONB()))
    op.create_index(
        "ix_test_results_failed_by_test",
        "test_results",
        ["test_key", "run_id"],
        postgresql_where=sa.text("status IN (1, 3)"),
    )


def downgrade() -> None:
    op.drop_index("ix_test_results_failed_by_test", table_name="test_results")
    op.drop_column("runs", "affected")
