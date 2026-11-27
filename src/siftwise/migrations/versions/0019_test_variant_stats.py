"""Per-variant test stats: test_variant_stats

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-27 12:00:00.000000

``test_variant_stats``: per (test, named variant) runs, mean duration and last seen time over
the stats window, recomputed by the rollup like ``test_stats``. ``/select`` sizes a CI leg's
estimate and time budget with them. A new, empty table: the next repo rollup
(``POST /repos/{repo}/rollup``, or ``siftwise rollup``) fills it; until then every variant
is unknown to ``/select``, which then estimates as before.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "test_variant_stats",
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("test_key", sa.Integer(), nullable=False),
        sa.Column("variant", sa.String(255), nullable=False),
        sa.Column("runs", sa.Integer(), nullable=False),
        sa.Column("avg_duration_ms", sa.Float(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["repo_id"],
            ["repos.id"],
            name=op.f("fk_test_variant_stats_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["test_key"],
            ["tests.id"],
            name=op.f("fk_test_variant_stats_test_key_tests"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "repo_id", "test_key", "variant", name=op.f("pk_test_variant_stats")
        ),
    )
    op.create_index(
        op.f("ix_test_variant_stats_repo_id_variant"),
        "test_variant_stats",
        ["repo_id", "variant"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_test_variant_stats_repo_id_variant"), table_name="test_variant_stats")
    op.drop_table("test_variant_stats")
