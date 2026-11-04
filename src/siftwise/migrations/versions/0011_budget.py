"""time budgets: what a budget-limited shadow selection left out

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-04 12:00:00.000000

``shadow_selections.budget`` (JSONB, NULL unless a time budget left selected tests out) keeps
the budget and the selection it cut: the left-out tests' keys, Go subset, packages and files
(or ``full`` for a budget-limited full suite). The compare then tells a miss the budget left
out (cause ``budget``) from one the selection itself missed. New nullable column, and the
cause check gains ``budget``: no rewrite.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_CAUSES = ("new_test", "flaky_history", "config_gap", "no_link", "unknown")
CAUSES = ("budget", *OLD_CAUSES)


def _cause_check(causes: Sequence[str]) -> None:
    op.drop_constraint(op.f("ck_shadow_failures_cause"), "shadow_failures", type_="check")
    op.create_check_constraint(
        op.f("ck_shadow_failures_cause"),
        "shadow_failures",
        "cause IN (" + ", ".join(f"'{c}'" for c in causes) + ")",
    )


def upgrade() -> None:
    op.add_column(
        "shadow_selections",
        sa.Column("budget", postgresql.JSONB(none_as_null=True), nullable=True),
    )
    _cause_check(CAUSES)


def downgrade() -> None:
    op.execute("UPDATE shadow_failures SET cause = 'unknown' WHERE cause = 'budget'")
    _cause_check(OLD_CAUSES)
    op.drop_column("shadow_selections", "budget")
