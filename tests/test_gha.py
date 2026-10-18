"""`siftwise gha select|record|rollup`: context, outputs, comment, fail-open (no real GitHub)."""

import json
import os
import re
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


def pr_routes(api: respx.MockRouter, head: str) -> respx.Route:
    """The PR's current head (the staleness check) and no comment yet: the create route."""
    api.get(f"{GH}/repos/{REPO}/pulls/7").respond(json={"head": {"sha": head}})
    return no_comments(api)


RUN_URL = f"https://github.com/{REPO}/actions/runs/555/attempts/2"


def failure(
    test_id: str,
    verdict: str,
    variant: str | None = "linux",
    confidence: str = "high",
    reason: str = "why",
) -> dict[str, Any]:
    return {"test_id": test_id, "variant": variant, "verdict": verdict,
            "confidence": confidence, "reasons": [reason], "evidence": {}}  # fmt: skip


def triage_response(
    *results: dict[str, Any],
    variants: tuple[str | None, ...] = ("linux", "macos"),
    links_known: bool = True,
    **overrides: Any,
) -> dict[str, Any]:
    """A ``POST /repos/{repo}/triage`` response; counts computed from the results."""
    worst: dict[str, int] = {}
    for r in results:
        rank = gha.VERDICTS.index(r["verdict"])
        worst[r["test_id"]] = min(worst.get(r["test_id"], rank), rank)

    def counts(verdicts: list[str]) -> dict[str, int]:
        return {v: verdicts.count(v) for v in gha.VERDICTS}

    return {
        "repo": REPO, "ci_run_id": "555", "run_attempt": 2, "window_days": 90,
        "variants": [{
            "variant": v, "run_id": i, "commit_sha": "a" * 40, "as_of": None,
            "changed_files_known": True, "links_known": links_known,
            "failing": sum(r["variant"] == v for r in results),
            "counts": counts([r["verdict"] for r in results if r["variant"] == v]),
        } for i, v in enumerate(variants, start=1)],
        "failing_tests": len(worst),
        "counts": counts([gha.VERDICTS[rank] for rank in worst.values()]),
        "results": list(results),
        "truncated": False,
        **overrides,
    }  # fmt: skip


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
    assert "- **Mode:** selective" in summary and "- **Tests:** 2 of 10 known tests" in summary
    assert "|" not in summary  # a list: no table, so no empty header row
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
                     "go_module": None, "generated": []},
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
    api.post(f"{SERVER}/repos/{REPO}/triage").respond(json=triage_response(variants=(None,)))
    pr_routes(api, repo["head"])

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
    pr_routes(api, repo["head"])  # verdicts unavailable: the comment says so

    result = gh.run("record", "pull_request", pr_event(repo))

    assert result.exit_code == 0
    assert "::warning::siftwise: results not recorded" in result.output


def test_record_without_files_or_token_warns(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    pr_routes(api, repo["head"])
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


def test_find_junit_never_walks_into_skipped_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    for path in ("junit.xml", "pkg/sub/junit-b.xml", "node_modules/a/b/junit.xml",
                 ".venv/lib/junit.xml", "venv/lib/junit.xml", ".tox/py/junit.xml",
                 ".git/hooks/junit.xml", ".github/junit.xml"):  # fmt: skip
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("<testsuite/>")
    walked: list[str] = []
    real_walk = os.walk

    def recording_walk(top: str) -> Iterator[tuple[str, list[str], list[str]]]:
        for entry in real_walk(top):
            walked.append(entry[0].replace(os.sep, "/"))
            yield entry

    monkeypatch.setattr(os, "walk", recording_walk)

    found = gha.find_junit("**/junit*.xml", "")

    assert found == [Path("junit.xml"), Path("pkg/sub/junit-b.xml")]
    skipped = ("node_modules", ".venv", "venv", ".tox", ".git")
    assert walked and not [d for d in walked if any(s in d.split("/") for s in skipped)]


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("**/junit*.xml", ["junit.xml", "a/junit-1.xml", "a/b/junit-2.xml"]),
        ("a/**/*.xml", ["a/junit-1.xml", "a/b/junit-2.xml", "a/b/other.xml"]),
        ("a/*.xml", ["a/junit-1.xml"]),  # no **: plain glob, one level
        ("junit.xml", ["junit.xml"]),
        ("**/.hidden.xml", ["a/.hidden.xml"]),  # a dot in the pattern matches dot files
    ],
)
def test_find_junit_matches_like_glob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pattern: str, expected: list[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    for path in ("junit.xml", "a/junit-1.xml", "a/.hidden.xml", "a/b/junit-2.xml",
                 "a/b/other.xml", ".cache/junit.xml"):  # fmt: skip
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("<testsuite/>")
    assert gha.find_junit(pattern, "") == sorted(Path(p) for p in expected)


# --- rollup -------------------------------------------------------------------------------


def test_rollup_step(gh: Runner, repo: dict[str, str], api: respx.MockRouter) -> None:
    route = api.post(f"{SERVER}/repos/{REPO}/rollup").respond(
        json={"repo": REPO, "tests_updated": 12, "window_days": 90, "seconds": 1.5})  # fmt: skip
    api.post(f"{SERVER}/repos/{REPO}/triage").respond(json=triage_response())

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
    assert "- **Why the full suite:** build/config file changed: a\\|b" in markdown


def test_markdown_when_nothing_is_affected_or_siftwise_is_down() -> None:
    nothing = gha.selection_markdown(selection(command="", selected_count=0), "")
    assert "none of 10 known tests are affected" in nothing

    down = gha.selection_markdown(gha.fail_open_result("timed out"), "")
    assert "- **Tests:** all\n" in down
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


# --- failures: markdown -------------------------------------------------------------------


def row_of(markdown: str, text: str) -> str:
    [row] = [line for line in markdown.splitlines() if line.startswith("|") and text in line]
    return row


def test_failures_markdown_orders_groups_and_collapses() -> None:
    response = triage_response(
        failure("t::broken", "pre_existing"),
        failure("t::flake", "flaky", "macos", confidence="medium"),
        failure("t::other", "unknown", confidence="low"),
        failure("t::cart", "new_regression", reason="test file changed"),
        failure("t::cart", "new_regression", "macos", reason="test file changed"),
    )

    markdown = gha.failures_markdown(response, RUN_URL, max_rows=20)

    assert markdown.startswith("### Failures: 1 new regression, 1 unknown, 1 flaky, 1 pre-existing")
    visible, collapsed = markdown.split("<details>")
    assert visible.index("t::cart") < visible.index("t::other")
    assert row_of(visible, "t::cart") == (
        "| <code>t::cart</code> | **new regression** | high | all | test file changed |"
    )
    assert row_of(visible, "t::other") == "| <code>t::other</code> | unknown | low | linux | why |"
    assert collapsed.startswith(
        "<summary>1 flaky, 1 pre-existing: likely not caused by this change</summary>"
    )
    assert collapsed.index("t::flake") < collapsed.index("t::broken")
    assert "t::flake" not in visible and "more" not in markdown


def test_variants_all_only_when_every_variant_got_the_verdict() -> None:
    variants = ("linux", "macos", "windows")
    response = triage_response(
        # Failed on two legs, passed on windows: those two legs, never "all".
        failure("t::partial", "new_regression", "linux"),
        failure("t::partial", "new_regression", "macos"),
        # Failed everywhere, with different verdicts: a row per verdict.
        failure("t::mixed", "new_regression", "linux"),
        failure("t::mixed", "flaky", "macos"),
        failure("t::mixed", "flaky", "windows"),
        # The same verdict on every recorded variant: "all".
        *(failure("t::everywhere", "unknown", v) for v in variants),
        variants=variants,
    )

    markdown = gha.failures_markdown(response, RUN_URL)

    assert "| linux, macos |" in row_of(markdown, "t::partial")
    mixed = [line for line in markdown.splitlines() if "t::mixed" in line]
    assert ["| linux |" in mixed[0], "| macos, windows |" in mixed[1]] == [True, True]
    assert "| all |" in row_of(markdown, "t::everywhere")
    assert sum("| all |" in line for line in markdown.splitlines()) == 1


def test_one_variant_has_no_variants_column() -> None:
    response = triage_response(failure("t::a", "new_regression", None), variants=(None,))
    markdown = gha.failures_markdown(response, RUN_URL)
    assert "| Test | Verdict | Confidence | Why |" in markdown
    assert row_of(markdown, "t::a") == "| <code>t::a</code> | **new regression** | high | why |"


def unescaped_pipes(row: str) -> int:
    return len(re.findall(r"(?<!\\)\|", row))


def test_test_ids_and_reasons_are_escaped_for_the_table() -> None:
    test_id = "tests.test_x::test_y[a|b-`c`-<script>]"
    reason = "line one\nline two | <b>bold</b> `code` *x* [link](u)"
    response = triage_response(failure(test_id, "unknown", None, reason=reason), variants=(None,))

    markdown = gha.failures_markdown(response, RUN_URL)

    row = row_of(markdown, "test\\_y")
    assert unescaped_pipes(row) == 5  # 4 cells: nothing in a cell ends one
    assert "<code>tests.test\\_x::test\\_y\\[a\\|b-\\`c\\`-&lt;script&gt;\\]</code>" in row
    assert "line one line two \\| &lt;b&gt;bold&lt;/b&gt; \\`code\\` \\*x\\* \\[link\\](u)" in row
    assert "<script>" not in markdown and "<b>" not in markdown


@pytest.mark.parametrize(
    ("text", "escaped"),
    [("a|b", "a\\|b"), ("x`y", "x\\`y"), ("<i>", "&lt;i&gt;"), ("a\r\nb", "a  b"),
     ("back\\slash", "back\\\\slash"), ("a&lt;b", "a&amp;lt;b")],
)  # fmt: skip
def test_md_escape(text: str, escaped: str) -> None:
    assert gha._md(text) == escaped


def test_long_reasons_are_cut() -> None:
    response = triage_response(failure("t::a", "unknown", None, reason="x" * 300), variants=(None,))
    row = row_of(gha.failures_markdown(response, RUN_URL), "t::a")
    assert "x" * (gha.REASON_CHARS - 1) + "…" in row and "x" * gha.REASON_CHARS not in row


def test_comment_caps_each_table_and_links_to_the_summary() -> None:
    response = triage_response(
        *(failure(f"t::new{i:02}", "new_regression", None) for i in range(30)),
        *(failure(f"t::flaky{i:02}", "flaky", None) for i in range(25)),
        variants=(None,),
    )

    in_comment = gha.failures_markdown(response, RUN_URL, max_rows=gha.COMMENT_ROWS)
    in_summary = gha.failures_markdown(response, RUN_URL)

    assert in_comment.count("**new regression**") == 20
    assert in_comment.split("<details>")[1].count("| flaky |") == 20
    assert in_comment.rstrip().endswith(f"+15 more: [full list in the job summary]({RUN_URL})")
    assert in_comment.startswith("### Failures: 30 new regressions, 25 flaky")
    assert in_summary.count("**new regression**") == 30 and "more" not in in_summary


def test_summary_says_when_the_server_cut_the_results() -> None:
    response = triage_response(failure("t::a", "new_regression", None), variants=(None,),
                               failing_tests=600, truncated=True)  # fmt: skip
    assert "+599 more not listed (siftwise returns at most 500)." in gha.failures_markdown(
        response, RUN_URL)  # fmt: skip


def test_no_failures_unavailable_and_unlinked_variants() -> None:
    assert gha.failures_markdown(triage_response(), RUN_URL) == (
        "### Failures\n\nNo failing tests in 2 recorded variants.\n")  # fmt: skip
    assert gha.unavailable_markdown("nothing recorded for this attempt") == (
        "### Failures\n\nFailure verdicts unavailable: nothing recorded for this attempt.\n"
    )
    unlinked = triage_response(failure("t::a", "unknown"), links_known=False)
    assert "Change links weren't recorded for <code>linux</code>" in gha.failures_markdown(
        unlinked, RUN_URL)  # fmt: skip


# --- failures: the comment's sections ------------------------------------------------------


def failures_section(sha: str, attempt: int = 2, text: str = "### Failures: 1 flaky") -> str:
    return f"<!-- siftwise:failures sha={sha} attempt={attempt} -->\n{text}"


def test_split_and_compose_comment() -> None:
    old = f"{gha.COMMENT_MARKER}\n### siftwise test selection\n\n| a | b |\n{gha.COMMENT_FOOTER}\n"
    assert gha.split_comment(old) == ("### siftwise test selection\n\n| a | b |", None)

    composed = gha.compose_comment("### selection", failures_section("a" * 40))
    assert composed.count(gha.COMMENT_MARKER) == 1
    assert gha.split_comment(composed) == ("### selection", failures_section("a" * 40))
    assert gha.split_comment(gha.compose_comment(None, failures_section("b" * 40))) == (
        None, failures_section("b" * 40))  # fmt: skip


def existing_comment(api: respx.MockRouter, body: str) -> respx.Route:
    api.get(f"{GH}/repos/{REPO}/issues/7/comments").respond(
        json=[{"body": body, "url": f"{GH}/c/42"}])  # fmt: skip
    api.post(f"{GH}/repos/{REPO}/issues/7/comments").respond(201, json={})
    return api.patch(f"{GH}/c/42").respond(json={})


def sent_body(route: respx.Route) -> str:
    body: str = json.loads(route.calls.last.request.content)["body"]
    return body


def test_select_upgrades_an_old_format_comment_in_place(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    api.post(f"{SERVER}/select").respond(json=selection())
    patched = existing_comment(api, f"{gha.COMMENT_MARKER}\nold selection\n{gha.COMMENT_FOOTER}")

    assert gh.run("select", "pull_request", pr_event(repo)).exit_code == 0

    body = sent_body(patched)
    assert body.count(gha.COMMENT_MARKER) == 1 and gha.SELECTION_MARKER in body
    assert "old selection" not in body and "90% saved" in body


@pytest.mark.parametrize("same_commit", [True, False])
def test_select_keeps_failures_of_its_commit_only(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter, same_commit: bool
) -> None:
    api.post(f"{SERVER}/select").respond(json=selection())
    sha = repo["head"] if same_commit else "0" * 40
    patched = existing_comment(api, gha.compose_comment("old", failures_section(sha)))

    assert gh.run("select", "pull_request", pr_event(repo)).exit_code == 0

    assert ("### Failures: 1 flaky" in sent_body(patched)) is same_commit


# --- failures: rollup and record ------------------------------------------------------------


def test_rollup_triages_into_the_comment_summary_and_outputs(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    triage = api.post(f"{SERVER}/repos/{REPO}/triage").respond(json=triage_response(
        failure("t::cart", "new_regression"), failure("t::flake", "flaky", "macos")))  # fmt: skip
    rollup = api.post(f"{SERVER}/repos/{REPO}/rollup").respond(
        json={"repo": REPO, "tests_updated": 3, "window_days": 90, "seconds": 0.5})  # fmt: skip
    created = pr_routes(api, repo["head"])

    result = gh.run("rollup", "pull_request", pr_event(repo))

    assert result.exit_code == 0, result.output
    assert json.loads(triage.calls.last.request.content) == {
        "ci_run_id": "555", "run_attempt": 2, "limit": 500}  # fmt: skip
    assert rollup.called
    body = sent_body(created)
    assert f"<!-- siftwise:failures sha={repo['head']} attempt=2 -->" in body
    assert "| <code>t::cart</code> | **new regression** | high | linux | why |" in body
    assert gha.SELECTION_MARKER not in body  # no selection section to keep
    outputs = gh.outputs()
    assert outputs["verdicts-available"] == "true"
    assert json.loads(outputs["counts"]) == {
        "new_regression": 1, "unknown": 0, "flaky": 1, "pre_existing": 0}  # fmt: skip
    report = Path(outputs["report"]).read_text(encoding="utf-8")
    assert "### Failures: 1 new regression, 1 flaky" in report
    assert report in gh.summary.read_text(encoding="utf-8")


def test_rollup_adds_failures_to_an_old_format_comment(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    api.post(f"{SERVER}/repos/{REPO}/triage").respond(
        json=triage_response(failure("t::cart", "new_regression")))  # fmt: skip
    api.post(f"{SERVER}/repos/{REPO}/rollup").respond(json={})
    api.get(f"{GH}/repos/{REPO}/pulls/7").respond(json={"head": {"sha": repo["head"]}})
    patched = existing_comment(api, f"{gha.COMMENT_MARKER}\nold selection\n{gha.COMMENT_FOOTER}")

    assert gh.run("rollup", "pull_request", pr_event(repo)).exit_code == 0

    body = sent_body(patched)
    assert body.count(gha.COMMENT_MARKER) == 1
    assert body.index(gha.SELECTION_MARKER) < body.index("old selection") < body.index("t::cart")


def test_rollup_leaves_the_comment_to_a_newer_push_or_attempt(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    api.post(f"{SERVER}/repos/{REPO}/triage").respond(
        json=triage_response(failure("t::cart", "new_regression")))  # fmt: skip
    api.post(f"{SERVER}/repos/{REPO}/rollup").respond(json={})
    pulls = api.get(f"{GH}/repos/{REPO}/pulls/7").respond(json={"head": {"sha": "0" * 40}})
    patched = existing_comment(api, gha.compose_comment("sel", failures_section(repo["head"], 3)))

    pushed = gh.run("rollup", "pull_request", pr_event(repo))
    assert pushed.exit_code == 0 and not patched.called
    assert "newer commits: PR comment not updated" in gh.summary.read_text()

    pulls.respond(json={"head": {"sha": repo["head"]}})
    rerun = gh.run("rollup", "pull_request", pr_event(repo))  # attempt 2; attempt 3 wrote it
    assert rerun.exit_code == 0 and not patched.called
    assert "A newer attempt of this run already updated the PR comment." in gh.summary.read_text()


@pytest.mark.parametrize(
    ("fail_on", "exit_code"),
    [("", 0), ("new_regression", 1), ("flaky", 1), ("unknown,pre_existing", 0),
     ("new_regression, unknown", 1)],
)  # fmt: skip
def test_rollup_fail_on(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter, fail_on: str, exit_code: int
) -> None:
    # A new regression on Linux and flaky on macOS: fail-on flaky matches the macOS verdict.
    api.post(f"{SERVER}/repos/{REPO}/triage").respond(json=triage_response(
        failure("t::cart", "new_regression"), failure("t::cart", "flaky", "macos")))  # fmt: skip
    api.post(f"{SERVER}/repos/{REPO}/rollup").respond(json={})
    pr_routes(api, repo["head"])

    result = gh.run("rollup", "pull_request", pr_event(repo), SIFTWISE_FAIL_ON=fail_on)

    assert result.exit_code == exit_code, result.output
    assert ("::error::siftwise: fail-on matched" in result.output) is bool(exit_code)
    if exit_code:
        assert "t::cart" in result.output


@pytest.mark.parametrize(
    ("server", "problem"),
    [
        (httpx.Response(404, json={"detail": "no runs recorded for CI run 555 attempt 2"}),
         "nothing recorded for this attempt"),
        (httpx.Response(404, json={"detail": "unknown repo 'acme/shop'"}),
         "nothing recorded for this attempt"),
        (httpx.ConnectError("refused"), "could not reach siftwise"),
        (httpx.Response(500, json={"detail": "boom"}), "siftwise returned 500"),
    ],
    ids=["no-runs", "unknown-repo", "unreachable", "500"],
)  # fmt: skip
def test_unavailable_verdicts_fail_open_unless_fail_on_is_set(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter, server: Any, problem: str
) -> None:
    api.post(f"{SERVER}/repos/{REPO}/triage").mock(side_effect=[server, server])
    api.post(f"{SERVER}/repos/{REPO}/rollup").respond(json={})
    created = pr_routes(api, repo["head"])

    open_ = gh.run("rollup", "pull_request", pr_event(repo))

    assert open_.exit_code == 0, open_.output
    assert "::warning::siftwise: failure verdicts unavailable" in open_.output
    assert "::error::" not in open_.output
    assert gh.outputs()["verdicts-available"] == "false"
    assert f"Failure verdicts unavailable: {problem}" in sent_body(created)
    assert f"Failure verdicts unavailable: {problem}" in gh.summary.read_text()

    closed = gh.run("rollup", "pull_request", pr_event(repo), SIFTWISE_FAIL_ON="new_regression")

    assert closed.exit_code == 1
    assert "::error::siftwise verdicts unavailable (" in closed.output
    assert "); failing because fail-on is set" in closed.output
    assert problem in closed.output


def test_rollup_on_a_fork_pr(gh: Runner, repo: dict[str, str], api: respx.MockRouter) -> None:
    # No siftwise token and a read-only GitHub token: no calls at all (none are mocked).
    event = pr_event(repo, fork=True)

    result = gh.run("rollup", "pull_request", event, SIFTWISE_API_TOKEN="")

    assert result.exit_code == 0, result.output
    summary = gh.summary.read_text()
    assert "Failure verdicts unavailable: no siftwise token" in summary
    assert "from a fork: no PR comment" in summary
    # fail-on can't gate without a token: a warning, never a failure (every fork PR would fail).
    gated = gh.run("rollup", "pull_request", event, SIFTWISE_API_TOKEN="",
                   SIFTWISE_FAIL_ON="new_regression")  # fmt: skip
    assert gated.exit_code == 0, gated.output
    assert ("::warning::fail-on skipped: no siftwise token (fork PR?); this run is not gated"
            in gated.output)  # fmt: skip
    assert "::error::" not in gated.output


def test_record_without_a_token_skips_fail_on(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    Path("junit.xml").write_text("<testsuite/>")
    pr_routes(api, repo["head"])

    result = gh.run("record", "pull_request", pr_event(repo), SIFTWISE_API_TOKEN="",
                    SIFTWISE_FAIL_ON="new_regression,unknown")  # fmt: skip

    assert result.exit_code == 0, result.output
    assert "fail-on skipped: no siftwise token (fork PR?); this run is not gated" in result.output


def test_record_comment_errors_are_warnings_and_verdicts_still_reach_the_summary(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    # A single job without `pull-requests: write`: every comment API call is refused.
    Path("junit.xml").write_text("<testsuite/>")
    api.get(f"{GH}/repos/{REPO}/actions/runs/555").respond(403)
    api.post(f"{SERVER}/runs").respond(201, json={"run_id": 9, "created": True})
    api.post(f"{SERVER}/repos/{REPO}/triage").respond(json=triage_response(
        failure("t::cart", "new_regression", None), variants=(None,)))  # fmt: skip
    api.get(f"{GH}/repos/{REPO}/pulls/7").respond(403)
    api.get(f"{GH}/repos/{REPO}/issues/7/comments").respond(403)

    result = gh.run("record", "pull_request", pr_event(repo))

    assert result.exit_code == 0, result.output
    assert "::warning::siftwise: PR comment failed: 403" in result.output
    row = "| <code>t::cart</code> | **new regression** | high | why |"
    summary = gh.summary.read_text(encoding="utf-8")
    assert row in summary and "PR comment failed: HTTP 403" in summary
    outputs = gh.outputs()
    assert outputs["verdicts-available"] == "true"
    assert row in Path(outputs["report"]).read_text(encoding="utf-8")


def test_rollup_without_triage_and_bad_fail_on(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    api.post(f"{SERVER}/repos/{REPO}/rollup").respond(json={})

    off = gh.run("rollup", "push", {}, SIFTWISE_TRIAGE="false")
    assert off.exit_code == 0 and "Failures" not in gh.summary.read_text()

    typo = gh.run("rollup", "push", {}, SIFTWISE_FAIL_ON="new_regresion")
    assert typo.exit_code == 1
    assert "::error::siftwise: unknown fail-on verdict(s): new_regresion" in typo.output


def test_record_in_a_single_job_triages_after_the_upload(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    Path("junit.xml").write_text("<testsuite/>")
    api.get(f"{GH}/repos/{REPO}/actions/runs/555").respond(404)
    upload = api.post(f"{SERVER}/runs").respond(
        201, json={"run_id": 9, "created": True, "counts": {"total": 1}})  # fmt: skip
    triage = api.post(f"{SERVER}/repos/{REPO}/triage").respond(json=triage_response(
        failure("t::cart", "new_regression", None), variants=(None,)))  # fmt: skip
    created = pr_routes(api, repo["head"])

    result = gh.run("record", "pull_request", pr_event(repo), SIFTWISE_FAIL_ON="new_regression")

    assert result.exit_code == 1  # fail-on matched
    assert upload.called and triage.called
    assert "| <code>t::cart</code> | **new regression** | high | why |" in sent_body(created)


def test_record_in_a_matrix_leg_leaves_triage_to_rollup(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    Path("junit.xml").write_text("<testsuite/>")
    api.get(f"{GH}/repos/{REPO}/actions/runs/555").respond(404)
    api.post(f"{SERVER}/runs").respond(201, json={"run_id": 9, "created": True})
    triage = api.post(f"{SERVER}/repos/{REPO}/triage").respond(json=triage_response())

    result = gh.run("record", "pull_request", pr_event(repo), SIFTWISE_VARIANT="linux",
                    SIFTWISE_FAIL_ON="new_regression")  # fmt: skip

    assert result.exit_code == 0, result.output
    assert not triage.called
    assert "fail-on has no effect" in result.output
    assert "verdicts-available" not in gh.outputs()


def test_record_with_nothing_recorded_fails_only_with_fail_on(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    created = pr_routes(api, repo["head"])  # no JUnit files: nothing to upload or triage

    result = gh.run("record", "pull_request", pr_event(repo), SIFTWISE_FAIL_ON="unknown")

    assert result.exit_code == 1
    assert ("::error::siftwise verdicts unavailable (nothing recorded for this attempt "
            "(no JUnit files)); failing because fail-on is set") in result.output  # fmt: skip
    assert "Failure verdicts unavailable: nothing recorded" in sent_body(created)
