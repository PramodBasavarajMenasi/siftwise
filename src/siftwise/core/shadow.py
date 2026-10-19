"""Shadow mode: live recall.

A shadow run runs the full suite while ``POST /select`` stores what siftwise would have
selected (``store_selection``). Once the run is recorded, ``compare_ci_run`` checks every
failure against that selection with the selector's own ``WouldRun`` and triage's verdict:
caught, missed, or flaky (counted apart, never a miss). ``recall`` sums it up over days.

Units: a test in one CI run attempt. It's flaky only if every variant it failed in says flaky;
otherwise it's missed if any non-flaky failing variant's selection wouldn't have run it.

Every miss gets a cause (``miss_cause``), from evidence the server has: triage's history and
links for the test, and the run's recorded change. When the evidence doesn't say, ``unknown``.
"""

import logging
import posixpath
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

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
from siftwise.core.selector import (
    JS_EXTENSIONS,
    Mode,
    Selection,
    SelectionScope,
    WouldRun,
    is_generated,
    is_go_subtest,
    is_ignorable,
    locate_test,
)
from siftwise.core.triage import SAME_COMMIT, TriageResult, Verdict, triage_ci_run

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
# Why a shadow selection wasn't stored (``SelectResponse.shadow_reason``).
UNKNOWN_REPO = "unknown_repo"
INCOMPLETE_SELECTION = "incomplete_selection"  # a selected test has no key: would under-count
COMMIT_CONFLICT = "commit_conflict"  # the attempt and variant hold another commit's selection
STORAGE_ERROR = "storage_error"  # the database failed (set by the /select route)

# Why a failure was missed (``shadow_failures.cause``), most specific first: a unit missed in
# several variants takes the first of its variants' causes in this order.
NEW_TEST = "new_test"  # no result before the run
FLAKY_HISTORY = "flaky_history"  # flaky before, but not called flaky now
CONFIG_GAP = "config_gap"  # a non-code file in the test's own directory changed
NO_LINK = "no_link"  # the change is known and nothing in it links to the test
UNKNOWN_CAUSE = "unknown"
CAUSES = (NEW_TEST, FLAKY_HISTORY, CONFIG_GAP, NO_LINK, UNKNOWN_CAUSE)
UNCLASSIFIED = "unclassified"  # compared before causes existed (migration 0009): re-compare
LINKED_NOT_SELECTED = (
    "linked to the change but not selected (selector didn't know the test at select time?)"
)
# What an import graph or path mapping can follow; any other changed file is "non-code".
CODE_EXTENSIONS = frozenset({".py", ".go"}) | JS_EXTENSIONS
CHANGED_SHOWN = 5  # changed files listed as a no_link miss's context


@dataclass(frozen=True)
class ShadowTarget:
    """The CI run attempt (and variant) a shadow selection is stored for."""

    ci_run_id: str
    run_attempt: int
    variant: str | None
    commit_sha: str


def store_selection(
    session: Session, repo: str, target: ShadowTarget, selection: Selection
) -> str | None:
    """Store ``selection`` for a shadow run. None if it's stored (by this call, or already for
    the same commit by another leg), else why not: ``UNKNOWN_REPO``, ``INCOMPLETE_SELECTION``
    or ``COMMIT_CONFLICT``. Commits."""
    repo_id = session.scalars(select(Repo.id).where(Repo.name == repo)).one_or_none()
    if repo_id is None:
        return UNKNOWN_REPO
    scope = SelectionScope.of(selection)
    keys = _test_keys(session, repo_id, sorted(scope.test_ids))
    # Selected tests come from history, so all have keys; a missing one can't be stored, and
    # the comparison would call it not selected: refuse rather than under-count.
    if len(keys) < len(scope.test_ids):
        logger.warning("shadow selection not stored: %d selected tests unknown",
                       len(scope.test_ids) - len(keys))  # fmt: skip
        return INCOMPLETE_SELECTION
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
        return None
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
    return None if stored == target.commit_sha else COMMIT_CONFLICT


@dataclass(frozen=True)
class MissCause:
    cause: str  # one of CAUSES
    detail: str
    suggestion: str | None = None  # a [[depends]] rule to consider (config_gap only)


# --- counting ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class FailureRow:
    """A failing test in one compared variant."""

    test_id: str
    variant: str | None
    would_run: bool
    verdict: str
    selective: bool  # the variant's selection was selective
    cause: MissCause | None = None  # a miss's cause; None if not a miss or not classified


@dataclass(frozen=True)
class Unit:
    """A failing test in one CI run attempt, over its failing variants."""

    test_id: str
    outcome: str  # CAUGHT, MISSED or FLAKY
    verdict: str  # a miss: the worst verdict of its missed variants; else the worst overall
    selective: bool  # every failing variant's selection was selective
    variants: tuple[str | None, ...]  # a miss: the variants that missed it; else all failing
    cause: MissCause | None = None  # a miss: the most specific of its missed variants' causes


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
        cause = None
        if outcome == MISSED:
            causes = [row.cause for row in missed if row.cause is not None]
            cause = min(causes, key=lambda c: CAUSES.index(c.cause), default=None)
        result.append(Unit(test_id, outcome, verdict, selective, variants, cause))
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
    missed_by_cause: Counter[str] = field(default_factory=Counter)

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
            self.missed_by_cause[unit.cause.cause if unit.cause else UNCLASSIFIED] += 1
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


# --- miss causes ----------------------------------------------------------------------------


@dataclass(frozen=True)
class RunChange:
    """What a recorded run's upload stored about its change."""

    changed_files: tuple[str, ...] = ()
    links_known: bool = False  # the diff was known and runs.affected stored
    go_module: str | None = None
    generated: tuple[str, ...] = ()


def miss_cause(result: TriageResult, change: RunChange, file_path: str | None = None) -> MissCause:
    """Why the selection missed this failure, from evidence only; ``unknown`` when it doesn't
    say. First that applies: a test new in this run; a test with flaky history (but not called
    flaky now); unknown change links, or links the selection didn't act on (unknown); a
    non-code file changed in the test's own directory (config gap, with a ``[[depends]]`` rule
    to consider); otherwise nothing in the known change links to it (no link)."""
    history = result.history
    if not history.seen:
        return MissCause(NEW_TEST, "no result for this test before this run")
    if history.flaky:
        kinds = Counter(event.kind for event in history.flaky)
        same, other = kinds[SAME_COMMIT], len(history.flaky) - kinds[SAME_COMMIT]
        return MissCause(FLAKY_HISTORY, f"flaky {len(history.flaky)} time(s) before ({same} "
                         f"same-commit, {other} unrelated fail-then-pass), but not called flaky "
                         "now")  # fmt: skip
    if not change.links_known:
        return MissCause(UNKNOWN_CAUSE, "the run's change links weren't recorded (unknown diff, "
                         "or an import graph failed or was skipped)")  # fmt: skip
    if result.links:
        return MissCause(UNKNOWN_CAUSE, LINKED_NOT_SELECTED)
    location = locate_test(result.test_id, file_path or history.file_path, change.go_module)
    nearby = _non_code_in(location[0], change) if location and location[0] else []
    if location and nearby:
        shown = _shown(nearby)
        suggestion = None
        if result.verdict is not Verdict.PRE_EXISTING:  # failing on main anyway: no rule helps
            dirs = sorted({posixpath.dirname(path) for path in nearby})
            suggestion = depends_rule(location[1], [f"{d}/**" for d in dirs])
        return MissCause(CONFIG_GAP, f"non-code file(s) changed in the test's directory, which "
                         f"no import graph follows: {shown}", suggestion)  # fmt: skip
    relevant = [p for p in change.changed_files if not is_ignorable(p, change.generated)]
    context = f"; changed: {_shown(relevant)}" if relevant else ""
    return MissCause(NO_LINK, f"nothing in the change links to this test{context}")


def _non_code_in(directory: str, change: RunChange) -> list[str]:
    return sorted(
        path
        for path in change.changed_files
        if path.startswith(directory + "/")
        and posixpath.splitext(path)[1] not in CODE_EXTENSIONS
        and not is_generated(path, change.generated)
    )


def _shown(paths: Sequence[str]) -> str:
    shown = ", ".join(paths[:CHANGED_SHOWN])
    more = len(paths) - CHANGED_SHOWN
    return f"{shown} and {more} more" if more > 0 else shown


def depends_rule(tests: str, on: Sequence[str]) -> str:
    """A ``.siftwise.toml`` ``[[depends]]`` rule, as TOML (basic strings, escaped)."""
    quoted = ", ".join(_toml_string(glob) for glob in on)
    return f"[[depends]]\ntests = {_toml_string(tests)}\non = [{quoted}]"


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


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
    keys, paths = _failing_tests(session, selections[0].repo_id, failing_ids)
    changes = _run_changes(session, run_ids)

    variants: list[VariantComparison] = []
    rows: list[FailureRow] = []
    # Inserted together at the end (results first: failures refer to them), so the number of
    # statements doesn't grow with the number of variants.
    stored_results: list[ShadowResult] = []
    stored_failures: list[ShadowFailure] = []
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
        change = changes.get(v.run_id, RunChange())
        change = RunChange(change.changed_files, v.links_known, change.go_module,
                           change.generated)  # fmt: skip
        variant_rows = []
        for r in results:
            runs_it = would_run(r.test_id)
            missed = not runs_it and r.verdict is not Verdict.FLAKY
            cause = miss_cause(r, change, paths.get(r.test_id)) if missed else None
            variant_rows.append(FailureRow(r.test_id, v.variant, runs_it, r.verdict.value,
                                           selective, cause))  # fmt: skip
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
        stored_results.append(ShadowResult(
            run_id=v.run_id, selection_id=selection.id, failing=compared.failing,
            caught=compared.caught, missed=compared.missed, flaky=compared.flaky,
            tests_total=compared.tests_total, tests_would_run=compared.tests_would_run,
            ms_total=compared.ms_total, ms_would_run=compared.ms_would_run,
        ))  # fmt: skip
        stored_failures.extend(
            ShadowFailure(
                run_id=v.run_id,
                test_key=keys[r.test_id],
                would_run=row.would_run,
                verdict=r.verdict.value,
                confidence=r.confidence.value,
                cause=row.cause.cause if row.cause else None,
                cause_detail=row.cause.detail if row.cause else None,
                suggestion=row.cause.suggestion if row.cause else None,
            )
            for r, row in zip(results, variant_rows, strict=True)
        )
    session.add_all(stored_results)
    session.flush()
    session.add_all(stored_failures)

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


def _failing_tests(
    session: Session, repo_id: int, test_ids: Sequence[str]
) -> tuple[dict[str, int], dict[str, str]]:
    """The failing tests' keys, and their reported file paths (Jest needs one to be located)."""
    keys: dict[str, int] = {}
    paths: dict[str, str] = {}
    for test_id, key, path in session.execute(
        text(
            "SELECT test_id, id, file_path FROM tests "
            "WHERE repo_id = :repo_id AND test_id = ANY(:ids)"
        ),
        {"repo_id": repo_id, "ids": list(test_ids)},
    ):
        keys[test_id] = key
        if path:
            paths[test_id] = path
    return keys, paths


def _run_changes(session: Session, run_ids: Sequence[int]) -> dict[int, RunChange]:
    """Each run's stored change: changed files, and from ``runs.affected`` the Go module and
    generated globs. ``links_known`` is filled in from triage."""
    files: dict[int, list[str]] = defaultdict(list)
    for run_id, path in session.execute(
        text("SELECT run_id, path FROM changed_files WHERE run_id = ANY(:ids) ORDER BY path"),
        {"ids": list(run_ids)},
    ):
        files[run_id].append(path)
    changes = {}
    for run_id, affected in session.execute(
        text("SELECT id, affected FROM runs WHERE id = ANY(:ids)"), {"ids": list(run_ids)}
    ):
        graph: Mapping[str, Any] = affected or {}
        changes[run_id] = RunChange(
            tuple(files[run_id]),
            go_module=graph.get("go_module"),
            generated=tuple(graph.get("generated") or ()),
        )
    return changes


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
    cause: MissCause | None = None


@dataclass
class Suggestion:
    """A ``[[depends]]`` rule that would have caught config-gap misses (never applied)."""

    rule: str
    misses: int = 0
    test_ids: set[str] = field(default_factory=set)


@dataclass
class Period:
    """One week of shadow runs (by selection time): Monday 00:00 UTC to the next Monday."""

    start: datetime
    end: datetime
    all: Tally = field(default_factory=Tally)
    selective: Tally = field(default_factory=Tally)
    full_runs: int = 0
    time: Time = field(default_factory=Time)


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
    suggestions: list[Suggestion] = field(default_factory=list)
    # Misses linked to the change but not selected: possibly a selector bug.
    linked_not_selected: int = 0
    periods: list[Period] | None = None  # by week, when asked for


def week_start(at: datetime) -> datetime:
    """Monday 00:00 UTC of ``at``'s week."""
    day = at.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


def recall(
    session: Session,
    repo: str,
    days: int,
    now: datetime | None = None,
    *,
    by_week: bool = False,
) -> Recall | None:
    """Recall over the shadow selections stored in the last ``days`` days. None for an
    unknown repo. A run counts as selective only if every compared variant's selection was:
    full selections (fallbacks, unknown diffs) catch everything and never enter
    ``selective``. ``by_week``: also per week (by selection time), empty weeks included."""
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
    attempts: dict[tuple[str, int], list[tuple[str, float, float, datetime, str, datetime]]] = (
        defaultdict(list)
    )
    for (
        ci_run_id,
        run_attempt,
        mode,
        ms_total,
        ms_would_run,
        at,
        commit,
        selected_at,
    ) in session.execute(
        text(
            """
            SELECT s.ci_run_id, s.run_attempt, s.mode, sr.ms_total, sr.ms_would_run,
                   COALESCE(r.started_at, r.created_at), r.commit_sha, s.created_at
            FROM shadow_selections s
            JOIN shadow_results sr ON sr.selection_id = s.id
            JOIN runs r ON r.id = sr.run_id
            WHERE s.repo_id = :repo_id AND s.created_at >= :since
            """
        ),
        params,
    ):
        attempts[(ci_run_id, run_attempt)].append(
            (mode, ms_total, ms_would_run, at, commit, selected_at)
        )
    failures: dict[tuple[str, int], list[FailureRow]] = defaultdict(list)
    for (
        ci_run_id,
        run_attempt,
        mode,
        variant,
        test_id,
        would_run,
        verdict,
        cause,
        detail,
        suggestion,
    ) in session.execute(
        text(
            """
            SELECT s.ci_run_id, s.run_attempt, s.mode, r.variant, t.test_id, f.would_run,
                   f.verdict, f.cause, f.cause_detail, f.suggestion
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
        failures[(ci_run_id, run_attempt)].append(FailureRow(
            test_id, variant, would_run, verdict, mode == Mode.SELECTIVE.value,
            MissCause(cause, detail or "", suggestion) if cause else None,
        ))  # fmt: skip

    periods: dict[datetime, Period] = {}
    if by_week:
        start = week_start(report.since)
        while start <= now:
            periods[start] = Period(start, start + timedelta(days=7))
            start += timedelta(days=7)
        report.periods = list(periods.values())
    suggestions: dict[str, Suggestion] = {}
    for attempt, variants in attempts.items():
        selective = all(mode == Mode.SELECTIVE.value for mode, *_ in variants)
        at = max(row[3] for row in variants)
        commit = variants[0][4]
        targets: list[Recall | Period] = [report]
        period = periods.get(week_start(min(row[5] for row in variants)))
        if period is not None:
            targets.append(period)
        for target in targets:
            target.all.runs += 1
            if selective:
                target.selective.runs += 1
            else:
                target.full_runs += 1
            target.time.total_ms += sum(row[1] for row in variants)
            target.time.would_run_ms += sum(row[2] for row in variants)
        for unit in units(failures[attempt]):
            for target in targets:
                target.all.add(unit)
                if selective:
                    target.selective.add(unit)
            if unit.outcome != MISSED:
                continue
            report.misses.append(Miss(
                unit.test_id, attempt[0], attempt[1], commit, unit.variants, unit.verdict,
                Mode.SELECTIVE.value if unit.selective else Mode.FULL.value, at, unit.cause,
            ))  # fmt: skip
            if unit.cause and unit.cause.detail == LINKED_NOT_SELECTED:
                report.linked_not_selected += 1
            if unit.cause and unit.cause.suggestion:
                rule = suggestions.setdefault(unit.cause.suggestion,
                                              Suggestion(unit.cause.suggestion))  # fmt: skip
                rule.misses += 1
                rule.test_ids.add(unit.test_id)
    report.misses.sort(key=lambda m: (m.at, m.test_id), reverse=True)
    report.suggestions = sorted(suggestions.values(), key=lambda s: (-s.misses, s.rule))
    return report
