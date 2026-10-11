"""Per-test history rollup (``test_stats``) and history queries.

``test_stats`` is derived data: each row is recomputed from raw results (never incrementally
adjusted), so the rollup is independent of ingest order: backfilling old runs after newer ones
gives the same stats.

The rollup reads a **window**: runs from the ``SIFTWISE_STATS_WINDOW_DAYS`` days (default 90; 0 =
all history) up to the repo's most recent run. Anchoring on the newest run rather than "now"
keeps backfilled history meaningful. Reading is driven by the window's runs (results are
fetched by ``run_id``), so ingest cost depends on how much happened in the window, not on how
much history exists.

Definitions (all counts are within the window):

* Run time is ``COALESCE(runs.started_at, runs.created_at)`` (``Run.occurred_at``). Anything
  "latest" is ordered by it, tie-broken by run id.
* A test's *outcome* in a run is the status of its final attempt (highest ``attempt``).
  ``error`` counts as a failure everywhere.
* A *run* is one variant (matrix leg, e.g. ``ubuntu-py3.12``) of a CI run attempt; each
  variant is uploaded as its own run. Results are compared only within the same variant.
* ``runs``: runs whose outcome is not ``skipped``.
* ``failures``: runs whose outcome is ``failed``/``error``; ``last_failed_at`` is the latest
  such run's time.
* ``avg_duration_ms``: mean over all non-skipped attempts that report a duration.
* ``flaky_score``: of the (commit, variant) pairs where the test ran (non-skipped), the
  fraction where it had both a failed/error and a passed result, across all attempts and
  runs of that commit and variant. Passing on one OS and failing on another is not flaky.
* Broken on main is tracked per variant: a variant is broken if its latest non-skipped
  outcome on main is a failure, starting at the oldest run of that unbroken failing streak.
  When a variant fails on main for the whole window, its older main results are read too, so
  the streak start is found even if it began before the window. ``broken_on_main_variants``
  lists every broken variant with its streak start (oldest first);
  ``broken_on_main_since_sha`` is the earliest of them. Both are ``NULL`` once every variant
  passes on main again.
* ``last_seen_at``: the latest run (any status, skipped included) the test appears in.
  ``last_seen_run_id``: a run of the latest *CI run* the test appears in, with CI runs ordered
  as ``selector.recent_ci_runs_sql`` orders them (latest variant time, then highest run id).
  So "last seen in one of the latest N CI runs" is the same as "appeared in one of them",
  which is how the selector finds known tests without reading raw results.
* ``file_path``: the test's latest reported file path (``tests.file_path``).

Batch ingest (``POST /runs?defer_rollup=true``) skips the rollup; ``recompute_repo_stats``
(``POST /repos/{repo}/rollup``) then recomputes every test in the window. Until it runs,
``test_stats`` is stale.

Tests are rolled up in batches of test keys sized so a batch has at most ``BATCH_ROWS``
(test, run) rows: each statement's working set fits in memory (no temp files), and the cost
grows linearly with the window. Within a batch there are no sorts of the window's rows:
per-run outcomes come from the primary key's order, and everything else is hash aggregates
(see ``_ROLLUP_SQL``).
"""

from collections.abc import Iterator, Sequence

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from siftwise.core.models import (
    FAILED_CODES_SQL,
    PASSED_CODE,
    SKIPPED_CODE,
    Repo,
    Run,
    Test,
    TestResult,
    TestStats,
)

# First key of the two-int advisory lock, so other advisory-lock users can't collide with it.
_ROLLUP_LOCK_NAMESPACE = 1

# 0 means "no window": use a span longer than any repo's history.
_ALL_HISTORY_DAYS = 365 * 100

# (test, run) rows per rollup statement, at most: a batch has BATCH_ROWS // (runs in the
# window) test keys. The work_mem below holds this many (rdflib: 2,959 runs, 253 tests a batch).
BATCH_ROWS = 750_000
_MAX_BATCH_TESTS = 10_000
_ROLLUP_WORK_MEM = "64MB"

# Final attempt's status without sorting: attempts are unique per (run, test) and statuses are
# 0..3, so the highest attempt * 4 + status belongs to the final attempt.
_OUTCOME = "(max(attempt * 4 + status) % 4)"

# Recompute stats for the test keys in [:lo, :hi] (and in :keys, if {key_filter} uses it).
#
# The window's runs drive the reads: for each, a LATERAL lookup by the primary key prefix
# (run_id, test_key) reads only this batch's results, already in test_key order, so the
# per-run GROUP BY needs no sort. Everything after that is hash aggregation over at most a
# batch of (test, run) rows; positions in time order (``ord``) and in the selector's CI-run
# order (``seen_ord``) are precomputed per run, so "latest" and "oldest" are plain max/min.
#
# The previous version computed all of this with window functions over the whole window in
# one statement: several sorts of every row, ~16 min and up to 15 GB of temp files on rdflib.
_ROLLUP_SQL = """
WITH bounds AS MATERIALIZED (
    SELECT max(COALESCE(started_at, created_at)) - make_interval(days => :window_days)
               AS window_start
    FROM runs WHERE repo_id = :repo_id
),
repo_runs AS MATERIALIZED (  -- every run, with its position in run-time order and CI run
    SELECT id, commit_sha, is_main, variant, COALESCE(started_at, created_at) AS run_at,
           -- int, not bigint: per_run below carries these on every row of the batch
           dense_rank() OVER (ORDER BY COALESCE(variant, ''))::int AS variant_no,
           row_number() OVER (ORDER BY COALESCE(started_at, created_at), id)::int AS ord,
           -- ordered like selector.recent_ci_runs_sql: latest leg time, then highest run id
           max(COALESCE(started_at, created_at)) OVER ci AS ci_at, max(id) OVER ci AS ci_id
    FROM runs WHERE repo_id = :repo_id
    WINDOW ci AS (PARTITION BY COALESCE(ci_run_id, 'run:' || id), run_attempt)
),
win_runs AS MATERIALIZED (
    SELECT ru.id, ru.is_main, ru.variant_no, ru.ord,
           dense_rank() OVER (ORDER BY ru.commit_sha, ru.variant_no)::int AS pair,  -- flaky's unit
           row_number() OVER (ORDER BY ru.ci_at, ru.ci_id, ru.id)::int AS seen_ord
    FROM repo_runs ru CROSS JOIN bounds
    WHERE ru.run_at >= bounds.window_start
),
per_run AS MATERIALIZED (  -- one row per (test, window run)
    SELECT tr.test_key, wr.ord, wr.seen_ord, wr.pair, wr.is_main, wr.variant_no, tr.outcome,
           tr.any_failed, tr.any_passed, tr.duration_sum, tr.duration_count
    FROM win_runs wr
    CROSS JOIN LATERAL (
        SELECT test_key, {outcome}::smallint AS outcome,
               bool_or(status IN {failed}) AS any_failed,
               bool_or(status = {passed}) AS any_passed,
               sum(duration_ms) FILTER (WHERE status <> {skipped}) AS duration_sum,
               count(duration_ms) FILTER (WHERE status <> {skipped})::int AS duration_count
        FROM test_results
        WHERE run_id = wr.id AND test_key BETWEEN :lo AND :hi{key_filter}
        GROUP BY test_key
    ) tr
),
per_test AS (
    SELECT test_key,
           count(*) FILTER (WHERE outcome <> {skipped}) AS runs,
           count(*) FILTER (WHERE outcome IN {failed}) AS failures,
           max(ord) FILTER (WHERE outcome IN {failed}) AS last_failed_ord,
           sum(duration_sum)::numeric / NULLIF(sum(duration_count), 0) AS avg_duration_ms,
           max(ord) AS last_seen_ord,
           max(seen_ord) AS last_seen_pos
    FROM per_run GROUP BY test_key
),
pairs AS (  -- per (test, commit, variant)
    SELECT test_key, bool_or(any_failed) AND bool_or(any_passed) AS flaky,
           bool_or(any_failed OR any_passed) AS ran
    FROM per_run GROUP BY test_key, pair
),
flaky AS (
    SELECT test_key, avg(CASE WHEN flaky THEN 1.0 ELSE 0.0 END) FILTER (WHERE ran) AS score
    FROM pairs GROUP BY test_key
),
main_v AS (  -- per (test, variant) on main in the window
    SELECT test_key, variant_no, max(ord) FILTER (WHERE outcome = {passed}) AS last_pass
    FROM per_run WHERE is_main AND outcome <> {skipped}
    GROUP BY test_key, variant_no
),
edge AS (  -- failing on main for the whole window: the streak may have started earlier
    SELECT test_key, variant_no FROM main_v WHERE last_pass IS NULL
),
old_main AS (  -- older main outcomes, only for those, to find where the streak started
    SELECT edge.test_key, edge.variant_no, ru.ord, {outcome} AS outcome
    FROM edge
    CROSS JOIN LATERAL (
        SELECT run_id, attempt, status FROM test_results WHERE test_key = edge.test_key
        OFFSET 0  -- fence: read only these tests' history, by test_key
    ) tr
    JOIN repo_runs ru ON ru.id = tr.run_id
    CROSS JOIN bounds
    WHERE ru.is_main AND ru.variant_no = edge.variant_no AND ru.run_at < bounds.window_start
    GROUP BY edge.test_key, edge.variant_no, ru.ord
),
last_pass AS (  -- the latest pass on main per (test, variant), before the window if need be
    SELECT mv.test_key, mv.variant_no,
           COALESCE(mv.last_pass,
                    (SELECT max(om.ord) FROM old_main om
                     WHERE om.test_key = mv.test_key AND om.variant_no = mv.variant_no
                       AND om.outcome = {passed}),
                    0) AS ord
    FROM main_v mv
),
main_failures AS (  -- non-skipped main outcomes; after the last pass they are all failures
    SELECT test_key, variant_no, ord FROM per_run
    WHERE is_main AND outcome IN {failed}
    UNION ALL
    SELECT test_key, variant_no, ord FROM old_main WHERE outcome IN {failed}
),
streaks AS (  -- per broken (test, variant): the oldest failure after its last pass
    SELECT f.test_key, min(f.ord) AS start_ord
    FROM main_failures f JOIN last_pass lp USING (test_key, variant_no)
    WHERE f.ord > lp.ord
    GROUP BY f.test_key, f.variant_no
),
broken AS (
    SELECT s.test_key,
           (array_agg(ru.commit_sha ORDER BY s.start_ord))[1] AS since_sha,
           jsonb_agg(jsonb_build_object('variant', ru.variant, 'since_sha', ru.commit_sha)
                     ORDER BY s.start_ord) AS variants
    FROM streaks s JOIN repo_runs ru ON ru.ord = s.start_ord
    GROUP BY s.test_key
)
INSERT INTO test_stats AS ts (
    repo_id, test_id, runs, failures, last_failed_at, flaky_score, avg_duration_ms,
    broken_on_main_since_sha, broken_on_main_variants, last_seen_at, last_seen_run_id, file_path
)
SELECT :repo_id, t.test_id, pt.runs, pt.failures, failed_run.run_at,
       COALESCE(flaky.score, 0)::float8, pt.avg_duration_ms::float8,
       broken.since_sha, broken.variants, seen_run.run_at, seen.id, t.file_path
FROM per_test pt
JOIN tests t ON t.id = pt.test_key
JOIN repo_runs seen_run ON seen_run.ord = pt.last_seen_ord
JOIN win_runs seen ON seen.seen_ord = pt.last_seen_pos
LEFT JOIN repo_runs failed_run ON failed_run.ord = pt.last_failed_ord
LEFT JOIN flaky ON flaky.test_key = pt.test_key
LEFT JOIN broken ON broken.test_key = pt.test_key
ON CONFLICT (repo_id, test_id) DO UPDATE SET
    runs = EXCLUDED.runs,
    failures = EXCLUDED.failures,
    last_failed_at = EXCLUDED.last_failed_at,
    flaky_score = EXCLUDED.flaky_score,
    avg_duration_ms = EXCLUDED.avg_duration_ms,
    broken_on_main_since_sha = EXCLUDED.broken_on_main_since_sha,
    broken_on_main_variants = EXCLUDED.broken_on_main_variants,
    last_seen_at = EXCLUDED.last_seen_at,
    last_seen_run_id = EXCLUDED.last_seen_run_id,
    file_path = EXCLUDED.file_path
"""

_CODES = {
    "outcome": _OUTCOME,
    "failed": FAILED_CODES_SQL,
    "passed": str(PASSED_CODE),
    "skipped": str(SKIPPED_CODE),
}
# Batch rollup: every test key in [lo, hi].
_RANGE_ROLLUP_SQL = text(_ROLLUP_SQL.format(key_filter="", **_CODES))
# After one ingest: only the run's tests within [lo, hi].
_KEYS_ROLLUP_SQL = text(
    _ROLLUP_SQL.format(key_filter=" AND test_key = ANY(CAST(:keys AS integer[]))", **_CODES)
)


def _window_days(window_days: int) -> int:
    return window_days if window_days > 0 else _ALL_HISTORY_DAYS


def _lock(session: Session, repo_id: int) -> None:
    """Per-repo transaction advisory lock, and memory for the rollup's hash aggregates.

    Without the lock, two concurrent ingests touching the same tests could each compute stats
    without seeing the other's uncommitted results, and the later upsert would overwrite with
    stale numbers. With the lock, the second rollup starts after the first commits.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :key)"),
        {"ns": _ROLLUP_LOCK_NAMESPACE, "key": repo_id},
    )
    session.execute(text(f"SET LOCAL work_mem = '{_ROLLUP_WORK_MEM}'"))
    # Batches are small, and parallel hash joins would put work_mem-sized tables in shared
    # memory: Docker's default /dev/shm is 64MB, so they can fail there.
    session.execute(text("SET LOCAL max_parallel_workers_per_gather = 0"))


def _batches(
    session: Session, repo_id: int, window_days: int, keys: Sequence[int]
) -> Iterator[Sequence[int]]:
    window_runs = session.scalar(
        text(
            "SELECT count(*) FROM runs WHERE repo_id = :repo_id"
            " AND COALESCE(started_at, created_at) >= ("
            "   SELECT max(COALESCE(started_at, created_at)) - make_interval(days => :days)"
            "   FROM runs WHERE repo_id = :repo_id)"
        ),
        {"repo_id": repo_id, "days": window_days},
    )
    size = max(1, min(_MAX_BATCH_TESTS, BATCH_ROWS // max(int(window_runs or 0), 1)))
    for start in range(0, len(keys), size):
        yield keys[start : start + size]


def recompute_test_stats(session: Session, repo_id: int, run_id: int, window_days: int) -> None:
    """Recompute ``test_stats`` for the tests in ``run_id`` over the window. Does not commit."""
    _lock(session, repo_id)
    days = _window_days(window_days)
    keys = list(
        session.scalars(
            text("SELECT DISTINCT test_key FROM test_results WHERE run_id = :run ORDER BY 1"),
            {"run": run_id},
        )
    )
    for batch in _batches(session, repo_id, days, keys):
        session.execute(
            _KEYS_ROLLUP_SQL,
            {
                "repo_id": repo_id,
                "window_days": days,
                "lo": batch[0],
                "hi": batch[-1],
                "keys": list(batch),
            },
        )


def recompute_repo_stats(session: Session, repo_id: int, window_days: int) -> int:
    """Recompute ``test_stats`` for every test with results in the window. Does not commit.

    Returns the number of tests updated. Used after a batch (``defer_rollup``) ingest.
    """
    _lock(session, repo_id)
    days = _window_days(window_days)
    keys = list(session.scalars(select(Test.id).where(Test.repo_id == repo_id).order_by(Test.id)))
    updated = 0
    for batch in _batches(session, repo_id, days, keys):
        result = session.execute(
            _RANGE_ROLLUP_SQL,
            {
                "repo_id": repo_id,
                "window_days": days,
                "lo": batch[0],
                "hi": batch[-1],
            },
        )
        updated += int(getattr(result, "rowcount", 0) or 0)
    return updated


def get_test(session: Session, repo: str, test_id: str) -> Test | None:
    return session.scalars(
        select(Test)
        .join(Repo, Test.repo_id == Repo.id)
        .where(Repo.name == repo, Test.test_id == test_id)
    ).one_or_none()


def get_test_stats(session: Session, repo: str, test_id: str) -> TestStats | None:
    return session.scalars(
        select(TestStats)
        .join(Repo, TestStats.repo_id == Repo.id)
        .where(Repo.name == repo, TestStats.test_id == test_id)
    ).one_or_none()


def get_recent_results(
    session: Session, test_key: int, limit: int = 20
) -> list[tuple[TestResult, Run]]:
    """The test's latest result rows (every attempt), newest run first."""
    rows = session.execute(
        select(TestResult, Run)
        .join(Run, TestResult.run_id == Run.id)
        .where(TestResult.test_key == test_key)
        .order_by(Run.occurred_at.desc(), Run.id.desc(), TestResult.attempt.desc())
        .limit(limit)
    ).all()
    return [(result, run) for result, run in rows]
