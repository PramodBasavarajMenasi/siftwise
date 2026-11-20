"""CI selections, run outcomes and shadow mode's live recall.

``POST /select`` from CI stores the selection it returned (``store_selection``): every CI
selection since 0.4, a shadow run's (one that runs the full suite) since 0.2. Once the run
attempt is recorded, ``compare_ci_run`` (``POST /repos/{repo}/outcome``, or the older
``/shadow``) triages it once and stores each leg's outcome: its failures by verdict, whether
the recorded selection runs each (the selector's own ``WouldRun``) and its risk rank. For a
shadow run, each failure is also caught, missed, or flaky (counted apart, never a miss), and
``recall`` sums those up over days.

Units: a test in one CI run attempt. It's flaky only if every variant it failed in says flaky;
otherwise it's missed if any non-flaky failing variant's selection wouldn't have run it.

Every miss gets a cause (``miss_cause``), from evidence the server has: triage's history and
links for the test, and the run's recorded change. When the evidence doesn't say, ``unknown``.

A budget-limited selection (a time budget left selected tests out) is stored as what would
have run, plus the selection the budget cut (``ci_selections.budget``): a miss that
selection would have run is a ``budget`` miss, counted apart, so a budget never hides misses
and never passes its own misses off as the selector's.
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
    AttemptOutcome,
    CiSelection,
    Repo,
    RunFailure,
    RunOutcome,
)
from siftwise.core.selector import (
    JS_EXTENSIONS,
    Budget,
    Mode,
    SelectedTest,
    Selection,
    SelectionScope,
    WouldRun,
    format_duration,
    is_generated,
    is_go_subtest,
    is_ignorable,
    locate_test,
    measured_time,
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
READ_ONLY_TOKEN = "read_only_token"  # a CI selection with a read token: selected, not stored
TOP_RISK = 5  # highest-risk tests stored with a selection, with their reasons
REASON_CHARS = 200  # each stored reason cut to this

# Why a failure was missed (``run_failures.cause``), most specific first: a unit missed in
# several variants takes the first of its variants' causes in this order.
BUDGET = "budget"  # the selection had it, a time budget left it out
NEW_TEST = "new_test"  # no result before the run
FLAKY_HISTORY = "flaky_history"  # flaky before, but not called flaky now
FIXTURE = "fixture"  # the selection treated changed fixture files; the test doesn't name them
CONFIG_GAP = "config_gap"  # a non-code file in the test's own directory changed
NO_LINK = "no_link"  # the change is known and nothing in it links to the test
UNKNOWN_CAUSE = "unknown"
CAUSES = (BUDGET, NEW_TEST, FLAKY_HISTORY, FIXTURE, CONFIG_GAP, NO_LINK, UNKNOWN_CAUSE)
UNCLASSIFIED = "unclassified"  # compared before causes existed (migration 0009): re-compare
LINKED_NOT_SELECTED = (
    "linked to the change but not selected (selector didn't know the test at select time?)"
)
# What an import graph or path mapping can follow; any other changed file is "non-code".
CODE_EXTENSIONS = frozenset({".py", ".go"}) | JS_EXTENSIONS
CHANGED_SHOWN = 5  # changed files listed as a no_link miss's context


@dataclass(frozen=True)
class ShadowTarget:
    """The CI run attempt (and variant) a selection is stored for."""

    ci_run_id: str
    run_attempt: int
    variant: str | None
    commit_sha: str


@dataclass(frozen=True)
class CiRun:
    """What a CI /select call says about its run besides the target (``schemas.CiIn``)."""

    provider: str
    branch: str
    is_main: bool
    pr_number: int | None = None
    event: str | None = None
    client_version: str | None = None
    changed_count: int | None = None


def _by_rank(test: SelectedTest) -> tuple[bool, int, str]:
    return (test.rank is None, test.rank or 0, test.test_id)


def store_selection(
    session: Session,
    repo: str,
    target: ShadowTarget,
    selection: Selection,
    *,
    shadow: bool = True,
    ci: CiRun | None = None,
) -> str | None:
    """Store ``selection`` for a CI run attempt (``shadow``: a shadow run's, which runs the
    full suite instead). None if it's stored (by this call, or already for the same commit
    by another leg), else why not: ``UNKNOWN_REPO``, ``INCOMPLETE_SELECTION`` or
    ``COMMIT_CONFLICT``. The selected tests are stored in risk-rank order. Commits."""
    repo_id = session.scalars(select(Repo.id).where(Repo.name == repo)).one_or_none()
    if repo_id is None:
        return UNKNOWN_REPO
    scope = SelectionScope.of(selection)
    budget = selection.budget
    # The selection a budget cut: only the shadow compare reads it (budget misses).
    before = budget.before if shadow and budget is not None and budget.limited else None
    named = scope.test_ids | (before.test_ids if before else frozenset())
    keys = _test_keys(session, repo_id, sorted(named))
    # Selected tests come from history, so all have keys; a missing one can't be stored, and
    # the comparison would call it not selected: refuse rather than under-count.
    if len(keys) < len(named):
        logger.warning("selection not stored: %d selected tests unknown",
                       len(named) - len(keys))  # fmt: skip
        return INCOMPLETE_SELECTION
    in_order = sorted(selection.tests, key=_by_rank)
    top = [
        {
            "key": keys[t.test_id],
            "reasons": [reason[:REASON_CHARS] for reason in t.reasons[:3]],
            "why": t.rank_reason[:REASON_CHARS],
        }
        for t in in_order[:TOP_RISK]
    ]
    inserted = session.execute(
        pg_insert(CiSelection)
        .values(
            repo_id=repo_id,
            ci_run_id=target.ci_run_id,
            run_attempt=target.run_attempt,
            variant=target.variant,
            commit_sha=target.commit_sha,
            shadow=shadow,
            mode=selection.mode.value,
            reason=selection.reason,
            reason_code=selection.reason_code.value if selection.reason_code else None,
            reason_items=list(selection.reason_items),
            selected_keys=[keys[t.test_id] for t in in_order],
            ranked=True,
            go_selected_keys=sorted(keys[test_id] for test_id in scope.go_test_ids),
            go_packages=sorted(scope.go_packages),
            python_files=sorted(scope.python_files),
            java_classes=sorted(scope.java_classes),
            fixture_files=list(selection.fixture_files),
            selected_count=selection.selected_count,
            total_known=selection.total_known,
            estimated_ms_total=selection.estimated_ms_total,
            estimated_ms_would_run=selection.estimated_ms_would_run,
            budget=None if budget is None else _budget_json(budget, before, keys),
            command_order=selection.order.value,
            top_risk=top or None,
            provider=ci.provider if ci else None,
            branch=ci.branch if ci else None,
            is_main=ci.is_main if ci else None,
            pr_number=ci.pr_number if ci else None,
            event=ci.event if ci else None,
            client_version=ci.client_version if ci else None,
            changed_count=ci.changed_count if ci else None,
        )
        .on_conflict_do_nothing(
            # SHADOW_UNIQUE_INDEX: one selection per attempt and variant. The '' is written
            # into the SQL, never bound: once psycopg auto-prepares the statement and Postgres
            # picks a generic plan, a bound '' no longer matches the index expression and
            # every store fails (from the 11th on one connection).
            index_elements=[
                CiSelection.repo_id,
                CiSelection.ci_run_id,
                CiSelection.run_attempt,
                text("coalesce(variant, '')"),
            ]
        )
        .returning(CiSelection.id)
    ).scalar_one_or_none()
    session.commit()
    if inserted is not None:
        return None
    # Another leg stored it first (selections of one commit are the same), or a different
    # commit claims this attempt: that one isn't ours to compare.
    stored = session.scalars(
        select(CiSelection.commit_sha).where(
            CiSelection.repo_id == repo_id,
            CiSelection.ci_run_id == target.ci_run_id,
            CiSelection.run_attempt == target.run_attempt,
            func.coalesce(CiSelection.variant, "") == (target.variant or ""),
        )
    ).one()
    return None if stored == target.commit_sha else COMMIT_CONFLICT


def _budget_json(
    budget: Budget, before: SelectionScope | None, keys: Mapping[str, int]
) -> dict[str, Any]:
    """A stored budget: what it did, and for a limited shadow selection the selection it cut."""
    stored: dict[str, Any] = {
        "seconds": budget.budget_ms / 1000,
        "applied": budget.applied,
        "limited": budget.limited,
        "full_suite": budget.full_suite,
        "note": budget.note,
        "estimated_ms": budget.estimated_ms,
        "left_out": budget.left_out,
        "left_out_ms": budget.left_out_ms,
        "left_out_risk_share": budget.left_out_risk_share,
        "unknown_ms": budget.unknown_ms,
    }
    if before is not None:
        stored["before"] = {
            "full": before.full,
            "keys": sorted(keys[test_id] for test_id in before.test_ids),
            "go_keys": sorted(keys[test_id] for test_id in before.go_test_ids),
            "go_packages": sorted(before.go_packages),
            "python_files": sorted(before.python_files),
            "java_classes": sorted(before.java_classes),
        }
    return stored


def budget_limited(selection: CiSelection) -> bool:
    """A time budget left selected tests out of this selection."""
    return bool((selection.budget or {}).get("limited"))


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
    budget_runs: int = 0  # runs whose selection a time budget cut
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

    @property
    def missed_by_budget(self) -> int:
        """Misses a time budget left out (the selection had them)."""
        return self.missed_by_cause[BUDGET]

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


def miss_cause(
    result: TriageResult,
    change: RunChange,
    file_path: str | None = None,
    fixtures: Sequence[str] = (),
) -> MissCause:
    """Why the selection missed this failure, from evidence only; ``unknown`` when it doesn't
    say. First that applies: a test new in this run; a test with flaky history (but not called
    flaky now); the selection treated changed ``fixtures`` as test data and the test links to
    nothing but them (fixture: it uses a fixture without naming it, with a ``[[depends]]`` rule
    to consider); unknown change links, or links the selection didn't act on (unknown); a
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
    if fixtures and all(link.changed in fixtures for link in result.links):
        location = locate_test(result.test_id, file_path or history.file_path, change.go_module)
        suggestion = None
        if location and result.verdict is not Verdict.PRE_EXISTING:
            dirs = sorted({posixpath.dirname(path) or "." for path in fixtures})
            suggestion = depends_rule(location[1], [f"{d}/**" for d in dirs])
        return MissCause(FIXTURE, f"fixture file(s) changed and this test doesn't name their "
                         f"fixture (it wasn't among the tests referencing it): "
                         f"{_shown(sorted(fixtures))}", suggestion)  # fmt: skip
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
class LegOutcome:
    """One recorded leg's stored outcome (``run_outcomes``)."""

    variant: str | None
    run_id: int
    selection: str | None  # the leg's selection: its mode, "shadow", or None (none recorded)
    failing: int = 0
    new_regressions: int = 0
    pre_existing: int = 0
    flaky: int = 0
    unknown: int = 0
    failures_selected: int | None = None  # None: no selection
    first_failure_rank: int | None = None
    measured_ms: float | None = None


@dataclass(frozen=True)
class AttemptComparison:
    """A CI run attempt's outcome: every leg's (``legs``), and the comparison with its shadow
    selection(s) (the rest: empty, ``shadow`` false, when it wasn't a shadow run)."""

    shadow: bool  # a shadow selection was stored for the attempt
    selections: tuple[CiSelection, ...] = ()  # the shadow ones
    variants: tuple[VariantComparison, ...] = ()
    units: tuple[Unit, ...] = ()
    legs: tuple[LegOutcome, ...] = ()

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
    def budget_limited(self) -> bool:
        """A compared variant's selection was cut by a time budget."""
        by_variant = {s.variant: s for s in self.selections}
        return any(
            budget_limited(by_variant.get(v.variant) or by_variant[None])
            for v in self.variants
            if v.status == SHADOW_COMPARED
        )

    @property
    def tally(self) -> Tally:
        tally = Tally(runs=int(self.counted), budget_runs=int(self.budget_limited))
        for unit in self.units:
            tally.add(unit)
        return tally

    @property
    def time(self) -> Time:
        compared = [v for v in self.variants if v.status == SHADOW_COMPARED]
        return Time(sum(v.ms_total for v in compared), sum(v.ms_would_run for v in compared))


def compare_ci_run(
    session: Session,
    repo: str,
    ci_run_id: str,
    run_attempt: int,
    window_days: int = 90,
    *,
    fail_on: Sequence[str] | None = None,
) -> AttemptComparison | None:
    """Triage every recorded leg of a CI run attempt once and store each leg's outcome (its
    failures, what the recorded selection did with them), compare a shadow run with its
    selection, store the attempt's ``fail_on`` verdicts, all replacing an earlier call's, and
    commit. None for an unknown repo. A server error marks the attempt's selections
    ``compare_error`` and re-raises."""
    repo_id = session.scalars(select(Repo.id).where(Repo.name == repo)).one_or_none()
    if repo_id is None:
        return None
    selections = tuple(
        session.scalars(
            select(CiSelection)
            .where(
                CiSelection.repo_id == repo_id,
                CiSelection.ci_run_id == ci_run_id,
                CiSelection.run_attempt == run_attempt,
            )
            .order_by(func.coalesce(CiSelection.variant, ""))
        )
    )
    ids = [s.id for s in selections]
    try:
        with session.begin_nested():
            result = _compare(session, repo_id, repo, ci_run_id, run_attempt, window_days,
                              selections, fail_on)  # fmt: skip
    except Exception:
        logger.exception("run outcome failed for %s run %s attempt %s",
                         repo, ci_run_id, run_attempt)  # fmt: skip
        if ids:
            session.execute(
                update(CiSelection)
                .where(CiSelection.id.in_(ids))
                .values(status=SHADOW_COMPARE_ERROR, compared_at=func.now())
            )
            session.commit()
        raise
    session.commit()
    return result


def _compare(
    session: Session,
    repo_id: int,
    repo: str,
    ci_run_id: str,
    run_attempt: int,
    window_days: int,
    selections: Sequence[CiSelection],
    fail_on: Sequence[str] | None,
) -> AttemptComparison:
    by_variant = {s.variant: s for s in selections}
    shadow_ids = {s.id for s in selections if s.shadow}
    scopes, befores, names = _scopes(session, selections)
    triaged = triage_ci_run(session, repo, ci_run_id, run_attempt, window_days) or []
    run_ids = [v.run_id for v in triaged]
    # A re-call replaces the earlier outcome (run_failures go with it).
    session.execute(delete(RunOutcome).where(RunOutcome.run_id.in_(run_ids)))
    measured = _measured(session, run_ids)
    # Each test's duration is needed for what a shadow selection would have run, and for legs
    # ingested before 0017 (no measured time yet).
    timed_ids = [
        v.run_id
        for v in triaged
        if measured.get(v.run_id) is None
        or ((sel := by_variant.get(v.variant) or by_variant.get(None)) and sel.id in shadow_ids)
    ]
    durations = _durations(session, timed_ids)
    missing = {i: measured_time(durations[i]) for i in timed_ids if measured.get(i) is None}
    if missing:
        session.execute(
            text(
                "UPDATE runs r SET tests_run = m.tests, measured_ms = m.ms "
                "FROM unnest(CAST(:ids AS bigint[]), CAST(:tests AS integer[]), "
                "CAST(:ms AS double precision[])) AS m(id, tests, ms) "
                "WHERE r.id = m.id AND r.measured_ms IS NULL"
            ),
            {"ids": list(missing), "tests": [t for t, _ in missing.values()],
             "ms": [ms for _, ms in missing.values()]},
        )  # fmt: skip
        measured.update({i: ms for i, (_, ms) in missing.items()})
    failing_ids = sorted({
        r.test_id for v in triaged for r in (v.report.results if v.report else ())
    })  # fmt: skip
    keys, paths = _failing_tests(session, repo_id, failing_ids)
    changes = _run_changes(session, run_ids)
    ranks = {
        s.id: {names[key]: n for n, key in enumerate(s.selected_keys, start=1)}
        for s in selections
        if s.ranked
    }
    shadow_run = bool(shadow_ids)

    variants: list[VariantComparison] = []
    legs: list[LegOutcome] = []
    rows: list[FailureRow] = []
    # Inserted together at the end (outcomes first: failures refer to them), so the number of
    # statements doesn't grow with the number of variants.
    stored_outcomes: list[RunOutcome] = []
    stored_failures: list[RunFailure] = []
    used: dict[int, list[str]] = defaultdict(list)  # selection id -> its variants' statuses
    for v in triaged:
        selection = by_variant.get(v.variant) or by_variant.get(None)
        if selection is not None and selection.commit_sha != v.commit_sha:
            used[selection.id].append(SHADOW_COMMIT_MISMATCH)
            if selection.id in shadow_ids:
                variants.append(VariantComparison(v.variant, v.run_id, SHADOW_COMMIT_MISMATCH))
            selection = None  # another commit's: not this leg's selection
        elif selection is None and shadow_run:
            variants.append(VariantComparison(v.variant, v.run_id, NO_SELECTION))
        shadow = selection is not None and selection.id in shadow_ids
        if selection is not None:
            used[selection.id].append(SHADOW_COMPARED)
        would_run = WouldRun(scopes[selection.id]) if selection is not None else None
        rank_of = ranks.get(selection.id, {}) if selection is not None else {}
        before = befores.get(selection.id) if selection is not None else None
        results = v.report.results if v.report else []
        change = changes.get(v.run_id, RunChange())
        change = RunChange(change.changed_files, v.links_known, change.go_module,
                           change.generated)  # fmt: skip
        variant_rows = []
        for r in results:
            runs_it = would_run(r.test_id) if would_run is not None else None
            cause = None
            if shadow and not runs_it and r.verdict is not Verdict.FLAKY:
                assert selection is not None
                if before is not None and before(r.test_id):
                    cause = MissCause(BUDGET, _budget_detail(selection.budget or {}))
                else:
                    cause = miss_cause(r, change, paths.get(r.test_id), selection.fixture_files)
            variant_rows.append((r, runs_it, rank_of.get(r.test_id), cause))
        verdicts = Counter(r.verdict.value for r in results)
        ranked = [rank for _, _, rank, _ in variant_rows if rank is not None]
        leg = LegOutcome(
            variant=v.variant,
            run_id=v.run_id,
            selection=None if selection is None else "shadow" if shadow else selection.mode,
            failing=len(results),
            new_regressions=verdicts[Verdict.NEW_REGRESSION.value],
            pre_existing=verdicts[Verdict.PRE_EXISTING.value],
            flaky=verdicts[Verdict.FLAKY.value],
            unknown=verdicts[Verdict.UNKNOWN.value],
            failures_selected=None if selection is None else sum(
                1 for _, runs_it, _, _ in variant_rows if runs_it),
            first_failure_rank=min(ranked, default=None),
            measured_ms=measured.get(v.run_id),
        )  # fmt: skip
        legs.append(leg)
        outcome = RunOutcome(
            run_id=v.run_id, selection_id=selection.id if selection is not None else None,
            failing=leg.failing, new_regressions=leg.new_regressions,
            pre_existing=leg.pre_existing, flaky=leg.flaky, unknown=leg.unknown,
            failures_selected=leg.failures_selected, first_failure_rank=leg.first_failure_rank,
        )  # fmt: skip
        if shadow:
            assert selection is not None and would_run is not None
            selective = selection.mode == Mode.SELECTIVE.value
            shadow_rows = [
                FailureRow(r.test_id, v.variant, bool(runs_it), r.verdict.value, selective, cause)
                for r, runs_it, _, cause in variant_rows
            ]
            rows.extend(shadow_rows)
            timed = [(t, ms) for t, ms in durations[v.run_id] if not is_go_subtest(t)]
            ran = [(t, ms) for t, ms in timed if would_run(t)]
            outcomes = Counter(
                FLAKY if row.verdict == FLAKY else CAUGHT if row.would_run else MISSED
                for row in shadow_rows
            )
            compared = VariantComparison(
                variant=v.variant,
                run_id=v.run_id,
                status=SHADOW_COMPARED,
                failing=len(shadow_rows),
                caught=outcomes[CAUGHT],
                missed=outcomes[MISSED],
                flaky=outcomes[FLAKY],
                tests_total=len(timed),
                tests_would_run=len(ran),
                ms_total=float(sum(ms for _, ms in timed)),
                ms_would_run=float(sum(ms for _, ms in ran)),
            )
            variants.append(compared)
            outcome.caught, outcome.missed = compared.caught, compared.missed
            outcome.tests_would_run = compared.tests_would_run
            outcome.ms_would_run = compared.ms_would_run
        stored_outcomes.append(outcome)
        stored_failures.extend(
            RunFailure(
                run_id=v.run_id,
                test_key=keys[r.test_id],
                would_run=runs_it,
                rank=rank,
                verdict=r.verdict.value,
                confidence=r.confidence.value,
                cause=cause.cause if cause else None,
                cause_detail=cause.detail if cause else None,
                suggestion=cause.suggestion if cause else None,
            )
            for r, runs_it, rank, cause in variant_rows
        )
    session.add_all(stored_outcomes)
    session.flush()
    session.add_all(stored_failures)
    if triaged:
        session.execute(
            pg_insert(AttemptOutcome)
            .values(
                repo_id=repo_id,
                ci_run_id=ci_run_id,
                run_attempt=run_attempt,
                fail_on=None if fail_on is None else sorted(set(fail_on)),
            )
            .on_conflict_do_update(
                index_elements=[
                    AttemptOutcome.repo_id,
                    AttemptOutcome.ci_run_id,
                    AttemptOutcome.run_attempt,
                ],
                set_={"computed_at": func.now(), "fail_on": text("excluded.fail_on")},
            )
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
    shadows = tuple(s for s in selections if s.id in shadow_ids)
    return AttemptComparison(shadow_run, shadows, tuple(variants), tuple(units(rows)),
                             tuple(legs))  # fmt: skip


def _measured(session: Session, run_ids: Sequence[int]) -> dict[int, float | None]:
    """Each run's measured test time (``runs.measured_ms``; NULL before 0017's ingest)."""
    rows = session.execute(
        text("SELECT id, measured_ms FROM runs WHERE id = ANY(:ids)"), {"ids": list(run_ids)}
    )
    return {run_id: ms for run_id, ms in rows}


def _scopes(
    session: Session, selections: Sequence[CiSelection]
) -> tuple[dict[int, SelectionScope], dict[int, WouldRun], dict[int, str]]:
    """Each selection's scope, for a budget-limited shadow one the selection the budget cut,
    and the test IDs of the keys they name."""
    keys = {key for s in selections for key in s.selected_keys}
    for s in selections:
        before = (s.budget or {}).get("before") or {}
        keys.update(before.get("keys") or ())
    names: dict[int, str] = {
        key: test_id
        for key, test_id in session.execute(
            text("SELECT id, test_id FROM tests WHERE id = ANY(:keys)"), {"keys": sorted(keys)}
        )
    }
    scopes = {
        s.id: SelectionScope(
            full=s.mode == Mode.FULL.value,
            test_ids=frozenset(names[key] for key in s.selected_keys),
            go_test_ids=frozenset(names[key] for key in s.go_selected_keys),
            go_packages=frozenset(s.go_packages),
            python_files=frozenset(s.python_files),
            java_classes=frozenset(s.java_classes),
        )
        for s in selections
    }
    befores = {}
    for s in selections:
        if not (s.budget or {}).get("before"):
            continue
        before = (s.budget or {})["before"]
        befores[s.id] = WouldRun(SelectionScope(
            full=bool(before.get("full")),
            test_ids=frozenset(names[key] for key in before.get("keys") or ()),
            go_test_ids=frozenset(names[key] for key in before.get("go_keys") or ()),
            go_packages=frozenset(before.get("go_packages") or ()),
            python_files=frozenset(before.get("python_files") or ()),
            java_classes=frozenset(before.get("java_classes") or ()),
        ))  # fmt: skip
    return scopes, befores, names


def selection_scopes(
    session: Session, selections: Sequence[CiSelection]
) -> tuple[dict[int, SelectionScope], dict[int, str]]:
    """Stored selections' scopes (``WouldRun`` reads them), and the test IDs of their keys."""
    scopes, _, names = _scopes(session, selections)
    return scopes, names


def _budget_detail(budget: Mapping[str, Any]) -> str:
    seconds = budget.get("seconds")
    within = f"the {format_duration(seconds * 1000)} " if seconds else "the "
    what = "full suite" if budget.get("full_suite") else "selection"
    return f"left out by {within}budget target: the {what} without it would have run this test"


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
            SELECT status, created_at FROM ci_selections
            WHERE repo_id = :repo_id AND shadow AND created_at >= :since
              AND status <> 'compared'
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
    attempts: dict[
        tuple[str, int], list[tuple[str, float, float, datetime, str, datetime, bool]]
    ] = defaultdict(list)
    for (
        ci_run_id,
        run_attempt,
        mode,
        ms_total,
        ms_would_run,
        at,
        commit,
        selected_at,
        budget_limited,
    ) in session.execute(
        text(
            """
            SELECT s.ci_run_id, s.run_attempt, s.mode, COALESCE(r.measured_ms, 0),
                   COALESCE(sr.ms_would_run, 0), COALESCE(r.started_at, r.created_at),
                   r.commit_sha, s.created_at,
                   COALESCE(CAST(s.budget ->> 'limited' AS boolean), false)
            FROM ci_selections s
            JOIN run_outcomes sr ON sr.selection_id = s.id
            JOIN runs r ON r.id = sr.run_id
            WHERE s.repo_id = :repo_id AND s.shadow AND s.created_at >= :since
            """
        ),
        params,
    ):
        attempts[(ci_run_id, run_attempt)].append(
            (mode, ms_total, ms_would_run, at, commit, selected_at, budget_limited)
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
            FROM ci_selections s
            JOIN run_outcomes sr ON sr.selection_id = s.id
            JOIN runs r ON r.id = sr.run_id
            JOIN run_failures f ON f.run_id = sr.run_id
            JOIN tests t ON t.id = f.test_key
            WHERE s.repo_id = :repo_id AND s.shadow AND s.created_at >= :since
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
        budget_limited = any(row[6] for row in variants)
        at = max(row[3] for row in variants)
        commit = variants[0][4]
        targets: list[Recall | Period] = [report]
        period = periods.get(week_start(min(row[5] for row in variants)))
        if period is not None:
            targets.append(period)
        for target in targets:
            target.all.runs += 1
            target.all.budget_runs += budget_limited
            if selective:
                target.selective.runs += 1
                target.selective.budget_runs += budget_limited
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
