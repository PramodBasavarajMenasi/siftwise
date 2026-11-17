"""known_flaky expiry and `siftwise flaky` (#72): the entry format, the one expiry rule and
the date that decides it, the PR-can't-extend rule, the expired-entry note on every surface,
the flaky-tests route, and the command."""

import json
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from siftwise.api.main import create_app
from siftwise.cli import gha
from siftwise.cli import main as cli
from siftwise.cli.repoconfig import (
    KnownFlakyEntry,
    RepoConfig,
    RepoConfigError,
    active_known_flaky,
    loads_repo_config,
)
from siftwise.config import Settings
from siftwise.core.models import Run
from siftwise.db import get_session
from tests.test_gha import (  # noqa: F401  (fixtures)
    GH,
    REPO,
    RUN_URL,
    SERVER,
    Runner,
    api,
    base_with_config,
    gh,
    pr_event,
    record_routes,
    recorded_runs,
    repo,
    triage_response,
)

UNTIL = date(2026, 12, 31)
POOL = "tests/test_db.py::test_pool"

# --- the format ----------------------------------------------------------------------------


def test_the_plain_list_works_as_before() -> None:
    config = loads_repo_config('known_flaky = ["tests/test_net.py::*", "tests.a::*"]')

    assert config.known_flaky == (KnownFlakyEntry("tests/test_net.py::*"),
                                  KnownFlakyEntry("tests.a::*"))  # fmt: skip
    active = active_known_flaky(config.known_flaky, date(2099, 1, 1))
    assert active.globs == ("tests/test_net.py::*", "tests.a::*") and active.expired == ()
    assert loads_repo_config("").known_flaky == () == RepoConfig().known_flaky


@pytest.mark.parametrize(
    "toml",
    [f'{{ test = "{POOL}", until = "2026-12-31" }}', f'{{ test = "{POOL}", until = 2026-12-31 }}'],
)
def test_an_entry_with_an_expiry(toml: str) -> None:
    config = loads_repo_config(f'known_flaky = ["tests/test_net.py::*", {toml}]')

    assert config.known_flaky[1] == KnownFlakyEntry(POOL, UNTIL)
    assert config.known_flaky[1].describe() == f"{POOL} (until 2026-12-31)"


@pytest.mark.parametrize(
    ("toml", "error"),
    [
        ("known_flaky = 3", "known_flaky must be a list"),
        ("known_flaky = [3]", "known_flaky #1 must be a glob or a { test, until } table, not int"),
        ('known_flaky = [""]', "known_flaky #1: an empty glob"),
        (
            f'known_flaky = [{{ test = "{POOL}", expires = "2026-12-31" }}]',
            "unknown field(s) expires (only 'test' and 'until')",
        ),
        ('known_flaky = [{ until = "2026-12-31" }]', "'test' must be a non-empty glob"),
        (
            f'known_flaky = [{{ test = "{POOL}", until = "2026-13-01" }}]',
            "'until' must be a date as YYYY-MM-DD, not '2026-13-01'",
        ),
        (f'known_flaky = [{{ test = "{POOL}", until = "next week" }}]', "not 'next week'"),
        (f'known_flaky = [{{ test = "{POOL}", until = "20261231" }}]', "not '20261231'"),
        (
            f'known_flaky = [{{ test = "{POOL}", until = 2026-12-31T10:00:00 }}]',
            "'until' must be a date (YYYY-MM-DD), not a date and time",
        ),
        (f'known_flaky = [{{ test = "{POOL}", until = 20261231 }}]', "not 20261231"),
    ],
)
def test_invalid_entries_are_clear_errors(toml: str, error: str) -> None:
    with pytest.raises(RepoConfigError, match="known_flaky") as raised:
        loads_repo_config(toml)
    assert error in str(raised.value)


# --- the one expiry rule and its date -----------------------------------------------------


def test_an_entry_applies_through_its_until_date_and_expires_the_day_after() -> None:
    entries = (KnownFlakyEntry("a::*"), KnownFlakyEntry(POOL, UNTIL))

    on_the_day = active_known_flaky(entries, UNTIL)
    day_after = active_known_flaky(entries, UNTIL + timedelta(days=1))

    assert (on_the_day.globs, on_the_day.expired) == (("a::*", POOL), ())
    assert (day_after.globs, day_after.expired) == (("a::*",), (KnownFlakyEntry(POOL, UNTIL),))


@pytest.mark.parametrize(
    ("started_at", "day"),
    [
        ("2026-12-31T23:59:59Z", date(2026, 12, 31)),
        ("2027-01-01T00:00:00+00:00", date(2027, 1, 1)),
        ("2026-12-31T23:30:00-02:00", date(2027, 1, 1)),  # the UTC date
        (datetime(2026, 12, 31, 12, tzinfo=UTC), date(2026, 12, 31)),
    ],
)
def test_the_run_start_in_utc_decides(started_at: str | datetime, day: date) -> None:
    assert cli.known_flaky_date(started_at) == day


def test_without_a_run_start_today_in_utc_decides() -> None:
    assert cli.known_flaky_date(None) == datetime.now(UTC).date()
    assert cli.known_flaky_date("not a time") == datetime.now(UTC).date()


# --- record: the base's list, as of the run's start ---------------------------------------

BASE_LIST = f'known_flaky = ["tests.test_net::*", {{ test = "{POOL}", until = "2026-12-31" }}]\n'


def started(api: respx.MockRouter, at: str) -> None:  # noqa: F811
    api.get(f"{GH}/repos/{REPO}/actions/runs/555").respond(200, json={"run_started_at": at})


def test_record_applies_an_entry_on_its_last_day(
    gh: Runner,  # noqa: F811
    repo: dict[str, str],  # noqa: F811
    api: respx.MockRouter,  # noqa: F811
) -> None:
    upload = record_routes(api)
    started(api, "2026-12-31T23:00:00Z")
    shas = base_with_config(repo, BASE_LIST)
    Path(".siftwise.toml").write_text(BASE_LIST)

    result = gh.run("record", "pull_request", pr_event(shas), SIFTWISE_VARIANT="linux")

    assert result.exit_code == 0, result.output
    [meta] = recorded_runs(upload)
    assert meta["known_flaky"] == ["tests.test_net::*", POOL]
    assert "known_flaky_expired" not in meta  # an older server would reject the field
    assert meta["known_flaky_changed"] is False


def test_record_leaves_out_and_reports_an_expired_entry(
    gh: Runner,  # noqa: F811
    repo: dict[str, str],  # noqa: F811
    api: respx.MockRouter,  # noqa: F811
) -> None:
    upload = record_routes(api)
    started(api, "2027-01-01T00:30:00Z")
    shas = base_with_config(repo, BASE_LIST)
    Path(".siftwise.toml").write_text(BASE_LIST)

    result = gh.run("record", "pull_request", pr_event(shas), SIFTWISE_VARIANT="linux")

    assert result.exit_code == 0, result.output
    [meta] = recorded_runs(upload)
    assert meta["known_flaky"] == ["tests.test_net::*"]
    assert meta["known_flaky_expired"] == [{"test": POOL, "until": "2026-12-31"}]
    assert f"known_flaky entry expired: {POOL} (until 2026-12-31)" in result.output


@pytest.mark.parametrize(
    "head_list",
    [
        f'known_flaky = ["tests.test_net::*", {{ test = "{POOL}", until = "2027-06-30" }}]\n',
        'known_flaky = ["tests.test_net::*", "tests.test_new::*"]\n',
    ],
    ids=["extends-until", "adds-entry"],
)
def test_a_pr_cannot_extend_or_add_an_entry_for_its_own_run(
    gh: Runner,  # noqa: F811
    repo: dict[str, str],  # noqa: F811
    api: respx.MockRouter,  # noqa: F811
    head_list: str,
) -> None:
    upload = record_routes(api)
    started(api, "2027-01-15T10:00:00Z")
    shas = base_with_config(repo, BASE_LIST)
    Path(".siftwise.toml").write_text(head_list)  # the PR's own edit

    result = gh.run("record", "pull_request", pr_event(shas), SIFTWISE_VARIANT="linux")

    assert result.exit_code == 0, result.output
    [meta] = recorded_runs(upload)
    assert meta["known_flaky"] == ["tests.test_net::*"]  # the base's list, POOL expired
    assert meta["known_flaky_expired"] == [{"test": POOL, "until": "2026-12-31"}]
    assert meta["known_flaky_changed"] is True
    assert "known_flaky changes in this PR apply after merge" in result.output


def test_a_push_applies_its_own_list_as_of_its_start(
    gh: Runner,  # noqa: F811
    repo: dict[str, str],  # noqa: F811
    api: respx.MockRouter,  # noqa: F811
) -> None:
    upload = record_routes(api)
    started(api, "2027-01-01T08:00:00Z")
    Path(".siftwise.toml").write_text(BASE_LIST)
    push = {"before": repo["base"], "repository": {"default_branch": "main"}}

    result = gh.run("record", "push", push, GITHUB_SHA=repo["head"], GITHUB_REF_NAME="feature",
                    SIFTWISE_VARIANT="linux")  # fmt: skip

    assert result.exit_code == 0, result.output
    [meta] = recorded_runs(upload)
    assert meta["known_flaky"] == ["tests.test_net::*"]
    assert meta["known_flaky_expired"] == [{"test": POOL, "until": "2026-12-31"}]


def test_an_older_server_gets_the_run_without_the_expired_list(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text("<testsuite/>")
    meta = {"repo": REPO, "known_flaky_expired": [{"test": POOL, "until": "2026-12-31"}]}
    old = {"detail": [{"loc": ["metadata", "known_flaky_expired"], "type": "extra_forbidden"}]}
    warned: list[str] = []
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{SERVER}/runs").mock(side_effect=[
            httpx.Response(422, json=old), httpx.Response(201, json={"run_id": 1})])  # fmt: skip
        result = cli.upload_run(SERVER, "t", [junit], meta, warn=warned.append)

    assert result == {"run_id": 1}
    sent = [json.loads(dict(_form(c.request))["metadata"]) for c in route.calls]
    assert "known_flaky_expired" in sent[0] and "known_flaky_expired" not in sent[1]
    assert "doesn't take known_flaky_expired" in warned[0]


def _form(request: httpx.Request) -> list[tuple[str, str]]:
    """The multipart form's text fields."""
    body = request.read().decode("utf-8", "replace")
    fields = []
    for part in body.split("--" + request.headers["content-type"].split("boundary=")[1]):
        if 'name="metadata"' in part:
            fields.append(("metadata", part.split("\r\n\r\n", 1)[1].rsplit("\r\n", 1)[0]))
    return fields


# --- local triage, the comment ------------------------------------------------------------


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()  # fmt: skip


def test_local_triage_uses_the_bases_list_as_of_today(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "commit.gpgsign", "false")
    yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    (tmp_path / ".siftwise.toml").write_text(
        f'known_flaky = ["a::*", {{ test = "b::*", until = "{yesterday}" }}]\n')  # fmt: skip
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    head = loads_repo_config('known_flaky = ["a::*", { test = "b::*", until = "2999-01-01" }]')

    flaky = cli.known_flaky(head, "HEAD", cwd=tmp_path)

    assert flaky.globs == ("a::*",)  # the agent's or PR's extension doesn't apply
    assert flaky.expired == (KnownFlakyEntry("b::*", date.fromisoformat(yesterday)),)
    assert flaky.changed
    assert cli.expired_note(flaky.expired[0]) == (
        f"known_flaky entry expired: b::* (until {yesterday})"
    )


def test_the_comment_names_expired_entries_even_without_failures() -> None:
    response = triage_response(variants=("linux", "macos"))
    for v in response["variants"]:
        v["known_flaky_expired"] = [{"test": POOL, "until": "2026-12-31"}]

    text = gha.failures_markdown(response, RUN_URL)

    assert text == (
        "### Failures\n\n"
        f"> known_flaky entry expired: ` {POOL} ` (until 2026-12-31).\n\n"
        "No failing tests in 2 recorded variants.\n"
    )
    assert "expired" not in gha.failures_markdown(triage_response(), RUN_URL)


# --- the server: stored, triaged, reported ------------------------------------------------

SHA = "d" * 40


def client(db_session: Session) -> TestClient:
    app = create_app(Settings(_env_file=None, api_token="t"))
    app.dependency_overrides[get_session] = lambda: db_session
    return TestClient(app)


AUTH = {"Authorization": "Bearer t"}


def upload(c: TestClient, xml: str, **meta: Any) -> None:
    body = {"repo": REPO, "commit_sha": SHA, "branch": "main", "is_main": True,
            "ci_run_id": "1", **meta}  # fmt: skip
    response = c.post("/runs", files=[("files", ("j.xml", xml.encode(), "application/xml"))],
                      data={"metadata": json.dumps(body)}, headers=AUTH)  # fmt: skip
    assert response.status_code == 201, response.text


def suite(*cases: tuple[str, str]) -> str:
    rows = "".join(
        f'<testcase classname="tests.t" name="{name}">'
        + (
            "<failure message='x'/>"
            if outcome == "fail"
            else "<skipped/>"
            if outcome == "skip"
            else ""
        )
        + "</testcase>"
        for name, outcome in cases
    )
    return f"<testsuite>{rows}</testsuite>"


def test_the_expired_list_is_stored_and_returned_with_the_runs_triage(
    db_session: Session,
) -> None:
    with client(db_session) as c:
        upload(c, suite(("a", "fail")), known_flaky=["x::*"],
               known_flaky_expired=[{"test": POOL, "until": "2026-12-31"}])  # fmt: skip
        response = c.post(f"/repos/{REPO}/triage", json={"ci_run_id": "1"}, headers=AUTH)

    stored = db_session.scalars(select(Run.known_flaky_expired)).one()
    assert stored == [{"test": POOL, "until": "2026-12-31"}]
    assert response.json()["variants"][0]["known_flaky_expired"] == [
        {"test": POOL, "until": "2026-12-31"}]  # fmt: skip


def test_a_run_without_expired_entries_stores_none(db_session: Session) -> None:
    with client(db_session) as c:
        upload(c, suite(("a", "pass")))

    assert db_session.scalars(select(Run.known_flaky_expired)).one() == []


def test_flaky_tests_are_those_that_failed_and_passed_on_one_commit_and_variant(
    db_session: Session,
) -> None:
    other = "e" * 40
    with client(db_session) as c:
        # tests.t::flips fails then passes on SHA/linux (a re-run attempt), and on a second
        # commit too; ::os fails on linux and passes on macos only (a platform difference);
        # ::always fails everywhere; ::skips is skipped.
        upload(c, suite(("flips", "fail"), ("os", "fail"), ("always", "fail"), ("skips", "skip")),
               variant="linux")  # fmt: skip
        upload(c, suite(("flips", "pass"), ("os", "fail"), ("always", "fail")), variant="linux",
               run_attempt=2)  # fmt: skip
        upload(c, suite(("os", "pass"), ("always", "fail")), variant="macos")
        upload(c, suite(("flips", "fail")), commit_sha=other, ci_run_id="2", variant="linux")
        upload(c, suite(("flips", "pass")), commit_sha=other, ci_run_id="2", variant="linux",
               rerun=1)  # fmt: skip
        response = c.get(f"/repos/{REPO}/flaky", params={"days": 30}, headers=AUTH).json()
        filtered = c.get(f"/repos/{REPO}/flaky", params={"min_runs": 5}, headers=AUTH).json()
        missing = c.get("/repos/acme/none/flaky", headers=AUTH)

    assert [(t["test_id"], t["flaky"], t["pairs"], t["runs"]) for t in response["tests"]] == [
        ("tests.t::flips", 2, 2, 4)]  # fmt: skip
    assert filtered["tests"] == []  # it ran in 4 runs
    assert response["main_known_flaky"] == []
    assert missing.status_code == 404


# --- the command ----------------------------------------------------------------------------

FLAKY_RESPONSE = {
    "repo": REPO, "days": 30, "window_start": "2026-10-18T00:00:00Z",
    "window_end": "2026-11-17T00:00:00Z", "truncated": False,
    "tests": [
        {"test_id": "tests/test_net.py::test_fetch", "file_path": "tests/test_net.py", "runs": 40,
         "pairs": 30, "flaky": 4, "last_flaky_at": "2026-11-15T10:00:00Z"},
        {"test_id": POOL, "file_path": "tests/test_db.py", "runs": 40, "pairs": 30, "flaky": 2,
         "last_flaky_at": "2026-11-10T10:00:00Z"},
        {"test_id": 'tests/test_x.py::test_q["a"]', "file_path": None, "runs": 9, "pairs": 9,
         "flaky": 1, "last_flaky_at": "2026-11-01T10:00:00Z"},
    ],
    "main_known_flaky": ["tests/test_net.py::*"], "main_known_flaky_at": "2026-11-16T09:00:00Z",
}  # fmt: skip


def run_flaky(cwd: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> Any:
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("SIFTWISE_API_TOKEN", "t")
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{SERVER}/repos/{REPO}/flaky").respond(200, json=FLAKY_RESPONSE)
        result = CliRunner().invoke(cli.app, ["flaky", "--repo", REPO, "--server", SERVER, *args])
    return result, route


def test_flaky_compares_with_the_checkouts_list_and_suggests_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(tmp_path, "init", "-q")
    listed = f'{{ test = "{POOL}", until = "2000-01-01" }}, "tests/test_net.py::*"'
    (tmp_path / ".siftwise.toml").write_text(f"known_flaky = [{listed}]\n")

    result, route = run_flaky(tmp_path, monkeypatch, "--days", "30", "--until", "2027-03-01")

    assert result.exit_code == 0, result.output
    assert route.calls[0].request.url.params["days"] == "30"
    out = result.stdout
    assert "tests/test_net.py::test_fetch  [known_flaky: tests/test_net.py::*]" in out
    assert f"{POOL}  [expired: {POOL} (until 2000-01-01)]" in out
    assert ".siftwise.toml (as of today, UTC)" in out
    assert out.rstrip().endswith(
        f'    {{ test = "{POOL}", until = "2027-03-01" }},\n'
        '    { test = "tests/test_x.py::test_q[\\"a\\"]", until = "2027-03-01" },'
    )
    suggested = out.split("Suggested known_flaky entries")[1].split(":", 1)[1]
    assert loads_repo_config(f"known_flaky = [{suggested}]").known_flaky[1].test == (
        'tests/test_x.py::test_q["a"]'
    )  # pasteable TOML


def test_flaky_outside_a_checkout_uses_the_newest_main_runs_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _ = run_flaky(tmp_path, monkeypatch, "--json", "--until", "none")

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["known_flaky_source"] == "the list the newest main run applied (2026-11-16)"
    assert [t["known_flaky"] for t in data["tests"]] == ["tests/test_net.py::*", None, None]
    assert data["suggested"] == [json.dumps(POOL), json.dumps('tests/test_x.py::test_q["a"]')]


def test_flaky_rejects_a_bad_until(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIFTWISE_API_TOKEN", "t")
    result = CliRunner().invoke(cli.app, ["flaky", "--repo", REPO, "--until", "soon"])

    assert result.exit_code == 2
    assert "--until must be YYYY-MM-DD or 'none'" in result.stderr
