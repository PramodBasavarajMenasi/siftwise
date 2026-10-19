"""Shadow mode (core/shadow.py): storing selections, comparing shadow runs, recall."""

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.orm import Session

from siftwise.core import shadow
from siftwise.core.junit import Status
from siftwise.core.models import (
    SHADOW_COMMIT_MISMATCH,
    SHADOW_COMPARE_ERROR,
    SHADOW_COMPARED,
    SHADOW_NO_UPLOAD,
    SHADOW_PENDING,
    Run,
    ShadowFailure,
    ShadowResult,
    ShadowSelection,
    Test,
)
from siftwise.core.selector import Mode, Runner, SelectedTest, Selection
from siftwise.core.shadow import (
    CAUGHT,
    FLAKY,
    MISSED,
    NO_SELECTION,
    FailureRow,
    ShadowTarget,
    Tally,
    compare_ci_run,
    recall,
    store_selection,
    units,
)
from tests.test_triage_db import OTHER, REPO, TEST, F, P, record, sha

FLAKY_TEST = "tests.test_flaky::test_sometimes"
NEW = "tests.test_new::test_it"  # never recorded before the shadow run
PASSING = "tests.test_io::test_read"
GO = "github.com/acme/shop/pay"
SHADOW = "shadow-1"
CART_CHANGE = ("src/cart.py",)  # links TEST (path mapping): a new regression


def selection(
    *tests: str | tuple[str, Runner],
    mode: Mode = Mode.SELECTIVE,
    go_packages: Sequence[str] = (),
    python_files: Sequence[str] = (),
) -> Selection:
    """Selected tests as test IDs (pytest) or (test ID, runner)."""
    selected = tuple(
        SelectedTest(t, ("r",), Runner.PYTEST, None)
        if isinstance(t, str)
        else SelectedTest(t[0], ("r",), t[1], None)
        for t in tests
    )
    return Selection(
        mode=mode,
        reason="why",
        tests=selected,
        selected_count=len(selected),
        total_known=10,
        commands=(),
        go_packages_run_whole=tuple(go_packages),
        python_files_run_whole=tuple(python_files),
        estimated_ms_total=100.0,
        estimated_ms_would_run=10.0,
    )


def store(
    session: Session,
    sel: Selection,
    *,
    ci: str = SHADOW,
    variant: str | None = None,
    commit: int = 50,
) -> bool:
    """Stored (or already stored for this commit)."""
    target = ShadowTarget(ci, 1, variant, sha(commit))
    return store_selection(session, REPO, target, sel) is None


def shadow_run(
    session: Session,
    tests: Mapping[str, Status],
    *,
    commit: int = 50,
    variant: str | None = "linux",
    ci: str = SHADOW,
    hour: float = 6,
) -> Run:
    results: dict[str, Status | Sequence[Status]] = dict(tests)
    return record(session, commit=commit, hour=hour, branch="pr", variant=variant,
                  ci_run_id=ci, tests=results, changed=CART_CHANGE)  # fmt: skip


def compare(session: Session, ci: str = SHADOW) -> shadow.AttemptComparison:
    result = compare_ci_run(session, REPO, ci, 1)
    assert result is not None
    return result


@pytest.fixture
def seeded(db_session: Session) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: P, OTHER: P, FLAKY_TEST: P, PASSING: P,
                                                f"{GO}::TestA": P, f"{GO}::TestC": P})  # fmt: skip
    # The shadow commit passed FLAKY_TEST in an earlier CI run: rule 1 calls its failure flaky.
    record(db_session, commit=50, hour=5, branch="pr", tests={FLAKY_TEST: P})


ALL_FAILING = {TEST: F, OTHER: F, FLAKY_TEST: F, NEW: F, PASSING: P}


# --- comparing ------------------------------------------------------------------------------


@pytest.mark.usefixtures("seeded")
def test_failures_are_caught_missed_or_flaky(db_session: Session) -> None:
    assert store(db_session, selection(TEST))
    run = shadow_run(db_session, ALL_FAILING)

    result = compare(db_session)

    assert (result.shadow, result.counted, result.mode) == (True, True, "selective")
    outcomes = {u.test_id: (u.outcome, u.verdict) for u in result.units}
    assert outcomes == {
        TEST: (CAUGHT, "new_regression"),
        OTHER: (MISSED, "unknown"),  # history, not linked to the change
        NEW: (MISSED, "unknown"),  # no history, its file didn't change
        FLAKY_TEST: (FLAKY, "flaky"),  # same commit passed before: neither caught nor missed
    }
    tally = result.tally
    assert (tally.caught, tally.missed, tally.flaky_excluded, tally.failures) == (1, 2, 1, 4)
    assert tally.recall == 1 / 3
    assert tally.missed_by_verdict == {"unknown": 2}
    [variant] = result.variants
    assert (variant.status, variant.failing, variant.caught, variant.missed, variant.flaky) == (
        SHADOW_COMPARED, 4, 1, 2, 1)  # fmt: skip
    # Measured on the full-suite run: 5 tests with equal durations, 1 would have run.
    assert (variant.tests_total, variant.tests_would_run) == (5, 1)
    assert variant.ms_total == 5 * variant.ms_would_run > 0
    assert result.time.saved == pytest.approx(0.8)

    stored = db_session.scalars(select(ShadowSelection)).one()
    assert stored.status == SHADOW_COMPARED and stored.compared_at is not None
    row = db_session.get(ShadowResult, run.id)
    assert row is not None and (row.caught, row.missed, row.flaky) == (1, 2, 1)
    failures = db_session.scalars(select(ShadowFailure).where(ShadowFailure.run_id == run.id))
    assert sorted((f.would_run, f.verdict) for f in failures) == [
        (False, "flaky"),
        (False, "unknown"),
        (False, "unknown"),
        (True, "new_regression"),
    ]


@pytest.mark.usefixtures("seeded")
def test_tests_a_stored_selection_runs_but_does_not_name(db_session: Session) -> None:
    # Go: TestA selected with -run (its new subtest runs, TestC doesn't); a Python file run
    # whole runs a test history has never seen.
    sel = selection((f"{GO}::TestA", Runner.GO), python_files=["tests/test_new.py"])
    assert store(db_session, sel)
    shadow_run(db_session, {f"{GO}::TestA/new_sub": F, f"{GO}::TestC": F, NEW: F})

    outcomes = {u.test_id: u.outcome for u in compare(db_session).units}

    assert outcomes == {f"{GO}::TestA/new_sub": CAUGHT, f"{GO}::TestC": MISSED, NEW: CAUGHT}


@pytest.mark.usefixtures("seeded")
def test_whole_go_package_and_full_selection(db_session: Session) -> None:
    assert store(db_session, selection(go_packages=[GO]))
    shadow_run(db_session, {f"{GO}::TestC": F, OTHER: F})
    assert {u.test_id: u.outcome for u in compare(db_session).units} == {
        f"{GO}::TestC": CAUGHT, OTHER: MISSED}  # fmt: skip

    assert store(db_session, selection(mode=Mode.FULL), ci="shadow-full")
    shadow_run(db_session, {OTHER: F, NEW: F}, ci="shadow-full")
    full = compare(db_session, "shadow-full")
    assert full.mode == "full"
    assert {u.outcome for u in full.units} == {CAUGHT}
    assert full.time.saved == 0


@pytest.mark.usefixtures("seeded")
def test_each_variant_uses_its_own_selection_else_the_attempts(db_session: Session) -> None:
    assert store(db_session, selection(TEST))  # every leg
    assert store(db_session, selection(OTHER), variant="mac")
    shadow_run(db_session, {TEST: F, OTHER: F}, variant="linux")
    shadow_run(db_session, {TEST: F, OTHER: F}, variant="mac")

    result = compare(db_session)

    by_test = {u.test_id: (u.outcome, u.variants) for u in result.units}
    assert by_test == {TEST: (MISSED, ("mac",)), OTHER: (MISSED, ("linux",))}
    assert [(v.variant, v.caught, v.missed) for v in result.variants] == [
        ("linux", 1, 1), ("mac", 1, 1)]  # fmt: skip
    assert {s.status for s in result.selections} == {SHADOW_COMPARED}


@pytest.mark.usefixtures("seeded")
def test_runs_that_cannot_be_compared(db_session: Session) -> None:
    # Another commit recorded under the attempt the selection was stored for.
    assert store(db_session, selection(TEST))
    shadow_run(db_session, {TEST: F}, commit=51)
    mismatch = compare(db_session)
    assert (mismatch.counted, mismatch.mode) == (False, None)
    assert [v.status for v in mismatch.variants] == [SHADOW_COMMIT_MISMATCH]
    assert [s.status for s in mismatch.selections] == [SHADOW_COMMIT_MISMATCH]
    assert db_session.scalars(select(ShadowResult)).all() == []

    # A selection for one leg only; another leg was recorded.
    assert store(db_session, selection(TEST), ci="shadow-2", variant="windows")
    shadow_run(db_session, {TEST: F}, ci="shadow-2", variant="linux")
    other_leg = compare(db_session, "shadow-2")
    assert [v.status for v in other_leg.variants] == [NO_SELECTION]
    assert [s.status for s in other_leg.selections] == [SHADOW_NO_UPLOAD]

    # Nothing recorded at all (the job was cancelled before record, say).
    assert store(db_session, selection(TEST), ci="shadow-3")
    assert [s.status for s in compare(db_session, "shadow-3").selections] == [SHADOW_NO_UPLOAD]


@pytest.mark.usefixtures("seeded")
def test_not_a_shadow_run_and_unknown_repo(db_session: Session) -> None:
    shadow_run(db_session, {TEST: F})
    assert compare(db_session).shadow is False
    assert compare_ci_run(db_session, "nobody/nothing", SHADOW, 1) is None


@pytest.mark.usefixtures("seeded")
def test_comparing_again_replaces_the_comparison(db_session: Session) -> None:
    assert store(db_session, selection(TEST))
    shadow_run(db_session, ALL_FAILING)

    first = compare(db_session)
    second = compare(db_session)

    assert first.units == second.units
    assert db_session.scalar(select(func.count()).select_from(ShadowResult)) == 1
    assert db_session.scalar(select(func.count()).select_from(ShadowFailure)) == 4


@pytest.mark.usefixtures("seeded")
def test_a_compare_error_is_recorded(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    assert store(db_session, selection(TEST))
    shadow_run(db_session, ALL_FAILING)

    def broken(*_: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(shadow, "_durations", broken)
    with pytest.raises(RuntimeError):
        compare(db_session)

    assert db_session.scalars(select(ShadowSelection.status)).one() == SHADOW_COMPARE_ERROR
    assert db_session.scalars(select(ShadowResult)).all() == []


# --- storing --------------------------------------------------------------------------------


@pytest.mark.usefixtures("seeded")
def test_storing_a_selection(db_session: Session) -> None:
    sel = selection(TEST, (f"{GO}::TestA", Runner.GO), go_packages=[GO], python_files=["a.py"])
    assert store(db_session, sel)

    row = db_session.scalars(select(ShadowSelection)).one()
    assert (row.ci_run_id, row.run_attempt, row.variant, row.commit_sha) == (
        SHADOW, 1, None, sha(50))  # fmt: skip
    assert (row.mode, row.status, row.selected_count, row.total_known) == (
        "selective", SHADOW_PENDING, 2, 10)  # fmt: skip
    assert (len(row.selected_keys), len(row.go_selected_keys)) == (2, 1)
    assert (row.go_packages, row.python_files) == ([GO], ["a.py"])
    assert (row.estimated_ms_total, row.estimated_ms_would_run) == (100.0, 10.0)


@pytest.mark.usefixtures("seeded")
def test_the_first_leg_to_store_wins(db_session: Session) -> None:
    assert store(db_session, selection(TEST))
    assert store(db_session, selection(OTHER))  # same commit: stored (by the other leg)
    conflict = ShadowTarget(SHADOW, 1, None, sha(51))  # another commit: not ours
    assert store_selection(db_session, REPO, conflict, selection(TEST)) == "commit_conflict"
    assert store(db_session, selection(TEST), variant="mac")  # another variant: its own row

    rows = db_session.scalars(select(ShadowSelection).order_by(ShadowSelection.id)).all()
    assert [(r.variant, r.selected_count, r.commit_sha) for r in rows] == [
        (None, 1, sha(50)), ("mac", 1, sha(50))]  # fmt: skip
    test_key = db_session.scalars(select(Test.id).where(Test.test_id == TEST)).one()
    assert rows[0].selected_keys == [test_key]  # the first leg's selection, kept


@pytest.mark.usefixtures("seeded")
def test_a_selection_naming_unknown_tests_or_repos_is_not_stored(db_session: Session) -> None:
    target = ShadowTarget(SHADOW, 1, None, sha(50))
    ghost = selection("tests.test_ghost::test_x")
    assert store_selection(db_session, REPO, target, ghost) == "incomplete_selection"
    assert store_selection(db_session, "nobody/nothing", target, selection()) == "unknown_repo"
    assert db_session.scalars(select(ShadowSelection)).all() == []


# --- recall ---------------------------------------------------------------------------------


@pytest.mark.usefixtures("seeded")
def test_recall_over_shadow_runs(db_session: Session) -> None:
    # A selective run: 1 caught, 2 missed, 1 flaky.
    assert store(db_session, selection(TEST))
    shadow_run(db_session, ALL_FAILING)
    compare(db_session)
    # A full one: everything caught, never in the selective figures.
    assert store(db_session, selection(mode=Mode.FULL), ci="shadow-full")
    shadow_run(db_session, {OTHER: F}, ci="shadow-full", hour=7)
    compare(db_session, "shadow-full")
    # Not counted: never compared (old: cancelled; recent: in progress), no upload, mismatch.
    for ci in ("cancelled", "running", "no-upload", "mismatch"):
        assert store(db_session, selection(TEST), ci=ci)
    db_session.execute(update(ShadowSelection).where(ShadowSelection.ci_run_id == "cancelled")
                       .values(created_at=func.now() - timedelta(hours=7)))  # fmt: skip
    compare(db_session, "no-upload")
    shadow_run(db_session, {TEST: F}, ci="mismatch", commit=51, hour=8)
    compare(db_session, "mismatch")
    # Outside the window.
    assert store(db_session, selection(TEST), ci="old")
    shadow_run(db_session, {OTHER: F}, ci="old", hour=9)
    compare(db_session, "old")
    db_session.execute(update(ShadowSelection).where(ShadowSelection.ci_run_id == "old")
                       .values(created_at=func.now() - timedelta(days=31)))  # fmt: skip

    report = recall(db_session, REPO, 30)

    assert report is not None
    assert (report.all.runs, report.selective.runs, report.full_runs) == (2, 1, 1)
    assert (report.all.caught, report.all.missed, report.all.flaky_excluded) == (2, 2, 1)
    assert (report.selective.caught, report.selective.missed) == (1, 2)
    assert report.all.recall == 0.5
    assert report.selective.recall == 1 / 3
    assert report.all.missed_by_verdict == {"unknown": 2}
    assert report.not_counted == {"cancelled": 1, "no_upload": 1, "commit_mismatch": 1}
    assert report.in_progress == 1
    assert report.time.saved is not None and 0 < report.time.saved < 1
    assert [(m.test_id, m.ci_run_id, m.verdict, m.selection_mode) for m in report.misses] == [
        (NEW, SHADOW, "unknown", "selective"), (OTHER, SHADOW, "unknown", "selective")]  # fmt: skip
    assert report.misses[0].commit_sha == sha(50)
    assert report.misses[0].variants == ("linux",)


def test_recall_of_an_unknown_repo_or_nothing(db_session: Session) -> None:
    assert recall(db_session, REPO, 30) is None
    record(db_session, commit=1, hour=1, tests={TEST: P})
    report = recall(db_session, REPO, 30)
    assert report is not None
    assert (report.all.runs, report.all.recall, report.time.saved) == (0, None, None)


# --- units (pure) ---------------------------------------------------------------------------


def row(variant: str, would_run: bool, verdict: str, selective: bool = True) -> FailureRow:
    return FailureRow("t::x", variant, would_run, verdict, selective)


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        # flaky on one leg, a caught regression on another: counted, caught
        ([row("a", False, "flaky"), row("b", True, "new_regression")],
         (CAUGHT, "new_regression", ("b",))),
        # flaky legs never make a miss; a missed non-flaky leg does
        ([row("a", False, "flaky"), row("b", False, "unknown")], (MISSED, "unknown", ("b",))),
        # flaky everywhere: excluded
        ([row("a", False, "flaky"), row("b", True, "flaky")], (FLAKY, "flaky", ("a", "b"))),
        # a miss on any leg is a miss, under the worst missed verdict
        ([row("a", True, "new_regression"), row("b", False, "pre_existing"),
          row("c", False, "unknown")], (MISSED, "unknown", ("b", "c"))),
    ],
)  # fmt: skip
def test_units_over_variants(rows: list[FailureRow], expected: tuple[object, ...]) -> None:
    [unit] = units(rows)
    assert (unit.outcome, unit.verdict, unit.variants) == expected


def test_a_unit_is_selective_only_if_every_leg_was() -> None:
    [unit] = units([row("a", True, "unknown"), row("b", True, "unknown", selective=False)])
    assert not unit.selective


def test_tally_recall() -> None:
    assert Tally().recall is None
    tally = Tally()
    for unit in units([FailureRow("a", None, True, "unknown", True),
                       FailureRow("b", None, False, "pre_existing", True),
                       FailureRow("c", None, False, "flaky", True)]):  # fmt: skip
        tally.add(unit)
    assert (tally.recall, tally.failures, dict(tally.missed_by_verdict)) == (
        0.5, 3, {"pre_existing": 1})  # fmt: skip


def count_compare_queries(session: Session, ci: str) -> int:
    statements: list[str] = []

    def before(*args: Any) -> None:
        if not args[2].startswith(("SAVEPOINT", "RELEASE")):  # the test session's own
            statements.append(args[2])

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", before)
    try:
        compare(session, ci)
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return len(statements)


def test_compare_query_count_does_not_grow_with_failures(db_session: Session) -> None:
    many = [f"tests.test_m{i}::test_x" for i in range(60)]
    record(db_session, commit=1, hour=0, tests=dict.fromkeys(many, P))
    # Every test failed before, so both comparisons' triage sees the same kind of history
    # (the second sees the first's failures too).
    record(db_session, commit=2, hour=1, tests=dict.fromkeys(many, F), branch="x")
    for ci, failing in (("few", many[:2]), ("lots", many)):
        assert store(db_session, selection(many[0]), ci=ci)
        tests = {**dict.fromkeys(many, P), **dict.fromkeys(failing, F)}
        shadow_run(db_session, tests, ci=ci)

    few, lots = count_compare_queries(db_session, "few"), count_compare_queries(db_session, "lots")

    assert few == lots
    assert lots <= 24, lots  # one variant: triage_ci_run's queries plus 9 of shadow's own
