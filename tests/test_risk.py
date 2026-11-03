"""Risk order (core/risk.py and the selector's ordered commands): the score's signals, ties,
determinism, flaky and new tests, and that ordering never changes what's selected."""

import math
import shlex
from datetime import UTC, datetime, timedelta

import pytest

from siftwise.core.risk import Link, Order, Ranked, RiskInputs, rank, score, unit_key
from siftwise.core.selector import (
    FailedRun,
    KnownTest,
    Mode,
    RepoHistory,
    Selection,
    SelectionScope,
    SelectorConfig,
    select_tests,
)

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


def inputs(**kwargs: object) -> RiskInputs:
    """An established test (10 runs, no failure) unless told otherwise."""
    return RiskInputs(**{"test_id": "t::a", "link": Link.HISTORY, "runs": 10, "failures": 0,
                         **kwargs})  # type: ignore[arg-type]  # fmt: skip


# --- the score ---------------------------------------------------------------------------


def test_the_link_is_the_strongest_signal() -> None:
    assert score(inputs(link=Link.DIRECT), NOW).score == 6.0
    assert score(inputs(link=Link.INDIRECT), NOW).score == 3.0
    assert score(inputs(link=Link.HISTORY), NOW).score == 0.0
    # Even a test failing every run, today, with no link stays below a direct link.
    broken = inputs(runs=10, failures=10, last_failed_at=NOW)
    assert score(broken, NOW).score == 5.0 < score(inputs(link=Link.DIRECT), NOW).score


def test_recency_halves_every_seven_days_back_from_the_newest_run() -> None:
    def recency(days: float) -> float:
        # Rate kept out: one failure in a huge number of runs.
        test = inputs(runs=10**9, failures=1, last_failed_at=NOW - timedelta(days=days))
        return score(test, NOW).score

    assert recency(0) == 3.0
    assert recency(7) == 1.5
    assert recency(14) == 0.75
    # Anchored on the repo's newest run, not on today: a quiet repo scores the same later.
    test = inputs(runs=10**9, failures=1, last_failed_at=NOW - timedelta(days=7))
    assert score(test, NOW).score == 1.5
    assert score(test, NOW + timedelta(days=300)).score == 0.0  # 3 * 0.5**(307/7), rounded


def test_the_failure_rate_counts() -> None:
    assert score(inputs(runs=10, failures=5), NOW).score == 1.0  # 2 * 50%
    assert score(inputs(runs=10, failures=0), NOW).score == 0.0


def test_flaky_failures_are_discounted() -> None:
    real = inputs(runs=10, failures=5, last_failed_at=NOW)
    flaky = inputs(runs=10, failures=5, last_failed_at=NOW, flaky_score=0.8)
    always_flaky = inputs(runs=10, failures=5, last_failed_at=NOW, flaky_score=1.0)

    assert score(real, NOW).score == 4.0  # 3 + 1
    assert score(flaky, NOW).score == pytest.approx(0.8)  # (3 + 1) * 0.2
    assert score(always_flaky, NOW).score == 0.0
    assert "flaky 80%: failures discounted" in score(flaky, NOW).why


@pytest.mark.parametrize(("runs", "new"), [(None, True), (0, True), (2, True), (3, False)])
def test_new_tests_rank_high(runs: int | None, new: bool) -> None:
    risk = score(inputs(runs=runs, failures=None), NOW)
    assert risk.score == (2.0 if new else 0.0)
    assert ("new: little history" in risk.why) is new


def test_the_reason_names_what_scored() -> None:
    risk = score(inputs(link=Link.DIRECT, runs=20, failures=2,
                        last_failed_at=NOW - timedelta(days=2, hours=3)), NOW)  # fmt: skip
    assert risk.why == "direct link; failed 2 days before the latest run; fails 10% of runs"
    assert score(inputs(last_failed_at=NOW - timedelta(hours=3), failures=1), NOW).why == (
        "history only; failed less than a day before the latest run; fails 10% of runs"
    )


def test_ties_go_to_the_shorter_test_then_the_test_id() -> None:
    tests = [
        inputs(test_id="t::slow", avg_duration_ms=900.0),
        inputs(test_id="t::unknown"),
        inputs(test_id="t::fast_b", avg_duration_ms=10.0),
        inputs(test_id="t::fast_a", avg_duration_ms=10.0),
        inputs(test_id="t::risky", link=Link.INDIRECT, avg_duration_ms=5000.0),
    ]
    ranked = rank(tests, NOW)
    assert sorted(ranked, key=lambda t: ranked[t][0]) == [
        "t::risky", "t::fast_a", "t::fast_b", "t::slow", "t::unknown"]  # fmt: skip


def test_the_order_is_deterministic() -> None:
    tests = [inputs(test_id=f"t::{n}", runs=7, failures=n % 3,
                    last_failed_at=NOW - timedelta(hours=n) if n % 3 else None,
                    flaky_score=n / 10) for n in range(10)]  # fmt: skip
    first = rank(tests, NOW)
    assert rank(list(reversed(tests)), NOW) == first
    # Scores are rounded: no platform-dependent last digit.
    assert all(r.score == round(r.score, 3) for _, r in first.values())


# --- the selector: same tests, ordered commands -------------------------------------------


def history(*tests: KnownTest, **kwargs: object) -> RepoHistory:
    return RepoHistory(tests={t.test_id: t for t in tests}, newest_run_at=NOW,
                       **kwargs)  # type: ignore[arg-type]  # fmt: skip


def both(hist: RepoHistory, *changed: str, **kwargs: object) -> tuple[Selection, Selection]:
    """The selection in the default order and in risk order, checked to select the same."""
    config = SelectorConfig(**{k: v for k, v in kwargs.items() if k != "affected"})  # type: ignore[arg-type]
    affected = kwargs.get("affected") or {}
    default = select_tests(hist, changed, True, config, affected_files=affected,  # type: ignore[arg-type]
                           order=Order.DEFAULT)  # fmt: skip
    risky = select_tests(hist, changed, True, config, affected_files=affected,  # type: ignore[arg-type]
                         order=Order.RISK)  # fmt: skip
    assert SelectionScope.of(default) == SelectionScope.of(risky)
    assert default.tests == risky.tests  # same tests, same order, same scores
    assert (default.order, risky.order) == (Order.DEFAULT, Order.RISK)
    return default, risky


ESTABLISHED = {"runs": 50, "failures": 0}


def test_pytest_arguments_come_riskiest_first() -> None:
    hist = history(
        KnownTest("tests.test_cart::test_total", **ESTABLISHED),  # type: ignore[arg-type]
        KnownTest(
            "tests.test_tax::test_rate",
            runs=50,
            failures=10,
            last_failed_at=NOW - timedelta(days=10),
        ),
        KnownTest("tests.test_zip::test_zip", **ESTABLISHED),  # type: ignore[arg-type]
        failed_runs=[
            FailedRun(
                9,
                "f" * 40,
                frozenset({"src/shop/cart.py"}),
                frozenset({"tests.test_tax::test_rate"}),
            )
        ],
        recent_main_failures={},
    )
    # engine.py has no test of its own name: only test_zip.py's import reaches it.
    default, risky = both(
        hist, "src/shop/cart.py", "src/shop/engine.py",
        affected={"tests/test_zip.py": ["src/shop/engine.py"]},
    )  # fmt: skip

    by_id = {t.test_id: t for t in risky.tests}
    # cart: direct (path mapping); zip: indirect (an import, and a whole file); tax: co-change
    # only, failed 10 days ago: 0 + 3 * 0.5**(10/7) + 2 * 0.2 = 1.51. (A day ago it would be
    # 3.12, above zip: recent failures can outrank an indirect link.)
    assert by_id["tests.test_cart::test_total"].score == 6.0
    assert by_id["tests.test_zip::test_zip"].score == 3.0
    assert by_id["tests.test_tax::test_rate"].score == round(3 * 0.5 ** (10 / 7) + 0.4, 3)
    assert [by_id[i].rank for i in ("tests.test_cart::test_total", "tests.test_zip::test_zip",
                                   "tests.test_tax::test_rate")] == [1, 2, 3]  # fmt: skip
    assert default.commands == (
        "pytest tests/test_cart.py::test_total tests/test_tax.py::test_rate tests/test_zip.py",
    )
    assert risky.commands == (
        "pytest tests/test_cart.py::test_total tests/test_zip.py tests/test_tax.py::test_rate",
    )


def test_a_changed_test_file_with_no_known_tests_runs_first() -> None:
    hist = history(KnownTest("tests.test_a::test_a", runs=50, failures=50,
                             last_failed_at=NOW))  # fmt: skip
    _, risky = both(hist, "tests/test_new.py", "tests/test_a.py")
    # The new file has no ranked test: it's the change itself, so it goes first.
    assert risky.commands == ("pytest tests/test_new.py tests/test_a.py",)


def test_go_packages_and_jest_files_come_riskiest_first() -> None:
    go_hist = history(
        KnownTest("example.com/m/a::TestA", **ESTABLISHED),  # type: ignore[arg-type]
        KnownTest("example.com/m/b::TestB", runs=50, failures=25, last_failed_at=NOW),
    )
    default, risky = both(go_hist, "a/x.go", "b/y.go", go_module="example.com/m")
    assert default.commands == ("go test ./a", "go test ./b")
    assert risky.commands == ("go test ./b", "go test ./a")

    js_hist = history(
        KnownTest("cart::adds", "src/cart.test.ts", **ESTABLISHED),  # type: ignore[arg-type]
        KnownTest("tax::rounds", "src/tax.test.ts", runs=50, failures=5, last_failed_at=NOW),
    )
    default, risky = both(js_hist, "src/cart.ts", "src/tax.ts")
    assert default.commands == ("jest src/cart.test.ts src/tax.test.ts",)
    assert risky.commands == ("jest src/tax.test.ts src/cart.test.ts",)


def test_commands_of_several_runners_come_by_their_best_rank() -> None:
    hist = history(
        KnownTest("tests.test_a::test_a", **ESTABLISHED),  # type: ignore[arg-type]
        KnownTest("example.com/m/b::TestB", runs=50, failures=40, last_failed_at=NOW),
    )
    default, risky = both(hist, "src/a.py", "b/y.go", go_module="example.com/m")
    assert default.commands == ("pytest tests/test_a.py::test_a", "go test ./b")
    assert risky.commands == ("go test ./b", "pytest tests/test_a.py::test_a")


def test_full_suite_and_empty_selections_have_nothing_to_order() -> None:
    hist = history(KnownTest("tests.test_a::test_a"))
    full = select_tests(hist, ["pyproject.toml"], True, order=Order.RISK)
    assert full.mode is Mode.FULL and full.commands == ("pytest",) and full.tests == ()
    nothing = select_tests(hist, ["README.md"], True, order=Order.RISK)
    assert nothing.mode is Mode.SELECTIVE and nothing.commands == ()


def test_scores_and_ranks_come_with_the_default_order_too() -> None:
    hist = history(KnownTest("tests.test_a::test_a"), KnownTest("tests.test_b::test_b"))
    default, _ = both(hist, "src/a.py", "src/b.py")
    assert [(t.rank, t.score, t.rank_reason) for t in default.tests] == [
        (1, 8.0, "direct link; new: little history"),
        (2, 8.0, "direct link; new: little history"),
    ]


# --- the evaluated risk-per-time orders --------------------------------------------------------


def test_risk_per_time_puts_a_quick_risky_unit_before_a_slow_riskier_one() -> None:
    ranked = {
        "slow::e2e": Ranked(1, 6.0, Link.INDIRECT, 60_000.0),  # 6 / 60 s
        "quick::unit": Ranked(2, 4.0, Link.DIRECT, 200.0),  # 4 / 0.2 s
    }
    for order, first in ((Order.RISK, "slow::e2e"), (Order.RISK_PER_TIME, "quick::unit"),
                         (Order.RISK_PER_TIME_TIERED, "quick::unit")):  # fmt: skip
        key = unit_key(order, ranked)
        assert min(ranked, key=lambda test_id: key([test_id])) == first, order


def test_the_tiered_variant_keeps_a_direct_link_first() -> None:
    ranked = {
        "slow::direct": Ranked(2, 6.0, Link.DIRECT, 60_000.0),
        "quick::history": Ranked(1, 3.0, Link.HISTORY, 100.0),
    }
    by_time = unit_key(Order.RISK_PER_TIME, ranked)
    tiered = unit_key(Order.RISK_PER_TIME_TIERED, ranked)
    assert min(ranked, key=lambda t: by_time([t])) == "quick::history"
    assert min(ranked, key=lambda t: tiered([t])) == "slow::direct"


def test_the_floor_and_unknown_durations() -> None:
    ranked = {
        "tiny::a": Ranked(2, 5.0, Link.DIRECT, 1.0),  # floored to 100 ms: 0.05 per ms
        "tiny::b": Ranked(1, 6.0, Link.DIRECT, 5.0),  # 0.06 per ms: first, not 1.2 vs 5.0
        "unknown::c": Ranked(3, 6.0, Link.DIRECT, None),  # the median, 5 ms: floored too
        "go::TestX/sub": Ranked(4, 6.0, Link.DIRECT, 0.0),  # inside its parent's time
    }
    key = unit_key(Order.RISK_PER_TIME, ranked)
    assert key(["tiny::a"]) == (-0.05, 2.0)
    assert key(["tiny::b"]) == (-0.06, 1.0)
    assert key(["unknown::c"]) == (-0.06, 3.0)
    assert key(["go::TestX/sub"]) == (-0.06, 4.0)
    assert key([]) == (-math.inf,)  # a changed file with no ranked test: first


@pytest.mark.parametrize("order", list(Order))
def test_no_order_changes_what_is_selected(order: Order) -> None:
    hist = history(
        KnownTest("tests.test_a::test_a", avg_duration_ms=9000.0, **ESTABLISHED),  # type: ignore[arg-type]
        KnownTest(
            "tests.test_b::test_b", avg_duration_ms=5.0, runs=50, failures=20, last_failed_at=NOW
        ),
        KnownTest("example.com/m/c::TestC", avg_duration_ms=30.0),
    )
    changed = ["src/a.py", "src/b.py", "c/x.go"]
    default = select_tests(hist, changed, True, SelectorConfig(go_module="example.com/m"))
    other = select_tests(hist, changed, True, SelectorConfig(go_module="example.com/m"),
                         order=order)  # fmt: skip
    assert SelectionScope.of(other) == SelectionScope.of(default)
    assert other.tests == default.tests
    # The same commands and arguments, only in another order.
    words = [sorted(shlex.split(c)) for c in other.commands]
    assert sorted(words) == sorted(sorted(shlex.split(c)) for c in default.commands)
