"""compact test_results: a tests table and 4-byte test keys

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-11 16:00:00.000000

Every result stored its test ID and file path as text (about 110 bytes per row on rdflib).
They move to ``tests(id, repo_id, test_id, file_path, file_path_at)``, and ``test_results``
becomes ``(run_id, test_key, duration_ms, attempt, status, message)`` with the primary key
``(run_id, test_key, attempt)``; status and attempt are smallints. The surrogate ``id`` and
the separate ``run_id`` index go: the primary key's prefix serves per-run lookups.

Results keep no file path of their own: ``tests.file_path`` is the latest one reported, by run
time (``file_path_at``).

The upgrade rewrites ``test_results`` into a new table and swaps it in, in one transaction:
the old and the new table coexist until it commits, and writes to ``test_results`` are blocked
meanwhile (minutes on tens of millions of rows). Run ``VACUUM ANALYZE test_results`` after it,
so index-only scans of the failure index don't visit the heap until autovacuum gets there.
``test_stats`` is unchanged and needs no rollup.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Must match siftwise.core.models.STATUS_CODES.
_TO_CODE = "CASE {col} WHEN 'passed' THEN 0 WHEN 'failed' THEN 1 WHEN 'skipped' THEN 2 ELSE 3 END"
_TO_TEXT = (
    "CASE {col} WHEN 0 THEN 'passed' WHEN 1 THEN 'failed' WHEN 2 THEN 'skipped' ELSE 'error' END"
)


def upgrade() -> None:
    # Index builds over the whole table: give them memory instead of temp files.
    op.execute("SET LOCAL maintenance_work_mem = '512MB'")
    op.create_table(
        "tests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("test_id", sa.Text(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column("file_path_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["repo_id"], ["repos.id"], name=op.f("fk_tests_repo_id_repos"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tests")),
        sa.UniqueConstraint("repo_id", "test_id", name=op.f("uq_tests_repo_id_test_id")),
    )
    # One row per (repo, test ID). A plain GROUP BY hashes into the few distinct tests; an
    # ordered aggregate here would make Postgres sort every result (GBs of temp files).
    op.execute(
        """
        INSERT INTO tests (repo_id, test_id)
        SELECT ru.repo_id, tr.test_id
        FROM test_results tr JOIN runs ru ON ru.id = tr.run_id
        GROUP BY ru.repo_id, tr.test_id
        ORDER BY ru.repo_id, tr.test_id
        """
    )
    # The latest non-null file path by run time, sorting only the results that have one.
    op.execute(
        """
        UPDATE tests t SET file_path = p.file_path, file_path_at = p.run_at
        FROM (
            SELECT DISTINCT ON (ru.repo_id, tr.test_id) ru.repo_id, tr.test_id, tr.file_path,
                   COALESCE(ru.started_at, ru.created_at) AS run_at
            FROM test_results tr JOIN runs ru ON ru.id = tr.run_id
            WHERE tr.file_path IS NOT NULL
            ORDER BY ru.repo_id, tr.test_id, COALESCE(ru.started_at, ru.created_at) DESC,
                     ru.id DESC
        ) p
        WHERE t.repo_id = p.repo_id AND t.test_id = p.test_id
        """
    )
    op.create_table(
        "test_results_new",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("test_key", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("attempt", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("status", sa.SmallInteger(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
    )
    # Hash join against the small tests table: rows keep the old table's (ingest) order.
    op.execute(
        f"""
        INSERT INTO test_results_new (run_id, test_key, duration_ms, attempt, status, message)
        SELECT tr.run_id, t.id, tr.duration_ms, tr.attempt, {_TO_CODE.format(col="tr.status")},
               tr.message
        FROM test_results tr
        JOIN runs ru ON ru.id = tr.run_id
        JOIN tests t ON t.repo_id = ru.repo_id AND t.test_id = tr.test_id
        """
    )
    op.drop_table("test_results")
    op.rename_table("test_results_new", "test_results")
    op.create_primary_key(
        op.f("pk_test_results"), "test_results", ["run_id", "test_key", "attempt"]
    )
    op.create_foreign_key(
        op.f("fk_test_results_run_id_runs"),
        "test_results",
        "runs",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        op.f("fk_test_results_test_key_tests"),
        "test_results",
        "tests",
        ["test_key"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        op.f("ck_test_results_status"), "test_results", "status BETWEEN 0 AND 3"
    )
    op.create_index(op.f("ix_test_results_test_key"), "test_results", ["test_key"])
    op.create_index(
        "ix_test_results_failed",
        "test_results",
        ["run_id", "test_key"],
        postgresql_where=sa.text("status IN (1, 3)"),
    )
    op.execute("ANALYZE tests")
    op.execute("ANALYZE test_results")


def downgrade() -> None:
    op.execute("SET LOCAL maintenance_work_mem = '512MB'")
    # 0001 created id as a bigserial: a sequence owned by the column.
    op.execute("CREATE SEQUENCE test_results_id_seq")
    op.create_table(
        "test_results_old",
        sa.Column(
            "id",
            sa.BigInteger(),
            server_default=sa.text("nextval('test_results_id_seq')"),
            nullable=False,
        ),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("test_id", sa.Text(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("attempt", sa.Integer(), server_default="1", nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
    )
    # Every result gets its test's latest file path: per-result paths were not kept.
    op.execute(
        f"""
        INSERT INTO test_results_old
            (run_id, test_id, file_path, status, duration_ms, attempt, message)
        SELECT tr.run_id, t.test_id, t.file_path, {_TO_TEXT.format(col="tr.status")},
               tr.duration_ms, tr.attempt, tr.message
        FROM test_results tr JOIN tests t ON t.id = tr.test_key
        ORDER BY tr.run_id, tr.test_key, tr.attempt
        """
    )
    op.drop_table("test_results")
    op.drop_table("tests")
    op.rename_table("test_results_old", "test_results")
    op.execute("ALTER SEQUENCE test_results_id_seq OWNED BY test_results.id")
    op.create_primary_key(op.f("pk_test_results"), "test_results", ["id"])
    op.create_foreign_key(
        op.f("fk_test_results_run_id_runs"),
        "test_results",
        "runs",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        op.f("ck_test_results_test_status"),
        "test_results",
        "status IN ('passed', 'failed', 'skipped', 'error')",
    )
    op.create_index(op.f("ix_test_results_run_id"), "test_results", ["run_id"])
    op.create_index(op.f("ix_test_results_test_id"), "test_results", ["test_id"])
    op.create_index(
        "ix_test_results_failed",
        "test_results",
        ["run_id", "test_id"],
        postgresql_where=sa.text("status IN ('failed', 'error')"),
    )
