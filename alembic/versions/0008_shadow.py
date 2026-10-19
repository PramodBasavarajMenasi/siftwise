"""shadow mode: stored selections of shadow runs, and their comparison with the results

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-19 12:00:00.000000

A shadow run runs the full suite while siftwise stores what it would have selected
(``shadow_selections``). After the run is recorded, ``POST /repos/{repo}/shadow`` compares
each recorded variant with it (``shadow_results``, ``shadow_failures``): live recall.
New tables only; nothing is rewritten.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = ("pending", "compared", "no_upload", "commit_mismatch", "compare_error")


def upgrade() -> None:
    op.create_table(
        "shadow_selections",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("ci_run_id", sa.String(length=255), nullable=False),
        sa.Column("run_attempt", sa.Integer(), nullable=False),
        sa.Column("variant", sa.String(length=255), nullable=True),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("selected_keys", postgresql.ARRAY(sa.Integer()), nullable=False),
        sa.Column("go_selected_keys", postgresql.ARRAY(sa.Integer()), nullable=False),
        sa.Column("go_packages", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("python_files", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("selected_count", sa.Integer(), nullable=False),
        sa.Column("total_known", sa.Integer(), nullable=False),
        sa.Column("estimated_ms_total", sa.Float(), nullable=False),
        sa.Column("estimated_ms_would_run", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("compared_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("mode IN ('full', 'selective')", name=op.f("ck_shadow_selections_mode")),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in STATUSES) + ")",
            name=op.f("ck_shadow_selections_status"),
        ),
        sa.ForeignKeyConstraint(
            ["repo_id"],
            ["repos.id"],
            name=op.f("fk_shadow_selections_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shadow_selections")),
    )
    op.create_index(
        "uq_shadow_selections_repo_ci_run_attempt_variant",
        "shadow_selections",
        ["repo_id", "ci_run_id", "run_attempt", sa.text("COALESCE(variant, '')")],
        unique=True,
    )
    op.create_index(
        op.f("ix_shadow_selections_repo_id_created_at"),
        "shadow_selections",
        ["repo_id", "created_at"],
    )

    op.create_table(
        "shadow_results",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("selection_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "compared_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("failing", sa.Integer(), nullable=False),
        sa.Column("caught", sa.Integer(), nullable=False),
        sa.Column("missed", sa.Integer(), nullable=False),
        sa.Column("flaky", sa.Integer(), nullable=False),
        sa.Column("tests_total", sa.Integer(), nullable=False),
        sa.Column("tests_would_run", sa.Integer(), nullable=False),
        sa.Column("ms_total", sa.Float(), nullable=False),
        sa.Column("ms_would_run", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_shadow_results_run_id_runs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["selection_id"],
            ["shadow_selections.id"],
            name=op.f("fk_shadow_results_selection_id_shadow_selections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_shadow_results")),
    )
    op.create_index(op.f("ix_shadow_results_selection_id"), "shadow_results", ["selection_id"])

    op.create_table(
        "shadow_failures",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("test_key", sa.Integer(), nullable=False),
        sa.Column("would_run", sa.Boolean(), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["shadow_results.run_id"],
            name=op.f("fk_shadow_failures_run_id_shadow_results"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["test_key"],
            ["tests.id"],
            name=op.f("fk_shadow_failures_test_key_tests"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "test_key", name=op.f("pk_shadow_failures")),
    )


def downgrade() -> None:
    op.drop_table("shadow_failures")
    op.drop_index(op.f("ix_shadow_results_selection_id"), table_name="shadow_results")
    op.drop_table("shadow_results")
    op.drop_index(op.f("ix_shadow_selections_repo_id_created_at"), table_name="shadow_selections")
    op.drop_index(
        "uq_shadow_selections_repo_ci_run_attempt_variant", table_name="shadow_selections"
    )
    op.drop_table("shadow_selections")
