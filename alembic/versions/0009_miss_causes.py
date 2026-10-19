"""miss causes: why a shadow run's selection missed each failure

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-19 18:00:00.000000

``shadow_failures`` gets a cause per miss (new_test, flaky_history, config_gap, no_link,
unknown), the evidence in a line, and a ``[[depends]]`` rule to consider for config gaps.
Existing misses keep NULL ("unclassified") until their run is compared again
(``POST /repos/{repo}/shadow`` is idempotent). New nullable columns only: no rewrite.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CAUSES = ("new_test", "flaky_history", "config_gap", "no_link", "unknown")


def upgrade() -> None:
    op.add_column("shadow_failures", sa.Column("cause", sa.String(length=32), nullable=True))
    op.add_column("shadow_failures", sa.Column("cause_detail", sa.Text(), nullable=True))
    op.add_column("shadow_failures", sa.Column("suggestion", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_shadow_failures_cause"),
        "shadow_failures",
        "cause IN (" + ", ".join(f"'{c}'" for c in CAUSES) + ")",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_shadow_failures_cause"), "shadow_failures", type_="check")
    op.drop_column("shadow_failures", "suggestion")
    op.drop_column("shadow_failures", "cause_detail")
    op.drop_column("shadow_failures", "cause")
