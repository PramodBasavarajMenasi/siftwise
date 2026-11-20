"""Every CI selection: shadow tables generalised to ci_selections, run_outcomes, run_failures

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-20 12:00:00.000000

Until 0016 the server stored a selection only for shadow runs. From 0017 every /select call
from CI (the GitHub Action or the GitLab template, sending ``ci``) stores its selection, so the
three shadow tables become the general ones (renamed, no data copied):

* ``shadow_selections`` -> ``ci_selections``: ``shadow`` (true for every existing row), the
  full-suite reason as a code and the files behind it, the command order, the CI context
  (provider, branch, main, PR number, event, client version), the changed-file count, the top
  five tests by risk (``top_risk``), and ``ranked``: ``selected_keys`` are in risk-rank order
  (false for existing rows, which are sorted by key). ``budget`` now holds every budget asked
  for, with ``applied`` and ``limited``: existing rows were all limited.
* ``shadow_results`` -> ``run_outcomes``: one row per recorded leg of a triaged CI run
  attempt, with or without a selection (``selection_id`` nullable); its failures by verdict,
  how many the selection ran, the risk rank of the first. Shadow-only columns become nullable.
  The measured test time moves to ``runs`` (``tests_total`` -> ``runs.tests_run``,
  ``ms_total`` -> ``runs.measured_ms``, now computed at ingest for every upload).
* ``shadow_failures`` -> ``run_failures``: every failing test with its verdict; ``would_run``
  NULL when the leg has no selection; ``rank`` its rank in the selection.
* ``attempt_outcomes``: one row per triaged CI run attempt, with the ``fail_on`` verdicts the
  client gates on.
* ``runs.client_version`` and ``runs.client`` (github, gitlab, cli, backfill): the siftwise
  client that uploaded the run, so the dashboard can say why a run has no selection.

Downgrade keeps the shadow data: it deletes what 0016 can't hold (non-shadow selections, legs
compared without a selection, attempt outcomes) and moves the measured time back.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (old table, new table) and their constraints and indexes, old name -> new name.
TABLES = (
    ("shadow_selections", "ci_selections"),
    ("shadow_results", "run_outcomes"),
    ("shadow_failures", "run_failures"),
)
CONSTRAINTS = {
    "ci_selections": (
        ("pk_shadow_selections", "pk_ci_selections"),
        ("ck_shadow_selections_mode", "ck_ci_selections_mode"),
        ("ck_shadow_selections_status", "ck_ci_selections_status"),
        ("fk_shadow_selections_repo_id_repos", "fk_ci_selections_repo_id_repos"),
    ),
    "run_outcomes": (
        ("pk_shadow_results", "pk_run_outcomes"),
        ("fk_shadow_results_run_id_runs", "fk_run_outcomes_run_id_runs"),
        (
            "fk_shadow_results_selection_id_shadow_selections",
            "fk_run_outcomes_selection_id_ci_selections",
        ),
    ),
    "run_failures": (
        ("pk_shadow_failures", "pk_run_failures"),
        ("ck_shadow_failures_cause", "ck_run_failures_cause"),
        ("fk_shadow_failures_run_id_shadow_results", "fk_run_failures_run_id_run_outcomes"),
        ("fk_shadow_failures_test_key_tests", "fk_run_failures_test_key_tests"),
    ),
}
INDEXES = (
    (
        "uq_shadow_selections_repo_ci_run_attempt_variant",
        "uq_ci_selections_repo_ci_run_attempt_variant",
    ),
    ("ix_shadow_selections_repo_id_created_at", "ix_ci_selections_repo_id_created_at"),
    ("ix_shadow_results_selection_id", "ix_run_outcomes_selection_id"),
)
SEQUENCE = ("shadow_selections_id_seq", "ci_selections_id_seq")
SHADOW_ONLY = ("caught", "missed", "tests_would_run", "ms_would_run")
# Budget keys 0017 adds (0016 stored only limited budgets, without them).
BUDGET_KEYS = ("applied", "limited", "note", "estimated_ms", "unknown_ms")


def _rename(forward: bool) -> None:
    def pick(pair: tuple[str, str]) -> tuple[str, str]:
        return pair if forward else (pair[1], pair[0])

    for old, new in (pick(t) for t in TABLES):
        op.rename_table(old, new)
    for table, pairs in CONSTRAINTS.items():
        name = table if forward else dict((n, o) for o, n in TABLES)[table]
        for old, new in (pick(p) for p in pairs):
            op.execute(f"ALTER TABLE {name} RENAME CONSTRAINT {old} TO {new}")
    for old, new in (pick(p) for p in INDEXES):
        op.execute(f"ALTER INDEX {old} RENAME TO {new}")
    old, new = pick(SEQUENCE)
    op.execute(f"ALTER SEQUENCE {old} RENAME TO {new}")


def upgrade() -> None:
    _rename(forward=True)

    # --- ci_selections ------------------------------------------------------------------
    op.add_column(
        "ci_selections", sa.Column("shadow", sa.Boolean(), server_default=sa.true(), nullable=False)
    )
    op.alter_column("ci_selections", "shadow", server_default=sa.false())
    op.add_column("ci_selections", sa.Column("reason_code", sa.String(32), nullable=True))
    op.add_column(
        "ci_selections",
        sa.Column(
            "reason_items",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
    )
    op.add_column("ci_selections", sa.Column("command_order", sa.String(16), nullable=True))
    op.add_column("ci_selections", sa.Column("changed_count", sa.Integer(), nullable=True))
    op.add_column("ci_selections", sa.Column("provider", sa.String(16), nullable=True))
    op.add_column("ci_selections", sa.Column("branch", sa.String(255), nullable=True))
    op.add_column("ci_selections", sa.Column("is_main", sa.Boolean(), nullable=True))
    op.add_column("ci_selections", sa.Column("pr_number", sa.Integer(), nullable=True))
    op.add_column("ci_selections", sa.Column("event", sa.String(64), nullable=True))
    op.add_column("ci_selections", sa.Column("client_version", sa.String(32), nullable=True))
    op.add_column(
        "ci_selections",
        sa.Column("ranked", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "ci_selections",
        sa.Column("top_risk", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    # Every stored budget was a limiting one.
    op.execute(
        'UPDATE ci_selections SET budget = budget || \'{"applied": true, "limited": true}\''
        "::jsonb WHERE budget IS NOT NULL"
    )

    # --- runs: measured test time, at ingest from now on ----------------------------------
    op.add_column("runs", sa.Column("tests_run", sa.Integer(), nullable=True))
    op.add_column("runs", sa.Column("measured_ms", sa.Float(), nullable=True))
    op.add_column("runs", sa.Column("client_version", sa.String(32), nullable=True))
    op.add_column("runs", sa.Column("client", sa.String(16), nullable=True))
    op.execute(
        "UPDATE runs r SET tests_run = o.tests_total, measured_ms = o.ms_total "
        "FROM run_outcomes o WHERE o.run_id = r.id"
    )

    # --- run_outcomes -----------------------------------------------------------------------
    op.drop_column("run_outcomes", "tests_total")
    op.drop_column("run_outcomes", "ms_total")
    op.alter_column("run_outcomes", "selection_id", nullable=True)
    for column in SHADOW_ONLY:
        op.alter_column("run_outcomes", column, nullable=True)
    for column in ("new_regressions", "pre_existing", "unknown"):
        op.add_column(
            "run_outcomes",
            sa.Column(column, sa.Integer(), server_default=sa.text("0"), nullable=False),
        )
    op.add_column("run_outcomes", sa.Column("failures_selected", sa.Integer(), nullable=True))
    op.add_column("run_outcomes", sa.Column("first_failure_rank", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE run_outcomes o SET
            new_regressions = f.new_regressions, pre_existing = f.pre_existing,
            unknown = f.unknown, failures_selected = f.selected
        FROM (
            SELECT run_id,
                   count(*) FILTER (WHERE verdict = 'new_regression') AS new_regressions,
                   count(*) FILTER (WHERE verdict = 'pre_existing') AS pre_existing,
                   count(*) FILTER (WHERE verdict = 'unknown') AS unknown,
                   count(*) FILTER (WHERE would_run) AS selected
            FROM run_failures GROUP BY run_id
        ) f
        WHERE f.run_id = o.run_id
        """
    )
    op.execute("UPDATE run_outcomes SET failures_selected = 0 WHERE failures_selected IS NULL")

    # --- run_failures -----------------------------------------------------------------------
    op.alter_column("run_failures", "would_run", nullable=True)
    op.add_column("run_failures", sa.Column("rank", sa.Integer(), nullable=True))

    # --- attempt_outcomes -------------------------------------------------------------------
    op.create_table(
        "attempt_outcomes",
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("ci_run_id", sa.String(255), nullable=False),
        sa.Column("run_attempt", sa.Integer(), nullable=False),
        sa.Column(
            "computed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("fail_on", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["repo_id"],
            ["repos.id"],
            name=op.f("fk_attempt_outcomes_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "repo_id", "ci_run_id", "run_attempt", name=op.f("pk_attempt_outcomes")
        ),
    )


def downgrade() -> None:
    op.drop_table("attempt_outcomes")
    # What 0016 can't hold: selections of runs that weren't shadow runs, and legs compared
    # without a shadow selection (their failures go with them: ON DELETE CASCADE).
    op.execute(
        "DELETE FROM run_outcomes o WHERE o.selection_id IS NULL OR EXISTS ("
        "SELECT 1 FROM ci_selections s WHERE s.id = o.selection_id AND NOT s.shadow)"
    )
    op.execute("DELETE FROM ci_selections WHERE NOT shadow")

    op.drop_column("run_failures", "rank")
    op.alter_column("run_failures", "would_run", nullable=False)

    for column in ("first_failure_rank", "failures_selected", "unknown", "pre_existing",
                   "new_regressions"):  # fmt: skip
        op.drop_column("run_outcomes", column)
    for column in SHADOW_ONLY:
        op.alter_column("run_outcomes", column, nullable=False)
    op.alter_column("run_outcomes", "selection_id", nullable=False)
    op.add_column("run_outcomes", sa.Column("tests_total", sa.Integer(), nullable=True))
    op.add_column("run_outcomes", sa.Column("ms_total", sa.Float(), nullable=True))
    op.execute(
        "UPDATE run_outcomes o SET tests_total = COALESCE(r.tests_run, 0), "
        "ms_total = COALESCE(r.measured_ms, 0) FROM runs r WHERE r.id = o.run_id"
    )
    op.alter_column("run_outcomes", "tests_total", nullable=False)
    op.alter_column("run_outcomes", "ms_total", nullable=False)

    for column in ("client", "client_version", "measured_ms", "tests_run"):
        op.drop_column("runs", column)

    # 0016 read any stored budget as a limiting one: keep only those, as 0016 wrote them.
    op.execute(
        "UPDATE ci_selections SET budget = NULL "
        "WHERE budget IS NOT NULL AND NOT COALESCE((budget->>'limited')::boolean, true)"
    )
    op.execute(
        "UPDATE ci_selections SET budget = budget - "
        + " - ".join(f"'{key}'" for key in BUDGET_KEYS)
        + " WHERE budget IS NOT NULL"
    )
    for column in ("top_risk", "ranked", "client_version", "event", "pr_number", "is_main",
                   "branch", "provider", "changed_count", "command_order", "reason_items",
                   "reason_code", "shadow"):  # fmt: skip
        op.drop_column("ci_selections", column)

    _rename(forward=False)
