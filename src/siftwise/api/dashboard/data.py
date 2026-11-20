"""What the dashboard's pages read. Recall, flaky tests and verdicts come from the same core
functions as the API (``shadow.recall``, ``flaky.flaky_tests``, ``triage.triage_ci_runs``);
this module only lists rows those don't cover (repos, recent CI run attempts, a shadow run's
stored comparison). Read-only."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from siftwise.core.tokens import ALL_REPOS, Principal
from siftwise.core.triage import recorded_failures

RECENT_RUNS = 20  # CI run attempts on the runs page


# A CI run = one CI run attempt with its matrix legs (variants); an upload without a CI run ID
# counts on its own. Re-run uploads never count.
_CI_RUNS = """count(DISTINCT (r.ci_run_id, r.run_attempt)) FILTER (WHERE r.ci_run_id IS NOT NULL{w})
           + count(*) FILTER (WHERE r.ci_run_id IS NULL{w})"""
_IN_WINDOW = " AND COALESCE(r.started_at, r.created_at) >= :since"

_ACTIVITY_SQL = f"""
SELECT p.name, a.runs_total, a.runs_window, a.newest, a.oldest,
       s.shadow_total, s.shadow_window, s.shadow_newest, s.shadow_oldest
FROM repos p
CROSS JOIN LATERAL (
    SELECT {_CI_RUNS.format(w="")} AS runs_total,
           {_CI_RUNS.format(w=_IN_WINDOW)} AS runs_window,
           max(COALESCE(r.started_at, r.created_at)) AS newest,
           min(COALESCE(r.started_at, r.created_at)) AS oldest
    FROM runs r WHERE r.repo_id = p.id AND r.rerun = 0
) a
CROSS JOIN LATERAL (  -- shadow runs by selection time, as recall counts them
    SELECT count(DISTINCT (x.ci_run_id, x.run_attempt)) AS shadow_total,
           count(DISTINCT (x.ci_run_id, x.run_attempt))
               FILTER (WHERE x.created_at >= :since) AS shadow_window,
           max(x.created_at) AS shadow_newest, min(x.created_at) AS shadow_oldest
    FROM shadow_selections x WHERE x.repo_id = p.id
) s
WHERE CAST(:only AS text) IS NULL OR p.name = :only
ORDER BY a.newest DESC NULLS LAST, p.name  -- newest run first; repos with no runs last
"""


@dataclass(frozen=True)
class Activity:
    """What a repo has recorded, in all and in the window (since a time, to now)."""

    name: str
    runs_total: int = 0  # CI runs (see _CI_RUNS)
    runs_window: int = 0
    newest_run_at: datetime | None = None
    oldest_run_at: datetime | None = None
    shadow_total: int = 0  # shadow runs: CI run attempts with a stored selection
    shadow_window: int = 0
    newest_shadow_at: datetime | None = None
    oldest_shadow_at: datetime | None = None


def activity(session: Session, since: datetime | None, only: str | None = None
             ) -> list[Activity]:  # fmt: skip
    """Every repo's activity (``only``: that repo's alone). ``since`` None: the whole history
    (the window columns then equal the totals)."""
    rows = session.execute(
        text(_ACTIVITY_SQL),
        {"only": only, "since": since or datetime.min.replace(tzinfo=UTC)},
    ).all()
    return [Activity(*row) for row in rows]


def repos_for(session: Session, principal: Principal, since: datetime | None) -> list[Activity]:
    """The repos this token can see. A one-repo token sees its repo even before its first
    upload (no runs yet), and never another's name."""
    only = None if principal.repo == ALL_REPOS else principal.repo
    found = activity(session, since, only)
    if only is not None and not found:
        found = [Activity(only)]
    return found


def repo_activity(session: Session, repo: str, since: datetime | None) -> Activity:
    found = activity(session, since, repo)
    return found[0] if found else Activity(repo)


@dataclass
class RunRow:
    """One CI run attempt (its variants together) and its failing tests."""

    ci_run_id: str
    run_attempt: int
    at: datetime
    branch: str
    is_main: bool
    commit_sha: str
    variants: int
    failing: int = 0
    shadow_mode: str | None = None  # the stored shadow selection's mode, if a shadow run


def recent_runs(session: Session, repo: str, limit: int = RECENT_RUNS) -> list[RunRow] | None:
    """The newest ``limit`` CI run attempts, each with its failing tests (final attempt failed
    or errored, as triage counts them; distinct over the variants). Verdicts are on each run's
    page: counting them here was measured too slow on rdflib (20 attempts with failures, 245
    uploads, 2,090 failing tests: 11 s warm, 26 s cold, in one batched load). None for an
    unknown repo."""
    repo_id = session.execute(
        text("SELECT id FROM repos WHERE name = :name"), {"name": repo}
    ).scalar_one_or_none()
    if repo_id is None:
        return None
    rows = session.execute(
        text(
            """
            SELECT r.ci_run_id, r.run_attempt, max(COALESCE(r.started_at, r.created_at)) AS at,
                   min(r.branch) AS branch, bool_or(r.is_main) AS is_main,
                   min(r.commit_sha) AS commit_sha, count(*) AS variants,
                   array_agg(r.id) AS run_ids
            FROM runs r
            WHERE r.repo_id = :repo_id AND r.rerun = 0 AND r.ci_run_id IS NOT NULL
            GROUP BY r.ci_run_id, r.run_attempt
            ORDER BY at DESC, r.ci_run_id DESC, r.run_attempt DESC
            LIMIT :limit
            """
        ),
        {"repo_id": repo_id, "limit": limit},
    ).all()
    runs = [RunRow(r.ci_run_id, r.run_attempt, r.at, r.branch, r.is_main, r.commit_sha,
                   r.variants) for r in rows]  # fmt: skip
    if not runs:
        return runs
    failures = recorded_failures(session, [i for r in rows for i in r.run_ids])
    for run, row in zip(runs, rows, strict=True):
        run.failing = len({f.test_id for i in row.run_ids for f in failures.get(i, ())})
    modes = shadow_modes(session, repo_id, [(r.ci_run_id, r.run_attempt) for r in runs])
    for run in runs:
        run.shadow_mode = modes.get((run.ci_run_id, run.run_attempt))
    return runs


def shadow_modes(
    session: Session, repo_id: int, keys: list[tuple[str, int]]
) -> dict[tuple[str, int], str]:
    """Each attempt's stored shadow selection mode ("selective" wins over "full" when legs
    differ: one selective leg makes the run selective-looking; the run page has the detail)."""
    out: dict[tuple[str, int], str] = {}
    for ci, attempt, mode in session.execute(
        text(
            """
            SELECT s.ci_run_id, s.run_attempt, s.mode FROM shadow_selections s
            JOIN unnest(CAST(:ci AS text[]), CAST(:attempt AS integer[])) AS a(ci, attempt)
              ON s.ci_run_id = a.ci AND s.run_attempt = a.attempt
            WHERE s.repo_id = :repo_id
            ORDER BY s.mode DESC
            """
        ),
        {"repo_id": repo_id, "ci": [k[0] for k in keys], "attempt": [k[1] for k in keys]},
    ):
        out.setdefault((ci, attempt), mode)
    return out


@dataclass(frozen=True)
class ShadowVariant:
    variant: str | None
    mode: str
    failing: int
    caught: int
    missed: int
    flaky: int
    tests_total: int
    tests_would_run: int
    ms_total: float
    ms_would_run: float


@dataclass(frozen=True)
class ShadowMissRow:
    test_id: str
    variant: str | None
    verdict: str
    cause: str | None
    cause_detail: str | None


@dataclass
class ShadowAttempt:
    variants: list[ShadowVariant]
    misses: list[ShadowMissRow]


def shadow_attempt(session: Session, repo: str, ci_run_id: str, attempt: int) -> ShadowAttempt:
    """A shadow run's stored comparison (``POST /repos/{repo}/shadow`` wrote it): per variant,
    and its misses with their causes. Empty if it wasn't compared."""
    variants = [
        ShadowVariant(*row)
        for row in session.execute(
            text(
                """
                SELECT r.variant, s.mode, x.failing, x.caught, x.missed, x.flaky,
                       x.tests_total, x.tests_would_run, x.ms_total, x.ms_would_run
                FROM shadow_results x
                JOIN runs r ON r.id = x.run_id
                JOIN repos p ON p.id = r.repo_id
                JOIN shadow_selections s ON s.id = x.selection_id
                WHERE p.name = :repo AND r.ci_run_id = :ci AND r.run_attempt = :attempt
                ORDER BY COALESCE(r.variant, '')
                """
            ),
            {"repo": repo, "ci": ci_run_id, "attempt": attempt},
        )
    ]
    misses = [
        ShadowMissRow(*row)
        for row in session.execute(
            text(
                """
                SELECT t.test_id, r.variant, f.verdict, f.cause, f.cause_detail
                FROM shadow_failures f
                JOIN runs r ON r.id = f.run_id
                JOIN repos p ON p.id = r.repo_id
                JOIN tests t ON t.id = f.test_key
                WHERE p.name = :repo AND r.ci_run_id = :ci AND r.run_attempt = :attempt
                  AND NOT f.would_run AND f.verdict <> 'flaky'
                ORDER BY t.test_id, COALESCE(r.variant, '')
                LIMIT 200
                """
            ),
            {"repo": repo, "ci": ci_run_id, "attempt": attempt},
        )
    ]
    return ShadowAttempt(variants, misses)


def attempt_meta(session: Session, repo: str, ci_run_id: str, attempt: int) -> RunRow | None:
    row = session.execute(
        text(
            """
            SELECT r.ci_run_id, r.run_attempt, max(COALESCE(r.started_at, r.created_at)) AS at,
                   min(r.branch) AS branch, bool_or(r.is_main) AS is_main,
                   min(r.commit_sha) AS commit_sha, count(*) AS variants
            FROM runs r JOIN repos p ON p.id = r.repo_id
            WHERE p.name = :repo AND r.ci_run_id = :ci AND r.run_attempt = :attempt
              AND r.rerun = 0
            GROUP BY r.ci_run_id, r.run_attempt
            """
        ),
        {"repo": repo, "ci": ci_run_id, "attempt": attempt},
    ).one_or_none()
    if row is None:
        return None
    return RunRow(row.ci_run_id, row.run_attempt, row.at, row.branch, row.is_main,
                  row.commit_sha, row.variants)  # fmt: skip
