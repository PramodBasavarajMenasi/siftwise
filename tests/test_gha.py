"""`siftwise gha select|record|rollup`: context, outputs, comment, fail-open (no real GitHub)."""

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner, Result

from siftwise.cli import gha
from siftwise.cli.main import app

SERVER = "http://siftwise.test"
GH = "https://api.github.test"
REPO = "acme/shop"


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A checkout: main, then a PR branch changing src/cart.py. Returns the commit SHAs."""
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
        yield router


class Runner:
    """Runs a `siftwise gha` command with a GitHub-like environment and files."""

    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path / "github"
        self.dir.mkdir()
        self.output = self.dir / "output"
        self.summary = self.dir / "summary"
        self.github_env = self.dir / "env"
        self.event_path = self.dir / "event.json"

    def run(self, step: str, event_name: str, event: dict[str, Any], **env: str) -> Result:
        self.event_path.write_text(json.dumps(event))
        base_env = {
            "GITHUB_EVENT_NAME": event_name,
            "GITHUB_EVENT_PATH": str(self.event_path),
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_SHA": "f" * 40,  # the merge commit: never what's recorded
            "GITHUB_REF_NAME": "7/merge",
            "GITHUB_HEAD_REF": "feature",
            "GITHUB_RUN_ID": "555",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_API_URL": GH,
            "GITHUB_OUTPUT": str(self.output),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "GITHUB_ENV": str(self.github_env),
            "RUNNER_TEMP": str(self.dir),
            "SIFTWISE_SERVER": SERVER,
            "SIFTWISE_API_TOKEN": "sift-token",
            "SIFTWISE_GITHUB_TOKEN": "gh-token",
        }
        return CliRunner().invoke(app, ["gha", step], env={**base_env, **env})

    def outputs(self, path: Path | None = None) -> dict[str, str]:
        """Parse the heredoc-style ``name<<delimiter`` file GitHub reads."""
        lines = (path or self.output).read_text().splitlines() if (path or self.output).exists() \
            else []  # fmt: skip
        values: dict[str, str] = {}
        i = 0
        while i < len(lines):
            name, delimiter = lines[i].split("<<", 1)
            end = lines.index(delimiter, i + 1)
            values[name] = "\n".join(lines[i + 1 : end])
            i = end + 1
        return values


@pytest.fixture
def gh(tmp_path: Path) -> Runner:
    return Runner(tmp_path)


def pr_event(shas: dict[str, str], *, fork: bool = False, number: int = 7) -> dict[str, Any]:
    return {
        "number": number,
        "pull_request": {
            "number": number,
            "head": {"sha": shas["head"], "ref": "feature",
                     "repo": {"full_name": "someone/shop" if fork else REPO}},
            "base": {"sha": shas["base"], "ref": "main", "repo": {"full_name": REPO}},
        },
        "repository": {"default_branch": "main"},
    }  # fmt: skip


def selection(**overrides: Any) -> dict[str, Any]:
    return {
        "repo": REPO,
        "mode": "selective",
        "reason": "2 of 10 known tests selected",
        "selected_count": 2,
        "total_known": 10,
        "command": "pytest tests/test_cart.py",
        "commands": ["pytest tests/test_cart.py"],
        "estimated_ms": {"total": 600_000.0, "would_run": 60_000.0},
        "tests": [],
        **overrides,
    }


def no_comments(api: respx.MockRouter) -> respx.Route:
    api.get(f"{GH}/repos/{REPO}/issues/7/comments").respond(json=[])
    return api.post(f"{GH}/repos/{REPO}/issues/7/comments").respond(201, json={})


# --- context ------------------------------------------------------------------------------


def context(tmp_path: Path, event_name: str, event: dict[str, Any], **env: str) -> gha.Context:
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event))
    return gha.load_context({"GITHUB_EVENT_NAME": event_name, "GITHUB_EVENT_PATH": str(path),
                             "GITHUB_REPOSITORY": REPO, "GITHUB_SHA": "f" * 40,
                             "GITHUB_REF_NAME": "main", **env})  # fmt: skip


def test_pull_request_context_uses_the_pr_head_and_base(
    tmp_path: Path, repo: dict[str, str]
) -> None:
    ctx = context(tmp_path, "pull_request", pr_event(repo), GITHUB_HEAD_REF="feature")
    assert (ctx.head_sha, ctx.base, ctx.branch) == (repo["head"], repo["base"], "feature")
    assert (ctx.is_main, ctx.pr_number, ctx.fork) == (False, 7, False)

    forked = context(tmp_path, "pull_request", pr_event(repo, fork=True))
    assert forked.fork


def test_push_context_diffs_the_push(tmp_path: Path, repo: dict[str, str]) -> None:
    event = {"before": repo["base"], "repository": {"default_branch": "main"}}
    ctx = context(tmp_path, "push", event, GITHUB_SHA=repo["head"], GITHUB_REF_NAME="main")
    assert (ctx.base, ctx.head_sha, ctx.is_main, ctx.pr_number) == (
        repo["base"], repo["head"], True, None)  # fmt: skip

    # A new branch: no previous tip, so the first parent (like backfill).
    new_branch = context(tmp_path, "push", {**event, "before": "0" * 40},
                         GITHUB_SHA=repo["head"], GITHUB_REF_NAME="feature")  # fmt: skip
    assert (new_branch.base, new_branch.is_main) == (f"{repo['head']}^", False)


@pytest.mark.parametrize("event_name", ["schedule", "workflow_dispatch", "pull_request_target"])
def test_other_events_have_no_diff_base(
    tmp_path: Path, repo: dict[str, str], event_name: str
) -> None:
    ctx = context(tmp_path, event_name, pr_event(repo))
    assert ctx.base is None
    assert ctx.pr_number is None  # never comment from pull_request_target


def test_base_ref_input_wins(tmp_path: Path, repo: dict[str, str]) -> None:
    path = tmp_path / "event.json"
    path.write_text(json.dumps(pr_event(repo)))
    ctx = gha.load_context({"GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(path),
                            "GITHUB_REPOSITORY": REPO}, base_ref="origin/release")  # fmt: skip
    assert ctx.base == "origin/release"


# --- select -------------------------------------------------------------------------------


def test_select_sets_outputs_summary_and_comment(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    select = api.post(f"{SERVER}/select").respond(json=selection())
    created = no_comments(api)

    result = gh.run("select", "pull_request", pr_event(repo))

    assert result.exit_code == 0, result.output
    body = json.loads(select.calls.last.request.content)
    assert body["changed_files"] == ["src/cart.py"]  # PR base...PR head, not the merge commit
    assert body["junit_dir"] == str(gh.dir / "siftwise-junit")
    outputs = gh.outputs()
    assert {k: outputs[k] for k in ("mode", "command", "selected", "total")} == {
        "mode": "selective", "command": "pytest tests/test_cart.py", "selected": "2",
        "total": "10"}  # fmt: skip
    assert outputs["junit-dir"] == str(gh.dir / "siftwise-junit")
    assert (gh.dir / "siftwise-junit").is_dir()
    exported = gh.outputs(gh.github_env)
    assert exported["SIFTWISE_JUNIT_DIR"] == str(gh.dir / "siftwise-junit")
    summary = gh.summary.read_text()
    assert "**selective**" in summary and "2 of 10 known tests" in summary
    assert "1m 00s of 10m 00s (90% saved)" in summary
    comment = json.loads(created.calls.last.request.content)["body"]
    assert comment.startswith(gha.COMMENT_MARKER)
    assert "90% saved" in comment
    assert created.calls.last.request.headers["authorization"] == "Bearer gh-token"


def test_select_updates_the_existing_comment(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    api.post(f"{SERVER}/select").respond(json=selection())
    # The marker comment is on the second page of comments.
    page2 = f"{GH}/repos/{REPO}/issues/7/comments?per_page=100&page=2"
    api.get(page2).respond(json=[{"body": f"{gha.COMMENT_MARKER}\nold", "url": f"{GH}/c/42"}])
    api.get(f"{GH}/repos/{REPO}/issues/7/comments").respond(
        json=[{"body": "unrelated", "url": f"{GH}/c/1"}], headers={"link": f'<{page2}>; rel="next"'}
    )
    patched = api.patch(f"{GH}/c/42").respond(json={})
    created = api.post(f"{GH}/repos/{REPO}/issues/7/comments").respond(201, json={})

    assert gh.run("select", "pull_request", pr_event(repo)).exit_code == 0

    assert patched.called and not created.called


def test_select_on_a_fork_pr_skips_the_comment(gh: Runner, repo: dict[str, str]) -> None:
    # Forks get no secrets: no siftwise token, a read-only GitHub token.
    result = gh.run("select", "pull_request", pr_event(repo, fork=True), SIFTWISE_API_TOKEN="")

    assert result.exit_code == 0, result.output
    assert gh.outputs()["mode"] == "full"
    assert gh.outputs()["command"] == ""
    assert "no siftwise token" in gh.outputs()["reason"]
    assert "from a fork: no PR comment" in gh.summary.read_text()


def test_comment_permission_error_is_only_a_warning(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    api.post(f"{SERVER}/select").respond(json=selection())
    api.get(f"{GH}/repos/{REPO}/issues/7/comments").respond(403)

    result = gh.run("select", "pull_request", pr_event(repo))

    assert result.exit_code == 0
    assert "::warning::siftwise: PR comment failed: 403" in result.output
    assert "pull-requests: write" in gh.summary.read_text()


@pytest.mark.parametrize(
    "server",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("timed out"),
        httpx.Response(500, json={"detail": "boom"}),
        httpx.Response(401, json={"detail": "invalid or missing bearer token"}),
    ],
    ids=["unreachable", "timeout", "500", "401"],
)
def test_select_fails_open(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter, server: Any
) -> None:
    api.post(f"{SERVER}/select").mock(side_effect=[server])
    no_comments(api)

    result = gh.run("select", "pull_request", pr_event(repo))

    assert result.exit_code == 0, result.output
    outputs = gh.outputs()
    assert (outputs["mode"], outputs["command"]) == ("full", "")
    assert outputs["reason"].startswith("siftwise unavailable:")
    assert "::warning::" in result.output


def test_select_for_an_unknown_repo_says_no_history(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    api.post(f"{SERVER}/select").respond(404, json={"detail": "unknown repo 'acme/shop'"})
    no_comments(api)

    assert gh.run("select", "pull_request", pr_event(repo)).exit_code == 0
    assert (gh.outputs()["mode"], gh.outputs()["reason"]) == (
        "full", "no test history for this repo yet")  # fmt: skip


def test_select_fails_on_a_shallow_clone(
    gh: Runner, repo: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{Path.cwd()}", str(shallow))
    monkeypatch.chdir(shallow)

    result = gh.run("select", "pull_request", pr_event(repo))

    assert result.exit_code == 1
    assert "::error::" in result.output and "fetch-depth: 0" in result.output


# --- record -------------------------------------------------------------------------------


def upload_metadata(route: respx.Route) -> tuple[dict[str, Any], list[str], httpx.URL]:
    request = route.calls.last.request
    content_type = request.headers["content-type"]
    boundary = content_type.split("boundary=")[1].encode()
    parts = request.content.split(b"--" + boundary)
    meta: dict[str, Any] = {}
    names = []
    for part in parts:
        if b'name="metadata"' in part:
            meta = json.loads(part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0])
        elif b'name="files"' in part:
            names.append(part.split(b'filename="', 1)[1].split(b'"', 1)[0].decode())
    return meta, names, request.url


def test_record_uploads_with_run_metadata(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    (Path("junit.xml")).write_text("<testsuite/>")
    junit_dir = gh.dir / "siftwise-junit"
    junit_dir.mkdir()
    (junit_dir / "siftwise-1.xml").write_text("<testsuite/>")
    api.get(f"{GH}/repos/{REPO}/actions/runs/555").respond(
        json={"run_started_at": "2026-10-11T10:00:00Z"})  # fmt: skip
    upload = api.post(f"{SERVER}/runs").respond(
        201, json={"run_id": 9, "created": True, "counts": {"total": 3}})  # fmt: skip

    result = gh.run("record", "pull_request", pr_event(repo), SIFTWISE_VARIANT="linux-py3.12",
                    SIFTWISE_JUNIT_DIR=str(junit_dir))  # fmt: skip

    assert result.exit_code == 0, result.output
    meta, names, url = upload_metadata(upload)
    assert meta == {
        "repo": REPO,
        "commit_sha": repo["head"],  # the PR head, not GITHUB_SHA (the merge commit)
        "branch": "feature",
        "is_main": False,
        "ci_run_id": "555",
        "run_attempt": 2,
        "variant": "linux-py3.12",
        "started_at": "2026-10-11T10:00:00Z",
        "changed_files": ["src/cart.py"],
        "changed_files_known": True,
        # For triage: what the change links to (here, no test file imports src/cart.py).
        "affected": {"affected_packages": {}, "affected_files": {}, "depends": [],
                     "go_module": None},
    }  # fmt: skip
    assert sorted(names) == ["junit.xml", "siftwise-1.xml"]
    assert url.params["defer_rollup"] == "true"  # a variant: one rollup job after the matrix
    assert "stats update deferred" in gh.summary.read_text()


def test_record_without_variant_rolls_up_and_falls_back_to_the_select_time(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    Path("junit-unit.xml").write_text("<testsuite/>")
    api.get(f"{GH}/repos/{REPO}/actions/runs/555").respond(403)  # no actions: read
    upload = api.post(f"{SERVER}/runs").respond(
        201, json={"run_id": 9, "created": True, "counts": {"total": 1}})  # fmt: skip

    result = gh.run("record", "pull_request", pr_event(repo),
                    SIFTWISE_SELECT_STARTED_AT="2026-10-11T09:59:00+00:00")  # fmt: skip

    assert result.exit_code == 0, result.output
    meta, _, url = upload_metadata(upload)
    assert meta["started_at"] == "2026-10-11T09:59:00+00:00"
    assert meta["variant"] is None
    assert "defer_rollup" not in url.params


@pytest.mark.parametrize(
    "server", [httpx.ConnectError("refused"), httpx.Response(409, json={"detail": "conflict"})]
)
def test_record_never_fails_the_job(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter, server: Any
) -> None:
    Path("junit.xml").write_text("<testsuite/>")
    api.get(f"{GH}/repos/{REPO}/actions/runs/555").respond(404)
    api.post(f"{SERVER}/runs").mock(side_effect=[server])

    result = gh.run("record", "pull_request", pr_event(repo))

    assert result.exit_code == 0
    assert "::warning::siftwise: results not recorded" in result.output


def test_record_without_files_or_token_warns(gh: Runner, repo: dict[str, str]) -> None:
    nothing = gh.run("record", "pull_request", pr_event(repo))
    assert nothing.exit_code == 0 and "no JUnit files match" in nothing.output

    Path("junit.xml").write_text("<testsuite/>")
    no_token = gh.run("record", "pull_request", pr_event(repo), SIFTWISE_API_TOKEN="")
    assert no_token.exit_code == 0 and "results not recorded" in no_token.output


def test_find_junit_skips_tool_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for path in (
        "junit.xml",
        "reports/junit-a.xml",
        ".venv/lib/junit.xml",
        "node_modules/x/junit.xml",
    ):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("<testsuite/>")
    assert gha.find_junit("**/junit*.xml", "") == [Path("junit.xml"), Path("reports/junit-a.xml")]


# --- rollup -------------------------------------------------------------------------------


def test_rollup_step(gh: Runner, repo: dict[str, str], api: respx.MockRouter) -> None:
    route = api.post(f"{SERVER}/repos/{REPO}/rollup").respond(
        json={"repo": REPO, "tests_updated": 12, "window_days": 90, "seconds": 1.5})  # fmt: skip

    result = gh.run("rollup", "push", {})

    assert result.exit_code == 0, result.output
    assert route.calls.last.request.headers["authorization"] == "Bearer sift-token"
    assert "12 test stats updated" in gh.summary.read_text()

    route.mock(side_effect=[httpx.ConnectError("refused")])
    down = gh.run("rollup", "push", {})
    assert down.exit_code == 0 and "::warning::siftwise: stats not updated" in down.output


# --- formatting ---------------------------------------------------------------------------


def test_full_mode_markdown_shows_the_reason_escaped() -> None:
    result = selection(mode="full", reason="build/config file changed: a|b", command="pytest")
    markdown = gha.selection_markdown(result, "abcdef1234")

    assert "for `abcdef1`" in markdown
    assert "all (10 known)" in markdown
    assert "build/config file changed: a\\|b" in markdown  # a pipe would end the table cell


def test_markdown_when_nothing_is_affected_or_siftwise_is_down() -> None:
    nothing = gha.selection_markdown(selection(command="", selected_count=0), "")
    assert "none of 10 known tests are affected" in nothing

    down = gha.selection_markdown(gha.fail_open_result("timed out"), "")
    assert "| Tests | all |" in down
    assert "siftwise unavailable: timed out" in down


def test_outputs_cannot_inject_variables(tmp_path: Path) -> None:
    out = tmp_path / "out"
    gha._append({"GITHUB_OUTPUT": str(out)}, "GITHUB_OUTPUT", {"command": "a\nmode=hacked"})
    text = out.read_text()
    assert text.startswith("command<<siftwise_")
    assert Runner(tmp_path).outputs(out) == {"command": "a\nmode=hacked"}


@pytest.mark.parametrize(
    ("ms", "shown"), [(0, "0s"), (59_400, "59s"), (61_000, "1m 01s"), (3_725_000, "1h 02m")]
)
def test_durations(ms: float, shown: str) -> None:
    assert gha._duration(ms) == shown
