"""known_flaky expiry: the entries a run left out because they had expired

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-18 12:00:00.000000

``.siftwise.toml`` ``known_flaky`` entries may carry an ``until`` date. ``record`` applies
only the entries in force on the day the CI run started; ``runs.known_flaky_expired`` keeps
the others ([{test, until}]), so the failures section of the PR comment (written where no
checkout exists, after a matrix) can say they expired. A new column with a default: no
rewrite.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "known_flaky_expired",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "known_flaky_expired")
