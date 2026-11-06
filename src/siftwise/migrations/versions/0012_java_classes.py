"""Java selection: the test classes a shadow selection runs whole

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-06 12:00:00.000000

``shadow_selections.java_classes`` lists the Java test classes the selection's commands run
(``-Dtest=a.FooTest``, ``--tests a.FooTest``), so the compare credits every test in them, new
ones included, as ``python_files`` does for Python files run whole. A new column with a
default: no rewrite.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "shadow_selections",
        sa.Column(
            "java_classes",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )


def downgrade() -> None:
    op.drop_column("shadow_selections", "java_classes")
