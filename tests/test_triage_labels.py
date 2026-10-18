"""Proxy labels for the triage eval (scripts/eval/triage_labels.py), on synthetic timelines."""

from datetime import UTC, datetime, timedelta

import pytest

from scripts.eval.triage_labels import (
    FAIL,
    FLAKY,
    PASS,
    PRE_EXISTING,
    REGRESSION,
    SKIP,
    Label,
    Run,
    Scored,
    fold_subtests,
    label,
    metrics,
)

T0 = datetime(2026, 10, 1, tzinfo=UTC)
TEST = "tests.test_cart::test_total"


def run(n: int, commit: str, *, branch: str = "main", variant: str = "") -> Run:
    return Run(n, T0 + timedelta(hours=n), commit, branch, branch == "main", variant)


def no_fix(test: str, failed: Run, passed: Run) -> bool | None:
    raise AssertionError("the fix diff must not be needed here")


class linked:
    """A LinkedFix that answers ``value`` and records what it was asked."""

    def __init__(self, value: bool | None) -> None:
        self.value = value
        self.calls: list[tuple[str, str]] = []

    def __call__(self, test: str, failed: Run, passed: Run) -> bool | None:
        self.calls.append((failed.commit, passed.commit))
        return self.value


# --- flaky ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("rerun_at", "when"), [(1, "before"), (5, "after")])
def test_same_commit_pass_is_flaky_before_or_after(rerun_at: int, when: str) -> None:
    failed = run(3, "c", branch="pr")
    timeline = sorted(
        [(run(0, "m"), PASS), (failed, FAIL), (run(rerun_at, "c", branch="pr"), PASS)],
        key=lambda item: item[0].order,
    )
    assert label(TEST, failed, timeline, no_fix) == Label(
        FLAKY, f"the same commit passed in run {rerun_at}", when
    )


def test_flaky_wins_over_pre_existing() -> None:
    failed = run(5, "c", branch="pr")
    timeline = [(run(1, "a"), FAIL), (run(2, "b"), FAIL), (failed, FAIL),
                (run(6, "c", branch="pr"), PASS)]  # fmt: skip
    assert label(TEST, failed, timeline, no_fix).kind == FLAKY


# --- pre-existing ---------------------------------------------------------------------------


def test_failing_on_main_for_two_commits_is_pre_existing() -> None:
    failed = run(5, "c", branch="pr")
    timeline = [(run(1, "a"), PASS), (run(2, "b"), FAIL), (run(3, "d"), FAIL), (failed, FAIL)]
    assert label(TEST, failed, timeline, no_fix) == Label(
        PRE_EXISTING, "failing on main for 2 commits"
    )


def test_main_streak_may_continue_after_the_run() -> None:
    failed = run(3, "c", branch="pr")
    timeline = [(run(1, "a"), PASS), (run(2, "b"), FAIL), (failed, FAIL), (run(4, "d"), FAIL),
                (run(5, "e"), PASS)]  # fmt: skip
    assert label(TEST, failed, timeline, no_fix).kind == PRE_EXISTING


def test_reruns_of_one_main_commit_are_not_a_streak() -> None:
    failed = run(5, "c", branch="pr")
    # Main failed twice, but on one commit, then passed: a one-off.
    timeline = [(run(1, "a"), PASS), (run(2, "b"), FAIL), (run(3, "b"), FAIL), (failed, FAIL),
                (run(6, "d"), PASS)]  # fmt: skip
    assert label(TEST, failed, timeline, no_fix) == Label(
        None, "main failed just before, once: a one-off"
    )


def test_skipped_main_runs_are_ignored() -> None:
    failed = run(5, "c", branch="pr")
    timeline = [(run(1, "a"), FAIL), (run(2, "b"), SKIP), (run(3, "d"), FAIL), (failed, FAIL)]
    assert label(TEST, failed, timeline, no_fix).kind == PRE_EXISTING


# --- regression on main ---------------------------------------------------------------------


def test_failure_introduced_on_main_that_persists_is_a_regression() -> None:
    failed = run(2, "b")
    timeline = [(run(1, "a"), PASS), (failed, FAIL), (run(3, "c"), FAIL), (run(4, "d"), PASS)]
    assert label(TEST, failed, timeline, no_fix) == Label(
        REGRESSION, "introduced on main, failing for 2 commits"
    )


def test_one_off_main_failure_is_excluded_not_flaky() -> None:
    failed = run(2, "b")
    timeline = [(run(1, "a"), PASS), (failed, FAIL), (run(3, "c"), PASS)]
    assert label(TEST, failed, timeline, no_fix) == Label(None, "a one-off failure on main")


def test_later_main_failures_of_a_streak_are_pre_existing() -> None:
    later = run(3, "c")
    timeline = [(run(1, "a"), PASS), (run(2, "b"), FAIL), (later, FAIL)]
    assert label(TEST, later, timeline, no_fix).kind == PRE_EXISTING


# --- regression on a branch -----------------------------------------------------------------


def test_branch_failure_fixed_by_a_linked_change_is_a_regression() -> None:
    failed = run(3, "c1", branch="pr")
    fix = run(5, "c3", branch="pr")
    timeline = [(run(1, "a"), PASS), (failed, FAIL), (run(4, "c2", branch="pr"), FAIL),
                (run(4, "x", branch="other"), PASS), (fix, PASS)]  # fmt: skip
    check = linked(True)
    assert label(TEST, failed, timeline, check) == Label(
        REGRESSION, "fixed on the branch by c3, a linked change"
    )
    assert check.calls == [("c1", "c3")]


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (False, "passed later after a change unrelated to the test"),
        (None, "fix diff unknown (no compare or import graph)"),
    ],
)
def test_branch_failure_with_an_unrelated_or_unknown_fix_is_excluded(
    value: bool | None, reason: str
) -> None:
    failed = run(3, "c1", branch="pr")
    timeline = [(run(1, "a"), PASS), (failed, FAIL), (run(5, "c2", branch="pr"), PASS)]
    assert label(TEST, failed, timeline, linked(value)) == Label(None, reason)


@pytest.mark.parametrize(
    ("timeline_before", "reason"),
    [
        ([], "no main history of the test before the run"),
        ([(run(1, "a"), SKIP)], "no main history of the test before the run"),
    ],
)
def test_branch_failure_without_main_history_is_excluded(
    timeline_before: list[tuple[Run, str]], reason: str
) -> None:
    failed = run(3, "c1", branch="pr")
    timeline = [*timeline_before, (failed, FAIL), (run(5, "c2", branch="pr"), PASS)]
    assert label(TEST, failed, timeline, no_fix) == Label(None, reason)


def test_branch_failure_never_passing_again_is_excluded() -> None:
    failed = run(3, "c1", branch="pr")
    timeline = [(run(1, "a"), PASS), (failed, FAIL), (run(5, "c2", branch="pr"), FAIL)]
    assert label(TEST, failed, timeline, no_fix) == Label(None, "never passed again on its branch")


# --- folding and metrics --------------------------------------------------------------------


def test_go_subtests_fold_into_a_failing_parent() -> None:
    assert fold_subtests(["p::TestA", "p::TestA/x", "p::TestA/y", "p::TestB/z", "q::t"]) == [
        "p::TestA", "p::TestB/z", "q::t"]  # fmt: skip


def scored(verdict: str, kind: str | None, confidence: str = "high", when: str = "") -> Scored:
    return Scored("t", 1, verdict, confidence, Label(kind, "why", when))


def test_metrics() -> None:
    m = metrics([
        scored("new_regression", REGRESSION),
        scored("new_regression", REGRESSION, "medium"),
        scored("new_regression", FLAKY, when="after"),
        scored("flaky", REGRESSION),  # the worst error
        scored("flaky", FLAKY, when="before"),
        scored("unknown", REGRESSION, "low"),
        scored("unknown", None, "low"),
        scored("pre_existing", PRE_EXISTING),
    ])  # fmt: skip

    assert (m.failures, m.labelled, m.excluded) == (8, 7, {"why": 1})
    assert m.precision("new_regression", REGRESSION) == (2, 3)
    assert m.precision("new_regression", REGRESSION, "high") == (1, 2)
    assert m.precision("flaky", FLAKY) == (1, 2)
    assert m.recall(REGRESSION, "new_regression") == (2, 4)
    assert [s.verdict for s in m.regressions_called_flaky] == ["flaky"]
    assert (m.unknown_all, m.unknown_labelled) == (2, 1)
    assert m.flaky_when == {"after": 1, "before": 1}
