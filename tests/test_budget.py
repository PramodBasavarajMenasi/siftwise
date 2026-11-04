"""Time budgets (``select_tests(budget_ms=...)``): packing whole units by risk, the estimates,
full-suite fallbacks, and that no budget changes nothing."""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from siftwise.core.risk import Order
from siftwise.core.selector import (
    KnownTest,
    Mode,
    RepoHistory,
    Selection,
    SelectionScope,
    SelectorConfig,
    WouldRun,
    format_duration,
    select_tests,
)

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
MODULE = "example.com/m"
CONFIG = SelectorConfig(go_module=MODULE)
A, B, C = (f"{MODULE}/{p}::Test{p.upper()}" for p in "abc")
CHANGED = ("a/x.go", "b/x.go", "c/x.go")


def go_history(
    a: float | None = 1000, b: float | None = 2000, c: float | None = 3000
) -> RepoHistory:
    """Three Go packages, one test each, riskiest first: a failed yesterday, b fails now and
    then, c never."""
    tests = [
        KnownTest(A, avg_duration_ms=a, runs=50, failures=5, last_failed_at=NOW - timedelta(1)),
        KnownTest(
            B, avg_duration_ms=b, runs=50, failures=5, last_failed_at=NOW - timedelta(days=60)
        ),
        KnownTest(C, avg_duration_ms=c, runs=50, failures=0),
    ]
    return RepoHistory(tests={t.test_id: t for t in tests}, newest_run_at=NOW)


def select(history: RepoHistory | None, budget_ms: float | None, *changed: str,
           known: bool = True) -> Selection:  # fmt: skip
    return select_tests(history, changed or CHANGED, known, CONFIG, order=Order.RISK,
                        budget_ms=budget_ms)  # fmt: skip


def ids(selection: Selection) -> list[str]:
    return [t.test_id for t in selection.tests]


def test_no_budget_changes_nothing() -> None:
    history = go_history()
    plain = select_tests(history, CHANGED, True, CONFIG, order=Order.RISK)
    assert plain.budget is None
    assert select(history, None) == plain


def test_a_budget_everything_fits_in_changes_nothing_but_says_so() -> None:
    history = go_history()
    plain = select(history, None)
    fits = select(history, 6000)
    assert fits.budget is not None
    assert (fits.budget.applied, fits.budget.limited, fits.budget.estimated_ms) == (
        True,
        False,
        6000,
    )
    assert dataclasses.replace(fits, budget=None) == plain


def test_units_are_packed_in_risk_order_while_they_fit() -> None:
    plain = select(go_history(), None)
    assert plain.commands == ("go test ./a", "go test ./b", "go test ./c")  # risk order
    limited = select(go_history(), 3500)
    budget = limited.budget
    assert budget is not None and budget.limited
    assert limited.commands == ("go test ./a", "go test ./b")
    assert ids(limited) == [A, B] and limited.selected_count == 2
    assert limited.go_packages_run_whole == (f"{MODULE}/a", f"{MODULE}/b")
    assert (budget.estimated_ms, budget.left_out, budget.left_out_ms) == (3000, 1, 3000)
    scores = {t.test_id: t.score or 0.0 for t in plain.tests}
    assert budget.left_out_risk_share == pytest.approx(scores[C] / sum(scores.values()))
    assert limited.mode is Mode.SELECTIVE
    assert limited.reason == (
        "budget-limited (estimated 3s of a 4s target): 2 of 3 selected tests run; 1 left out "
        f"(~3s estimated, {budget.left_out_risk_share:.0%} of the selection's risk)"
    )
    # The time estimate is what runs.
    assert limited.estimated_ms_would_run == 3000


def test_an_exact_fit_is_kept() -> None:
    limited = select(go_history(), 3000)
    assert limited.commands == ("go test ./a", "go test ./b")


def test_the_first_unit_runs_even_when_it_alone_is_over_the_budget() -> None:
    limited = select(go_history(), 500)
    assert limited.commands == ("go test ./a",)
    assert limited.budget is not None
    assert (limited.budget.estimated_ms, limited.budget.left_out) == (1000, 2)


def test_a_unit_that_does_not_fit_is_skipped_and_the_walk_goes_on() -> None:
    # b ranks above c but doesn't fit; c still does: one big unit doesn't waste the budget.
    limited = select(go_history(a=1000, b=5000, c=100), 1500)
    assert limited.commands == ("go test ./a", "go test ./c")
    assert limited.budget is not None
    assert (limited.budget.estimated_ms, limited.budget.left_out) == (1100, 1)


def test_an_unknown_duration_counts_as_the_90th_percentile_of_the_known_ones() -> None:
    history = go_history(c=None)
    # Known: 1000 and 2000 ms; the 90th percentile is 2000, so c costs 2000.
    assert select(history, 4999).commands == ("go test ./a", "go test ./b")
    fits = select(history, 5000)
    assert fits.budget is not None and not fits.budget.limited
    assert fits.budget.unknown_ms == 2000


def test_a_whole_package_counts_every_known_test_in_it() -> None:
    history = go_history()
    other = KnownTest(f"{MODULE}/a::TestOther", avg_duration_ms=4000, runs=50, failures=0)
    sub = KnownTest(f"{MODULE}/a::TestA/sub", avg_duration_ms=900, runs=50, failures=0)
    history = dataclasses.replace(history, tests={**history.tests, other.test_id: other,
                                                  sub.test_id: sub})  # fmt: skip
    # ./a runs whole: TestA 1000 + TestOther 4000 (the subtest is inside TestA's time).
    limited = select(history, 100, "a/x.go")
    assert limited.budget is not None and limited.budget.estimated_ms == 5000


def test_a_new_test_file_counts_as_one_unknown_test_and_runs_first() -> None:
    tests = [
        KnownTest("tests.test_a::test_a", avg_duration_ms=100, runs=50, failures=0),
        KnownTest("tests.test_b::test_b", avg_duration_ms=300, runs=50, failures=0),
    ]
    history = RepoHistory(tests={t.test_id: t for t in tests}, newest_run_at=NOW)
    limited = select_tests(history, ["tests/test_new.py", "tests/test_a.py", "tests/test_b.py"],
                           True, order=Order.RISK, budget_ms=400)  # fmt: skip
    # test_new.py has no known test: it costs the 90th percentile (300) and, being the change
    # itself, comes first; test_a.py (100) still fits, test_b.py (300) doesn't.
    assert limited.commands == ("pytest tests/test_new.py tests/test_a.py",)
    assert limited.python_files_run_whole == ("tests/test_a.py", "tests/test_new.py")
    assert limited.budget is not None and limited.budget.left_out == 1


def test_without_durations_the_budget_is_not_applied() -> None:
    history = go_history(a=None, b=None, c=None)
    result = select(history, 1000)
    assert result.budget is not None
    assert (result.budget.applied, result.budget.limited) == (False, False)
    assert "no test durations" in result.budget.note
    assert result.commands == select(history, None).commands


def test_a_left_out_test_would_not_run_but_the_selection_before_the_budget_would() -> None:
    limited = select(go_history(), 1000)
    assert limited.budget is not None and limited.budget.before is not None
    runs, before = WouldRun(limited), WouldRun(limited.budget.before)
    assert (runs(A), runs(B), runs(C)) == (True, False, False)
    assert (before(A), before(B), before(C)) == (True, True, True)
    assert limited.budget.before == SelectionScope.of(select(go_history(), None))


# --- full-suite fallbacks ----------------------------------------------------------------


def test_a_full_suite_fallback_packs_the_full_suite_by_risk() -> None:
    packed = select(go_history(), 1500, known=False)
    budget = packed.budget
    assert budget is not None and (budget.limited, budget.full_suite) == (True, True)
    assert packed.mode is Mode.SELECTIVE
    assert packed.commands == ("go test ./a",)
    assert packed.reason.startswith(
        "budget-limited full suite (estimated 1s of a 2s target; siftwise would run the full "
        "suite: changed files are unknown for this change): 1 of 3 known tests run; 2 left out"
    )
    assert packed.tests[0].reasons == ("full suite: changed files are unknown for this change",)
    # A miss outside the pack was left out by the budget: the full suite runs everything.
    assert budget.before == SelectionScope(full=True)
    assert budget.left_out == 2


def test_a_full_suite_that_fits_runs_as_it_is() -> None:
    result = select(go_history(), 60_000, known=False)
    assert result.mode is Mode.FULL and result.commands == ("go test ./...",)
    assert result.budget is not None
    assert (result.budget.limited, result.budget.full_suite) == (False, True)


def test_a_full_suite_with_no_history_cannot_be_budgeted() -> None:
    result = select(None, 1000, known=False)
    assert result.mode is Mode.FULL
    assert result.budget is not None and not result.budget.applied
    assert "no test history" in result.budget.note


def test_a_packed_full_suite_leaves_out_tests_no_runner_can_run_and_says_so() -> None:
    history = go_history()
    java = KnownTest("com.example.CartTest::testTotal", avg_duration_ms=10, runs=5, failures=0)
    history = dataclasses.replace(history, tests={**history.tests, java.test_id: java})
    packed = select(history, 1000, known=False)
    assert packed.budget is not None and packed.budget.limited
    assert packed.commands == ("go test ./a",)
    assert packed.budget.left_out == 3  # b, c and the Java test
    assert java.test_id not in ids(packed)
    # When everything runnable fits, the full suite runs as it is: its commands run it all.
    roomy = select(history, 60_000, known=False)
    assert roomy.mode is Mode.FULL and roomy.budget is not None and not roomy.budget.limited


def test_a_build_file_change_is_a_budget_limited_full_suite_too() -> None:
    packed = select(go_history(), 1000, "go.mod")
    assert packed.budget is not None and packed.budget.full_suite and packed.budget.limited
    assert "build/config file changed: go.mod" in packed.reason


@pytest.mark.parametrize(
    ("ms", "text"),
    [(0, "0s"), (38_000, "38s"), (60_000, "1m"), (290_000, "4m 50s"), (3_600_000, "1h"),
     (3_900_000, "1h 5m")],
)  # fmt: skip
def test_durations_read_as_people_write_them(ms: float, text: str) -> None:
    assert format_duration(ms) == text


def test_a_test_skipped_in_every_run_costs_nothing_but_one_that_ran_unmeasured_does() -> None:
    history = go_history()
    skipped = KnownTest(f"{MODULE}/a::TestSkipped", avg_duration_ms=None, runs=0, failures=0)
    history = dataclasses.replace(history, tests={**history.tests, skipped.test_id: skipped})
    # ./a: TestA 1000 + TestSkipped 0 (only ever skipped: it will most likely skip again).
    assert select(history, 100, "a/x.go").budget.estimated_ms == 1000  # type: ignore[union-attr]
    unmeasured = dataclasses.replace(skipped, runs=4)  # ran, but reported no duration
    history = dataclasses.replace(history, tests={**history.tests, skipped.test_id: unmeasured})
    # The 90th percentile of the positive known durations (1000, 2000, 3000): 3000.
    assert select(history, 100, "a/x.go").budget.estimated_ms == 4000  # type: ignore[union-attr]
