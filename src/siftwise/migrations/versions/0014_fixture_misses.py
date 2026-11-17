"""Test fixtures: the fixture files a shadow selection handled, and the ``fixture`` miss cause

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-17 18:00:00.000000

``shadow_selections.fixture_files`` lists the changed files under ``.siftwise.toml``
``fixtures`` that the selection treated as test data (their referencing tests ran instead of
the full suite). A failure such a selection missed gets the cause ``fixture``: the test uses
the fixture without naming it where siftwise looks. A new column with a default: no rewrite.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_CAUSES = ("budget", "new_test", "flaky_history", "config_gap", "no_link", "unknown")
CAUSES = ("budget", "new_test", "flaky_history", "fixture", "config_gap", "no_link", "unknown")


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
        sa.Column(
            "fixture_files",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )
    _cause_check(CAUSES)


def downgrade() -> None:
    op.execute("UPDATE shadow_failures SET cause = 'no_link' WHERE cause = 'fixture'")
    _cause_check(OLD_CAUSES)
    op.drop_column("shadow_selections", "fixture_files")
