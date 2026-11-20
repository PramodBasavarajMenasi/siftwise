"""Every CI selection recorded (#85): /select with ``ci`` stores it, /repos/{repo}/outcome
stores each leg's outcome once, and what the clients send and fall back to."""

from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from siftwise import __version__
from siftwise.api.main import create_app
from siftwise.config import Settings
from siftwise.core import tokens
from siftwise.core.history import recompute_repo_stats
from siftwise.core.models import AttemptOutcome, CiSelection, Repo, Run, RunFailure, RunOutcome
from siftwise.core.schemas import CiIn
from siftwise.core.selector import FullReason, Mode, Runner, SelectedTest, measured_time
from siftwise.core.shadow import CiRun, ShadowTarget, compare_ci_run, recall, store_selection
from siftwise.core.tokens import Scope
from siftwise.db import get_session
from tests.test_shadow import selection
from tests.test_triage_db import OTHER, REPO, TEST, F, P, record, sha

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CI_RUN = "ci-85"
CI = {
    "provider": "github",
    "ci_run_id": CI_RUN,
    "run_attempt": 1,
    "commit_sha": sha(50),
    "branch": "feature/x",
    "is_main": False,
    "pr_number": 12,
    "event": "pull_request",
    "client_version": "0.4.0",
}
THIRD = "tests.test_ship::test_rate"
CHANGE = ("src/cart.py",)  # links TEST by path mapping


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app = create_app(Settings(_env_file=None, api_token=TOKEN))
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seeded(db_session: Session) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: P, OTHER: P, THIRD: P})
    repo_id = db_session.scalars(select(Repo.id).where(Repo.name == REPO)).one()
    recompute_repo_stats(db_session, repo_id, 90)  # known tests, for /select


def post_select(client: TestClient, headers: dict[str, str] = AUTH, **body: Any) -> httpx2.Response:
    response: httpx2.Response = client.post(
        "/select", json={"repo": REPO, "changed_files": list(CHANGE), **body}, headers=headers
    )
    return response


def ci_run(session: Session, tests: dict[str, Any], *, variant: str | None = "linux",
           ci: str = CI_RUN, rerun: int = 0) -> Run:  # fmt: skip
    return record(session, commit=50, hour=6, branch="feature/x", variant=variant,
                  ci_run_id=ci, tests=tests, changed=CHANGE, rerun=rerun)  # fmt: skip


def ranked(*tests: str, mode: Mode = Mode.SELECTIVE) -> Any:
    """A selection whose tests carry risk ranks in the order given."""
    sel = selection(*tests, mode=mode)
    ordered = tuple(
        SelectedTest(t, ("because",), Runner.PYTEST, None, score=1.0, rank=n, rank_reason="r")
        for n, t in enumerate(tests, start=1)
    )
    import dataclasses

    return dataclasses.replace(sel, tests=tuple(reversed(ordered)))


# --- storing --------------------------------------------------------------------------------


@pytest.mark.usefixtures("seeded")
def test_a_ci_selection_is_stored_in_risk_order_with_its_context(db_session: Session) -> None:
    sel = ranked(OTHER, TEST)  # OTHER ranks first
    target = ShadowTarget(CI_RUN, 1, None, sha(50))
    ci = CiRun("github", "feature/x", False, 12, "pull_request", "0.4.0", 3)
    assert store_selection(db_session, REPO, target, sel, shadow=False, ci=ci) is None
    row = db_session.scalars(select(CiSelection)).one()
    keys: dict[str, int] = {
        t: k for t, k in db_session.execute(text("SELECT test_id, id FROM tests"))
    }
    assert row.selected_keys == [keys[OTHER], keys[TEST]] and row.ranked and not row.shadow
    assert (row.provider, row.branch, row.is_main, row.pr_number, row.event) == (
        "github", "feature/x", False, 12, "pull_request")  # fmt: skip
    assert (row.client_version, row.changed_count, row.command_order) == ("0.4.0", 3, "default")
    assert row.top_risk is not None and [t["key"] for t in row.top_risk] == row.selected_keys
    assert row.top_risk[0]["reasons"] == ["because"] and row.reason_code is None


@pytest.mark.usefixtures("seeded")
def test_select_records_a_ci_selection_with_a_write_token(
    client: TestClient, db_session: Session
) -> None:
    body = post_select(client, ci=CI).json()
    assert (body["recorded"], body["record_reason"], body["shadow_stored"]) == (True, None, None)
    row = db_session.scalars(select(CiSelection)).one()
    assert (row.shadow, row.mode, row.ci_run_id, row.commit_sha, row.changed_count) == (
        False, "selective", CI_RUN, sha(50), 1)  # fmt: skip
    assert row.command_order == "risk" and row.ranked
    # Without ci (a laptop, MCP), nothing is stored and nothing said.
    body = post_select(client).json()
    assert (body["recorded"], body["record_reason"]) == (None, None)
    assert len(db_session.scalars(select(CiSelection)).all()) == 1


@pytest.mark.usefixtures("seeded")
def test_a_read_token_still_selects_but_records_nothing(
    client: TestClient, db_session: Session
) -> None:
    _, read = tokens.create_token(db_session, REPO, Scope.READ, "ci")
    response = post_select(client, {"Authorization": f"Bearer {read}"}, ci=CI)
    assert response.status_code == 200
    body = response.json()
    assert (body["mode"], body["recorded"], body["record_reason"]) == (
        "selective", False, "read_only_token")  # fmt: skip
    assert db_session.scalars(select(CiSelection)).all() == []


@pytest.mark.usefixtures("seeded")
def test_ci_and_shadow_together_store_one_shadow_row(
    client: TestClient, db_session: Session
) -> None:
    shadow = {k: CI[k] for k in ("ci_run_id", "run_attempt", "commit_sha")}
    body = post_select(client, ci=CI, shadow=shadow).json()
    assert (body["recorded"], body["shadow_stored"]) == (True, True)
    row = db_session.scalars(select(CiSelection)).one()
    assert row.shadow and row.branch == "feature/x"
    # Two different runs in one call: a client bug.
    other = {**shadow, "ci_run_id": "another"}
    assert post_select(client, ci={**CI, "ci_run_id": "x"}, shadow=other).status_code == 422


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("branch", "a\nb"),
        ("branch", "x" * 256),
        ("branch", "tab\there"),
        ("event", "Pull Request"),
        ("pr_number", 0),
        ("client_version", "0.4 beta"),
        ("provider", "jenkins"),
    ],
)
def test_ci_context_from_ci_is_validated(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        CiIn.model_validate({**CI, field: value})


def test_full_suite_selections_carry_a_reason_code() -> None:
    from tests.test_selector import PY, history, select

    # Every code the selector has, named once: the dashboard groups fallbacks by them.
    assert {c.value for c in FullReason} == {
        "changed_files_unknown", "no_net_changes", "build_file", "no_history", "java_build",
        "fixture_unreferenced", "unmapped_file", "no_runner",
    }  # fmt: skip
    no_runner = history(
        "tests.test_cart::test_total",
        "com.acme.CartTest::testTotal",
        recent_main_failures={"com.acme.CartTest::testTotal": (7, "c" * 40)},
    )
    cases = [
        (select(PY, "src/shop/cart.py", known=False), FullReason.CHANGED_FILES_UNKNOWN, ()),
        (select(PY), FullReason.NO_NET_CHANGES, ()),
        (select(PY, "pyproject.toml", "Dockerfile"), FullReason.BUILD_FILE,
         ("Dockerfile", "pyproject.toml")),
        (select(None, "src/shop/cart.py"), FullReason.NO_HISTORY, ()),
        (select(PY, "src/shop/cart.py", "proto/shop.proto"), FullReason.UNMAPPED_FILE,
         ("proto/shop.proto",)),
        (select(no_runner, "src/shop/cart.py"), FullReason.NO_RUNNER,
         ("com.acme.CartTest::testTotal",)),
    ]  # fmt: skip
    for result, code, items in cases:
        assert (result.mode, result.reason_code, result.reason_items) == (Mode.FULL, code, items)
    # The reason text is what it always was.
    assert cases[2][0].reason == "build/config file changed: Dockerfile, pyproject.toml"
    # A selective run has none.
    assert select(PY, "src/shop/cart.py").reason_code is None


# --- outcomes ---------------------------------------------------------------------------------


@pytest.mark.usefixtures("seeded")
def test_an_outcome_stores_verdicts_selection_and_first_failure_rank(
    db_session: Session,
) -> None:
    store_selection(db_session, REPO, ShadowTarget(CI_RUN, 1, None, sha(50)),
                    ranked(OTHER, TEST), shadow=False)  # fmt: skip
    run = ci_run(db_session, {TEST: F, OTHER: P, THIRD: F})  # THIRD ran outside the selection
    result = compare_ci_run(db_session, REPO, CI_RUN, 1, fail_on=["new_regression"])
    assert result is not None and not result.shadow and result.variants == ()
    (leg,) = result.legs
    assert (leg.selection, leg.failing, leg.failures_selected, leg.first_failure_rank) == (
        "selective", 2, 1, 2)  # fmt: skip
    assert leg.new_regressions + leg.unknown + leg.pre_existing + leg.flaky == 2
    assert leg.measured_ms == run.measured_ms
    outcome = db_session.get(RunOutcome, run.id)
    assert outcome is not None and outcome.caught is None and outcome.ms_would_run is None
    failures = {
        test_id: (would_run, rank, cause)
        for test_id, would_run, rank, cause in db_session.execute(
            text(
                "SELECT t.test_id, f.would_run, f.rank, f.cause FROM run_failures f "
                "JOIN tests t ON t.id = f.test_key"
            )
        )
    }
    assert failures == {TEST: (True, 2, None), THIRD: (False, None, None)}
    gate = db_session.scalars(select(AttemptOutcome)).one()
    assert (gate.ci_run_id, gate.run_attempt, gate.fail_on) == (CI_RUN, 1, ["new_regression"])
    # Not a shadow run: recall never counts it.
    report = recall(db_session, REPO, 365)
    assert report is not None and report.all.runs == 0


@pytest.mark.usefixtures("seeded")
def test_an_outcome_without_a_selection_still_stores_verdicts(db_session: Session) -> None:
    run = ci_run(db_session, {TEST: F, OTHER: P})
    result = compare_ci_run(db_session, REPO, CI_RUN, 1)
    assert result is not None and [leg.selection for leg in result.legs] == [None]
    outcome = db_session.get(RunOutcome, run.id)
    assert outcome is not None and outcome.selection_id is None
    assert (outcome.failing, outcome.failures_selected, outcome.first_failure_rank) == (
        1,
        None,
        None,
    )
    failure = db_session.scalars(select(RunFailure)).one()
    assert (failure.would_run, failure.rank) == (None, None)
    assert db_session.scalars(select(AttemptOutcome)).one().fail_on is None


@pytest.mark.usefixtures("seeded")
def test_an_outcome_is_replaced_by_a_recall_and_skips_re_runs(db_session: Session) -> None:
    store_selection(db_session, REPO, ShadowTarget(CI_RUN, 1, None, sha(50)), ranked(TEST),
                    shadow=False)  # fmt: skip
    run = ci_run(db_session, {TEST: F, OTHER: P})
    ci_run(db_session, {TEST: P}, rerun=1)  # a re-run upload: its own leg? never
    first = compare_ci_run(db_session, REPO, CI_RUN, 1, fail_on=["unknown"])
    again = compare_ci_run(db_session, REPO, CI_RUN, 1)
    assert first is not None and again is not None
    assert [leg.run_id for leg in again.legs] == [run.id]
    assert db_session.query(RunOutcome).count() == 1
    assert db_session.scalars(select(AttemptOutcome)).one().fail_on is None  # the latest call's


@pytest.mark.usefixtures("seeded")
def test_a_full_selection_runs_every_failure_unranked(db_session: Session) -> None:
    store_selection(db_session, REPO, ShadowTarget(CI_RUN, 1, None, sha(50)),
                    selection(mode=Mode.FULL), shadow=False)  # fmt: skip
    ci_run(db_session, {TEST: F, OTHER: F})
    result = compare_ci_run(db_session, REPO, CI_RUN, 1)
    assert result is not None
    (leg,) = result.legs
    assert (leg.selection, leg.failures_selected, leg.first_failure_rank) == ("full", 2, None)


def test_measured_time_is_one_definition_for_ingest_and_the_compare(db_session: Session) -> None:
    run = record(db_session, commit=1, hour=1, ci_run_id="m", tests={
        TEST: P, OTHER: [F, P], "github.com/acme/shop/pay::TestA": P,
        "github.com/acme/shop/pay::TestA/sub": P})  # fmt: skip
    rows = db_session.execute(
        text(
            "SELECT DISTINCT ON (r.test_key) t.test_id, r.duration_ms FROM test_results r "
            "JOIN tests t ON t.id = r.test_key WHERE r.run_id = :id "
            "ORDER BY r.test_key, r.attempt DESC"
        ),
        {"id": run.id},
    ).all()
    tests_run, ms = measured_time((t, d) for t, d in rows)
    assert (run.tests_run, run.measured_ms) == (tests_run, ms) == (3, ms)


# --- the API --------------------------------------------------------------------------------


@pytest.mark.usefixtures("seeded")
def test_outcome_over_the_api(client: TestClient, db_session: Session) -> None:
    post_select(client, ci=CI)
    ci_run(db_session, {TEST: F, OTHER: P}, variant=None)
    response = client.post(f"/repos/{REPO}/outcome",
                           json={"ci_run_id": CI_RUN, "fail_on": ["new_regression", "unknown"]},
                           headers=AUTH)  # fmt: skip
    assert response.status_code == 200
    body = response.json()
    assert (body["recorded"], body["fail_on"], body["shadow"]["shadow"]) == (
        True, ["new_regression", "unknown"], False)  # fmt: skip
    (leg,) = body["legs"]
    assert (leg["selection"], leg["failing"], leg["failures_selected"]) == ("selective", 1, 1)
    assert leg["first_failure_rank"] == 1
    # The old route stores the same outcome and answers as before.
    old = client.post(f"/repos/{REPO}/shadow", json={"ci_run_id": CI_RUN}, headers=AUTH)
    assert old.status_code == 200 and old.json()["shadow"] is False
    assert db_session.query(RunOutcome).count() == 1
    assert client.post(f"/repos/{REPO}/outcome", json={"ci_run_id": CI_RUN, "fail_on": ["oops"]},
                       headers=AUTH).status_code == 422  # fmt: skip


@pytest.mark.usefixtures("seeded")
def test_outcome_needs_a_write_token(client: TestClient, db_session: Session) -> None:
    _, read = tokens.create_token(db_session, REPO, Scope.READ, "ci")
    response = client.post(f"/repos/{REPO}/outcome", json={"ci_run_id": CI_RUN},
                           headers={"Authorization": f"Bearer {read}"})  # fmt: skip
    assert response.status_code == 403


def test_healthz_reports_the_version(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok", "version": __version__}


# --- the /select retry without new fields: only for a server that doesn't know them ---------

SERVER = "http://siftwise.test"


def _old_server(*fields: str) -> dict[str, Any]:
    return {"detail": [{"type": "extra_forbidden", "loc": ["body", f], "msg": "Extra inputs "
                        "are not permitted"} for f in fields]}  # fmt: skip


@pytest.mark.parametrize(
    "detail",
    [
        # A real validation error with ci in the body: not about ci at all.
        [{"type": "string_type", "loc": ["body", "changed_files", 0],
          "msg": "Input should be a valid string", "input": 7}],
        # A bad value inside ci (a 0.4 server): dropping ci would hide it.
        [{"type": "string_pattern_mismatch", "loc": ["body", "ci", "branch"],
          "msg": "String should match pattern", "input": "a\\nb"}],
        # ci unknown, but something else wrong too.
        [{"type": "extra_forbidden", "loc": ["body", "ci"], "msg": "Extra inputs"},
         {"type": "missing", "loc": ["body", "repo"], "msg": "Field required"}],
        # A field siftwise never sends as optional.
        [{"type": "extra_forbidden", "loc": ["body", "surprise"], "msg": "Extra inputs"}],
        "not a list",
    ],
    ids=["changed_files", "inside-ci", "mixed", "other-field", "plain-detail"],
)  # fmt: skip
def test_select_is_not_retried_for_any_other_422(detail: object) -> None:
    import respx

    from siftwise.cli import main as cli

    body = {"repo": REPO, "changed_files": ["src/cart.py"], "ci": CI}
    with respx.mock(assert_all_called=True) as api:
        route = api.post(f"{SERVER}/select").respond(422, json={"detail": detail})
        with pytest.raises(cli.SiftwiseError, match="siftwise returned 422") as raised:
            cli.post_select(SERVER, "t", body)
    assert route.call_count == 1
    assert raised.value.status == 422


def test_select_is_retried_without_the_fields_an_older_server_does_not_know() -> None:
    import json

    import httpx
    import respx

    from siftwise.cli import main as cli

    body = {"repo": REPO, "changed_files": ["src/cart.py"], "ci": CI, "import_depths": {}}
    with respx.mock(assert_all_called=True) as api:
        route = api.post(f"{SERVER}/select")
        route.side_effect = [httpx.Response(422, json=_old_server("ci", "import_depths")),
                             httpx.Response(200, json={"mode": "full"})]  # fmt: skip
        assert cli.post_select(SERVER, "t", body) == {"mode": "full"}
    sent = json.loads(route.calls.last.request.content)
    assert "ci" not in sent and "import_depths" not in sent and sent["changed_files"]


@pytest.mark.parametrize(
    ("sent", "stored"),
    [("github", "github"), ("gitlab", "gitlab"), ("cli", "cli"), ("backfill", "backfill"),
     ("jenkins", None), ("GitHub", None), ("<script>", None), ("", None), (7, None)],
)  # fmt: skip
def test_an_uploads_client_is_one_of_four_else_unknown(
    client: TestClient, db_session: Session, sent: object, stored: str | None
) -> None:
    import json

    meta = {"repo": REPO, "commit_sha": sha(9), "branch": "main", "is_main": True,
            "ci_run_id": "c", "client_version": "0.4.0", "client": sent}  # fmt: skip
    response = client.post(
        "/runs",
        files=[
            (
                "files",
                (
                    "j.xml",
                    b'<testsuite><testcase classname="t" name="a"/></testsuite>',
                    "application/xml",
                ),
            )
        ],
        data={"metadata": json.dumps(meta)},
        headers=AUTH,
    )
    assert response.status_code == 201, response.text  # never refused for it
    run = db_session.scalars(select(Run)).one()
    assert (run.client, run.client_version) == (stored, "0.4.0")
