"""`siftwise gitlab select|record|rollup`: pipeline variables, dotenv outputs, the merge request
note (Notes API mocked with respx), fail-open, and the security requirements (no real GitLab)."""

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml  # type: ignore[import-untyped]  # PyYAML, through uvicorn[standard]
from typer.testing import CliRunner, Result

import siftwise
from siftwise.cli import gha, gitlab
from siftwise.cli.main import app
from tests.test_gha import failure, git, recorded_runs, selection, triage_response

ROOT = Path(__file__).resolve().parents[1]
REPO = "acme/shop"
SERVER = "http://siftwise:8000"  # a service hostname: no plain-http warning
GL = "https://gitlab.test/api/v4"
MR = f"{GL}/projects/42/merge_requests/7"
JOB_URL = "https://gitlab.test/acme/shop/-/jobs/900"
# Distinctive values: a test fails if any of them shows up where it mustn't.
SIFT_TOKEN = "sift-SECRET-0123456789abcdef"
GL_TOKEN = "glpat-SECRET-fedcba9876543210"
JOB_TOKEN = "glcbt-JOBSECRET-55555555"
SECRETS = (SIFT_TOKEN, GL_TOKEN, JOB_TOKEN)
BOT = 99  # the token's own user
SOURCE_SHA = "c" * 40  # a merged results pipeline's MR head


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A checkout: main, then an MR branch changing src/cart.py. Returns the commit SHAs."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "config", "commit.gpgsign", "false")
    (root / "src").mkdir()
    (root / "src" / "cart.py").write_text("x = 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    base = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", "-b", "feature")
    (root / "src" / "cart.py").write_text("x = 2\n")
    git(root, "commit", "-q", "-am", "change")
    head = git(root, "rev-parse", "HEAD")
    monkeypatch.chdir(root)
    return {"base": base, "head": head}


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.post(f"{SERVER}/repos/{REPO}/shadow").respond(json={"shadow": False})
        yield router


def mr_env(shas: dict[str, str], **overrides: str | None) -> dict[str, str | None]:
    """A merge request pipeline (detached: CI_COMMIT_SHA is the MR head)."""
    return {
        "CI_PIPELINE_SOURCE": "merge_request_event",
        "CI_MERGE_REQUEST_IID": "7",
        "CI_MERGE_REQUEST_EVENT_TYPE": "detached",
        "CI_COMMIT_SHA": shas["head"],
        "CI_MERGE_REQUEST_DIFF_BASE_SHA": shas["base"],
        "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME": "feature",
        "CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "main",
        "CI_MERGE_REQUEST_PROJECT_ID": "42",
        "CI_MERGE_REQUEST_SOURCE_PROJECT_ID": "42",
        "CI_MERGE_REQUEST_PROJECT_PATH": REPO,
        "CI_PROJECT_ID": "42",
        "CI_PROJECT_PATH": REPO,
        "CI_DEFAULT_BRANCH": "main",
        "CI_PIPELINE_ID": "555",
        "CI_PIPELINE_URL": "https://gitlab.test/acme/shop/-/pipelines/555",
        "CI_PIPELINE_CREATED_AT": "2026-10-26T10:00:00Z",
        "CI_JOB_URL": JOB_URL,
        "CI_API_V4_URL": GL,
        "CI_SERVER_URL": "https://gitlab.test",
        "CI_JOB_TOKEN": JOB_TOKEN,
        "SIFTWISE_SERVER": SERVER,
        "SIFTWISE_TOKEN": SIFT_TOKEN,
        "SIFTWISE_GITLAB_TOKEN": GL_TOKEN,
        **overrides,
    }


def fork_env(shas: dict[str, str], **overrides: str | None) -> dict[str, str | None]:
    """A fork's MR pipeline, run in the fork: none of the parent's variables."""
    return mr_env(shas, CI_PROJECT_ID="77", CI_PROJECT_PATH="someone/shop",
                  CI_MERGE_REQUEST_SOURCE_PROJECT_ID="77", SIFTWISE_TOKEN=None,
                  SIFTWISE_GITLAB_TOKEN=None, **overrides)  # fmt: skip


def run(step: str, env: dict[str, str | None], *args: str) -> Result:
    return CliRunner().invoke(app, ["gitlab", step, *args], env=env)


def dotenv(step: str) -> dict[str, str]:
    """The job's dotenv report, as GitLab reads it (the last value of a name wins)."""
    values: dict[str, str] = {}
    for line in (gitlab.OUT_DIR / f"{step}.env").read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        values[name] = value
    return values


def notes(api: respx.MockRouter, *existing: dict[str, Any], head: str = "") -> dict[str, Any]:
    """The Notes API (https://docs.gitlab.com/api/notes/): returns the create/update routes."""
    api.get(f"{GL}/user").respond(json={"id": BOT, "username": "project_42_bot"})
    api.get(f"{MR}/notes").respond(json=list(existing))
    if head:
        api.get(MR).respond(json={"iid": 7, "sha": head})
    return {
        "create": api.post(f"{MR}/notes").respond(201, json={"id": 1}),
        "update": api.put(url__regex=rf"{re.escape(MR)}/notes/\d+$").respond(json={}),
    }


def note(body: str, *, author: int = BOT, note_id: int = 5, system: bool = False) -> dict[str, Any]:
    return {"id": note_id, "body": body, "system": system, "author": {"id": author}}


def sent(route: respx.Route) -> str:
    return str(json.loads(route.calls.last.request.content)["body"])


def written() -> str:
    """Everything siftwise wrote under .siftwise-ci/."""
    if not gitlab.OUT_DIR.is_dir():
        return ""
    return "".join(p.read_text(encoding="utf-8") for p in sorted(gitlab.OUT_DIR.rglob("*"))
                   if p.is_file())  # fmt: skip


def assert_no_secrets(*texts: str) -> None:
    for text in texts:
        for secret in SECRETS:
            assert secret not in text


# --- the pipeline -------------------------------------------------------------------------


def test_a_merge_request_pipeline(repo: dict[str, str]) -> None:
    ctx = gitlab.load_context({k: v for k, v in mr_env(repo).items() if v is not None})

    assert (ctx.head_sha, ctx.base, ctx.branch) == (repo["head"], repo["base"], "feature")
    assert (ctx.repo, ctx.project_id, ctx.pr_number) == (REPO, "42", 7)
    assert (ctx.run_id, ctx.run_attempt, ctx.is_main, ctx.fork) == ("555", 1, False, False)
    assert ctx.change_request
    assert ctx.run_url == "https://gitlab.test/acme/shop/-/pipelines/555"
    assert ctx.api_url == GL


def test_a_merged_results_pipeline_records_the_mr_head_not_the_merge_commit(
    repo: dict[str, str],
) -> None:
    env = mr_env(repo, CI_COMMIT_SHA="f" * 40, CI_MERGE_REQUEST_EVENT_TYPE="merged_result",
                 CI_MERGE_REQUEST_SOURCE_BRANCH_SHA=SOURCE_SHA)  # fmt: skip
    ctx = gitlab.load_context({k: v for k, v in env.items() if v is not None})

    assert ctx.head_sha == SOURCE_SHA
    assert ctx.base == repo["base"]


def test_a_merge_train_has_no_diff_base(repo: dict[str, str]) -> None:
    env = mr_env(repo, CI_MERGE_REQUEST_EVENT_TYPE="merge_train")
    ctx = gitlab.load_context({k: v for k, v in env.items() if v is not None})

    assert ctx.base is None  # the train tests other MRs too: the full suite


def test_a_fork_records_into_the_target_project(repo: dict[str, str]) -> None:
    ctx = gitlab.load_context({k: v for k, v in fork_env(repo).items() if v is not None})

    assert ctx.fork and ctx.repo == REPO and ctx.project_id == "42"
    assert not gitlab.runs_in_parent({"CI_PROJECT_ID": "77", "CI_MERGE_REQUEST_PROJECT_ID": "42"})
    assert gitlab.runs_in_parent({"CI_PROJECT_ID": "42", "CI_MERGE_REQUEST_PROJECT_ID": "42"})


@pytest.mark.parametrize(
    ("branch", "before", "main", "base"),
    [("main", "base", True, "base"),  # a push to main: the pushed commits
     ("feature", "base", False, "base"),
     ("feature", "0" * 40, False, "head^")],  # a new branch: its first parent
)  # fmt: skip
def test_branch_pipelines(
    repo: dict[str, str], branch: str, before: str, main: bool, base: str
) -> None:
    env = {"CI_PIPELINE_SOURCE": "push", "CI_COMMIT_SHA": repo["head"], "CI_COMMIT_BRANCH": branch,
           "CI_COMMIT_BEFORE_SHA": repo.get(before, before), "CI_DEFAULT_BRANCH": "main",
           "CI_PROJECT_PATH": REPO, "CI_PIPELINE_ID": "555"}  # fmt: skip
    ctx = gitlab.load_context(env)

    assert ctx.is_main is main and ctx.branch == branch and ctx.pr_number is None
    assert ctx.base == (f"{repo['head']}^" if base == "head^" else repo["base"])
    assert not ctx.change_request


def test_scheduled_and_tag_pipelines_have_no_diff_base(repo: dict[str, str]) -> None:
    common = {"CI_COMMIT_SHA": repo["head"], "CI_DEFAULT_BRANCH": "main"}
    schedule = gitlab.load_context({**common, "CI_PIPELINE_SOURCE": "schedule",
                                    "CI_COMMIT_BRANCH": "main"})  # fmt: skip
    tag = gitlab.load_context({**common, "CI_PIPELINE_SOURCE": "web", "CI_COMMIT_TAG": "v1"})

    assert schedule.base is None and schedule.is_main  # main runs record history
    assert tag.base is None and not tag.is_main and tag.branch == "v1"


# --- select --------------------------------------------------------------------------------


def test_select_passes_the_selection_to_later_jobs_and_comments(
    repo: dict[str, str], api: respx.MockRouter
) -> None:
    command = "pytest --junitxml=.siftwise-ci/junit/siftwise-1.xml tests/test_cart.py"
    select = api.post(f"{SERVER}/select").respond(json=selection(command=command))
    routes = notes(api)

    result = run("select", mr_env(repo))

    assert result.exit_code == 0, result.output
    out = dotenv("select")
    assert out["SIFTWISE_MODE"] == "selective" and out["SIFTWISE_SELECTED"] == "2"
    assert out["SIFTWISE_JUNIT_DIR"] == ".siftwise-ci/junit"
    assert out["SIFTWISE_SHADOW_RUN"] == "false"
    # The command is a file: it can be longer than dotenv allows, and has quotes.
    assert out["SIFTWISE_COMMAND_FILE"] == ".siftwise-ci/command.sh"
    assert Path(out["SIFTWISE_COMMAND_FILE"]).read_text() == command + "\n"
    body = json.loads(select.calls.last.request.content)
    assert body["junit_dir"] == ".siftwise-ci/junit" and body["changed_files"] == ["src/cart.py"]
    assert select.calls.last.request.headers["authorization"] == f"Bearer {SIFT_TOKEN}"
    # The note: created with the GitLab token, the selection section in it.
    created = routes["create"].calls.last.request
    assert created.headers["private-token"] == GL_TOKEN
    assert gha.COMMENT_MARKER in sent(routes["create"]) and gha.SELECTION_MARKER in sent(
        routes["create"])  # fmt: skip
    assert "siftwise test selection" in (gitlab.OUT_DIR / "select.md").read_text()


def test_select_updates_its_own_note_only(repo: dict[str, str], api: respx.MockRouter) -> None:
    api.post(f"{SERVER}/select").respond(json=selection())
    # Anyone can post a note with the marker: only the token's own user's counts.
    planted = note(f"{gha.COMMENT_MARKER} not ours", author=1, note_id=3)
    ours = note(gha.compose_comment("old selection", None), note_id=8)
    routes = notes(api, planted, note("a system note", note_id=4, system=True), ours)

    result = run("select", mr_env(repo))

    assert result.exit_code == 0, result.output
    assert not routes["create"].called
    assert str(routes["update"].calls.last.request.url).endswith("/notes/8")
    assert "old selection" not in sent(routes["update"])


def test_notes_are_paged(repo: dict[str, str], api: respx.MockRouter) -> None:
    api.post(f"{SERVER}/select").respond(json=selection())
    api.get(f"{GL}/user").respond(json={"id": BOT})
    pages = api.get(f"{MR}/notes")
    pages.side_effect = [
        httpx.Response(200, json=[note("chatter", note_id=1)], headers={"x-next-page": "2"}),
        httpx.Response(200, json=[note(gha.compose_comment("x", None), note_id=9)],
                       headers={"x-next-page": ""}),
    ]  # fmt: skip
    update = api.put(f"{MR}/notes/9").respond(json={})

    result = run("select", mr_env(repo))

    assert result.exit_code == 0, result.output
    assert update.called
    assert [call.request.url.params["page"] for call in pages.calls] == ["1", "2"]


def test_a_note_the_token_cant_write_is_a_warning(
    repo: dict[str, str], api: respx.MockRouter
) -> None:
    api.post(f"{SERVER}/select").respond(json=selection())
    routes = notes(api)
    routes["create"].respond(403, json={"message": f"403 Forbidden {GL_TOKEN}"})

    result = run("select", mr_env(repo))

    assert result.exit_code == 0, result.output
    assert "WARNING: siftwise: merge request comment failed: HTTP 403" in result.output
    assert "api scope and at least the Reporter role" in result.output
    assert dotenv("select")["SIFTWISE_MODE"] == "selective"
    assert_no_secrets(result.output, written())


def test_without_a_gitlab_token_there_is_no_note(
    repo: dict[str, str], api: respx.MockRouter
) -> None:
    api.post(f"{SERVER}/select").respond(json=selection())

    result = run("select", mr_env(repo, SIFTWISE_GITLAB_TOKEN=None))

    assert result.exit_code == 0, result.output
    assert "WARNING: siftwise: no SIFTWISE_GITLAB_TOKEN: no merge request comment" in result.output
    assert dotenv("select")["SIFTWISE_MODE"] == "selective"


@pytest.mark.parametrize(
    ("route", "why"),
    [("down", "could not reach siftwise"), ("500", "siftwise returned 500")],
)
def test_select_fails_open_to_the_full_suite(
    repo: dict[str, str], api: respx.MockRouter, route: str, why: str
) -> None:
    if route == "down":
        api.post(f"{SERVER}/select").mock(side_effect=httpx.ConnectError(f"refused {SIFT_TOKEN}"))
    else:
        api.post(f"{SERVER}/select").respond(500, json={"detail": f"bad token {SIFT_TOKEN}"})
    notes(api)

    result = run("select", mr_env(repo))

    assert result.exit_code == 0, result.output
    assert f"WARNING: siftwise: {why}" in result.output
    assert "running the full suite" in result.output
    out = dotenv("select")
    assert out["SIFTWISE_MODE"] == "full"
    assert Path(out["SIFTWISE_COMMAND_FILE"]).read_text() == ""
    assert_no_secrets(result.output, written())  # redacted, though the errors carried it


def test_select_without_server_or_token_runs_the_full_suite(
    repo: dict[str, str], api: respx.MockRouter
) -> None:
    notes(api)
    unset = run("select", mr_env(repo, SIFTWISE_SERVER=None))
    no_token = run("select", fork_env(repo))

    for result in (unset, no_token):
        assert result.exit_code == 0, result.output
        assert dotenv("select")["SIFTWISE_MODE"] == "full"
    assert "SIFTWISE_SERVER is not set" in unset.output
    assert "merge request from a fork: no comment" in no_token.output
    assert not [call for call in api.calls if call.request.url.host == "siftwise"]


def test_a_shallow_clone_fails_select_with_the_fix(
    repo: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gha, "git_is_shallow", lambda: True)

    result = run("select", mr_env(repo))

    assert result.exit_code == 1
    assert "ERROR: siftwise needs the full git history" in result.output
    assert 'GIT_DEPTH: "0"' in result.output
    # Written first: the test jobs still run the full suite.
    assert dotenv("select")["SIFTWISE_MODE"] == "full"


@pytest.mark.parametrize(
    ("url", "warned"),
    [("http://siftwise.example.com", True), ("http://10.0.0.5:8000", True),
     ("http://[fd00::5]:8000", True), ("https://siftwise.example.com", False),
     ("http://siftwise:8000", False), ("http://localhost:8000", False),
     ("http://127.0.0.1:8000", False)],
)  # fmt: skip
def test_plain_http_outside_the_job_is_a_warning(url: str, warned: bool) -> None:
    assert gitlab.insecure_server(url) is warned


def test_select_warns_about_plain_http_and_fork_code_with_tokens(
    repo: dict[str, str], api: respx.MockRouter
) -> None:
    api.post("http://siftwise.example.com/select").respond(json=selection())
    notes(api)
    # A fork's MR pipeline that a member ran in the parent project: it has the tokens.
    env = mr_env(repo, SIFTWISE_SERVER="http://siftwise.example.com",
                 CI_MERGE_REQUEST_SOURCE_PROJECT_ID="77")  # fmt: skip

    result = run("select", env)

    assert result.exit_code == 0, result.output
    assert "is plain http: the token crosses the network unencrypted" in result.output
    assert "comes from a fork and runs with this project's CI/CD variables" in result.output


# --- record --------------------------------------------------------------------------------


def upload_route(api: respx.MockRouter) -> respx.Route:
    Path("junit.xml").write_text("<testsuite/>")
    return api.post(f"{SERVER}/runs").respond(
        201, json={"run_id": 9, "created": True, "counts": {"total": 1}})  # fmt: skip


def test_record_uploads_the_mr_head_with_the_pipeline(
    repo: dict[str, str], api: respx.MockRouter
) -> None:
    upload = upload_route(api)
    Path(gitlab.JUNIT_DIR).mkdir(parents=True)
    Path(gitlab.JUNIT_DIR, "siftwise-1.xml").write_text("<testsuite/>")
    env = mr_env(repo, CI_COMMIT_SHA="f" * 40, CI_MERGE_REQUEST_EVENT_TYPE="merged_result",
                 CI_MERGE_REQUEST_SOURCE_BRANCH_SHA=repo["head"], SIFTWISE_DEFER_ROLLUP="true",
                 SIFTWISE_TRIAGE="false", SIFTWISE_VARIANT="py3.12")  # fmt: skip

    result = run("record", env)

    assert result.exit_code == 0, result.output
    [meta] = recorded_runs(upload)
    assert meta["commit_sha"] == repo["head"]  # never the merge commit
    assert (meta["repo"], meta["branch"], meta["variant"]) == (REPO, "feature", "py3.12")
    assert (meta["ci_run_id"], meta["run_attempt"]) == ("555", 1)
    assert meta["started_at"] == "2026-10-26T10:00:00Z" and meta["is_main"] is False
    assert meta["changed_files"] == ["src/cart.py"] and meta["changed_files_known"]
    assert upload.calls.last.request.url.params["defer_rollup"] == "true"
    files = re.findall(rb'filename="([^"]+)"', upload.calls.last.request.content)
    assert sorted(files) == [b"junit.xml", b"siftwise-1.xml"]  # **/junit*.xml + the junit dir
    assert "stored: 2 file(s)" in result.output


def test_record_applies_the_mr_base_commits_known_flaky(
    repo: dict[str, str], api: respx.MockRouter
) -> None:
    upload = upload_route(api)
    root = Path.cwd()
    git(root, "checkout", "-q", "main")
    (root / ".siftwise.toml").write_text('known_flaky = ["tests/test_base.py::*"]\n')
    git(root, "add", ".siftwise.toml")
    git(root, "commit", "-q", "-m", "config")
    base = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", "feature")
    # The MR lists its own failures as flaky: they must not count.
    (root / ".siftwise.toml").write_text('known_flaky = ["**"]\n')
    env = mr_env({**repo, "base": base}, SIFTWISE_TRIAGE="false")

    result = run("record", env)

    assert result.exit_code == 0, result.output
    [meta] = recorded_runs(upload)
    assert meta["known_flaky"] == ["tests/test_base.py::*"] and meta["known_flaky_changed"]


def test_a_retried_job_says_its_upload_is_the_first_ones(
    repo: dict[str, str], api: respx.MockRouter
) -> None:
    api.post(f"{SERVER}/runs").respond(200, json={"run_id": 9, "created": False,
                                                   "counts": {"total": 1}})  # fmt: skip
    Path("junit.xml").write_text("<testsuite/>")

    result = run("record", mr_env(repo, CI_JOB_RETRY_COUNT="1", SIFTWISE_TRIAGE="false"))

    assert result.exit_code == 0, result.output
    assert "a retried job keeps its pipeline ID" in result.output
    assert "already stored" in result.output


def test_record_without_a_token_skips_the_upload(
    repo: dict[str, str], api: respx.MockRouter
) -> None:
    Path("junit.xml").write_text("<testsuite/>")

    result = run("record", fork_env(repo, SIFTWISE_TRIAGE="false"))

    assert result.exit_code == 0, result.output
    assert "no token (a fork, or a protected variable on an unprotected branch?)" in result.output
    assert not api.calls


# --- rollup --------------------------------------------------------------------------------


def triage_route(api: respx.MockRouter, *results: dict[str, Any]) -> respx.Route:
    api.post(f"{SERVER}/repos/{REPO}/rollup").respond(json={"tests_updated": 3, "seconds": 0.1})
    return api.post(f"{SERVER}/repos/{REPO}/triage").respond(
        json=triage_response(*results, variants=("py3.12",)))  # fmt: skip


def test_rollup_writes_the_failures_into_the_note_and_gates(
    repo: dict[str, str], api: respx.MockRouter
) -> None:
    triage = triage_route(api, failure("tests.test_cart::test_total", "new_regression", "py3.12"))
    routes = notes(api, note(gha.compose_comment("### siftwise test selection", None)),
                   head=repo["head"])  # fmt: skip

    result = run("rollup", mr_env(repo, SIFTWISE_FAIL_ON="new_regression"))

    assert result.exit_code == 1
    assert "ERROR: siftwise: fail-on matched new_regression" in result.output
    assert json.loads(triage.calls.last.request.content)["ci_run_id"] == "555"
    body = sent(routes["update"])
    assert "### siftwise test selection" in body  # the select job's section stays
    assert f"<!-- siftwise:failures sha={repo['head']} attempt=1 -->" in body
    assert "` tests.test_cart::test_total `" in body and "**new regression**" in body
    out = dotenv("rollup")
    assert out["SIFTWISE_VERDICTS_AVAILABLE"] == "true"
    assert out["SIFTWISE_COUNT_NEW_REGRESSION"] == "1" and out["SIFTWISE_COUNT_FLAKY"] == "0"
    assert out["SIFTWISE_REPORT"] == ".siftwise-ci/failures.md"
    assert "tests.test_cart::test_total" in gitlab.FAILURES_REPORT.read_text()


def test_the_note_links_the_full_list_in_the_jobs_artifacts(
    repo: dict[str, str], api: respx.MockRouter
) -> None:
    many = [failure(f"tests.test_a::test_{i}", "unknown", "py3.12") for i in range(25)]
    triage_route(api, *many)
    routes = notes(api, head=repo["head"])

    result = run("rollup", mr_env(repo))

    assert result.exit_code == 0, result.output
    assert f"+5 more: [full list: .siftwise-ci/failures.md in this job's artifacts]({JOB_URL})" in (
        sent(routes["create"]))  # fmt: skip


def test_rollup_leaves_the_note_of_a_newer_push_alone(
    repo: dict[str, str], api: respx.MockRouter
) -> None:
    triage_route(api, failure("t::a", "unknown", "py3.12"))
    routes = notes(api, head="e" * 40)

    result = run("rollup", mr_env(repo))

    assert result.exit_code == 0, result.output
    assert not routes["create"].called and not routes["update"].called
    assert "newer commits: comment not updated" in (gitlab.OUT_DIR / "rollup.md").read_text()


def test_a_fork_without_tokens_skips_fail_on(repo: dict[str, str], api: respx.MockRouter) -> None:
    result = run("rollup", fork_env(repo, SIFTWISE_FAIL_ON="new_regression"))

    assert result.exit_code == 0, result.output
    assert "fail-on skipped: no siftwise token in a merge request from a fork" in result.output
    assert dotenv("rollup")["SIFTWISE_VERDICTS_AVAILABLE"] == "false"
    assert not api.calls


def test_a_missing_token_elsewhere_fails_fail_on_closed(
    repo: dict[str, str], api: respx.MockRouter
) -> None:
    # A protected variable doesn't reach an unprotected branch: the gate must not go quiet.
    notes(api, head=repo["head"])
    result = run("rollup", mr_env(repo, SIFTWISE_TOKEN=None, SIFTWISE_FAIL_ON="unknown"))

    assert result.exit_code == 1
    assert "ERROR: siftwise verdicts unavailable (no siftwise server or token" in result.output


def test_rollup_with_siftwise_down_fails_open_without_fail_on(
    repo: dict[str, str], api: respx.MockRouter
) -> None:
    api.post(f"{SERVER}/repos/{REPO}/triage").mock(side_effect=httpx.ConnectError("refused"))
    api.post(f"{SERVER}/repos/{REPO}/rollup").mock(side_effect=httpx.ConnectError("refused"))
    routes = notes(api, head=repo["head"])

    result = run("rollup", mr_env(repo))

    assert result.exit_code == 0, result.output
    assert "Failure verdicts unavailable" in sent(routes["create"])
    assert "WARNING: siftwise: stats not updated" in result.output


# --- security ------------------------------------------------------------------------------


@pytest.mark.parametrize("step", ["select", "record", "rollup"])
def test_tokens_are_never_options(step: str) -> None:
    help_text = run(step, {}, "--help").output

    assert not re.search(r"--[\w-]*token", help_text, flags=re.IGNORECASE)
    for option in ("--token", "--gitlab-token", "--api-token", "--github-token"):
        assert run(step, {}, option, "x").exit_code == 2  # no such option


def test_secrets_never_reach_logs_files_or_the_note(
    repo: dict[str, str], api: respx.MockRouter
) -> None:
    # Every place an error could echo a secret does.
    api.post(f"{SERVER}/select").respond(500, json={"detail": f"{SIFT_TOKEN} {GL_TOKEN}"})
    api.post(f"{SERVER}/repos/{REPO}/triage").respond(
        json=triage_response(failure(f"t::{SIFT_TOKEN}", "unknown", "py3.12",
                                     reason=f"reason {GL_TOKEN} {JOB_TOKEN}"),
                             variants=("py3.12",)))  # fmt: skip
    api.post(f"{SERVER}/repos/{REPO}/rollup").mock(
        side_effect=httpx.ConnectError(f"refused {SIFT_TOKEN}"))  # fmt: skip
    api.post(f"{SERVER}/runs").mock(side_effect=httpx.ConnectError(f"refused {SIFT_TOKEN}"))
    Path("junit.xml").write_text("<testsuite/>")
    routes = notes(api, head=repo["head"])

    outputs = [run(step, mr_env(repo, SIFTWISE_TRIAGE="false")).output
               for step in ("select", "record", "rollup")]  # fmt: skip

    bodies = [json.loads(call.request.content)["body"]
              for route in routes.values() for call in route.calls]  # fmt: skip
    assert bodies and gitlab.MASKED in "".join(bodies)
    assert_no_secrets(*outputs, written(), *bodies)


def test_no_line_of_a_note_can_start_a_quick_action() -> None:
    body = "### title\n/close\n  /merge\n- `x`\n> /approve"

    assert gitlab.note_safe(body) == "### title\n\\/close\n  \\/merge\n- `x`\n> /approve"


def outside_code(body: str) -> str:
    """The note without its code spans: what GitLab renders as Markdown."""
    return re.sub(r"(`+) .*? \1(?!`)", "<code-span>", body)


HOSTILE = [
    "t::@all please look",
    "t::#1 and !2 and ~bug and %v1 and $x$ and &3",
    "t::[click](https://evil.test) ![img](https://evil.test/x.png)",
    "t::<img src=x onerror=alert(1)> <!-- siftwise:failures sha=0 attempt=9 -->",
    "t::%{project_path} https://evil.test/autolink",
    "t::a|b`c``d",
    "t::line one\n/close\n/merge",
]


def test_untrusted_text_in_the_note_is_inert(repo: dict[str, str], api: respx.MockRouter) -> None:
    results = [failure(test_id, "new_regression", "py3.12", reason=f"because {test_id}")
               for test_id in HOSTILE]  # fmt: skip
    results.append(failure(HOSTILE[0], "new_regression", "py3.13|x\n/close"))
    api.post(f"{SERVER}/repos/{REPO}/triage").respond(json=triage_response(
        *results, variants=("py3.12", "py3.13|x\n/close")))  # fmt: skip
    api.post(f"{SERVER}/repos/{REPO}/rollup").respond(json={"tests_updated": 0, "seconds": 0})
    routes = notes(api, head=repo["head"])

    result = run("rollup", mr_env(repo))

    assert result.exit_code == 0, result.output
    body = sent(routes["create"])
    rendered = outside_code(body)
    for marker in ("@", "evil.test", "<img", "](", "%{", "$x$", "#1", "!2", "~bug", "&3"):
        assert marker not in rendered, marker
    assert not re.search(r"^\s*/", body, flags=re.MULTILINE)  # no quick action
    # Only our own section markers: the planted one can't split the note.
    assert len(gha.FAILURES_MARKER.findall(body)) == 1
    assert gha.split_comment(body)[1] is not None
    # Every table row keeps its cells: a pipe can't add one.
    rows = [line for line in body.splitlines() if re.match(r"\| `+ t::", line)]
    assert len(rows) == len(HOSTILE)
    assert all(len(re.split(r"(?<!\\)\|", row)) == 6 + 1 for row in rows)  # 5 cells (Variants)


def test_code_spans() -> None:
    assert gitlab.code_span("a") == "` a `"
    assert gitlab.code_span("a`b``c") == "``` a`b``c ```"
    assert gitlab.code_span("a|b") == "` a|b `"
    assert gitlab.code_span("a|b", table=True) == "` a\\|b `"
    assert gitlab.code_span("a\nb\x1b[31m") == "` a b [31m `"
    assert gitlab.code_span("<!-- x -->") == "` <! -- x --> `"


def test_dotenv_values_are_one_safe_line() -> None:
    assert gitlab.dotenv_value('a "b" $X `c`\nD=1') == "a _b_ _X _c_ D=1"
    assert len(gitlab.dotenv_value("x" * 1000)) == 200


def test_the_dotenv_files_follow_gitlabs_rules(repo: dict[str, str], api: respx.MockRouter) -> None:
    api.post(f"{SERVER}/select").respond(json=selection(reason='it\'s\n$HOME `x` "q"'))
    notes(api)

    assert run("select", mr_env(repo)).exit_code == 0
    lines = (gitlab.OUT_DIR / "select.env").read_text(encoding="utf-8").splitlines()

    # https://docs.gitlab.com/ci/variables/dotenv_variables/: no empty lines or comments,
    # names of letters, digits and underscores, values without quotes or escapes, at most 5 KB.
    assert all(re.fullmatch(r"[A-Za-z0-9_]+=[^\"'`$\n]*", line) for line in lines), lines
    assert len({line.split("=")[0] for line in lines}) <= 20  # GitLab Self-Managed's default
    assert (gitlab.OUT_DIR / "select.env").stat().st_size < 5 * 1024


def test_log_lines_carry_no_control_characters() -> None:
    provider = gitlab.GitLabProvider("select", {})
    assert provider._log("a\x1b[0Ksection_start:1:x\r\nb") == "a [0Ksection_start:1:x  b"


# --- the template and the example ----------------------------------------------------------


class _GitLabYaml(yaml.SafeLoader):  # type: ignore[misc]
    pass


def _reference(loader: yaml.SafeLoader, node: yaml.Node) -> list[Any]:
    return ["!reference", loader.construct_sequence(node)]


_GitLabYaml.add_constructor("!reference", _reference)

TEMPLATE = ROOT / "gitlab" / "siftwise.gitlab-ci.yml"


def load_yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.load(path.read_text(encoding="utf-8"), Loader=_GitLabYaml)
    return data


def test_the_template_pins_this_release() -> None:
    template = load_yaml(TEMPLATE)

    # Bump at release, with __version__ (CONTRIBUTING "Releasing").
    assert template[".siftwise-base"]["variables"]["SIFTWISE_PIP_SPEC"] == (
        f"siftwise=={siftwise.__version__}")  # fmt: skip
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "/v" in text and "integrity: sha256-" in text  # tells users to pin a tag and hash


def test_the_template_jobs() -> None:
    template = load_yaml(TEMPLATE)
    select, record, rollup = (template[f".siftwise-{s}"] for s in ("select", "record", "rollup"))

    assert select["stage"] == ".pre" and select["allow_failure"] is True
    assert select["variables"]["GIT_DEPTH"] == "0" and record["variables"]["GIT_DEPTH"] == "0"
    assert select["artifacts"]["reports"]["dotenv"] == ".siftwise-ci/select.env"
    assert rollup["artifacts"]["reports"]["dotenv"] == ".siftwise-ci/rollup.env"
    assert rollup["stage"] == ".post" and rollup["when"] == "always"
    assert rollup["dependencies"] == []
    assert record["variables"]["SIFTWISE_TRIAGE"] == "false"
    assert record["after_script"][-1].endswith("siftwise gitlab record")
    for job in (select, rollup):
        assert job["script"][0] == ["!reference", [".siftwise-install", "script"]]


def test_the_template_never_passes_or_prints_secrets() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    commands = "\n".join(line for line in text.splitlines() if line.strip().startswith("- "))

    assert "set -x" not in commands and "CI_DEBUG_TRACE" not in commands
    assert "TOKEN" not in commands  # tokens stay in the environment
    assert "echo" not in commands


def test_the_example_carries_the_template_unchanged() -> None:
    example = ROOT / "examples" / "gitlab"

    assert (example / "siftwise.gitlab-ci.yml").read_bytes() == TEMPLATE.read_bytes()
    pipeline = load_yaml(example / ".gitlab-ci.yml")
    assert pipeline["include"] == [{"local": "siftwise.gitlab-ci.yml"}]
    assert pipeline["siftwise-select"]["extends"] == [".siftwise-select", ".example-server"]
    assert pipeline["siftwise-rollup"]["variables"]["SIFTWISE_FAIL_ON"] == "new_regression,unknown"
    # The only secret it needs is the note's; the siftwise token is made in each job.
    server = pipeline[".example-server"]
    assert "SIFTWISE_TOKEN" not in server["variables"]
    assert any("secrets.token_hex" in line for line in server["before_script"])


def test_the_examples_history_survives_a_dump_and_restore(
    db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """examples/gitlab/ci/history.py (also the dogfood workflow's) against real Postgres: a
    dump made at an OLDER migration loads into this siftwise's schema (its own schema first,
    then the newer migrations), new rows don't collide with the restored ones, a dump from a
    newer siftwise is skipped, and no secret from the environment reaches the log."""
    from sqlalchemy import create_engine, make_url, text

    from siftwise.migrate import upgrade

    admin = create_engine(db_url, isolation_level="AUTOCOMMIT")
    names = ("siftwise_history_a", "siftwise_history_b")
    urls = [make_url(db_url).set(database=n).render_as_string(hide_password=False) for n in names]
    with admin.connect() as conn:
        for name in names:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name}"))
            conn.execute(text(f"CREATE DATABASE {name}"))
    script = ROOT / "examples" / "gitlab" / "ci" / "history.py"
    monkeypatch.chdir(tmp_path)
    # A job's secrets are in its environment: none may reach the log.
    for name, secret in (("SIFTWISE_API_TOKEN", SIFT_TOKEN), ("SIFTWISE_GITLAB_TOKEN", GL_TOKEN),
                         ("CI_JOB_TOKEN", JOB_TOKEN)):  # fmt: skip
        monkeypatch.setenv(name, secret)

    def history(command: str, url: str) -> str:
        env = {**os.environ, "SIFTWISE_DATABASE_URL": url}
        proc = subprocess.run([sys.executable, str(script), command], check=True,
                              capture_output=True, text=True, env=env)  # fmt: skip
        assert_no_secrets(proc.stdout, proc.stderr)
        return proc.stdout

    try:
        # The source: an older siftwise's schema (before 0010 added runs.rerun), with a run.
        upgrade(urls[0], "0009")
        source = create_engine(urls[0])
        with source.begin() as conn:
            conn.execute(text("INSERT INTO repos (name) VALUES ('acme/shop')"))
            conn.execute(text("INSERT INTO runs (repo_id, commit_sha, branch, is_main) "
                              "SELECT id, repeat('a', 40), 'main', true FROM repos"))  # fmt: skip
        source.dispose()
        assert "1 runs, 1 on main, 0 tests" in history("dump", urls[0])
        assert (Path(".siftwise-history") / "alembic_version").read_text().strip() == "0009"

        out = history("restore", urls[1])
        assert re.search(r"restored \d+ tables from \.siftwise-history/ \(migration 0009\): "
                         r"1 runs, 1 on main, 0 tests", out), out  # fmt: skip
        assert "left empty" not in out
        target = create_engine(urls[1])
        with target.begin() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == (
                "0010"
            )  # then migrated on
            assert conn.execute(text("SELECT rerun FROM runs")).scalars().all() == [0]
            conn.execute(text("INSERT INTO repos (name) VALUES ('acme/other')"))  # a new ID
            assert conn.execute(text("SELECT count(*) FROM repos")).scalar() == 2
        target.dispose()

        # A dump from a siftwise newer than this one: not loaded.
        (Path(".siftwise-history") / "alembic_version").write_text("9999")
        out = history("restore", urls[1])
        assert "at migration 9999, which this siftwise doesn't have" in out
        assert "restored" not in out

        shutil.rmtree(".siftwise-history")
        assert "no .siftwise-history/ to load: starting empty" in history("restore", urls[1])
    finally:
        with admin.connect() as conn:
            for name in names:
                conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        admin.dispose()
