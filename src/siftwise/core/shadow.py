"""Shadow mode: live recall.

A shadow run runs the full suite while ``POST /select`` stores what siftwise would have
selected (``store_selection``). Once the run is recorded, ``compare_ci_run`` checks every
failure against that selection with the selector's own ``WouldRun`` and triage's verdict:
caught, missed, or flaky (counted apart, never a miss). ``recall`` sums it up over days.

Units: a test in one CI run attempt. It's flaky only if every variant it failed in says flaky;
otherwise it's missed if any non-flaky failing variant's selection wouldn't have run it.
"""

import logging
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from siftwise.core.models import (
    SHADOW_COMMIT_MISMATCH,
    SHADOW_COMPARE_ERROR,
    SHADOW_COMPARED,
    SHADOW_NO_UPLOAD,
    SHADOW_PENDING,
    Repo,
    ShadowFailure,
    ShadowResult,
    ShadowSelection,
)
from siftwise.core.selector import Mode, Selection, SelectionScope, WouldRun, is_go_subtest
from siftwise.core.triage import triage_ci_run

logger = logging.getLogger(__name__)

CAUGHT = "caught"
MISSED = "missed"
FLAKY = "flaky"
# Worst first, as in the PR comment: a miss is reported under its worst verdict.
VERDICT_ORDER = ("new_regression", "unknown", "flaky", "pre_existing")
# A pending selection younger than this is a run still in progress (GitHub's job limit is
# 6 hours); older, its compare never ran: reported as "cancelled".
IN_PROGRESS = timedelta(hours=6)
CANCELLED = "cancelled"
# A recorded variant with no stored selection of its own and none for the whole attempt.
NO_SELECTION = "no_selection"
MAX_MISSES = 100


@dataclass(frozen=True)
class ShadowTarget:
    """The CI run attempt (and variant) a shadow selection is stored for."""

    ci_run_id: str
    run_attempt: int
    variant: str | None
    commit_sha: str


def store_selection(
    session: Session, repo: str, target: ShadowTarget, selection: Selection
) -> bool:
    """Store ``selection`` for a shadow run. True if it's stored (by this call, or already
    for the same commit by another leg); False if the attempt and variant hold a selection of
    another commit, or the repo is unknown. Commits."""
    repo_id = session.scalars(select(Repo.id).where(Repo.name == repo)).one_or_none()
    if repo_id is None:
        return False
    scope = SelectionScope.of(selection)
    keys = _test_keys(session, repo_id, sorted(scope.test_ids))
    # Selected tests come from history, so all have keys; a missing one can't be stored, and
    # the comparison would call it not selected: refuse rather than under-count.
    if len(keys) < len(scope.test_ids):
        logger.warning("shadow selection not stored: %d selected tests unknown",
                       len(scope.test_ids) - len(keys))  # fmt: skip
        return False
    inserted = session.execute(
        pg_insert(ShadowSelection)
        .values(
            repo_id=repo_id,
            ci_run_id=target.ci_run_id,
            run_attempt=target.run_attempt,
            variant=target.variant,
            commit_sha=target.commit_sha,
            mode=selection.mode.value,
            reason=selection.reason,
            selected_keys=sorted(keys[test_id] for test_id in scope.test_ids),
            go_selected_keys=sorted(keys[test_id] for test_id in scope.go_test_ids),
            go_packages=sorted(scope.go_packages),
            python_files=sorted(scope.python_files),
            selected_count=selection.selected_count,
            total_known=selection.total_known,
            estimated_ms_total=selection.estimated_ms_total,
            estimated_ms_would_run=selection.estimated_ms_would_run,
        )
        .on_conflict_do_nothing(
            # SHADOW_UNIQUE_INDEX: one selection per attempt and variant.
            index_elements=[
                ShadowSelection.repo_id,
                ShadowSelection.ci_run_id,
                ShadowSelection.run_attempt,
                func.coalesce(ShadowSelection.variant, ""),
            ]
        )
        .returning(ShadowSelection.id)
    ).scalar_one_or_none()
    session.commit()
    if inserted is not None:
        return True
    # Another leg stored it first (selections of one commit are the same), or a different
    # commit claims this attempt: that one isn't ours to compare.
    stored = session.scalars(
        select(ShadowSelection.commit_sha).where(
            ShadowSelection.repo_id == repo_id,
            ShadowSelection.ci_run_id == target.ci_run_id,
            ShadowSelection.run_attempt == target.run_attempt,
            func.coalesce(ShadowSelection.variant, "") == (target.variant or ""),
        )
    ).one()
    return stored == target.commit_sha


# --- counting ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class FailureRow:
    """A failing test in one compared variant."""

    test_id: str
    variant: str | None
    would_run: bool
    verdict: str
    selective: bool  # the variant's selection was selective


@dataclass(frozen=True)
class Unit:
    """A failing test in one CI run attempt, over its failing variants."""

    test_id: str
    outcome: str  # CAUGHT, MISSED or FLAKY
    verdict: str  # a miss: the worst verdict of its missed variants; else the worst overall
    selective: bool  # every failing variant's selection was selective
    variants: tuple[str | None, ...]  # a miss: the variants that missed it; else all failing


def units(rows: Iterable[FailureRow]) -> list[Unit]:
    """One CI run attempt's failures, one unit per test (sorted by test ID)."""
    by_test: dict[str, list[FailureRow]] = defaultdict(list)
    for row in rows:
        by_test[row.test_id].append(row)
    result = []
    for test_id, failed in sorted(by_test.items()):
        selective = all(row.selective for row in failed)
        counted = [row for row in failed if row.verdict != FLAKY]
        missed = [row for row in counted if not row.would_run]
        if not counted:
            outcome, rows = FLAKY, failed
        elif missed:
            outcome, rows = MISSED, missed
        else:
            outcome, rows = CAUGHT, counted
        verdict = min((row.verdict for row in rows), key=_verdict_rank)
        variants = tuple(sorted({row.variant for row in rows}, key=lambda v: v or ""))
        result.append(Unit(test_id, outcome, verdict, selective, variants))
    return result


def _verdict_rank(verdict: str) -> int:
    return VERDICT_ORDER.index(verdict) if verdict in VERDICT_ORDER else len(VERDICT_ORDER)


@dataclass
class Tally:
    """Counts over units: flaky ones are excluded from recall."""

    runs: int = 0
    caught: int = 0
    missed: int = 0
    flaky_excluded: int = 0
    missed_by_verdict: Counter[str] = field(default_factory=Counter)

    @property
    def failures(self) -> int:
        return self.caught + self.missed + self.flaky_excluded

    @property
    def recall(self) -> float | None:
        counted = self.caught + self.missed
        return self.caught / counted if counted else None

    def add(self, unit: Unit) -> None:
        if unit.outcome == CAUGHT:
            self.caught += 1
        elif unit.outcome == MISSED:
            self.missed += 1
            self.missed_by_verdict[unit.verdict] += 1
        else:
            self.flaky_excluded += 1


@dataclass
class Time:
    """Measured test time of shadow runs (Go subtests left out: inside their parent's)."""

    total_ms: float = 0.0
    would_run_ms: float = 0.0

    @property
    def saved(self) -> float | None:
        return 1 - self.would_run_ms / self.total_ms if self.total_ms else None


# --- comparing ------------------------------------------------------------------------------


@dataclass(frozen=True)
class VariantComparison:
    variant: str | None
    run_id: int
    status: str  # SHADOW_COMPARED, SHADOW_COMMIT_MISMATCH or NO_SELECTION
    failing: int = 0
    caught: int = 0
    missed: int = 0
    flaky: int = 0
    tests_total: int = 0
    tests_would_run: int = 0
    ms_total: float = 0.0
    ms_would_run: float = 0.0


@dataclass(frozen=True)
class AttemptComparison:
    """A CI run attempt compared with its shadow selection(s)."""

    shadow: bool  # a selection was stored for the attempt
    selections: tuple[ShadowSelection, ...] = ()
    variants: tuple[VariantComparison, ...] = ()
    units: tuple[Unit, ...] = ()

    @property
    def counted(self) -> bool:
        return any(v.status == SHADOW_COMPARED for v in self.variants)

    @property
    def mode(self) -> str | None:
        """'selective' if every compared variant's selection was; None if nothing compared."""
        if not self.counted:
            return None
        by_variant = {s.variant: s for s in self.selections}
        modes = {
            (by_variant.get(v.variant) or by_variant[None]).mode
            for v in self.variants
            if v.status == SHADOW_COMPARED
        }
        return Mode.SELECTIVE.value if modes == {Mode.SELECTIVE.value} else Mode.FULL.value

    @property
    def tally(self) -> Tally:
        tally = Tally(runs=int(self.counted))
        for unit in self.units:
            tally.add(unit)
        return tally

    @property
    def time(self) -> Time:
        compared = [v for v in self.variants if v.status == SHADOW_COMPARED]
        return Time(sum(v.ms_total for v in compared), sum(v.ms_would_run for v in compared))


def compare_ci_run(
    session: Session, repo: str, ci_run_id: str, run_attempt: int, window_days: int = 90
) -> AttemptComparison | None:
    """Compare every recorded variant of a shadow CI run attempt with its stored selection,
    store the result (replacing an earlier comparison) and commit. None for an unknown repo;
    ``shadow=False`` when no selection was stored for the attempt. A server error marks the
    attempt's selections ``compare_error`` and re-raises."""
    repo_id = session.scalars(select(Repo.id).where(Repo.name == repo)).one_or_none()
    if repo_id is None:
        return None
    selections = tuple(
        session.scalars(
            select(ShadowSelection)
            .where(
                ShadowSelection.repo_id == repo_id,
                ShadowSelection.ci_run_id == ci_run_id,
                ShadowSelection.run_attempt == run_attempt,
            )
            .order_by(func.coalesce(ShadowSelection.variant, ""))
        )
    )
    if not selections:
        return AttemptComparison(shadow=False)
    ids = [s.id for s in selections]
    try:
        with session.begin_nested():
            result = _compare(session, repo, ci_run_id, run_attempt, window_days, selections)
    except Exception:
        logger.exception("shadow compare failed for %s run %s attempt %s",
                         repo, ci_run_id, run_attempt)  # fmt: skip
        session.execute(
            update(ShadowSelection)
            .where(ShadowSelection.id.in_(ids))
            .values(status=SHADOW_COMPARE_ERROR, compared_at=func.now())
        )
        session.commit()
        raise
    session.commit()
    return result


def _compare(
    session: Session,
    repo: str,
    ci_run_id: str,
    run_attempt: int,
    window_days: int,
    selections: Sequence[ShadowSelection],
) -> AttemptComparison:
    by_variant = {s.variant: s for s in selections}
    scopes = _scopes(session, selections)
    triaged = triage_ci_run(session, repo, ci_run_id, run_attempt, window_days) or []
    run_ids = [v.run_id for v in triaged]
    # A re-call replaces the earlier comparison (shadow_failures go with it).
    session.execute(delete(ShadowResult).where(ShadowResult.run_id.in_(run_ids)))
    durations = _durations(session, run_ids)
    failing_ids = sorted({
        r.test_id for v in triaged for r in (v.report.results if v.report else ())
    })  # fmt: skip
    keys = _test_keys(session, selections[0].repo_id, failing_ids)

    variants: list[VariantComparison] = []
    rows: list[FailureRow] = []
    used: dict[int, list[str]] = defaultdict(list)  # selection id -> its variants' statuses
    for v in triaged:
        selection = by_variant.get(v.variant) or by_variant.get(None)
        if selection is None:
            variants.append(VariantComparison(v.variant, v.run_id, NO_SELECTION))
            continue
        if selection.commit_sha != v.commit_sha:
            used[selection.id].append(SHADOW_COMMIT_MISMATCH)
            variants.append(VariantComparison(v.variant, v.run_id, SHADOW_COMMIT_MISMATCH))
            continue
        used[selection.id].append(SHADOW_COMPARED)
        would_run = WouldRun(scopes[selection.id])
        selective = selection.mode == Mode.SELECTIVE.value
        results = v.report.results if v.report else []
        variant_rows = [
            FailureRow(r.test_id, v.variant, would_run(r.test_id), r.verdict.value, selective)
            for r in results
        ]
        rows.extend(variant_rows)
        timed = [(t, ms) for t, ms in durations[v.run_id] if not is_go_subtest(t)]
        ran = [(t, ms) for t, ms in timed if would_run(t)]
        outcomes = Counter(
            FLAKY if row.verdict == FLAKY else CAUGHT if row.would_run else MISSED
            for row in variant_rows
        )
        compared = VariantComparison(
            variant=v.variant,
            run_id=v.run_id,
            status=SHADOW_COMPARED,
            failing=len(variant_rows),
            caught=outcomes[CAUGHT],
            missed=outcomes[MISSED],
            flaky=outcomes[FLAKY],
            tests_total=len(timed),
            tests_would_run=len(ran),
            ms_total=float(sum(ms for _, ms in timed)),
            ms_would_run=float(sum(ms for _, ms in ran)),
        )
        variants.append(compared)
        session.add(ShadowResult(
            run_id=v.run_id, selection_id=selection.id, failing=compared.failing,
            caught=compared.caught, missed=compared.missed, flaky=compared.flaky,
            tests_total=compared.tests_total, tests_would_run=compared.tests_would_run,
            ms_total=compared.ms_total, ms_would_run=compared.ms_would_run,
        ))  # fmt: skip
        session.flush()
        session.add_all(
            ShadowFailure(
                run_id=v.run_id,
                test_key=keys[r.test_id],
                would_run=row.would_run,
                verdict=r.verdict.value,
                confidence=r.confidence.value,
            )
            for r, row in zip(results, variant_rows, strict=True)
        )

    for selection in selections:
        statuses = used.get(selection.id, [])
        selection.status = (
            SHADOW_COMPARED if SHADOW_COMPARED in statuses
            else SHADOW_COMMIT_MISMATCH if statuses
            else SHADOW_NO_UPLOAD
        )  # fmt: skip
        selection.compared_at = datetime.now(UTC)
    session.flush()
    return AttemptComparison(True, tuple(selections), tuple(variants), tuple(units(rows)))


def _scopes(session: Session, selections: Sequence[ShadowSelection]) -> dict[int, SelectionScope]:
    keys = {key for s in selections for key in s.selected_keys}
    names: dict[int, str] = {
        key: test_id
        for key, test_id in session.execute(
            text("SELECT id, test_id FROM tests WHERE id = ANY(:keys)"), {"keys": sorted(keys)}
        )
    }
    return {
        s.id: SelectionScope(
            full=s.mode == Mode.FULL.value,
            test_ids=frozenset(names[key] for key in s.selected_keys),
            go_test_ids=frozenset(names[key] for key in s.go_selected_keys),
            go_packages=frozenset(s.go_packages),
            python_files=frozenset(s.python_files),
        )
        for s in selections
    }


def _durations(session: Session, run_ids: Sequence[int]) -> dict[int, list[tuple[str, int]]]:
    """Each run's tests with their final attempt's duration (0 if not reported)."""
    durations: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for run_id, test_id, ms in session.execute(
        text(
            """
            SELECT DISTINCT ON (tr.run_id, tr.test_key) tr.run_id, t.test_id, tr.duration_ms
            FROM test_results tr JOIN tests t ON t.id = tr.test_key
            WHERE tr.run_id = ANY(:ids)
            ORDER BY tr.run_id, tr.test_key, tr.attempt DESC
            """
        ),
        {"ids": list(run_ids)},
    ):
        durations[run_id].append((test_id, ms or 0))
    return durations


def _test_keys(session: Session, repo_id: int, test_ids: Sequence[str]) -> dict[str, int]:
    return {
        test_id: key
        for test_id, key in session.execute(
            text("SELECT test_id, id FROM tests WHERE repo_id = :repo_id AND test_id = ANY(:ids)"),
            {"repo_id": repo_id, "ids": list(test_ids)},
        )
    }


# --- recall ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Miss:
    test_id: str
    ci_run_id: str
    run_attempt: int
    commit_sha: str
    variants: tuple[str | None, ...]
    verdict: str
    selection_mode: str
    at: datetime


@dataclass
class Recall:
    since: datetime
    all: Tally = field(default_factory=Tally)
    selective: Tally = field(default_factory=Tally)
    full_runs: int = 0
    not_counted: Counter[str] = field(default_factory=Counter)
    in_progress: int = 0
    time: Time = field(default_factory=Time)
    misses: list[Miss] = field(default_factory=list)


def recall(session: Session, repo: str, days: int, now: datetime | None = None) -> Recall | None:
    """Recall over the shadow selections stored in the last ``days`` days. None for an
    unknown repo. A run counts as selective only if every compared variant's selection was:
    full selections (fallbacks, unknown diffs) catch everything and never enter
    ``selective``."""
    repo_id = session.scalars(select(Repo.id).where(Repo.name == repo)).one_or_none()
    if repo_id is None:
        return None
    now = now or datetime.now(UTC)
    report = Recall(since=now - timedelta(days=days))
    params = {"repo_id": repo_id, "since": report.since}

    for status, created_at in session.execute(
        text(
            """
            SELECT status, created_at FROM shadow_selections
            WHERE repo_id = :repo_id AND created_at >= :since AND status <> 'compared'
            """
        ),
        params,
    ):
        if status != SHADOW_PENDING:
            report.not_counted[status] += 1
        elif now - created_at < IN_PROGRESS:
            report.in_progress += 1
        else:
            report.not_counted[CANCELLED] += 1

    # Compared variants, by CI run attempt.
    attempts: dict[tuple[str, int], list[tuple[str, float, float, datetime, str]]] = defaultdict(
        list
    )
    for ci_run_id, run_attempt, mode, ms_total, ms_would_run, at, commit in session.execute(
        text(
            """
            SELECT s.ci_run_id, s.run_attempt, s.mode, sr.ms_total, sr.ms_would_run,
                   COALESCE(r.started_at, r.created_at), r.commit_sha
            FROM shadow_selections s
            JOIN shadow_results sr ON sr.selection_id = s.id
            JOIN runs r ON r.id = sr.run_id
            WHERE s.repo_id = :repo_id AND s.created_at >= :since
            """
        ),
        params,
    ):
        attempts[(ci_run_id, run_attempt)].append((mode, ms_total, ms_would_run, at, commit))
    failures: dict[tuple[str, int], list[FailureRow]] = defaultdict(list)
    for ci_run_id, run_attempt, mode, variant, test_id, would_run, verdict in session.execute(
        text(
            """
            SELECT s.ci_run_id, s.run_attempt, s.mode, r.variant, t.test_id, f.would_run,
                   f.verdict
            FROM shadow_selections s
            JOIN shadow_results sr ON sr.selection_id = s.id
            JOIN runs r ON r.id = sr.run_id
            JOIN shadow_failures f ON f.run_id = sr.run_id
            JOIN tests t ON t.id = f.test_key
            WHERE s.repo_id = :repo_id AND s.created_at >= :since
            """
        ),
        params,
    ):
        failures[(ci_run_id, run_attempt)].append(
            FailureRow(test_id, variant, would_run, verdict, mode == Mode.SELECTIVE.value)
        )

    for attempt, variants in attempts.items():
        selective = all(mode == Mode.SELECTIVE.value for mode, *_ in variants)
        at = max(row[3] for row in variants)
        commit = variants[0][4]
        report.all.runs += 1
        if selective:
            report.selective.runs += 1
        else:
            report.full_runs += 1
        report.time.total_ms += sum(row[1] for row in variants)
        report.time.would_run_ms += sum(row[2] for row in variants)
        for unit in units(failures[attempt]):
            report.all.add(unit)
            if selective:
                report.selective.add(unit)
            if unit.outcome == MISSED:
                report.misses.append(Miss(
                    unit.test_id, attempt[0], attempt[1], commit, unit.variants, unit.verdict,
                    Mode.SELECTIVE.value if unit.selective else Mode.FULL.value, at,
                ))  # fmt: skip
    report.misses.sort(key=lambda m: (m.at, m.test_id), reverse=True)
    return report
