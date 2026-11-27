"""Per-variant estimates and budgets (#104): ``test_variant_stats`` (rollup), ``load_history``'s
variant, /select's estimate and time budget for a CI leg, and that without a variant (or with
one that has no stats) every number is what it was."""

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from siftwise.api.main import create_app
from siftwise.config import Settings
from siftwise.core import history
from siftwise.core.ingest import create_run
from siftwise.core.junit import ParsedTestResult, Status
from siftwise.core.risk import Order
from siftwise.core.schemas import RunMetadata
from siftwise.core.selector import (
    KnownTest,
    RepoHistory,
    Selection,
    SelectorConfig,
    VariantHistory,
    VariantTest,
    load_history,
    select_tests,
)
from siftwise.db import get_session

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
REPO = "acme/shop"
T0 = datetime(2026, 11, 1, tzinfo=UTC)

# Python tests: two files everyone runs, one file only Windows runs, one test Linux skips.
CART = "tests.test_cart::test_total"
PRICES = "tests.test_prices::test_price"
WIN = "tests.test_win::test_registry"
SKIP = "tests.test_cart::test_posix_only"


def result(test_id: str, status: Status, ms: int) -> ParsedTestResult:
    classname, name = test_id.split("::")
    return ParsedTestResult(test_id=test_id, classname=classname, name=name, file_path=None,
                            status=status, duration_ms=ms, attempt=1)  # fmt: skip


def upload(
    session: Session, run: int, variant: str | None, results: list[ParsedTestResult],
    *, hour: int, rollup: bool = True, repo: str = REPO,
) -> None:  # fmt: skip
    meta = RunMetadata(repo=repo, commit_sha=f"{run:040x}", branch="main", is_main=True,
                       ci_run_id=str(run), variant=variant,
                       started_at=T0 + timedelta(hours=hour))  # fmt: skip
    create_run(session, meta, results, rollup=rollup)


def linux(cart: int = 100) -> list[ParsedTestResult]:
    return [result(CART, Status.PASSED, cart), result(PRICES, Status.PASSED, 50),
            result(SKIP, Status.SKIPPED, 0)]  # fmt: skip


def windows(cart: int = 300) -> list[ParsedTestResult]:
    return [result(CART, Status.PASSED, cart), result(PRICES, Status.PASSED, 150),
            result(SKIP, Status.PASSED, 20), result(WIN, Status.PASSED, 1000)]  # fmt: skip


@pytest.fixture
def matrix(db_session: Session) -> None:
    """Two CI runs, each with a linux and a windows leg (deferred, then one repo rollup)."""
    for run, hour in ((1, 1), (2, 2)):
        upload(db_session, run, "linux", linux(), hour=hour, rollup=False)
        upload(db_session, run, "windows", windows(), hour=hour, rollup=False)
    repo_id = db_session.scalar(text("SELECT id FROM repos WHERE name = :n"), {"n": REPO})
    history.recompute_repo_stats(db_session, repo_id, 90)
    db_session.commit()


def variant_rows(session: Session, repo: str = REPO) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in session.execute(text(
        "SELECT v.variant, t.test_id, v.runs, v.avg_duration_ms FROM test_variant_stats v "
        "JOIN tests t ON t.id = v.test_key JOIN repos r ON r.id = v.repo_id "
        "WHERE r.name = :repo ORDER BY 1, 2"), {"repo": repo})]  # fmt: skip


# --- the rollup -----------------------------------------------------------------------------


@pytest.mark.usefixtures("matrix")
def test_the_rollup_keeps_numbers_per_named_variant(db_session: Session) -> None:
    assert variant_rows(db_session) == [
        ("linux", SKIP, 0, None),  # skipped in every linux run: no runs, no duration
        ("linux", CART, 2, 100.0),
        ("linux", PRICES, 2, 50.0),
        ("windows", SKIP, 2, 20.0),
        ("windows", CART, 2, 300.0),
        ("windows", PRICES, 2, 150.0),
        ("windows", WIN, 2, 1000.0),
    ]
    # test_stats still has every variant together.
    stats = history.get_test_stats(db_session, REPO, CART)
    assert stats is not None and stats.avg_duration_ms == 200.0


def test_runs_without_a_variant_get_no_rows(db_session: Session) -> None:
    upload(db_session, 1, None, linux(), hour=1)
    upload(db_session, 2, None, windows(), hour=2)
    db_session.commit()
    assert variant_rows(db_session) == []


@pytest.mark.usefixtures("matrix")
def test_a_per_upload_rollup_updates_its_tests_rows(db_session: Session) -> None:
    upload(db_session, 3, "linux", linux(cart=400), hour=3)  # rolls up this run's tests
    db_session.commit()
    rows = {(v, t): (runs, ms) for v, t, runs, ms in variant_rows(db_session)}
    assert rows[("linux", CART)] == (3, 200.0)  # (100 + 100 + 400) / 3
    assert rows[("windows", CART)] == (2, 300.0)  # other variants' rows recomputed, unchanged
    assert rows[("windows", WIN)] == (2, 1000.0)  # not in the upload: untouched


@pytest.mark.usefixtures("matrix")
def test_a_variant_a_test_stopped_running_on_loses_its_row(db_session: Session) -> None:
    """90 days later only linux runs: windows' rows (and the windows-only test) leave the
    window, and every rollup drops them."""
    upload(db_session, 3, "linux", linux(), hour=24 * 91)
    db_session.commit()
    rows = variant_rows(db_session)
    assert {v for v, *_ in rows} == {"linux"}
    assert [(t, runs) for v, t, runs, _ in rows] == [(SKIP, 0), (CART, 1), (PRICES, 1)]


# --- load_history ---------------------------------------------------------------------------


@pytest.mark.usefixtures("matrix")
def test_load_history_reads_the_variant_asked_for(db_session: Session) -> None:
    loaded = load_history(db_session, REPO, variant="linux")
    assert loaded is not None and loaded.variant is not None
    assert loaded.variant.name == "linux"
    assert dict(loaded.variant.tests) == {
        CART: VariantTest(2, 100.0), PRICES: VariantTest(2, 50.0), SKIP: VariantTest(0, None),
    }  # fmt: skip


@pytest.mark.usefixtures("matrix")
@pytest.mark.parametrize("variant", [None, "", "macos"])
def test_load_history_without_a_known_variant_is_unchanged(
    db_session: Session, variant: str | None
) -> None:
    plain = load_history(db_session, REPO)
    loaded = load_history(db_session, REPO, variant=variant)
    assert plain is not None and plain.variant is None
    assert loaded == plain


# --- the estimate and the budget (pure) -----------------------------------------------------


def known(**ms: float | None) -> dict[str, KnownTest]:
    names = {"cart": CART, "prices": PRICES, "win": WIN, "skip": SKIP}
    return {names[k]: KnownTest(names[k], avg_duration_ms=v, runs=4, failures=0)
            for k, v in ms.items()}  # fmt: skip


ALL_VARIANTS = known(cart=200.0, prices=100.0, win=1000.0, skip=20.0)


def on(name: str, **ms: tuple[int, float | None]) -> VariantHistory:
    names = {"cart": CART, "prices": PRICES, "win": WIN, "skip": SKIP}
    return VariantHistory(name, {names[k]: VariantTest(*v) for k, v in ms.items()})


LINUX = on("linux", cart=(2, 100.0), prices=(2, 50.0), skip=(0, None))


def pick(
    tests: Mapping[str, KnownTest], variant: VariantHistory | None, *changed: str,
    known_diff: bool = True, budget_ms: float | None = None,
) -> Selection:  # fmt: skip
    repo = RepoHistory(tests=tests, newest_run_at=T0, variant=variant)
    return select_tests(repo, changed, known_diff, SelectorConfig(), order=Order.RISK,
                        budget_ms=budget_ms)  # fmt: skip


def test_a_full_suite_estimate_counts_only_the_legs_tests_at_its_durations() -> None:
    every = pick(ALL_VARIANTS, None, "pyproject.toml")  # a build file: full suite
    leg = pick(ALL_VARIANTS, LINUX, "pyproject.toml")
    assert every.mode.value == leg.mode.value == "full"
    assert (every.estimated_ms_total, every.estimated_variant) == (1320.0, None)
    # cart 100 + prices 50; the skipped-only test and the windows-only test don't count.
    assert (leg.estimated_ms_total, leg.estimated_variant) == (150.0, "linux")
    assert leg.estimated_ms_would_run == leg.estimated_ms_total  # the full suite runs it all


def test_a_selective_estimate_uses_the_variant_too() -> None:
    leg = pick(ALL_VARIANTS, LINUX, "tests/test_cart.py")
    assert [t.test_id for t in leg.tests] == [SKIP, CART]  # selection: unchanged by a variant
    assert (leg.estimated_ms_total, leg.estimated_ms_would_run) == (150.0, 100.0)


def test_a_test_new_to_the_variant_is_not_in_its_estimate() -> None:
    """WIN is known (it ran on windows) but never ran on linux within the window."""
    leg = pick(ALL_VARIANTS, LINUX, "tests/test_win.py")
    assert [t.test_id for t in leg.tests] == [WIN]  # still selected: selection ignores variants
    assert (leg.estimated_ms_total, leg.estimated_ms_would_run) == (150.0, 0.0)


def test_without_a_variant_the_selection_is_what_it_was() -> None:
    """``variant=None`` is the code path of every request before #104: compare with a
    history built without the field at all."""
    for changed, budget in ((("pyproject.toml",), None), (("tests/test_cart.py",), 250.0),
                            (("pyproject.toml",), 400.0)):  # fmt: skip
        before = select_tests(RepoHistory(tests=ALL_VARIANTS, newest_run_at=T0), changed, True,
                              SelectorConfig(), order=Order.RISK, budget_ms=budget)  # fmt: skip
        assert pick(ALL_VARIANTS, None, *changed, budget_ms=budget) == before


def test_budget_packing_uses_the_variants_durations() -> None:
    """A 300 ms budget on a full-suite fallback: with every variant's means (cart 200, prices
    100, skip 20, win 1000) only part fits; on linux (cart 100, prices 50, skip 0) the files
    that run there fit, and WIN, unseen on linux, costs its all-variant mean (1000)."""
    every = pick(ALL_VARIANTS, None, "pyproject.toml", budget_ms=300)
    leg = pick(ALL_VARIANTS, LINUX, "pyproject.toml", budget_ms=300)
    assert every.budget is not None and leg.budget is not None
    assert every.budget.limited and leg.budget.limited
    assert sorted(every.python_files_run_whole) == ["tests/test_cart.py"]  # 220 of 300
    assert sorted(leg.python_files_run_whole) == ["tests/test_cart.py", "tests/test_prices.py"]
    assert leg.budget.estimated_ms == 150.0  # cart file 100 + 0 (skipped there), prices 50
    # WIN alone is left out, at 1000 ms: the conservative cost.
    assert (leg.budget.left_out, leg.budget.left_out_ms) == (1, 1000.0)


def test_a_variant_seen_test_without_a_duration_costs_the_unknown_estimate() -> None:
    """On the variant it ran (runs > 0) but reported no duration: the unknown-duration cost,
    the 90th percentile of the known durations, like a test without one in every variant."""
    variant = on("linux", cart=(2, 100.0), prices=(2, None))
    leg = pick(known(cart=200.0, prices=100.0), variant, "pyproject.toml", budget_ms=10_000)
    assert leg.budget is not None
    assert leg.budget.unknown_ms == 100.0  # prices: priced at the only known duration


# --- /select --------------------------------------------------------------------------------


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app = create_app(Settings(_env_file=None, api_token=TOKEN))
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client


def ci(variant: str | None, run: str = "99") -> dict[str, Any]:
    return {"provider": "github", "ci_run_id": run, "run_attempt": 1, "variant": variant,
            "commit_sha": "c" * 40, "branch": "feature", "is_main": False}  # fmt: skip


def post(client: TestClient, **body: Any) -> dict[str, Any]:
    response = client.post("/select", headers=AUTH, json={
        "repo": REPO, "changed_files": ["pyproject.toml"], **body})  # fmt: skip
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()
    return data


NOT_THE_NUMBERS = ("recorded", "record_reason")  # what ci adds: it records the selection


def numbers(response: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in response.items() if k not in NOT_THE_NUMBERS}


@pytest.mark.usefixtures("matrix")
def test_select_estimates_a_ci_leg_with_its_variant(client: TestClient) -> None:
    every = post(client)
    on_linux = post(client, ci=ci("linux"))
    on_windows = post(client, ci=ci("windows", run="98"))
    assert every["estimated_ms"] == {"total": 1320.0, "would_run": 1320.0, "variant": None}
    assert on_linux["estimated_ms"] == {"total": 150.0, "would_run": 150.0, "variant": "linux"}
    assert on_windows["estimated_ms"] == {
        "total": 1470.0, "would_run": 1470.0, "variant": "windows"}  # fmt: skip
    # Only the estimate differs: the selection is the same for every leg.
    for response in (on_linux, on_windows):
        assert numbers(response) | {"estimated_ms": None} == numbers(every) | {
            "estimated_ms": None}  # fmt: skip


@pytest.mark.usefixtures("matrix")
def test_a_shadow_runs_variant_counts_too(client: TestClient) -> None:
    shadow = {"ci_run_id": "97", "run_attempt": 1, "variant": "linux", "commit_sha": "d" * 40}
    assert post(client, shadow=shadow)["estimated_ms"]["variant"] == "linux"


@pytest.mark.usefixtures("matrix")
@pytest.mark.parametrize("variant", [None, "macos"], ids=["no-variant", "unknown-variant"])
def test_an_unknown_variant_gets_todays_numbers(client: TestClient, variant: str | None) -> None:
    every = post(client, budget_seconds=1)
    leg = post(client, ci=ci(variant), budget_seconds=1)
    assert numbers(leg) == numbers(every)
    assert leg["estimated_ms"]["variant"] is None


def test_a_repo_without_variants_is_unchanged(client: TestClient, db_session: Session) -> None:
    upload(db_session, 1, None, linux(), hour=1)
    upload(db_session, 2, None, windows(), hour=2)
    db_session.commit()
    every = post(client, budget_seconds=0.2)
    leg = post(client, ci=ci("linux"), budget_seconds=0.2)
    assert numbers(leg) == numbers(every)
    assert every["budget_limited"] is True  # the budget did something: the same thing


@pytest.mark.usefixtures("matrix")
def test_a_budget_for_a_leg_packs_with_its_durations(client: TestClient) -> None:
    every = post(client, budget_seconds=0.3)
    on_linux = post(client, ci=ci("linux"), budget_seconds=0.3)
    assert every["budget"]["estimated_ms"] != on_linux["budget"]["estimated_ms"]
    assert on_linux["budget"]["estimated_ms"] == 150.0
