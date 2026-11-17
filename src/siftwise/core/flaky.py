"""Flaky tests over a window, for ``siftwise flaky`` (``GET /repos/{repo}/flaky``).

A test is flaky on a (commit, variant) pair when, across every attempt and run of that commit
and variant (re-run uploads included), it both failed (or errored) and passed: the same
definition as ``test_stats.flaky_score`` (core/history.py), counted instead of averaged and
over the window asked for. Results are compared only within a variant.

Reads only what it must: failures through the partial index ``ix_test_results_failed``, then
primary-key probes for passes and runs of the tests that failed. Read-only.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from siftwise.core.codes import FAILED_CODES_SQL, PASSED_CODE, SKIPPED_CODE

_FLAKY_SQL = f"""
WITH bounds AS (
    SELECT max(COALESCE(started_at, created_at)) AS newest FROM runs WHERE repo_id = :repo_id
),
win AS MATERIALIZED (
    SELECT r.id, r.commit_sha, COALESCE(r.variant, '') AS variant,
           COALESCE(r.started_at, r.created_at) AS run_at
    FROM runs r CROSS JOIN bounds
    WHERE r.repo_id = :repo_id
      AND COALESCE(r.started_at, r.created_at) >= bounds.newest - make_interval(days => :days)
),
failed AS MATERIALIZED (  -- (test, commit, variant) where some attempt failed or errored
    SELECT DISTINCT tr.test_key, w.commit_sha, w.variant
    FROM win w JOIN test_results tr ON tr.run_id = w.id AND tr.status IN {FAILED_CODES_SQL}
),
pair_runs AS MATERIALIZED (  -- each failed pair's runs: the same commit and variant
    SELECT f.test_key, f.commit_sha, f.variant, w.id AS run_id, w.run_at
    FROM failed f JOIN win w ON w.commit_sha = f.commit_sha AND w.variant = f.variant
),
flaky_pairs AS (  -- ... where some attempt passed (a primary-key probe per run)
    SELECT pr.test_key, max(pr.run_at) AS last_at
    FROM pair_runs pr
    GROUP BY pr.test_key, pr.commit_sha, pr.variant
    HAVING bool_or(EXISTS (
        SELECT 1 FROM test_results tr
        WHERE tr.run_id = pr.run_id AND tr.test_key = pr.test_key AND tr.status = {PASSED_CODE}
    ))
),
flaky AS (
    SELECT test_key, count(*) AS flaky, max(last_at) AS last_flaky_at
    FROM flaky_pairs GROUP BY test_key
),
ran AS (  -- the window runs (and commit-variant pairs) where each flaky test ran: per window
    -- run, one primary-key prefix lookup of the flaky tests' keys (the fence keeps the planner
    -- from scanning every result of the repo instead)
    SELECT tr.test_key, count(*) AS runs, count(DISTINCT (w.commit_sha, w.variant)) AS pairs
    FROM win w
    CROSS JOIN LATERAL (
        SELECT test_key FROM test_results
        WHERE run_id = w.id AND test_key = ANY(ARRAY(SELECT test_key FROM flaky))
        GROUP BY test_key
        HAVING bool_or(status <> {SKIPPED_CODE})
        OFFSET 0
    ) tr
    GROUP BY tr.test_key
)
SELECT t.test_id, t.file_path, ran.runs, ran.pairs, fl.flaky, fl.last_flaky_at
FROM flaky fl JOIN ran ON ran.test_key = fl.test_key JOIN tests t ON t.id = fl.test_key
WHERE ran.runs >= :min_runs
ORDER BY fl.flaky DESC, fl.last_flaky_at DESC, t.test_id
LIMIT :limit
"""


@dataclass(frozen=True)
class FlakyTest:
    test_id: str
    file_path: str | None
    runs: int  # window runs where it ran (not skipped)
    pairs: int  # (commit, variant) pairs where it ran
    flaky: int  # pairs where it both failed and passed
    last_flaky_at: datetime


@dataclass(frozen=True)
class FlakyReport:
    window_start: datetime | None  # None: the repo has no runs
    window_end: datetime | None
    tests: list[FlakyTest]
    # The known_flaky list the newest main run applied (record stores it), and that run's time:
    # what `siftwise flaky` compares against outside a checkout.
    main_known_flaky: list[str]
    main_known_flaky_at: datetime | None
    truncated: bool


def flaky_tests(
    session: Session, repo: str, days: int, min_runs: int = 1, limit: int = 200
) -> FlakyReport | None:
    """The repo's flaky tests over the ``days`` before its newest run, most flaky first.
    None for an unknown repo."""
    repo_id = session.execute(
        text("SELECT id FROM repos WHERE name = :name"), {"name": repo}
    ).scalar_one_or_none()
    if repo_id is None:
        return None
    newest = session.execute(
        text("SELECT max(COALESCE(started_at, created_at)) FROM runs WHERE repo_id = :r"),
        {"r": repo_id},
    ).scalar_one_or_none()
    session.execute(text("SET LOCAL jit = off"))  # compiling costs more than these queries
    rows = session.execute(
        text(_FLAKY_SQL),
        {"repo_id": repo_id, "days": days, "min_runs": min_runs, "limit": limit + 1},
    ).all()
    main = session.execute(
        text(
            "SELECT known_flaky, COALESCE(started_at, created_at) AS at FROM runs "
            "WHERE repo_id = :r AND is_main AND rerun = 0 ORDER BY at DESC, id DESC LIMIT 1"
        ),
        {"r": repo_id},
    ).one_or_none()
    return FlakyReport(
        window_start=newest - timedelta(days=days) if newest else None,
        window_end=newest,
        tests=[
            FlakyTest(r.test_id, r.file_path, r.runs, r.pairs, r.flaky, r.last_flaky_at)
            for r in rows[:limit]
        ],
        main_known_flaky=list(main.known_flaky) if main else [],
        main_known_flaky_at=main.at if main else None,
        truncated=len(rows) > limit,
    )
