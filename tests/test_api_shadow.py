"""Shadow mode over the API: /select stores, /repos/{repo}/shadow compares, /recall reports."""

from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from siftwise.api.main import create_app
from siftwise.api.routes import select as select_route
from siftwise.config import Settings
from siftwise.core.history import recompute_repo_stats
from siftwise.core.models import Repo, ShadowSelection
from siftwise.db import get_session
from tests.test_shadow import SHADOW, shadow_run
from tests.test_triage_db import OTHER, REPO, TEST, F, P, record, sha

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SHADOW_IN = {"ci_run_id": SHADOW, "run_attempt": 1, "commit_sha": sha(50).upper()}


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app = create_app(Settings(_env_file=None, api_token=TOKEN))
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seeded(db_session: Session) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: P, OTHER: P})
    repo_id = db_session.scalars(select(Repo.id).where(Repo.name == REPO)).one()
    recompute_repo_stats(db_session, repo_id, 90)  # known tests, for /select


def post_select(client: TestClient, **body: Any) -> httpx2.Response:
    response: httpx2.Response = client.post(
        "/select", json={"repo": REPO, "changed_files": ["src/cart.py"], **body}, headers=AUTH
    )
    return response


@pytest.mark.usefixtures("seeded")
def test_select_stores_a_shadow_selection(client: TestClient, db_session: Session) -> None:
    body = post_select(client, shadow=SHADOW_IN).json()

    assert (body["mode"], body["selected_count"], body["shadow_stored"]) == ("selective", 1, True)
    assert body["shadow_reason"] is None
    stored = db_session.scalars(select(ShadowSelection)).one()
    assert (stored.ci_run_id, stored.variant, stored.commit_sha, stored.mode) == (
        SHADOW, None, sha(50), "selective")  # fmt: skip
    assert (stored.selected_count, stored.total_known) == (1, 2)


@pytest.mark.usefixtures("seeded")
def test_select_without_shadow_stores_nothing(client: TestClient, db_session: Session) -> None:
    body = post_select(client).json()

    assert (body["shadow_stored"], body["shadow_reason"]) == (None, None)
    assert db_session.scalars(select(ShadowSelection)).all() == []


@pytest.mark.usefixtures("seeded")
def test_a_failed_store_never_fails_select(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object) -> bool:
        raise OperationalError("INSERT", {}, Exception("disk full"))

    monkeypatch.setattr(select_route, "store_selection", broken)
    response = post_select(client, shadow=SHADOW_IN)

    assert response.status_code == 200
    body = response.json()
    assert (body["mode"], body["shadow_stored"], body["shadow_reason"]) == (
        "selective", False, "storage_error")  # fmt: skip


@pytest.mark.usefixtures("seeded")
def test_select_says_why_a_shadow_selection_was_not_stored(client: TestClient) -> None:
    assert post_select(client, shadow=SHADOW_IN).json()["shadow_stored"] is True
    # Another commit claims the same CI run attempt.
    other = post_select(client, shadow={**SHADOW_IN, "commit_sha": sha(51)}).json()
    assert (other["shadow_stored"], other["shadow_reason"]) == (False, "commit_conflict")
    # Another leg of the same commit: already stored.
    again = post_select(client, shadow=SHADOW_IN).json()
    assert (again["shadow_stored"], again["shadow_reason"]) == (True, None)


@pytest.mark.parametrize(
    "shadow",
    [
        {"ci_run_id": SHADOW},  # no commit
        {**SHADOW_IN, "commit_sha": "abc123"},
        {**SHADOW_IN, "run_attempt": 0},
        {**SHADOW_IN, "surprise": True},
    ],
)
@pytest.mark.usefixtures("seeded")
def test_invalid_shadow_requests(client: TestClient, shadow: dict[str, Any]) -> None:
    assert post_select(client, shadow=shadow).status_code == 422


@pytest.mark.usefixtures("seeded")
def test_compare_and_recall(client: TestClient, db_session: Session) -> None:
    post_select(client, shadow=SHADOW_IN)
    shadow_run(db_session, {TEST: F, OTHER: F})

    response = client.post(f"/repos/{REPO}/shadow", json={"ci_run_id": SHADOW}, headers=AUTH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["shadow"], body["counted"], body["mode"]) == (True, True, "selective")
    assert (body["failing_tests"], body["caught"], body["missed"], body["flaky_excluded"]) == (
        2, 1, 1, 0)  # fmt: skip
    assert body["missed_by_verdict"] == {"new_regression": 0, "unknown": 1, "pre_existing": 0}
    assert body["missed_tests"] == [{"test_id": OTHER, "verdict": "unknown",
                                     "variants": ["linux"]}]  # fmt: skip
    [selection] = body["selections"]
    assert (selection["selected_count"], selection["total_known"], selection["status"]) == (
        1, 2, "compared")  # fmt: skip
    [variant] = body["variants"]
    assert (variant["variant"], variant["status"], variant["tests_total"]) == (
        "linux", "compared", 2)  # fmt: skip
    assert body["time"]["saved"] == 0.5

    recall = client.get(f"/repos/{REPO}/recall", params={"days": 7}, headers=AUTH).json()
    assert (recall["days"], recall["shadow_runs"], recall["selective_runs"]) == (7, 1, 1)
    assert recall["all"] == recall["selective"] == {
        "runs": 1, "failures": 2, "flaky_excluded": 0, "caught": 1, "missed": 1, "recall": 0.5,
        "missed_by_verdict": {"new_regression": 0, "unknown": 1, "pre_existing": 0},
    }  # fmt: skip
    [miss] = recall["misses"]
    assert (miss["test_id"], miss["ci_run_id"], miss["commit_sha"], miss["selection_mode"]) == (
        OTHER, SHADOW, sha(50), "selective")  # fmt: skip
    assert (recall["not_counted"], recall["in_progress"], recall["misses_truncated"]) == (
        {}, 0, False)  # fmt: skip


@pytest.mark.usefixtures("seeded")
def test_compare_of_a_run_that_was_not_a_shadow_run(client: TestClient) -> None:
    body = client.post(f"/repos/{REPO}/shadow", json={"ci_run_id": "7"}, headers=AUTH).json()

    assert (body["shadow"], body["counted"], body["selections"], body["variants"]) == (
        False, False, [], [])  # fmt: skip


def test_unknown_repo_invalid_days_and_auth(client: TestClient) -> None:
    assert client.post("/repos/no/repo/shadow", json={"ci_run_id": "1"},
                       headers=AUTH).status_code == 404  # fmt: skip
    assert client.get("/repos/no/repo/recall", headers=AUTH).status_code == 404
    for days in (0, 366):
        response = client.get(f"/repos/{REPO}/recall", params={"days": days}, headers=AUTH)
        assert response.status_code == 422
    assert client.get(f"/repos/{REPO}/recall").status_code == 401
    assert client.post(f"/repos/{REPO}/shadow", json={"ci_run_id": "1"}).status_code == 401
