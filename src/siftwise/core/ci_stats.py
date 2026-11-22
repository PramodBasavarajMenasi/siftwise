"""What siftwise did in CI, as numbers: the one place they are computed (#85, part 2).

The dashboard's overview, ``GET /repos/{repo}/ci`` and ``siftwise report`` all call
``ci_stats``, so the three never disagree. Every number is defined here, and the README's "How
these numbers work" says the same in words:

* A CI run is one CI run attempt (``ci_run_id``, ``run_attempt``) with at least one upload;
  re-run uploads (``runs.rerun`` > 0) are not runs. Each attempt counts once: a re-run of the
  whole workflow is a new attempt, so a new CI run. An upload without a CI run ID is a CI run
  of its own (it can't have a selection).
* A CI run is in the window when any of its uploads started (``Run.occurred_at``) within the
  last ``days`` days, counted back from now (as the dashboard's other pages count).
* ``main``: one of its uploads is on the default branch (``runs.is_main``); ``prs``: every
  other run (pull requests and pushes to other branches).
* Coverage: runs with a recorded selection (any ``ci_selections`` row for the attempt) of all
  runs. Every other number uses only runs with one; shadow runs (which ran the full suite) are
  counted apart and never feed the selective numbers.
* Selective: every non-shadow selection of the attempt (one, or one per leg) was selective; else
  full. A full run counts once under each reason code its selections give.
* Share selected: in a selective run, its selections' selected tests / known tests (summed when
  each leg has its own); the median over selective runs.
* Test time saved (estimated): in a selective run, for each uploaded leg, the leg's baseline
  minus its measured test time (``runs.measured_ms``). The baseline is the median measured test
  time of the same variant's full-suite legs (their selection was full, or a shadow run's) that
  started in the ``BASELINE_DAYS`` days before this leg, never after, and needs
  ``BASELINE_MIN_RUNS`` of them. A measured time of 0 (the upload reported no durations) is no
  measured time. Legs without a baseline or a measured time are left out of both (and
  counted); a run counts when one of its legs does. Under ``LOW_CONFIDENCE_RUNS`` counted runs
  the figure is still shown, flagged as too few for a reliable figure. Not the summed
  per-test estimate /select returns: matrix legs run different tests (skips, platform-only
  tests), and charging every leg every known test's mean overstated the saving (rdflib: 413 s
  estimated for a 231 s leg).
  Sums of test durations, not wall-clock CI time. Kept when negative.
* Shadow runs: the test time their selection would have saved, measured on the run itself
  (``runs.measured_ms`` - ``run_outcomes.ms_would_run``, per compared leg). Kept apart from the
  time saved: those runs ran everything.
* Failures in selective runs with a stored outcome: distinct failing tests per run; in the
  selection = the selection runs it (named, or its file/package/class ran whole); its rank =
  the best rank over the legs that named it. No recall: an unselected test didn't run there, so
  a failure it would have shown can't be seen. Recall comes from shadow runs only.
"""

import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

Branch = Literal["all", "main", "prs"]
BRANCHES: tuple[Branch, ...] = ("all", "main", "prs")
COUNTING = (
    "each CI run attempt counts once (a re-run of the workflow is a new attempt); re-run "
    "uploads of failing tests are not runs"
)
NO_REASON = "not_recorded"  # a full selection stored without a reason code
BASELINE_DAYS = 30  # a leg's baseline: full-suite legs of its variant this far back
BASELINE_MIN_RUNS = 3  # and at least this many of them
LOW_CONFIDENCE_RUNS = 5  # time saved from fewer counted runs is flagged as unreliable


@dataclass(frozen=True)
class Baseline:
    """A leg's full-suite baseline: the median measured test time of ``runs`` full-suite legs
    of its variant in the ``BASELINE_DAYS`` days before it."""

    ms: float
    runs: int


@dataclass(frozen=True)
class Saving:
    """Test time saved (estimated) over the legs that have a baseline and a measured time."""

    baseline_ms: float
    measured_ms: float
    legs: int  # legs counted
    left_out: int = 0  # legs without a baseline or a measured time

    @property
    def saved_ms(self) -> float:
        return self.baseline_ms - self.measured_ms


def saving(legs: Sequence[tuple[Baseline | None, float | None]]) -> Saving | None:
    """``(baseline, measured ms)`` per leg -> the saving over the legs with both (the rest are
    left out); None when no leg has both. One definition for this module and the run page."""
    # A measured 0 means the upload reported no durations: no measured time.
    counted = [(b.ms, ms) for b, ms in legs if b is not None and ms is not None and ms > 0]
    if not counted:
        return None
    return Saving(sum(b for b, _ in counted), sum(ms for _, ms in counted), len(counted),
                  len(legs) - len(counted))  # fmt: skip


# (variant) -> sorted [(started, measured ms)] of full-suite legs.
FullSuiteLegs = dict[str | None, list[tuple[datetime, float]]]
_FULL_SUITE_LEGS_SQL = """
SELECT r.variant, COALESCE(r.started_at, r.created_at) AS at, r.measured_ms
FROM runs r
JOIN LATERAL (
    SELECT s.mode, s.shadow FROM ci_selections s
    WHERE s.repo_id = r.repo_id AND s.ci_run_id = r.ci_run_id AND s.run_attempt = r.run_attempt
      AND (s.variant = r.variant OR s.variant IS NULL)
    ORDER BY s.variant IS NULL LIMIT 1
) s ON s.mode = 'full' OR s.shadow
WHERE r.repo_id = :repo_id AND r.rerun = 0 AND r.measured_ms > 0
  AND COALESCE(r.started_at, r.created_at) >= :since
  AND COALESCE(r.started_at, r.created_at) < :until
"""


def full_suite_legs(session: Session, repo_id: int, since: datetime | None,
                    until: datetime) -> FullSuiteLegs:  # fmt: skip
    """The full-suite legs (their selection, the variant's own else the attempt's, was full or
    a shadow run's) with a measured time, started in [since, until), by variant."""
    found: FullSuiteLegs = defaultdict(list)
    for variant, at, ms in session.execute(
        text(_FULL_SUITE_LEGS_SQL),
        {"repo_id": repo_id, "since": since or datetime.min.replace(tzinfo=UTC), "until": until},
    ):
        found[variant].append((at, ms))
    for legs in found.values():
        legs.sort()
    return found


def baseline(full: FullSuiteLegs, variant: str | None, at: datetime) -> Baseline | None:
    """The leg's baseline from the variant's full-suite legs started in the ``BASELINE_DAYS``
    days before ``at`` (never at or after it); None with fewer than ``BASELINE_MIN_RUNS``."""
    times = [ms for started, ms in full.get(variant, ())
             if at - timedelta(days=BASELINE_DAYS) <= started < at]  # fmt: skip
    if len(times) < BASELINE_MIN_RUNS:
        return None
    return Baseline(statistics.median(times), len(times))


def leg_baselines(
    session: Session, repo_id: int, legs: Sequence[tuple[str | None, datetime]]
) -> list[Baseline | None]:
    """``baseline`` for each ``(variant, started)`` leg, from one read."""
    if not legs:
        return []
    first, last = min(at for _, at in legs), max(at for _, at in legs)
    full = full_suite_legs(session, repo_id, first - timedelta(days=BASELINE_DAYS), last)
    return [baseline(full, variant, at) for variant, at in legs]


@dataclass
class Failures:
    """Failures in selective runs with a stored outcome."""

    runs: int = 0  # selective runs with a stored outcome
    runs_failing: int = 0  # of those, with a failing test
    failing: int = 0  # distinct failing tests, summed over runs
    in_selection: int = 0  # the selection runs them
    outside: int = 0  # they ran outside the selection
    ranks: list[int] = field(default_factory=list)  # each named failure's rank
    first_ranks: list[int] = field(default_factory=list)  # each run's best-ranked failure

    @property
    def median_rank(self) -> float | None:
        return statistics.median(self.ranks) if self.ranks else None

    @property
    def median_first_rank(self) -> float | None:
        return statistics.median(self.first_ranks) if self.first_ranks else None


@dataclass
class CiStats:
    """What siftwise did in CI over a window, for one branch filter (see the module doc)."""

    days: int | None
    branch: Branch
    since: datetime | None
    until: datetime
    runs: int = 0
    recorded: int = 0
    shadow_runs: int = 0
    selective_runs: int = 0
    full_runs: int = 0
    full_reasons: Counter[str] = field(default_factory=Counter)
    shares: list[float] = field(default_factory=list)  # each selective run's share selected
    savings: list[float] = field(default_factory=list)  # each selective run's saved ms
    baseline_ms: float = 0.0  # the counted legs' baselines, summed
    legs_counted: int = 0  # selective runs' legs with a baseline and a measured time
    legs_left_out: int = 0  # selective runs' legs without one of them
    shadow_measured_ms: float = 0.0
    shadow_would_save_ms: float = 0.0
    shadow_compared: int = 0  # shadow runs with a compared leg
    failures: Failures = field(default_factory=Failures)

    @property
    def counted(self) -> int:
        """Runs with a recorded selection that weren't shadow runs."""
        return self.selective_runs + self.full_runs

    @property
    def median_share(self) -> float | None:
        return statistics.median(self.shares) if self.shares else None

    @property
    def saved_ms(self) -> float | None:
        return sum(self.savings) if self.savings else None

    @property
    def median_saved_ms(self) -> float | None:
        return statistics.median(self.savings) if self.savings else None

    @property
    def low_confidence(self) -> bool:
        """Time saved rests on fewer than ``LOW_CONFIDENCE_RUNS`` counted runs."""
        return 0 < len(self.savings) < LOW_CONFIDENCE_RUNS


@dataclass
class _Attempt:
    key: tuple[str, int] | None  # None: an upload without a CI run ID
    main: bool
    legs: list[tuple[int, str | None, float | None]]  # (run id, variant, measured ms)
    started: list[datetime] = field(default_factory=list)  # each leg's Run.occurred_at
    selections: list[Any] = field(default_factory=list)


_ATTEMPTS_SQL = """
SELECT r.ci_run_id, r.run_attempt, bool_or(r.is_main) AS main,
       array_agg(r.id ORDER BY r.id) AS ids,
       array_agg(r.variant ORDER BY r.id) AS variants,
       array_agg(r.measured_ms ORDER BY r.id) AS measured,
       array_agg(COALESCE(r.started_at, r.created_at) ORDER BY r.id) AS started
FROM runs r
WHERE r.repo_id = :repo_id AND r.rerun = 0
GROUP BY r.ci_run_id, r.run_attempt, CASE WHEN r.ci_run_id IS NULL THEN r.id END
HAVING max(COALESCE(r.started_at, r.created_at)) >= :since
"""
_SELECTIONS_SQL = """
SELECT s.id, s.ci_run_id, s.run_attempt, s.variant, s.shadow, s.mode, s.reason_code,
       s.selected_count, s.total_known, s.estimated_ms_total
FROM ci_selections s
JOIN unnest(CAST(:ci AS text[]), CAST(:attempt AS integer[])) AS a(ci, attempt)
  ON s.ci_run_id = a.ci AND s.run_attempt = a.attempt
WHERE s.repo_id = :repo_id
"""


def ci_stats(
    session: Session,
    repo: str,
    days: int | None,
    branch: Branch = "all",
    now: datetime | None = None,
) -> CiStats | None:
    """The CI numbers for ``repo`` over the last ``days`` days (None: all history) and the
    branch filter; None for an unknown repo. A fixed number of queries."""
    by_branch = ci_stats_by_branch(session, repo, days, (branch,), now)
    return None if by_branch is None else by_branch[branch]


def ci_stats_by_branch(
    session: Session,
    repo: str,
    days: int | None,
    branches: Sequence[Branch] = BRANCHES,
    now: datetime | None = None,
) -> dict[Branch, CiStats] | None:
    """``ci_stats`` for several branch filters from one read (the dashboard shows all three)."""
    repo_id = session.execute(
        text("SELECT id FROM repos WHERE name = :name"), {"name": repo}
    ).scalar_one_or_none()
    if repo_id is None:
        return None
    now = now or datetime.now(UTC)
    since = None if days is None else now - timedelta(days=days)
    attempts = _attempts(session, repo_id, since)
    _attach_selections(session, repo_id, attempts)
    stats = {b: CiStats(days, b, since, now) for b in branches}
    selective: list[tuple[_Attempt, list[CiStats]]] = []
    shadow: list[tuple[_Attempt, list[CiStats]]] = []
    for attempt in attempts:
        targets = [s for b, s in stats.items() if b == "all" or (b == "main") == attempt.main]
        kind = _kind(attempt)
        for s in targets:
            s.runs += 1
            if kind is None:
                continue
            s.recorded += 1
            if kind == "shadow":
                s.shadow_runs += 1
            elif kind == "selective":
                s.selective_runs += 1
            else:
                s.full_runs += 1
                for code in sorted({sel.reason_code or NO_REASON for sel in attempt.selections
                                    if not sel.shadow and sel.mode == "full"}):  # fmt: skip
                    s.full_reasons[code] += 1
        if kind == "selective":
            selective.append((attempt, targets))
            share = _share(attempt)
            for s in targets:
                if share is not None:
                    s.shares.append(share)
        elif kind == "shadow":
            shadow.append((attempt, targets))
    _add_savings(session, repo_id, selective)
    _add_shadow_savings(session, shadow)
    _add_failures(session, selective)
    return stats


def _attempts(session: Session, repo_id: int, since: datetime | None) -> list[_Attempt]:
    rows = session.execute(
        text(_ATTEMPTS_SQL),
        {"repo_id": repo_id, "since": since or datetime.min.replace(tzinfo=UTC)},
    ).all()
    return [
        _Attempt(
            None if row.ci_run_id is None else (row.ci_run_id, row.run_attempt),
            row.main,
            list(zip(row.ids, row.variants, row.measured, strict=True)),
            list(row.started),
        )
        for row in rows
    ]


def _attach_selections(session: Session, repo_id: int, attempts: list[_Attempt]) -> None:
    by_key = {a.key: a for a in attempts if a.key is not None}
    if not by_key:
        return
    for row in session.execute(
        text(_SELECTIONS_SQL),
        {"repo_id": repo_id, "ci": [k[0] for k in by_key], "attempt": [k[1] for k in by_key]},
    ):
        by_key[(row.ci_run_id, row.run_attempt)].selections.append(row)


def _kind(attempt: _Attempt) -> str | None:
    """None (no recorded selection), "shadow", "selective" or "full"."""
    if not attempt.selections:
        return None
    if any(s.shadow for s in attempt.selections):
        return "shadow"
    return "selective" if all(s.mode == "selective" for s in attempt.selections) else "full"


def _share(attempt: _Attempt) -> float | None:
    known = sum(s.total_known for s in attempt.selections)
    return sum(s.selected_count for s in attempt.selections) / known if known else None


def _add_savings(session: Session, repo_id: int,
                 selective: list[tuple[_Attempt, list[CiStats]]]) -> None:  # fmt: skip
    """Each selective run's legs against their baselines (one read for all of them)."""
    legs = [(leg[1], at) for attempt, _ in selective
            for leg, at in zip(attempt.legs, attempt.started, strict=True)]  # fmt: skip
    found = iter(leg_baselines(session, repo_id, legs))
    for attempt, targets in selective:
        pairs = [(next(found), leg[2]) for leg in attempt.legs]
        run_saving = saving(pairs)
        for s in targets:
            if run_saving is None:
                s.legs_left_out += len(pairs)
                continue
            s.savings.append(run_saving.saved_ms)
            s.baseline_ms += run_saving.baseline_ms
            s.legs_counted += run_saving.legs
            s.legs_left_out += run_saving.left_out


def _add_shadow_savings(session: Session, shadow: list[tuple[_Attempt, list[CiStats]]]) -> None:
    run_ids = [leg[0] for attempt, _ in shadow for leg in attempt.legs]
    if not run_ids:
        return
    compared = {
        run_id: (measured, would_run)
        for run_id, measured, would_run in session.execute(
            text(
                "SELECT o.run_id, r.measured_ms, o.ms_would_run FROM run_outcomes o "
                "JOIN runs r ON r.id = o.run_id JOIN ci_selections s ON s.id = o.selection_id "
                "WHERE o.run_id = ANY(:ids) AND s.shadow AND r.measured_ms IS NOT NULL "
                "AND o.ms_would_run IS NOT NULL"
            ),
            {"ids": run_ids},
        )
    }
    for attempt, targets in shadow:
        legs = [compared[leg[0]] for leg in attempt.legs if leg[0] in compared]
        if not legs:
            continue
        measured = sum(m for m, _ in legs)
        would_save = sum(m - w for m, w in legs)
        for s in targets:
            s.shadow_compared += 1
            s.shadow_measured_ms += measured
            s.shadow_would_save_ms += would_save


def _add_failures(session: Session, selective: list[tuple[_Attempt, list[CiStats]]]) -> None:
    run_to_attempt = {leg[0]: i for i, (attempt, _) in enumerate(selective)
                      for leg in attempt.legs}  # fmt: skip
    if not run_to_attempt:
        return
    with_outcome: set[int] = set()
    for (run_id,) in session.execute(
        text("SELECT run_id FROM run_outcomes WHERE run_id = ANY(:ids)"),
        {"ids": list(run_to_attempt)},
    ):
        with_outcome.add(run_to_attempt[run_id])
    # Per attempt and test: does the selection run it on any leg, and its best rank.
    tests: dict[int, dict[int, tuple[bool, int | None]]] = defaultdict(dict)
    for run_id, key, would_run, rank in session.execute(
        text("SELECT run_id, test_key, would_run, rank FROM run_failures WHERE run_id = ANY(:ids)"),
        {"ids": list(run_to_attempt)},
    ):
        i = run_to_attempt[run_id]
        ran, best = tests[i].get(key, (False, None))
        ranks = [r for r in (best, rank) if r is not None]
        tests[i][key] = (ran or bool(would_run), min(ranks) if ranks else None)
    for i in sorted(with_outcome):
        failing = tests.get(i, {})
        ranks = sorted(rank for _, rank in failing.values() if rank is not None)
        in_selection = sum(1 for ran, _ in failing.values() if ran)
        for s in selective[i][1]:
            f = s.failures
            f.runs += 1
            f.runs_failing += bool(failing)
            f.failing += len(failing)
            f.in_selection += in_selection
            f.outside += len(failing) - in_selection
            f.ranks.extend(ranks)
            if ranks:
                f.first_ranks.append(ranks[0])
