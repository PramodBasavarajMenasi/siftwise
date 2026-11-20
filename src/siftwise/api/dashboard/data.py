"""What the dashboard's pages read. Recall, flaky tests and verdicts come from the same core
functions as the API (``shadow.recall``, ``flaky.flaky_tests``, ``triage.triage_ci_runs``);
this module only lists rows those don't cover (repos, recent CI run attempts, a shadow run's
stored comparison). Read-only."""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from siftwise.core.models import CiSelection, Repo
from siftwise.core.selector import WouldRun
from siftwise.core.shadow import budget_limited, selection_scopes
from siftwise.core.tokens import ALL_REPOS, Principal
from siftwise.core.triage import recorded_failures

RECENT_RUNS = 20  # CI run attempts on the runs page


# A CI run = one CI run attempt with its matrix legs (variants); an upload without a CI run ID
# counts on its own. Re-run uploads never count.
_CI_RUNS = """count(DISTINCT (r.ci_run_id, r.run_attempt)) FILTER (WHERE r.ci_run_id IS NOT NULL{w})
           + count(*) FILTER (WHERE r.ci_run_id IS NULL{w})"""
_IN_WINDOW = " AND COALESCE(r.started_at, r.created_at) >= :since"
# A CI run with a recorded selection: any row in ci_selections for its attempt (shadow ones
# too). Uploads without a CI run ID never have one.
_SELECTED = """count(DISTINCT (r.ci_run_id, r.run_attempt)) FILTER (WHERE r.ci_run_id IS NOT NULL
               AND EXISTS (SELECT 1 FROM ci_selections c WHERE c.repo_id = r.repo_id
                           AND c.ci_run_id = r.ci_run_id AND c.run_attempt = r.run_attempt){w})"""

_ACTIVITY_SQL = f"""
SELECT p.name, a.runs_total, a.runs_window, a.newest, a.oldest,
       s.shadow_total, s.shadow_window, s.shadow_newest, s.shadow_oldest,
       a.selected_total, a.selected_window
FROM repos p
CROSS JOIN LATERAL (
    SELECT {_CI_RUNS.format(w="")} AS runs_total,
           {_CI_RUNS.format(w=_IN_WINDOW)} AS runs_window,
           max(COALESCE(r.started_at, r.created_at)) AS newest,
           min(COALESCE(r.started_at, r.created_at)) AS oldest,
           {_SELECTED.format(w="")} AS selected_total,
           {_SELECTED.format(w=_IN_WINDOW)} AS selected_window
    FROM runs r WHERE r.repo_id = p.id AND r.rerun = 0
) a
CROSS JOIN LATERAL (  -- shadow runs by selection time, as recall counts them
    SELECT count(DISTINCT (x.ci_run_id, x.run_attempt)) AS shadow_total,
           count(DISTINCT (x.ci_run_id, x.run_attempt))
               FILTER (WHERE x.created_at >= :since) AS shadow_window,
           max(x.created_at) AS shadow_newest, min(x.created_at) AS shadow_oldest
    FROM ci_selections x WHERE x.repo_id = p.id AND x.shadow
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
    # CI runs (uploaded) with a recorded selection: every page shows it next to the CI runs,
    # so partial data never looks complete.
    selected_total: int = 0
    selected_window: int = 0


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
    selection: "SelectionSummary | None" = None  # what siftwise selected, if recorded


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
    summaries = selection_summaries(session, repo_id,
                                    [(r.ci_run_id, r.run_attempt) for r in runs])  # fmt: skip
    for run in runs:
        run.selection = summaries.get((run.ci_run_id, run.run_attempt))
    return runs


@dataclass(frozen=True)
class SelectionSummary:
    """A CI run attempt's recorded selection(s) for a list: one per attempt or per leg."""

    mode: str  # "selective" if every selection is, else "full"
    shadow: bool  # a shadow run: the full suite ran, the selection is what siftwise would run
    selected: int  # summed over the selections (one per attempt, or one per leg)
    known: int
    selections: int
    reason_code: str | None  # a full selection's (the first, when legs differ)
    budget_limited: bool


def selection_summaries(
    session: Session, repo_id: int, keys: list[tuple[str, int]]
) -> dict[tuple[str, int], SelectionSummary]:
    """Each attempt's recorded selection, summed over its rows."""
    rows: dict[tuple[str, int], list[Any]] = {}
    for row in session.execute(
        text(
            """
            SELECT s.ci_run_id, s.run_attempt, s.mode, s.shadow, s.selected_count,
                   s.total_known, s.reason_code,
                   COALESCE(CAST(s.budget ->> 'limited' AS boolean), false) AS limited
            FROM ci_selections s
            JOIN unnest(CAST(:ci AS text[]), CAST(:attempt AS integer[])) AS a(ci, attempt)
              ON s.ci_run_id = a.ci AND s.run_attempt = a.attempt
            WHERE s.repo_id = :repo_id
            ORDER BY COALESCE(s.variant, '')
            """
        ),
        {"repo_id": repo_id, "ci": [k[0] for k in keys], "attempt": [k[1] for k in keys]},
    ):
        rows.setdefault((row.ci_run_id, row.run_attempt), []).append(row)
    return {
        key: SelectionSummary(
            mode="selective" if all(r.mode == "selective" for r in found) else "full",
            shadow=any(r.shadow for r in found),
            selected=sum(r.selected_count for r in found),
            known=sum(r.total_known for r in found),
            selections=len(found),
            reason_code=next((r.reason_code for r in found if r.reason_code), None),
            budget_limited=any(r.limited for r in found),
        )
        for key, found in rows.items()
    }


_GO_PACKAGE_REASON = re.compile(r"in Go package (\S+), which has changed (.+)")


def grouped_reasons(reasons: Sequence[str]) -> list[str]:
    """Reasons for display: "in Go package X, which has changed A" repeated for several files
    becomes one line per package listing all its files, where the package first appears. The
    stored reasons are never changed."""
    shown: list[tuple[bool, str]] = []  # (is a Go package, the package or the reason)
    files: dict[str, list[str]] = {}
    for reason in reasons:
        match = _GO_PACKAGE_REASON.fullmatch(reason)
        if match is None:
            shown.append((False, reason))
            continue
        package = match[1]
        if package not in files:
            files[package] = []
            shown.append((True, package))
        if match[2] not in files[package]:
            files[package].append(match[2])
    return [
        f"in Go package {key}, which has changed {', '.join(files[key])}" if go else key
        for go, key in shown
    ]


@dataclass(frozen=True)
class RiskyTest:
    rank: int
    test_id: str
    reasons: list[str]
    why: str


@dataclass(frozen=True)
class LegTime:
    variant: str | None
    measured_ms: float | None
    tests_run: int | None


@dataclass
class RunSelection:
    """One recorded selection of a CI run attempt, for the run page."""

    row: CiSelection
    legs: list[LegTime]  # the uploaded legs it applies to, with their measured test time
    risky: list[RiskyTest] = field(default_factory=list)

    @property
    def budget_limited(self) -> bool:
        return budget_limited(self.row)

    @property
    def measured_ms(self) -> float | None:
        times = [leg.measured_ms for leg in self.legs if leg.measured_ms is not None]
        return sum(times) if times else None

    @property
    def saved_ms(self) -> float | None:
        """Test time saved (estimated): the full-suite estimate minus the measured time, per
        uploaded leg of a selective run; None for a full or shadow run, or nothing measured."""
        measured = [leg.measured_ms for leg in self.legs if leg.measured_ms is not None]
        if self.row.shadow or self.row.mode != "selective" or not measured:
            return None
        if not self.row.estimated_ms_total:  # no durations in history: nothing to compare
            return None
        return sum(self.row.estimated_ms_total - ms for ms in measured)


@dataclass
class RunSelections:
    """What siftwise selected for a CI run attempt, and what that says about each failure."""

    selections: list[RunSelection]
    # Why nothing was recorded (empty when something was): the run page says it.
    missing: str = ""
    upgrade: bool = False  # the reason is a client older than 0.4: link to upgrading
    would_run: dict[int, WouldRun] = field(default_factory=dict)  # by selection id
    # selection id -> its selected tests' ranks (None: stored before 0017, unranked)
    ranks: dict[int, dict[str, int | None]] = field(default_factory=dict)

    def for_variant(self, variant: str | None) -> RunSelection | None:
        by_variant = {s.row.variant: s for s in self.selections}
        return by_variant.get(variant) or by_variant.get(None)

    def place(self, test_id: str, variant: str | None) -> tuple[str, int | None]:
        """A failing test in the leg's selection: ("selected", rank), ("run whole", None) (its
        file, package or class ran whole), ("full suite", None), ("not selected", None), or
        ("", None) without a selection."""
        found = self.for_variant(variant)
        if found is None:
            return "", None
        if found.row.mode == "full":
            return "full suite", None
        ranks = self.ranks[found.row.id]
        if test_id in ranks:
            return "selected", ranks[test_id]
        if self.would_run[found.row.id](test_id):
            return "run whole", None
        return "not selected", None


# A run with no recorded selection: why, as far as the server can tell (runs.client_version).
# A run with no recorded selection: the one reason that applies, where the upload says
# (runs.client_version and runs.client: every 0.4 uploader sends both), else the list.
NO_SELECTION_FIRST_RUN = (
    "No selection was recorded: this was the repo's first CI run, and siftwise selects (and "
    "records) only for a repo it has history for."
)
NO_SELECTION_OLD_CLIENT = (
    "No selection was recorded: this run was uploaded by a siftwise client older than 0.4, "
    "which doesn't record selections. Upgrade the Action or the GitLab template to 0.4."
)
NO_SELECTION_NOT_REACHED = (
    "No selection was recorded: the select step didn't reach the server (siftwise then fails "
    "open to the full suite)."
)
NO_SELECTION_NO_SELECT_STEP = (
    "No selection was recorded: this run was uploaded with siftwise record or backfill, "
    "which run no select step."
)
NO_SELECTION_UNKNOWN = (
    "No selection was recorded, and the upload doesn't say why. It can be a client older "
    "than 0.4, a select step that didn't reach the server or had a read-only token, or a "
    "workflow with no select step."
)
CI_CLIENTS = frozenset({"github", "gitlab"})  # uploaders whose job has a select step
OTHER_CLIENTS = frozenset({"cli", "backfill"})


def no_selection_reason(first: bool, uploads: Sequence[tuple[str | None, str | None]]) -> str:
    """Why a CI run attempt has no recorded selection, from its uploads' (client_version,
    client): one reason where they tell, else the possibilities."""
    if first:
        return NO_SELECTION_FIRST_RUN
    clients = {client for _, client in uploads}
    if all(version is None for version, _ in uploads):
        return NO_SELECTION_OLD_CLIENT  # every 0.4 uploader sends its version
    if clients and clients <= CI_CLIENTS:
        return NO_SELECTION_NOT_REACHED
    if clients and clients <= OTHER_CLIENTS:
        return NO_SELECTION_NO_SELECT_STEP
    return NO_SELECTION_UNKNOWN


def run_selections(session: Session, repo: str, ci_run_id: str, attempt: int) -> RunSelections:
    """A CI run attempt's recorded selections (per attempt, or per leg), their legs' measured
    test time and highest-risk tests; or why nothing was recorded."""
    repo_id = session.scalars(select(Repo.id).where(Repo.name == repo)).one()
    rows = list(
        session.scalars(
            select(CiSelection)
            .where(
                CiSelection.repo_id == repo_id,
                CiSelection.ci_run_id == ci_run_id,
                CiSelection.run_attempt == attempt,
            )
            .order_by(CiSelection.variant.nulls_first())
        )
    )
    legs = session.execute(
        text(
            """
            SELECT r.variant, r.measured_ms, r.tests_run, r.client_version, r.client,
                   NOT EXISTS (SELECT 1 FROM runs o WHERE o.repo_id = r.repo_id
                               AND COALESCE(o.started_at, o.created_at)
                                   < COALESCE(r.started_at, r.created_at)) AS first
            FROM runs r
            WHERE r.repo_id = :repo_id AND r.ci_run_id = :ci AND r.run_attempt = :attempt
              AND r.rerun = 0
            ORDER BY COALESCE(r.variant, '')
            """
        ),
        {"repo_id": repo_id, "ci": ci_run_id, "attempt": attempt},
    ).all()
    if not rows:
        missing = no_selection_reason(
            any(leg.first for leg in legs), [(leg.client_version, leg.client) for leg in legs]
        )
        return RunSelections([], missing, upgrade=missing == NO_SELECTION_OLD_CLIENT)
    by_variant = {row.variant for row in rows}
    selections = [
        RunSelection(
            row,
            [
                LegTime(leg.variant, leg.measured_ms, leg.tests_run)
                for leg in legs
                if leg.variant == row.variant
                or (row.variant is None and leg.variant not in by_variant)
            ],
        )
        for row in rows
    ]
    scopes, names = selection_scopes(session, rows)
    keys = sorted({int(t["key"]) for row in rows for t in row.top_risk or ()})
    test_ids: dict[int, str] = {
        key: test_id
        for key, test_id in session.execute(
            text("SELECT id, test_id FROM tests WHERE id = ANY(:keys)"), {"keys": keys}
        )
    }
    found = RunSelections(selections)
    for selection in selections:
        row = selection.row
        found.would_run[row.id] = WouldRun(scopes[row.id])
        if row.ranked:  # a test's rank = its position in selected_keys
            found.ranks[row.id] = {
                names[key]: n for n, key in enumerate(row.selected_keys, start=1)
            }
            selection.risky = [
                RiskyTest(
                    n,
                    test_ids.get(int(t["key"]), "?"),
                    grouped_reasons(list(t.get("reasons") or ())),
                    str(t.get("why") or ""),
                )
                for n, t in enumerate(row.top_risk or (), start=1)
            ]
        else:  # stored before 0017: sorted by key, no ranks
            found.ranks[row.id] = dict.fromkeys(names[key] for key in row.selected_keys)
    return found


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
    """A shadow run's stored comparison (``POST /repos/{repo}/outcome`` wrote it): per variant,
    and its misses with their causes. Empty if it wasn't compared."""
    variants = [
        ShadowVariant(*row)
        for row in session.execute(
            text(
                """
                SELECT r.variant, s.mode, x.failing, COALESCE(x.caught, 0),
                       COALESCE(x.missed, 0), x.flaky, COALESCE(r.tests_run, 0),
                       COALESCE(x.tests_would_run, 0), COALESCE(r.measured_ms, 0),
                       COALESCE(x.ms_would_run, 0)
                FROM run_outcomes x
                JOIN runs r ON r.id = x.run_id
                JOIN repos p ON p.id = r.repo_id
                JOIN ci_selections s ON s.id = x.selection_id
                WHERE p.name = :repo AND r.ci_run_id = :ci AND r.run_attempt = :attempt
                  AND s.shadow
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
                FROM run_failures f
                JOIN run_outcomes x ON x.run_id = f.run_id
                JOIN ci_selections s ON s.id = x.selection_id
                JOIN runs r ON r.id = f.run_id
                JOIN repos p ON p.id = r.repo_id
                JOIN tests t ON t.id = f.test_key
                WHERE p.name = :repo AND r.ci_run_id = :ci AND r.run_attempt = :attempt
                  AND s.shadow AND f.would_run IS FALSE AND f.verdict <> 'flaky'
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
