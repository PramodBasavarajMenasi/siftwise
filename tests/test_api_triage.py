from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from siftwise.api.main import create_app
from siftwise.config import Settings
from siftwise.core.schemas import ChangeGraph
from siftwise.db import get_session
from tests.test_triage_db import OTHER, REPO, TEST, F, P, fail_then_pass, record, sha

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app = create_app(Settings(_env_file=None, api_token=TOKEN, stats_window_days=30))
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seeded(db_session: Session) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: P, OTHER: P, "t::broken": P})
    record(db_session, commit=2, hour=2, tests={TEST: P, OTHER: P, "t::broken": F})
    record(db_session, commit=3, hour=3, tests={TEST: P, OTHER: P, "t::broken": F})
    fail_then_pass(db_session, 10, 10)


def post_triage(client: TestClient, **body: Any) -> httpx2.Response:
    response: httpx2.Response = client.post(
        "/triage",
        json={"repo": REPO, "commit_sha": sha(99).upper(), "variant": "linux", **body},
        headers=AUTH,
    )
    return response


@pytest.mark.usefixtures("seeded")
def test_triage_response(client: TestClient) -> None:
    response = post_triage(
        client,
        changed_files=["src/db.py"],
        failures=[
            {"test_id": OTHER, "message": "boom"},
            {"test_id": "t::broken", "message": "AssertionError: assert 1 == 2"},
            {"test_id": TEST},
            {"test_id": "tests.test_new::test_it"},
        ],
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["commit_sha"], body["variant"], body["run_id"], body["window_days"]) == (
        sha(99), "linux", None, 30)  # fmt: skip
    assert body["counts"] == {"new_regression": 1, "pre_existing": 1, "flaky": 1, "unknown": 1}
    regression, broken, flaky, new = body["results"]

    assert (regression["verdict"], regression["confidence"]) == ("new_regression", "high")
    assert regression["evidence"]["links"] == [{
        "signal": "path mapping", "changed": "src/db.py",
        "reason": "tests/test_db.py tests changed src/db.py", "strength": "high",
    }]  # fmt: skip
    assert regression["evidence"]["history"]["last_main_pass"]["commit_sha"] == sha(3)

    assert broken["verdict"] == "pre_existing"
    assert broken["evidence"]["pre_existing"] == {
        "variant": "linux",
        "since_sha": sha(2),
        "since_run": broken["evidence"]["pre_existing"]["since_run"],
        "main_runs_failing": 2,
        "last_main_pass_sha": sha(1),
        "message_matches": True,
    }
    assert broken["evidence"]["pre_existing"]["since_run"]["branch"] == "main"

    assert flaky["verdict"] == "flaky"
    [event] = flaky["evidence"]["flaky"]
    assert event["kind"] == "unrelated_fail_then_pass"
    assert [r["commit_sha"] for r in event["passed"]] == [sha(11)]
    assert flaky["evidence"]["history"]["flaky_counts"] == {
        "same_commit": 0, "unrelated_fail_then_pass": 1}  # fmt: skip

    assert (new["verdict"], new["evidence"]["history"]["seen_before"]) == ("unknown", False)


@pytest.mark.usefixtures("seeded")
def test_triage_applies_known_flaky(client: TestClient) -> None:
    body = {"changed_files": ["src/unrelated.py"], "failures": [{"test_id": OTHER}]}

    plain = post_triage(client, **body).json()["results"][0]
    listed = post_triage(client, **body, known_flaky=["tests/test_db.py", "tests.test_db::*"])
    result = listed.json()["results"][0]

    assert (plain["verdict"], plain["known_flaky"]) == ("unknown", None)
    assert (result["verdict"], result["confidence"]) == ("flaky", "medium")
    assert result["known_flaky"] == "tests.test_db::*"
    assert result["reasons"][0] == "listed in known_flaky ('tests.test_db::*')"


@pytest.mark.usefixtures("seeded")
def test_recorded_run_sets_the_cut_off(client: TestClient, db_session: Session) -> None:
    run = record(db_session, commit=99, hour=50, tests={TEST: F}, branch="pr", ci_run_id="5",
                 graph=ChangeGraph())  # fmt: skip

    body = post_triage(client, ci_run_id="5", failures=[{"test_id": TEST}]).json()

    assert body["run_id"] == run.id
    assert body["as_of"].startswith("2026-10-03T02:00:00")


@pytest.mark.parametrize(
    "body",
    [
        {"failures": []},
        {"failures": [{"test_id": TEST}], "commit_sha": "abc123"},
        {"failures": [{"test_id": TEST}], "surprise": True},
        {"failures": [{"test_id": TEST}] * 5001},
    ],
)
def test_invalid_requests(client: TestClient, body: dict[str, Any]) -> None:
    assert post_triage(client, **body).status_code == 422


def test_unknown_repo_and_auth(client: TestClient) -> None:
    failures = [{"test_id": TEST}]
    assert post_triage(client, failures=failures).status_code == 404
    unauthorized = client.post(
        "/triage", json={"repo": REPO, "commit_sha": sha(1), "failures": failures}
    )
    assert unauthorized.status_code == 401


@pytest.mark.usefixtures("seeded")
def test_long_messages_are_truncated_not_rejected(client: TestClient) -> None:
    response = post_triage(client, failures=[{"test_id": TEST, "message": "x" * 100_000}])
    assert response.status_code == 200, response.text


# --- POST /repos/{repo}/triage ------------------------------------------------------------


def post_run_triage(client: TestClient, ci_run_id: str, **body: Any) -> httpx2.Response:
    response: httpx2.Response = client.post(
        f"/repos/{REPO}/triage", json={"ci_run_id": ci_run_id, **body}, headers=AUTH
    )
    return response


def record_matrix(session: Session) -> None:
    """Main history for two variants, then CI run "77": TEST fails on both legs (its source
    file changed), OTHER on Linux only (unrelated), t::broken on Linux (already broken on
    main there)."""
    for variant in ("linux", "macos"):
        record(session, commit=1, hour=1, variant=variant,
               tests={TEST: P, OTHER: P, "t::broken": P})  # fmt: skip
        record(session, commit=2, hour=2, variant=variant,
               tests={TEST: P, OTHER: P, "t::broken": F if variant == "linux" else P})  # fmt: skip
    record(session, commit=50, hour=5, branch="pr", ci_run_id="77", changed=["src/cart.py"],
           tests={TEST: F, OTHER: F, "t::broken": F})  # fmt: skip
    record(session, commit=50, hour=5, branch="pr", ci_run_id="77", changed=["src/cart.py"],
           variant="macos", tests={TEST: F, OTHER: P, "t::broken": P})  # fmt: skip


def test_run_triage_covers_every_variant(client: TestClient, db_session: Session) -> None:
    record_matrix(db_session)

    response = post_run_triage(client, "77")

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["ci_run_id"], body["run_attempt"], body["window_days"]) == ("77", 1, 30)
    assert [(v["variant"], v["failing"], v["links_known"]) for v in body["variants"]] == [
        ("linux", 3, True), ("macos", 1, True)]  # fmt: skip
    assert body["variants"][0]["counts"] == {
        "new_regression": 1, "unknown": 1, "flaky": 0, "pre_existing": 1}  # fmt: skip
    # Distinct tests, each under its worst verdict; worst first, a test's variants together.
    assert (body["failing_tests"], body["truncated"]) == (3, False)
    assert body["counts"] == {"new_regression": 1, "unknown": 1, "flaky": 0, "pre_existing": 1}
    assert [(r["test_id"], r["variant"], r["verdict"]) for r in body["results"]] == [
        (TEST, "linux", "new_regression"),
        (TEST, "macos", "new_regression"),
        (OTHER, "linux", "unknown"),
        ("t::broken", "linux", "pre_existing"),
    ]
    assert body["results"][0]["evidence"]["links"][0]["changed"] == "src/cart.py"


def test_run_triage_limit_cuts_whole_tests_and_keeps_true_counts(
    client: TestClient, db_session: Session
) -> None:
    record_matrix(db_session)

    cut = post_run_triage(client, "77", limit=3).json()
    tiny = post_run_triage(client, "77", limit=1).json()

    # TEST (2 rows) and OTHER (1) fit; t::broken would be a 4th row.
    assert [(r["test_id"], r["variant"]) for r in cut["results"]] == [
        (TEST, "linux"), (TEST, "macos"), (OTHER, "linux")]  # fmt: skip
    assert cut["truncated"] is True
    assert (cut["failing_tests"], cut["counts"]["pre_existing"]) == (3, 1)
    # A test with more variants than the limit: its first rows, never nothing.
    assert [(r["test_id"], r["variant"]) for r in tiny["results"]] == [(TEST, "linux")]
    assert tiny["counts"] == cut["counts"]


def test_run_triage_of_some_variants(client: TestClient, db_session: Session) -> None:
    record_matrix(db_session)
    record(db_session, commit=50, hour=5.5, branch="pr", ci_run_id="77", rerun=1,
           changed=["src/cart.py"], tests={OTHER: P})  # fmt: skip

    macos = post_run_triage(client, "77", variants=["macos"]).json()
    linux = post_run_triage(client, "77", variants=["linux"]).json()
    missing = post_run_triage(client, "77", variants=["windows"])

    assert [v["variant"] for v in macos["variants"]] == ["macos"]
    assert {r["variant"] for r in macos["results"]} == {"macos"}
    [variant] = linux["variants"]
    assert (variant["reruns"], variant["known_flaky_changed"]) == (1, False)
    other = next(r for r in linux["results"] if r["test_id"] == OTHER)
    assert (other["verdict"], other["evidence"]["history"]["rerun_passes"]) == ("flaky", [1])
    assert missing.status_code == 404
    assert missing.json()["detail"] == (
        "no runs recorded for CI run 77 attempt 1 (variants ['windows'])"
    )


def test_run_triage_reports_a_changed_known_flaky(client: TestClient, db_session: Session) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: P})
    record(db_session, commit=2, hour=2, branch="pr", ci_run_id="81", tests={TEST: F},
           known_flaky=["tests.test_cart::*"], known_flaky_changed=True)  # fmt: skip

    body = post_run_triage(client, "81").json()

    assert body["variants"][0]["known_flaky_changed"] is True
    [listed] = body["results"]
    assert (listed["verdict"], listed["known_flaky"]) == ("flaky", "tests.test_cart::*")


def test_run_triage_without_failures(client: TestClient, db_session: Session) -> None:
    record(db_session, commit=1, hour=1, ci_run_id="80", tests={TEST: P})

    body = post_run_triage(client, "80").json()

    assert (body["failing_tests"], body["results"], body["truncated"]) == (0, [], False)
    [variant] = body["variants"]
    assert (variant["failing"], variant["as_of"]) == (0, None)


@pytest.mark.parametrize(
    "body",
    [{"limit": 0}, {"limit": 2001}, {"run_attempt": 0}, {"ci_run_id": ""}, {"surprise": 1}],
)
def test_run_triage_invalid_requests(client: TestClient, body: dict[str, Any]) -> None:
    assert post_run_triage(client, **{"ci_run_id": "77", **body}).status_code == 422


def test_run_triage_unknown_repo_run_and_auth(client: TestClient, db_session: Session) -> None:
    assert post_run_triage(client, "77").json()["detail"] == f"unknown repo {REPO!r}"
    record(db_session, commit=1, hour=1, ci_run_id="77", tests={TEST: P})

    missing = post_run_triage(client, "77", run_attempt=2)

    assert missing.status_code == 404
    assert missing.json()["detail"] == "no runs recorded for CI run 77 attempt 2"
    assert client.post(f"/repos/{REPO}/triage", json={"ci_run_id": "77"}).status_code == 401
