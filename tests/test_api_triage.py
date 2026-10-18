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
