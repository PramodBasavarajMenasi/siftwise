from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from siftwise.api.main import create_app
from siftwise.config import Settings
from siftwise.core.triage import Change, Failure, Target, Verdict, triage_run
from siftwise.db import get_session
from tests.test_triage_db import (
    OTHER,
    REPO,
    TEST,
    F,
    P,
    fail_then_pass,
    record,
    sha,
    target,
)

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app = create_app(Settings(_env_file=None, api_token=TOKEN))
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seeded(db_session: Session) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: P, OTHER: P}, variant="macos")
    record(db_session, commit=1, hour=1, tests={TEST: P, OTHER: P})
    record(db_session, commit=2, hour=2, tests={TEST: P, OTHER: F}, variant="macos")
    record(db_session, commit=2, hour=2, tests={TEST: P, OTHER: P})
    record(db_session, commit=3, hour=3, tests={TEST: [F, P]}, variant="macos", branch="b")
    fail_then_pass(db_session, 10, 10)
    client_rollup = create_app(Settings(_env_file=None, api_token=TOKEN))
    client_rollup.dependency_overrides[get_session] = lambda: db_session
    with TestClient(client_rollup) as c:
        assert c.post(f"/repos/{REPO}/rollup", headers=AUTH).status_code == 200


def explain(client: TestClient, **body: Any) -> httpx2.Response:
    response: httpx2.Response = client.post(
        "/explain", json={"repo": REPO, "test_id": TEST, **body}, headers=AUTH
    )
    return response


@pytest.mark.usefixtures("seeded")
def test_explain_a_selected_test(client: TestClient) -> None:
    response = explain(client, changed_files=["src/cart.py"], variant="linux")

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["known"], body["window_days"]) == (True, 90)
    assert body["stats"]["runs"] > 0
    assert body["selection"]["mode"] == "selective"
    assert body["selection"]["selected"] is True
    assert body["selection"]["reasons"] == ["tests/test_cart.py tests changed src/cart.py"]
    assert [link["signal"] for link in body["selection"]["links"]] == ["path mapping"]
    # newest first, one entry per run with its final attempt
    assert body["recent"][0]["commit_sha"] == sha(11)
    retried = next(r for r in body["recent"] if r["commit_sha"] == sha(3))
    assert (retried["status"], retried["attempts"]) == ("passed", 2)
    assert {r["status"] for r in body["recent"]} <= {"passed", "failed"}
    # flaky evidence as triage counts it: the macOS retry and the feature branch's fail-then-pass
    assert body["flaky_counts"] == {"same_commit": 1, "unrelated_fail_then_pass": 1}
    assert {e["kind"] for e in body["flaky"]} == {"same_commit", "unrelated_fail_then_pass"}


@pytest.mark.usefixtures("seeded")
def test_explain_says_whether_known_flaky_lists_the_test(client: TestClient) -> None:
    listed = explain(client, known_flaky=["tests/nope/**", "tests.test_cart::*"]).json()
    plain = explain(client).json()

    assert (listed["known_flaky"], plain["known_flaky"]) == ("tests.test_cart::*", None)


@pytest.mark.usefixtures("seeded")
def test_explain_not_selected_and_full_suite(client: TestClient) -> None:
    not_selected = explain(client, changed_files=["src/db.py"]).json()["selection"]
    full = explain(client, changed_files=["pyproject.toml"]).json()["selection"]
    unknown_diff = explain(client, changed_files_known=False).json()["selection"]

    assert (not_selected["selected"], not_selected["reasons"], not_selected["links"]) == (
        False, [], [])  # fmt: skip
    assert full["mode"] == "full" and full["selected"] is True
    assert full["reasons"] == ["the full suite runs: build/config file changed: pyproject.toml"]
    assert full["links"][0]["strength"] == "low"
    assert unknown_diff["selected"] is True and unknown_diff["links"] == []


@pytest.mark.usefixtures("seeded")
def test_explain_broken_on_main_and_unknown_tests(client: TestClient) -> None:
    other = explain(client, test_id=OTHER).json()
    new = explain(client, test_id="tests.test_new::test_it", changed_files=["tests/test_new.py"])

    assert other["stats"]["broken_on_main_variants"] == [{"variant": "macos", "since_sha": sha(2)}]
    body = new.json()
    assert (body["known"], body["stats"], body["recent"]) == (False, None, [])
    assert body["selection"]["selected"] is True  # its changed test file runs whole


def test_explain_unknown_repo(client: TestClient) -> None:
    assert explain(client).status_code == 404


@pytest.mark.usefixtures("seeded")
def test_triage_without_a_commit_never_uses_same_commit_passes(db_session: Session) -> None:
    # The same commit passed before: flaky for that commit, but not for uncommitted code.
    record(db_session, commit=99, hour=30, tests={TEST: P}, branch="pr")
    failures = [Failure(TEST)]
    change = Change(("docs/x.txt",), True)

    committed = triage_run(db_session, target(99), failures, change)
    dirty = triage_run(db_session, Target(REPO, None, "linux"), failures, change)

    assert committed is not None and dirty is not None
    assert committed.results[0].history.same_commit_passes != ()
    assert committed.results[0].reasons[0].startswith("passed on this commit")
    assert dirty.results[0].history.same_commit_passes == ()
    # flaky from its other evidence instead (a retry on branch b and a fail-then-pass)
    assert dirty.results[0].verdict is Verdict.FLAKY
    assert dirty.results[0].reasons[0].startswith("flaky 2 times")


def test_triage_request_accepts_a_null_commit(client: TestClient, db_session: Session) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: P})
    body = {"repo": REPO, "commit_sha": None, "failures": [{"test_id": TEST}]}

    response = client.post("/triage", json=body, headers=AUTH)

    assert response.status_code == 200, response.text
    assert response.json()["commit_sha"] is None
    missing = client.post("/triage", json={"repo": REPO, "failures": [{"test_id": TEST}]},
                          headers=AUTH)  # fmt: skip
    assert missing.status_code == 422  # explicit: a commit, or null for uncommitted code
