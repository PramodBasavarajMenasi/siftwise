"""flaky handling: known_flaky per run, re-run uploads

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-19 20:00:00.000000

``runs.known_flaky`` keeps the ``known_flaky`` globs a run was recorded with (for a PR, the
base commit's ``.siftwise.toml``), so triage on the server applies the list the run was
judged by. ``runs.known_flaky_changed``: the PR's own list differs (it applies after merge).

``runs.rerun`` numbers the Action's re-runs of failing tests within one CI run attempt and
variant (0 = the run itself). It joins the idempotent ingest key, so a re-run upload no longer
collides with the original. New columns with defaults: no table rewrite.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "uq_runs_repo_ci_run_attempt_variant"


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "known_flaky",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )
    op.add_column(
        "runs",
        sa.Column("known_flaky_changed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("runs", sa.Column("rerun", sa.SmallInteger(), nullable=False, server_default="0"))
    op.drop_index(INDEX, table_name="runs")
    op.create_index(
        INDEX,
        "runs",
        ["repo_id", "ci_run_id", "run_attempt", sa.text("COALESCE(variant, '')"), "rerun"],
        unique=True,
    )


def downgrade() -> None:
    op.execute("DELETE FROM runs WHERE rerun > 0")
    op.drop_index(INDEX, table_name="runs")
    op.create_index(
        INDEX,
        "runs",
        ["repo_id", "ci_run_id", "run_attempt", sa.text("COALESCE(variant, '')")],
        unique=True,
    )
    op.drop_column("runs", "rerun")
    op.drop_column("runs", "known_flaky_changed")
    op.drop_column("runs", "known_flaky")
