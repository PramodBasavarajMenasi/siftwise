from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from siftwise.api.main import create_app
from siftwise.config import Settings
from siftwise.core.ingest import create_run
from siftwise.core.junit import ParsedTestResult, Status
from siftwise.core.schemas import RunMetadata
from siftwise.db import get_session

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
REPO = "acme/shop"
GO_PKG = "github.com/acme/shop/cart"


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app = create_app(Settings(_env_file=None, api_token=TOKEN))
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seeded(db_session: Session) -> None:
    """Two passing runs of a small Python + Go repo."""
    tests = [
        "tests.test_cart::test_total",
        "tests.test_cart.TestDiscount::test_applies",
        "tests.test_db::test_query",
        f"{GO_PKG}::TestAdd",
        f"{GO_PKG}::TestAdd/with_discount",
    ]
    for n in (1, 2):
        results = [
            ParsedTestResult(
                test_id=test_id,
                classname=test_id.split("::")[0],
                name=test_id.split("::")[1],
                file_path=None,
                status=Status.PASSED,
                duration_ms=1,
                attempt=1,
            )
            for test_id in tests
        ]
        meta = RunMetadata(
            repo=REPO,
            commit_sha=f"{n:040x}",
            branch="main",
            is_main=True,
            started_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=n),
        )
        create_run(db_session, meta, results)


def post_select(
    client: TestClient, headers: dict[str, str] | None = None, **body: Any
) -> httpx2.Response:
    response: httpx2.Response = client.post(
        "/select", json={"repo": REPO, **body}, headers=AUTH if headers is None else headers
    )
    return response


@pytest.mark.usefixtures("seeded")
def test_selective_response(client: TestClient) -> None:
    response = post_select(
        client, changed_files=["src/shop/cart.py", "cart/total.go"], order="default"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["repo"] == REPO
    assert body["order"] == "default"
    assert body["mode"] == "selective"
    assert body["reason"] == "4 of 5 known tests selected"
    assert (body["selected_count"], body["total_known"]) == (4, 5)
    assert body["commands"] == [
        "pytest tests/test_cart.py::TestDiscount::test_applies tests/test_cart.py::test_total",
        f"go test {GO_PKG}",  # changed .go file: the whole package runs
    ]
    assert body["command"] == " && ".join(body["commands"])
    by_id = {t["test_id"]: t for t in body["tests"]}
    assert set(by_id) == {
        "tests.test_cart::test_total",
        "tests.test_cart.TestDiscount::test_applies",
        f"{GO_PKG}::TestAdd",
        f"{GO_PKG}::TestAdd/with_discount",
    }
    assert by_id["tests.test_cart::test_total"] == {
        "test_id": "tests.test_cart::test_total",
        "reasons": ["tests/test_cart.py tests changed src/shop/cart.py"],
        "reason": "tests/test_cart.py tests changed src/shop/cart.py",
        "runner": "pytest",
        "file_path": None,
        # Added with risk ordering: old clients ignore them. Four direct, new tests tie at
        # 8.0; equal durations, so by test ID ("g..." < "tests.test_cart.T..." < "...::").
        "score": 8.0,
        "rank": 4,
        "rank_reason": "direct link; new: little history",
    }
    assert by_id[f"{GO_PKG}::TestAdd"]["runner"] == "go"


@pytest.mark.usefixtures("seeded")
def test_affected_packages_and_go_module(client: TestClient) -> None:
    # pricing has no tests; cart imports it (per go list), so cart's tests run whole.
    body = post_select(
        client,
        changed_files=["pricing/rules.go"],
        affected_packages={GO_PKG: ["github.com/acme/shop/pricing"]},
        go_module="github.com/acme/shop",
    ).json()

    assert body["mode"] == "selective"
    assert body["commands"] == ["go test ./cart"]
    assert body["go_packages_run_whole"] == [GO_PKG]
    assert {t["reason"] for t in body["tests"]} == {
        "imports changed package github.com/acme/shop/pricing"
    }


@pytest.mark.usefixtures("seeded")
def test_affected_files_and_deleted_files(client: TestClient) -> None:
    # money.py has no test_money.py; test_db.py imports it (per the CLI's import graph).
    body = post_select(
        client,
        changed_files=["src/shop/money.py", "tests/test_cart.py"],
        affected_files={"tests/test_db.py": ["src/shop/money.py"]},
        deleted_files=["tests/test_cart.py"],
    ).json()

    assert body["mode"] == "selective", body["reason"]
    assert body["commands"] == ["pytest tests/test_db.py"]  # deleted test_cart.py: not named
    assert body["python_files_run_whole"] == ["tests/test_db.py"]
    assert [(t["test_id"], t["reason"]) for t in body["tests"]] == [
        ("tests.test_db::test_query", "imports changed module src/shop/money.py")
    ]


@pytest.mark.usefixtures("seeded")
def test_declared_dependencies_and_always_run(client: TestClient) -> None:
    body = post_select(
        client,
        changed_files=["src/shop/payments.py"],
        depends=[{"tests": "tests/test_db.py", "on": ["src/shop/**"]}],
        always_run=["tests.test_cart::test_total"],
    ).json()

    assert body["mode"] == "selective"
    assert {t["test_id"]: t["reason"] for t in body["tests"]} == {
        "tests.test_db::test_query": (
            "declared dependency: tests/test_db.py on src/shop/payments.py"
        ),
        "tests.test_cart::test_total": "always-run pattern 'tests.test_cart::test_total'",
    }


@pytest.mark.usefixtures("seeded")
def test_docs_only_change_selects_nothing(client: TestClient) -> None:
    body = post_select(client, changed_files=["README.md", "docs/setup.md"]).json()

    assert body["mode"] == "selective"
    assert body["reason"] == "no tests affected"
    assert (body["selected_count"], body["total_known"]) == (0, 5)
    assert (body["command"], body["commands"], body["tests"]) == ("", [], [])


@pytest.mark.usefixtures("seeded")
@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"changed_files": ["src/shop/cart.py"], "changed_files_known": False},
         "changed files are unknown for this change"),
        ({}, "this PR has no net changes"),
        ({"changed_files": ["go.mod"]}, "build/config file changed: go.mod"),
        ({"changed_files": ["src/shop/util.py"]},
         "changed file maps to no known tests: src/shop/util.py"),
    ],
    ids=["unknown-diff", "empty", "build-file", "unmapped"],
)  # fmt: skip
def test_full_suite_fallbacks(client: TestClient, body: dict[str, Any], reason: str) -> None:
    result = post_select(client, **body).json()

    assert result["mode"] == "full"
    assert result["reason"] == reason
    assert (result["selected_count"], result["total_known"]) == (5, 5)
    assert result["command"] == "pytest && go test ./..."
    assert result["tests"] == []


@pytest.mark.usefixtures("seeded")
def test_changed_files_known_defaults_to_true(client: TestClient) -> None:
    assert post_select(client, changed_files=["src/shop/cart.py"]).json()["mode"] == "selective"


def test_unknown_repo_returns_404(client: TestClient) -> None:
    response = post_select(client, repo="nobody/nothing", changed_files=["a.py"])

    assert response.status_code == 404
    assert response.json()["detail"] == "unknown repo 'nobody/nothing'"


@pytest.mark.usefixtures("seeded")
def test_select_requires_token(client: TestClient) -> None:
    assert post_select(client, headers={}, changed_files=["a.py"]).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert post_select(client, headers=wrong, changed_files=["a.py"]).status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {"repo": ""},
        {"repo": REPO, "changed_files": "src/a.py"},  # a string, not a list
        {"repo": REPO, "changed_files_known": "maybe"},
        {"repo": REPO, "unexpected": 1},
        {"repo": REPO, "changed_files": ["x" * 4097]},
        {"repo": REPO, "depends": [{"tests": "t/**", "on": []}]},
        {"repo": REPO, "depends": [{"tests": "t/**"}]},
        {"repo": REPO, "depends": [{"tests": "t/**", "on": ["**"], "extra": 1}]},
        {"repo": REPO, "affected_packages": {"a": "b"}},
        {"repo": REPO, "always_run": [""]},
    ],
    ids=[
        "empty-repo",
        "files-not-list",
        "known-not-bool",
        "extra-field",
        "path-too-long",
        "depends-empty-on",
        "depends-missing-on",
        "depends-extra-field",
        "affected-not-list",
        "always-run-empty-glob",
    ],
)
def test_invalid_body_returns_422(client: TestClient, body: dict[str, Any]) -> None:
    response = client.post("/select", json=body, headers=AUTH)
    assert response.status_code == 422


def test_select_is_post_only(client: TestClient) -> None:
    response = client.get("/select", params={"repo": REPO}, headers=AUTH)
    assert response.status_code == 405


@pytest.mark.usefixtures("seeded")
def test_estimated_time_and_junit_dir(client: TestClient) -> None:
    body = post_select(
        client,
        changed_files=["src/shop/cart.py", "cart/total.go"],
        junit_dir="reports",
        order="default",
    ).json()

    # 1 ms each; the Go subtest's time is inside TestAdd's. test_db::test_query doesn't run.
    assert body["estimated_ms"] == {"total": 4.0, "would_run": 3.0}
    assert body["commands"][0].startswith("pytest --junitxml=reports/siftwise-1.xml ")


# --- risk order -----------------------------------------------------------------------------


@pytest.mark.usefixtures("seeded")
def test_risk_order_is_the_default_and_selects_the_same_tests(client: TestClient) -> None:
    changed = ["src/shop/cart.py", "cart/total.go"]
    risky = post_select(client, changed_files=changed).json()
    default = post_select(client, changed_files=changed, order="default").json()

    assert (risky["order"], default["order"]) == ("risk", "default")
    assert risky["tests"] == default["tests"]  # same tests, scores and ranks
    assert sorted(risky["commands"]) == sorted(default["commands"])
    assert sorted(t["rank"] for t in risky["tests"]) == [1, 2, 3, 4]
    # The Go test ranks first (same score, alphabetical tie-break), so its command leads.
    assert risky["commands"][0] == f"go test {GO_PKG}"


@pytest.mark.usefixtures("seeded")
def test_import_depth_changes_risk_order_only(client: TestClient) -> None:
    body = {
        "changed_files": ["src/shop/money.py"],
        "affected_files": {
            "tests/test_cart.py": ["src/shop/money.py"],
            "tests/test_db.py": ["src/shop/money.py"],
        },
    }
    old = post_select(client, **body).json()
    new = post_select(
        client,
        **body,
        import_depths={"affected_files": {"tests/test_cart.py": 2, "tests/test_db.py": 1}},
    ).json()

    assert {t["test_id"] for t in old["tests"]} == {t["test_id"] for t in new["tests"]}
    old_by_id = {t["test_id"]: t for t in old["tests"]}
    new_by_id = {t["test_id"]: t for t in new["tests"]}
    assert old_by_id["tests.test_cart::test_total"]["rank_reason"].startswith("indirect link")
    assert old_by_id["tests.test_db::test_query"]["rank_reason"].startswith("indirect link")
    assert new_by_id["tests.test_cart::test_total"]["rank_reason"].startswith("indirect link")
    assert new_by_id["tests.test_db::test_query"]["rank_reason"].startswith("direct link")
    assert old["commands"] == [
        "pytest tests/test_cart.py::TestDiscount::test_applies tests/test_cart.py::test_total "
        "tests/test_db.py::test_query"
    ]
    assert new["commands"] == [
        "pytest tests/test_db.py::test_query tests/test_cart.py::TestDiscount::test_applies "
        "tests/test_cart.py::test_total"
    ]


@pytest.mark.usefixtures("seeded")
def test_an_old_client_still_works(client: TestClient) -> None:
    """A body from before risk ordering (no order) is accepted; the new response fields are
    additions: every field an old client reads is still there."""
    body = post_select(client, changed_files=["src/shop/cart.py"]).json()
    old_fields = {"repo", "mode", "reason", "selected_count", "total_known", "command",
                  "commands", "go_packages_run_whole", "estimated_ms", "tests"}  # fmt: skip
    assert old_fields <= set(body)
    assert {"test_id", "reasons", "reason", "runner", "file_path"} <= set(body["tests"][0])


@pytest.mark.usefixtures("seeded")
def test_an_unknown_order_is_rejected(client: TestClient) -> None:
    assert (
        post_select(client, changed_files=["src/shop/cart.py"], order="fastest").status_code == 422
    )


# --- time budget ----------------------------------------------------------------------------


@pytest.mark.usefixtures("seeded")
def test_without_a_budget_the_response_says_none(client: TestClient) -> None:
    body = post_select(client, changed_files=["src/shop/cart.py"]).json()
    assert (body["budget_limited"], body["budget"]) == (False, None)


@pytest.mark.usefixtures("seeded")
def test_a_budget_limited_selection_says_what_it_left_out(client: TestClient) -> None:
    changed = ["src/shop/cart.py", "cart/total.go"]
    whole = post_select(client, changed_files=changed).json()
    # Every test takes 1 ms: a 1 ms budget runs the riskiest unit only, the Go package (its
    # test and subtest; the subtest's time is inside its parent's).
    body = post_select(client, changed_files=changed, budget_seconds=0.001).json()
    assert body["mode"] == "selective" and body["budget_limited"] is True
    assert body["reason"].startswith(
        "budget-limited (estimated 0s of a 0s target): 2 of 4 selected tests run; 2 left out"
    )
    assert body["commands"] == ["go test github.com/acme/shop/cart"]
    assert body["selected_count"] == 2
    budget = body["budget"]
    assert {k: budget[k] for k in ("seconds", "applied", "limited", "full_suite", "note",
                                   "estimated_ms", "left_out_ms", "unknown_test_ms")} == {
        "seconds": 0.001, "applied": True, "limited": True, "full_suite": False, "note": "",
        "estimated_ms": 1.0, "left_out_ms": 2.0, "unknown_test_ms": 1.0,
    }  # fmt: skip
    assert budget["left_out"] == 2
    assert 0 < budget["left_out_risk_share"] < 1
    kept = {t["test_id"] for t in body["tests"]}
    assert kept < {t["test_id"] for t in whole["tests"]}


@pytest.mark.usefixtures("seeded")
def test_a_budget_on_a_full_suite_fallback_packs_the_full_suite(client: TestClient) -> None:
    body = post_select(client, changed_files_known=False, budget_seconds=0.001).json()
    assert body["mode"] == "selective" and body["budget_limited"] is True
    assert body["budget"]["full_suite"] is True
    assert body["reason"].startswith(
        "budget-limited full suite (estimated 0s of a 0s target; siftwise would run the full "
        "suite: changed files are unknown for this change)"
    )
    roomy = post_select(client, changed_files_known=False, budget_seconds=60).json()
    assert roomy["mode"] == "full" and roomy["budget_limited"] is False
    assert roomy["budget"]["full_suite"] is True and roomy["budget"]["limited"] is False


@pytest.mark.usefixtures("seeded")
@pytest.mark.parametrize("seconds", [0, -5, 8 * 24 * 3600, "5m"])
def test_an_invalid_budget_is_rejected(client: TestClient, seconds: object) -> None:
    response = post_select(client, changed_files=["src/shop/cart.py"], budget_seconds=seconds)
    assert response.status_code == 422


@pytest.mark.usefixtures("seeded")
def test_an_unknown_field_is_a_422_naming_it(client: TestClient) -> None:
    """What a server from before time budgets answers to budget_seconds: the clients recognise
    this shape (tests.test_gha.OLD_SERVER_BUDGET_422) and fail open."""
    response = post_select(client, changed_files=["src/shop/cart.py"], budget_seconds_v9=300)
    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert (error["type"], error["loc"]) == ("extra_forbidden", ["body", "budget_seconds_v9"])


# --- JS/TS import graph -----------------------------------------------------------------------


def test_js_graph_details_reach_the_reasons(client: TestClient, db_session: Session) -> None:
    result = ParsedTestResult(test_id="src/sum.test.ts::adds", classname="src/sum.test.ts",
                              name="adds", file_path="src/sum.test.ts", status=Status.PASSED,
                              duration_ms=3, attempt=1)  # fmt: skip
    create_run(db_session, RunMetadata(repo="acme/web", commit_sha="a" * 40, branch="main",
                                       is_main=True), [result])  # fmt: skip
    body = {"repo": "acme/web", "changed_files": ["src/money.ts"],
            "affected_files": {"src/sum.test.ts": ["src/money.ts"]}}  # fmt: skip
    old_client = client.post("/select", json=body, headers=AUTH).json()
    assert old_client["mode"] == "selective"
    assert old_client["tests"][0]["reasons"] == ["imports changed module src/money.ts"]
    js_graph = {"nearest": {"src/sum.test.ts": {"module": "src/money.ts", "via": "src/sum.ts",
                                                "depth": 2}},
                "dynamic": {}, "notes": ["jest.config.ts can't be read without Node"]}  # fmt: skip
    new = client.post("/select", json={**body, "js_graph": js_graph}, headers=AUTH).json()
    assert new["tests"][0]["reasons"] == [
        "imports changed module src/money.ts (via src/sum.ts); jest.config.ts can't be read "
        "without Node"
    ]
    bad = {**js_graph, "nearest": {"src/sum.test.ts": {"module": "m", "depth": 0}}}
    assert client.post("/select", json={**body, "js_graph": bad}, headers=AUTH).status_code == 422
    bad_depth = {
        **body,
        "import_depths": {"affected_files": {"src/sum.test.ts": 0}},
    }
    assert client.post("/select", json=bad_depth, headers=AUTH).status_code == 422
