"""`siftwise gha select|record|rollup`: context, outputs, comment, fail-open (no real GitHub)."""

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner, Result

from siftwise.cli import gha, reruns
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
        # rollup (and record where it triages) ask whether the run was a shadow run: by
        # default it wasn't. Tests about shadow mode replace this route.
        router.post(f"{SERVER}/repos/{REPO}/shadow").respond(json={"shadow": False})
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
        "known_flaky": [],  # the PR base's .siftwise.toml has none
        "known_flaky_changed": False,
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
    assert "- **Why the full suite:** ` build/config file changed: a|b `" in markdown


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
        "| ` t::cart ` | **new regression** | high | all | ` test file changed ` |"
    )
    assert row_of(visible, "t::other") == "| ` t::other ` | unknown | low | ` linux ` | ` why ` |"
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

    assert "| ` linux `, ` macos ` |" in row_of(markdown, "t::partial")
    mixed = [line for line in markdown.splitlines() if "t::mixed" in line]
    assert ["| ` linux ` |" in mixed[0], "| ` macos `, ` windows ` |" in mixed[1]] == [True, True]
    assert "| all |" in row_of(markdown, "t::everywhere")
    assert sum("| all |" in line for line in markdown.splitlines()) == 1


def test_one_variant_has_no_variants_column() -> None:
    response = triage_response(failure("t::a", "new_regression", None), variants=(None,))
    markdown = gha.failures_markdown(response, RUN_URL)
    assert "| Test | Verdict | Confidence | Why |" in markdown
    assert row_of(markdown, "t::a") == "| ` t::a ` | **new regression** | high | ` why ` |"


def unescaped_pipes(row: str) -> int:
    return len(re.findall(r"(?<!\\)\|", row))


def test_test_ids_and_reasons_are_escaped_for_the_table() -> None:
    test_id = "tests.test_x::test_y[a|b-`c`-<script>]"
    reason = "line one\nline two | <b>bold</b> `code` *x* [link](u)"
    response = triage_response(failure(test_id, "unknown", None, reason=reason), variants=(None,))

    markdown = gha.failures_markdown(response, RUN_URL)

    row = row_of(markdown, "test_y")
    assert unescaped_pipes(row) == 5  # 4 cells: nothing in a cell ends one
    assert "`` tests.test_x::test_y[a\\|b-`c`-<script>] ``" in row
    assert "`` line one line two \\| <b>bold</b> `code` *x* [link](u) ``" in row


@pytest.mark.parametrize(
    ("text", "table", "span"),
    [("a", False, "` a `"), ("a`b``c", False, "``` a`b``c ```"), ("a|b", False, "` a|b `"),
     ("a|b", True, "` a\\|b `"), ("a\r\nb\x1b[1m", False, "` a  b [1m `"),
     ("<!-- siftwise -->", False, "` <! -- siftwise --> `")],
)  # fmt: skip
def test_code_span(text: str, table: bool, span: str) -> None:
    assert gha.code_span(text, table=table) == span


def outside_code(markdown: str) -> str:
    """The Markdown without its code spans: what GitHub renders as formatted text."""
    return re.sub(r"(`+) .*? \1(?!`)", "<code-span>", markdown)


# File names a pull request can add: each tries a link, an image, HTML, a mention, an
# autolink, a reference, or to break the table or the comment's own markers.
HOSTILE_FILES = [
    "docs/[click](https:/evil.test).py",
    "img/![x](https:/evil.test/p.png).py",
    "web/<img src=x onerror=alert(1)>.js",
    "pkgs/@acme/core/index.ts",
    "www.evil.test/setup.py",
    "a/#123 and GH-1/x.py",
    "cells|break|here.py",
    "x/<!-- siftwise:failures sha=0 attempt=9 -->.py",
    "tick`s``/x.py",
]
DANGEROUS = ("](", "<img", "@acme", "www.evil", "#123", "<!-- siftwise:failures sha=0")


def test_hostile_file_names_are_inert_in_the_selection(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    reason = f"build/config file changed: {', '.join(HOSTILE_FILES)}"
    api.post(f"{SERVER}/select").respond(json=selection(mode="full", reason=reason, command=""))
    api.get(f"{GH}/repos/{REPO}/issues/7/comments").respond(json=[])
    created = api.post(f"{GH}/repos/{REPO}/issues/7/comments").respond(201, json={})

    result = gh.run("select", "pull_request", pr_event(repo))

    assert result.exit_code == 0, result.output
    body = sent_body(created)
    for text in (body, gh.summary.read_text()):
        assert all(marker not in outside_code(text) for marker in DANGEROUS), text
        assert "build/config file changed: docs/[click](https:/evil.test).py" in text  # as is
    assert gha.split_comment(body)[1] is None  # no failures section was forged


def test_hostile_names_are_inert_in_the_failures_and_shadow_lines() -> None:
    results = [failure(f"t::{name}", "new_regression", "linux", reason=f"imports {name}")
               for name in HOSTILE_FILES]  # fmt: skip
    results.append(failure("t::@acme", "unknown", "mac|os <b>x</b> @acme"))
    response = triage_response(*results, variants=("linux", "mac|os <b>x</b> @acme"))
    shadow = compare_response(missed=len(HOSTILE_FILES), missed_tests=[
        {"test_id": f"t::{name}", "verdict": "unknown", "cause": "no_link"}
        for name in HOSTILE_FILES])  # fmt: skip

    markdown = gha.failures_markdown(response, RUN_URL) + "\n" + gha.shadow_markdown(shadow)

    assert all(marker not in outside_code(markdown) for marker in DANGEROUS)
    assert "<b>" not in outside_code(markdown)
    assert gha.FAILURES_MARKER.search(markdown) is None
    rows = [line for line in markdown.splitlines() if re.match(r"\| `+ t::", line)]
    assert len(rows) == len(HOSTILE_FILES) + 1
    assert all(unescaped_pipes(row) == 6 for row in rows)  # 5 cells (with Variants), intact


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
        "### Failures\n\nFailure verdicts unavailable: ` nothing recorded for this attempt `.\n"
    )
    unlinked = triage_response(failure("t::a", "unknown"), links_known=False)
    assert "Change links weren't recorded for ` linux `" in gha.failures_markdown(
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
    assert "| ` t::cart ` | **new regression** | high | ` linux ` | ` why ` |" in body
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
    assert f"Failure verdicts unavailable: ` {problem}" in sent_body(created)
    assert f"Failure verdicts unavailable: ` {problem}" in gh.summary.read_text()

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
    assert "Failure verdicts unavailable: ` no siftwise token" in summary
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
    row = "| ` t::cart ` | **new regression** | high | ` why ` |"
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
    assert "| ` t::cart ` | **new regression** | high | ` why ` |" in sent_body(created)


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
    assert "Failure verdicts unavailable: ` nothing recorded" in sent_body(created)


# --- flaky handling: known_flaky and re-runs ------------------------------------------------


def recorded_runs(route: respx.Route) -> list[dict[str, Any]]:
    """The metadata of every ``POST /runs`` call, in order."""
    metas = []
    for call in route.calls:
        content_type = call.request.headers["content-type"]
        boundary = content_type.split("boundary=")[1].encode()
        for part in call.request.content.split(b"--" + boundary):
            if b'name="metadata"' in part:
                metas.append(json.loads(part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]))
    return metas


def record_routes(api: respx.MockRouter) -> respx.Route:
    Path("junit.xml").write_text("<testsuite/>")
    api.get(f"{GH}/repos/{REPO}/actions/runs/555").respond(404)
    return api.post(f"{SERVER}/runs").respond(
        201, json={"run_id": 9, "created": True, "counts": {"total": 1}})  # fmt: skip


def base_with_config(repo: dict[str, str], content: str) -> dict[str, str]:
    """A new PR base on main with this .siftwise.toml; the PR branch stays as it was."""
    root = Path.cwd()
    git(root, "checkout", "-q", "main")
    (root / ".siftwise.toml").write_text(content)
    git(root, "add", ".siftwise.toml")
    git(root, "commit", "-q", "-m", "config")
    base = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", "feature")
    return {**repo, "base": base}


def test_a_pr_records_its_base_commits_known_flaky(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    upload = record_routes(api)
    shas = base_with_config(repo, 'known_flaky = ["tests.test_net::*"]\n')
    # The PR's own edit (untracked here) must not excuse its failures.
    Path(".siftwise.toml").write_text('known_flaky = ["tests.test_cart::*"]\n')

    base = gh.run("record", "pull_request", pr_event(shas), SIFTWISE_VARIANT="linux")
    head = gh.run("record", "pull_request", pr_event(shas), SIFTWISE_VARIANT="linux",
                  SIFTWISE_KNOWN_FLAKY_SOURCE="head")  # fmt: skip

    assert base.exit_code == head.exit_code == 0, base.output
    first, second = recorded_runs(upload)
    assert (first["known_flaky"], first["known_flaky_changed"]) == (["tests.test_net::*"], True)
    assert "known_flaky changes in this PR apply after merge" in base.output
    assert (second["known_flaky"], second["known_flaky_changed"]) == (
        ["tests.test_cart::*"], False)  # fmt: skip
    assert "apply after merge" not in head.output


def test_a_push_records_its_own_known_flaky(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    upload = record_routes(api)
    Path(".siftwise.toml").write_text('known_flaky = ["tests.test_cart::*"]\n')
    push = {"before": repo["base"], "repository": {"default_branch": "main"}}

    result = gh.run("record", "push", push, GITHUB_SHA=repo["head"], GITHUB_REF_NAME="feature",
                    SIFTWISE_VARIANT="linux")  # fmt: skip

    assert result.exit_code == 0, result.output
    [meta] = recorded_runs(upload)
    assert (meta["known_flaky"], meta["known_flaky_changed"]) == (["tests.test_cart::*"], False)


def test_an_invalid_known_flaky_source_or_config(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    upload = record_routes(api)
    Path(".siftwise.toml").write_text("known_flaky = 3\n")

    result = gh.run("record", "pull_request", pr_event(repo), SIFTWISE_VARIANT="linux",
                    SIFTWISE_KNOWN_FLAKY_SOURCE="main")  # fmt: skip

    assert result.exit_code == 0, result.output
    assert "::warning::siftwise: invalid known-flaky-source 'main'" in result.output
    [meta] = recorded_runs(upload)
    assert (meta["known_flaky"], meta["known_flaky_changed"]) == ([], False)


def test_parse_rerun_defaults_and_values() -> None:
    assert gha.parse_rerun("", "", "", "", "") == gha.Rerun()
    assert gha.parse_rerun("3", "unknown, new_regression", "50", "30", "uv run --frozen") == (
        gha.Rerun(3, frozenset({"unknown", "new_regression"}), 50, 1800, ("uv", "run", "--frozen"))
    )


@pytest.mark.parametrize(
    ("args", "warning", "expected"),
    [
        (("4", "", "", "", ""), "invalid rerun-unknown '4'", gha.Rerun()),
        (("x", "", "", "", ""), "invalid rerun-unknown 'x'", gha.Rerun()),
        (("1", "flaky", "", "", ""), "invalid rerun-verdicts 'flaky'", gha.Rerun(1)),
        (("1", "unknown,pre_existing", "", "", ""), "invalid rerun-verdicts", gha.Rerun(1)),
        (("1", "", "0", "", ""), "invalid rerun-max-tests '0'", gha.Rerun(1)),
        (("1", "", "101", "", ""), "invalid rerun-max-tests '101'", gha.Rerun(1)),
        (("1", "", "", "61", ""), "invalid rerun-timeout-minutes '61'", gha.Rerun(1)),
        (("1", "", "", "", "uv 'run"), "invalid rerun-prefix", gha.Rerun()),
    ],
)
def test_parse_rerun_warns_and_uses_the_default(
    capsys: pytest.CaptureFixture[str], args: tuple[str, ...], warning: str, expected: gha.Rerun
) -> None:
    assert gha.parse_rerun(*args) == expected
    assert f"::warning::siftwise: {warning}" in capsys.readouterr().out


class FakePytest:
    """Stands in for the re-run's pytest: writes a JUnit report with the given outcomes."""

    def __init__(self, *rounds: dict[str, str]) -> None:
        self.rounds = list(rounds)  # per call: test ID -> "passed" | "failed"
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: float) -> None:
        self.calls.append(argv)
        report = next(a.split("=", 1)[1] for a in argv if a.startswith("--junitxml="))
        cases = []
        for test_id, outcome in self.rounds.pop(0).items():
            classname, _, name = test_id.partition("::")
            body = "<failure message='boom'/>" if outcome == "failed" else ""
            cases.append(f'<testcase classname="{classname}" name="{name}">{body}</testcase>')
        Path(report).parent.mkdir(parents=True, exist_ok=True)
        Path(report).write_text(f"<testsuite>{''.join(cases)}</testsuite>")


def rerun_setup(
    api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch, fake: Any, *results: dict[str, Any]
) -> tuple[respx.Route, respx.Route]:
    Path("tests").mkdir(exist_ok=True)
    Path("tests/test_cart.py").write_text("def test_total(): pass\n")
    Path("tests/test_pay.py").write_text("def test_card(): pass\n")
    monkeypatch.setattr(gha, "run_command", fake)
    upload = record_routes(api)
    triage = api.post(f"{SERVER}/repos/{REPO}/triage").respond(
        json=triage_response(*results, variants=("linux",)))  # fmt: skip
    return upload, triage


CART, PAY = "tests.test_cart::test_total", "tests.test_pay::test_card"


def test_a_matrix_leg_re_runs_its_unknown_failures(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePytest({CART: "passed"})
    upload, triage = rerun_setup(
        api, monkeypatch, fake,
        failure(CART, "unknown"),
        failure("tests.test_gone::test_x", "unknown"),  # its file isn't in the checkout
        failure("github.com/acme/shop/pay::TestA", "unknown"),  # Go, but no go.mod here
        failure(PAY, "new_regression"),  # not in rerun-verdicts
    )  # fmt: skip

    result = gh.run("record", "pull_request", pr_event(repo), SIFTWISE_VARIANT="linux",
                    SIFTWISE_RERUN_UNKNOWN="2", SIFTWISE_RERUN_PREFIX="uv run")  # fmt: skip

    assert result.exit_code == 0, result.output
    # Only this leg's verdicts: the other legs may not be recorded yet.
    assert json.loads(triage.calls.last.request.content)["variants"] == ["linux"]
    [argv] = fake.calls  # passed on the first re-run: no second one
    assert argv[:3] == ["uv", "run", "pytest"]
    assert argv[4:] == ["-p", "no:cacheprovider", "tests/test_cart.py::test_total"]
    assert argv[3].startswith(f"--junitxml={gh.dir}")  # outside the checkout's JUnit globs
    run, rerun = recorded_runs(upload)
    assert (run.get("rerun"), rerun["rerun"]) == (None, 1)
    assert {k: v for k, v in rerun.items() if k not in ("rerun", "started_at")} == {
        k: v for k, v in run.items() if k != "started_at"}  # fmt: skip
    assert upload.calls.last.request.url.params["defer_rollup"] == "true"
    assert gh.outputs()["rerun-tests"] == "1" and gh.outputs()["rerun-passed"] == "1"
    line = ("siftwise: re-ran 1 failure (unknown) in 1 round: 1 passed on a re-run (now flaky); "
            "2 not re-run (a Go test, but there's no go.mod at the checkout's root; its file "
            "isn't in the checkout)")  # fmt: skip
    assert line in result.output and line in gh.summary.read_text()


def test_tests_still_failing_go_round_again_within_the_caps(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    Path("tests").mkdir()
    Path("tests/test_more.py").write_text("def test_x(): pass\n")
    fake = FakePytest(
        {CART: "failed", PAY: "failed"}, {CART: "failed", PAY: "passed"}, {CART: "failed"}
    )
    upload, _ = rerun_setup(
        api, monkeypatch, fake, failure(CART, "unknown"), failure(PAY, "new_regression"),
        failure("tests.test_more::test_x", "unknown"),
    )  # fmt: skip

    result = gh.run("record", "pull_request", pr_event(repo), SIFTWISE_VARIANT="linux",
                    SIFTWISE_RERUN_UNKNOWN="3", SIFTWISE_RERUN_VERDICTS="unknown,new_regression",
                    SIFTWISE_RERUN_MAX_TESTS="2")  # fmt: skip

    assert result.exit_code == 0, result.output
    # Worst first (the server's order), capped at 2; only tests still failing go round again.
    assert [call[4:] for call in fake.calls] == [
        ["tests/test_cart.py::test_total", "tests/test_pay.py::test_card"],
        ["tests/test_cart.py::test_total", "tests/test_pay.py::test_card"],
        ["tests/test_cart.py::test_total"],
    ]
    assert [m.get("rerun") for m in recorded_runs(upload)] == [None, 1, 2, 3]
    assert "re-ran 2 failures (unknown or new_regression) in 3 rounds: 1 passed" in result.output
    assert "1 not re-run (over rerun-max-tests (2))" in result.output


@pytest.mark.parametrize(
    ("fake", "warning"),
    [
        (subprocess.TimeoutExpired("pytest", 1), "re-run 1 hit the rerun-timeout-minutes limit"),
        (FileNotFoundError("pytest"), "couldn't run pytest"),
        (None, "re-run 1 wrote no JUnit report"),
    ],
)
def test_re_runs_fail_open(
    gh: Runner,
    repo: dict[str, str],
    api: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    fake: BaseException | None,
    warning: str,
) -> None:
    def broken(argv: list[str], timeout: float) -> None:
        if fake is not None:
            raise fake

    upload, _ = rerun_setup(api, monkeypatch, broken, failure(CART, "unknown"))

    result = gh.run("record", "pull_request", pr_event(repo), SIFTWISE_VARIANT="linux",
                    SIFTWISE_RERUN_UNKNOWN="1")  # fmt: skip

    assert result.exit_code == 0, result.output
    assert f"::warning::siftwise: re-runs stopped: {warning}" in result.output
    assert [m.get("rerun") for m in recorded_runs(upload)] == [None]  # nothing uploaded
    assert gh.outputs()["rerun-passed"] == "0"


def test_re_runs_fail_open_when_triage_or_the_upload_fails(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePytest({CART: "passed"})
    upload, triage = rerun_setup(api, monkeypatch, fake, failure(CART, "unknown"))
    triage.respond(500, json={"detail": "boom"})

    no_verdicts = gh.run("record", "pull_request", pr_event(repo), SIFTWISE_VARIANT="linux",
                         SIFTWISE_RERUN_UNKNOWN="1")  # fmt: skip

    assert no_verdicts.exit_code == 0
    assert "::warning::siftwise: re-runs skipped: verdicts unavailable" in no_verdicts.output
    assert fake.calls == []

    triage.respond(json=triage_response(failure(CART, "unknown"), variants=("linux",)))
    upload.side_effect = [httpx.Response(201, json={"run_id": 9, "created": True}),
                          httpx.ConnectError("refused")]  # fmt: skip
    not_recorded = gh.run("record", "pull_request", pr_event(repo), SIFTWISE_VARIANT="linux",
                          SIFTWISE_RERUN_UNKNOWN="1")  # fmt: skip

    assert not_recorded.exit_code == 0
    assert "::warning::siftwise: re-runs stopped: re-run 1 not recorded" in not_recorded.output


def test_a_re_run_reporting_other_test_ids_changes_nothing(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePytest({"test_cart::test_total": "passed"})  # another rootdir: another ID
    rerun_setup(api, monkeypatch, fake, failure(CART, "unknown"))

    result = gh.run("record", "pull_request", pr_event(repo), SIFTWISE_VARIANT="linux",
                    SIFTWISE_RERUN_UNKNOWN="2")  # fmt: skip

    assert len(fake.calls) == 1  # not reported: running it again wouldn't help
    assert gh.outputs()["rerun-passed"] == "0"
    assert ("0 passed on a re-run (now flaky), 1 not in the re-run's report under the same "
            "test ID") in result.output  # fmt: skip


def test_no_re_runs_by_default_or_without_failures_to_re_run(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePytest()
    _, triage = rerun_setup(api, monkeypatch, fake, failure(CART, "flaky"))

    off = gh.run("record", "pull_request", pr_event(repo), SIFTWISE_VARIANT="linux")
    nothing = gh.run("record", "pull_request", pr_event(repo), SIFTWISE_VARIANT="linux",
                     SIFTWISE_RERUN_UNKNOWN="1")  # fmt: skip

    assert off.exit_code == nothing.exit_code == 0
    assert triage.call_count == 1 and fake.calls == []  # only the second asked for verdicts
    assert "re-ran" not in off.output + nothing.output
    assert gh.outputs()["rerun-tests"] == "0"


def test_a_single_job_re_runs_then_triages_every_variant(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePytest({CART: "passed"})
    upload, triage = rerun_setup(api, monkeypatch, fake)
    triage.side_effect = [
        httpx.Response(200, json=triage_response(failure(CART, "unknown", None),
                                                 variants=(None,))),
        httpx.Response(200, json=triage_response(failure(CART, "flaky", None,
                       reason="passed on re-run 1 of this commit and variant"), variants=(None,))),
    ]  # fmt: skip
    created = pr_routes(api, repo["head"])

    result = gh.run("record", "pull_request", pr_event(repo), SIFTWISE_RERUN_UNKNOWN="1",
                    SIFTWISE_FAIL_ON="unknown")  # fmt: skip

    assert result.exit_code == 0, result.output  # flaky after the re-run: fail-on not hit
    first, second = (json.loads(c.request.content) for c in triage.calls)
    assert (first["variants"], "variants" in second) == ([None], False)
    assert "rollup" not in str(upload.calls.last.request.url)
    assert "passed on re-run 1 of this commit and variant" in sent_body(created)


def test_a_real_pytest_re_run_reports_the_same_test_id(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    # End to end: the node ID siftwise derives runs the test, and pytest's JUnit names it with
    # the recorded test ID. It fails once (a marker file), then passes.
    marker = gh.dir / "ran-once"
    Path("tests").mkdir()
    Path("tests/test_flaky.py").write_text(
        "import pathlib\n"
        f"MARKER = pathlib.Path({str(marker)!r})\n"
        "def test_once():\n"
        "    first = not MARKER.exists()\n"
        "    MARKER.touch()\n"
        "    assert not first\n"
    )
    marker.touch()  # the CI run's failure already happened
    upload = record_routes(api)
    api.post(f"{SERVER}/repos/{REPO}/triage").respond(json=triage_response(
        failure("tests.test_flaky::test_once", "unknown"), variants=("linux",)))  # fmt: skip

    result = gh.run("record", "pull_request", pr_event(repo), SIFTWISE_VARIANT="linux",
                    SIFTWISE_RERUN_UNKNOWN="1",
                    SIFTWISE_RERUN_PREFIX=f"{Path(sys.executable).as_posix()} -m")  # fmt: skip

    assert result.exit_code == 0, result.output
    assert gh.outputs()["rerun-passed"] == "1", result.output
    assert [m.get("rerun") for m in recorded_runs(upload)] == [None, 1]
    assert not Path(".pytest_cache").exists()  # -p no:cacheprovider: the checkout stays clean


class FakeTools:
    """Stands in for every runner's tool: writes each command's report the way the real tool
    would (gotestsum/jest-junit/pytest: the file named in the command or environment; go test
    -json: JSON on stdout; Maven/Gradle: TEST-*.xml in the checkout), with these outcomes."""

    def __init__(self, *rounds: dict[str, str]) -> None:
        self.rounds = list(rounds)  # per round: test ID -> "passed" | "failed"
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.round: dict[str, str] = {}

    def __call__(self, argv: list[str], timeout: float, **options: Any) -> None:
        self.calls.append((argv, options))
        outcomes = self.round
        if "go" in Path(argv[0]).name and "-json" in argv:
            events = [json.dumps({"Action": "pass" if outcome == "passed" else "fail",
                                  "Package": t.partition("::")[0], "Test": t.partition("::")[2]})
                      for t, outcome in outcomes.items() if "::Test" in t]  # fmt: skip
            options["stdout"].write_text("\n".join(events))
            return
        if "jest" in argv:
            env = options["env"]
            report = Path(env["JEST_JUNIT_OUTPUT_DIR"]) / env["JEST_JUNIT_OUTPUT_NAME"]
            self._write(report, {t: o for t, o in outcomes.items() if " " in t})
            return
        if argv[0] == "mvn":
            for classname in {t.partition("::")[0] for t in outcomes if t.startswith("com.")}:
                mine = {t: o for t, o in outcomes.items() if t.startswith(classname + "::")}
                self._write(Path("target/surefire-reports") / f"TEST-{classname}.xml", mine)
            return
        raise AssertionError(f"unexpected command {argv}")

    @staticmethod
    def _write(report: Path, outcomes: dict[str, str]) -> None:
        cases = []
        for test_id, outcome in outcomes.items():
            classname, _, name = test_id.partition("::")
            body = "<failure message='boom'/>" if outcome == "failed" else ""
            cases.append(f'<testcase classname="{classname}" name="{name}">{body}</testcase>')
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(f"<testsuite>{''.join(cases)}</testsuite>")


def uploaded_files(route: respx.Route) -> list[list[bytes]]:
    """The JUnit file names of every ``POST /runs`` call, in order."""
    return [re.findall(rb'name="files"; filename="([^"]+)"', call.request.content)
            for call in route.calls]  # fmt: skip


GO_TEST, JEST_TEST = "github.com/acme/shop/cart::TestTotal", "cart adds::cart adds"
JAVA_TEST = "com.acme.CartTest::computesTotal()"


def test_go_jest_and_maven_failures_are_re_run_in_one_round(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    Path("go.mod").write_text("module github.com/acme/shop\n")
    Path("pom.xml").write_text("<project/>")
    Path("web/src").mkdir(parents=True)
    Path("web/src/cart.test.js").write_text("test('adds', () => {})\n")
    Path("web/package.json").write_text('{"devDependencies": {"jest": "^30"}}')
    monkeypatch.setattr(reruns, "which", lambda name: None)  # no gotestsum: go test -json
    fake = FakeTools()
    fake.round = {GO_TEST: "passed", JEST_TEST: "failed", JAVA_TEST: "passed"}
    upload, _ = rerun_setup(
        api, monkeypatch, fake,
        failure(GO_TEST, "unknown"), failure(JEST_TEST, "unknown"), failure(JAVA_TEST, "unknown"),
        failure("Some suite::a thing", "unknown"),  # no runner: warned, not re-run
    )  # fmt: skip
    # The leg's own report names the Jest test's file (the triage response doesn't).
    Path("junit.xml").write_text(
        '<testsuite><testcase classname="cart adds" name="cart adds" file="web/src/cart.test.js">'
        "<failure/></testcase></testsuite>"
    )

    result = gh.run("record", "pull_request", pr_event(repo), SIFTWISE_VARIANT="linux",
                    SIFTWISE_RERUN_UNKNOWN="1", SIFTWISE_RERUN_PREFIX="npx")  # fmt: skip

    assert result.exit_code == 0, result.output
    commands = {argv[0]: (argv, options) for argv, options in fake.calls}
    go, _ = commands["go"]
    assert go == ["go", "test", "-json", "-count=1", "-run", "^(TestTotal)$",
                  "github.com/acme/shop/cart"]  # fmt: skip
    jest, jest_options = commands["npx"]
    assert jest[:5] == ["npx", "jest", "--ci", "--runTestsByPath", "src/cart.test.js"]
    assert Path(jest_options["cwd"]).resolve() == Path("web").resolve()
    maven, _ = commands["mvn"]
    assert "-Dtest=com.acme.CartTest#computesTotal" in maven
    # One upload for the round: every runner's report.
    _, rerun_files = uploaded_files(upload)
    assert sorted(rerun_files) == sorted([b"rerun-1-go-1.xml", b"rerun-1-jest-1.xml",
                                          b"0-TEST-com.acme.CartTest.xml"])  # fmt: skip
    assert not Path("target/surefire-reports/TEST-com.acme.CartTest.xml").exists()  # a copy
    assert [m.get("rerun") for m in recorded_runs(upload)] == [None, 1]
    assert gh.outputs()["rerun-tests"] == "3" and gh.outputs()["rerun-passed"] == "2"
    assert "::warning::siftwise: not re-run, no runner found for: Some suite::a thing" in (
        result.output
    )
    line = ("re-ran 3 failures (unknown) in 1 round: 2 passed on a re-run (now flaky); 1 not "
            "re-run (no runner found for it (pytest, Go, Jest, Maven or Gradle))")  # fmt: skip
    assert line in result.output


def test_a_missing_tool_stops_the_re_runs_with_a_warning(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    Path("go.mod").write_text("module github.com/acme/shop\n")
    monkeypatch.setattr(reruns, "which", lambda name: None)

    def missing(argv: list[str], timeout: float, **options: Any) -> None:
        raise FileNotFoundError(argv[0])

    upload, _ = rerun_setup(api, monkeypatch, missing, failure(GO_TEST, "unknown"))

    result = gh.run("record", "pull_request", pr_event(repo), SIFTWISE_VARIANT="linux",
                    SIFTWISE_RERUN_UNKNOWN="1")  # fmt: skip

    assert result.exit_code == 0, result.output
    assert "::warning::siftwise: re-runs stopped: couldn't run go (go)" in result.output
    assert [m.get("rerun") for m in recorded_runs(upload)] == [None]


def test_a_timed_out_maven_re_run_leaves_the_ci_runs_reports(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    Path("pom.xml").write_text("<project/>")
    reports = Path("target/surefire-reports")
    reports.mkdir(parents=True)
    ci = reports / "TEST-com.acme.CartTest.xml"
    ci.write_text("the CI run's report")

    def slow_maven(argv: list[str], timeout: float, **options: Any) -> None:
        ci.write_text("half a re-run")
        (reports / "TEST-com.acme.Other.xml").write_text("new")
        raise subprocess.TimeoutExpired(argv, timeout)

    upload, _ = rerun_setup(api, monkeypatch, slow_maven, failure(JAVA_TEST, "unknown"))

    result = gh.run("record", "pull_request", pr_event(repo), SIFTWISE_VARIANT="linux",
                    SIFTWISE_RERUN_UNKNOWN="1")  # fmt: skip

    assert result.exit_code == 0, result.output
    assert "re-run 1 hit the rerun-timeout-minutes limit" in result.output
    assert ci.read_text() == "the CI run's report"
    assert sorted(p.name for p in reports.iterdir()) == ["TEST-com.acme.CartTest.xml"]
    assert [m.get("rerun") for m in recorded_runs(upload)] == [None]


def test_the_comment_says_when_a_pr_changes_known_flaky() -> None:
    changed = triage_response(failure("t::a", "unknown"))
    changed["variants"][0]["known_flaky_changed"] = True
    quiet = triage_response(variants=("linux",))
    quiet["variants"][0]["known_flaky_changed"] = True

    assert "> known_flaky changes in this PR apply after merge." in gha.failures_markdown(
        changed, RUN_URL)  # fmt: skip
    assert gha.failures_markdown(quiet, RUN_URL) == (
        "### Failures\n\n> known_flaky changes in this PR apply after merge.\n\n"
        "No failing tests in 1 recorded variant.\n"
    )
    assert "apply after merge" not in gha.failures_markdown(triage_response(), RUN_URL)


# --- shadow mode --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "rate"),
    [("", 0.0), ("off", 0.0), (" OFF ", 0.0), ("always", 1.0), ("10%", 0.1), ("0%", 0.0),
     ("100%", 1.0), ("12.5%", 0.125), ("10", None), ("true", None), ("150%", None),
     ("-5%", None), ("ten%", None)],
)  # fmt: skip
def test_parse_shadow(value: str, rate: float | None) -> None:
    assert gha.parse_shadow(value) == rate


def test_sampling_is_deterministic_per_run_and_near_the_rate() -> None:
    runs = [str(n) for n in range(10_000)]
    picked = [r for r in runs if gha.sampled(REPO, r, 0.1)]
    assert 900 < len(picked) < 1100
    # Every leg (and every attempt) of a run computes the same answer.
    assert picked == [r for r in runs if gha.sampled(REPO, r, 0.1)]
    assert all(gha.sampled(REPO, r, 0.2) for r in picked)  # a higher rate keeps them
    assert not any(gha.sampled(REPO, r, 0.0) for r in runs[:100])
    assert all(gha.sampled(REPO, r, 1.0) for r in runs[:100])
    assert not gha.sampled(REPO, "", 1.0)  # no run ID: nothing to key the selection by


def test_shadow_run_forces_the_full_suite_and_stores_the_selection(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        # The full suite was decided before this call: if it failed now, the run is full.
        early = gh.outputs()
        assert (early["mode"], early["command"], early["shadow"]) == ("full", "", "true")
        return httpx.Response(200, json=selection(shadow_stored=True, shadow_reason=None))

    select = api.post(f"{SERVER}/select").mock(side_effect=respond)
    created = no_comments(api)

    result = gh.run("select", "pull_request", pr_event(repo), SIFTWISE_SHADOW="always",
                    SIFTWISE_VARIANT="linux")  # fmt: skip

    assert result.exit_code == 0, result.output
    sent = json.loads(select.calls.last.request.content)["shadow"]
    assert sent == {"ci_run_id": "555", "run_attempt": 2, "variant": "linux",
                    "commit_sha": repo["head"]}  # fmt: skip
    outputs = gh.outputs()
    assert {k: outputs[k] for k in ("mode", "command", "selected", "total", "shadow",
                                    "shadow-mode", "shadow-selected", "shadow-total")} == {
        "mode": "full", "command": "", "selected": "10", "total": "10", "shadow": "true",
        "shadow-mode": "selective", "shadow-selected": "2", "shadow-total": "10",
    }  # fmt: skip
    assert outputs["reason"] == gha.SHADOW_REASON
    summary = gh.summary.read_text()
    assert "- **Mode:** full (shadow run)" in summary
    assert "- **Would have run:** 2 of 10 known tests (~90% time saved, estimated)" in summary
    assert "Recall" not in summary
    assert "- **Would have run:** 2 of 10" in sent_body(created)
    assert "::warning::" not in result.output


def test_shadow_lines_when_siftwise_would_have_run_everything_or_nothing() -> None:
    full = selection(mode="full", reason="build/config file changed: a|b", command="")
    nothing = selection(command="", selected_count=0)
    forced = gha.shadow_result(full)

    assert "- **Would have run:** all (` build/config file changed: a|b `)" in \
        gha.selection_markdown(forced, "", shadow=True, would_have=full)  # fmt: skip
    assert "- **Would have run:** none of 10 known tests (none affected)" in \
        gha.selection_markdown(forced, "", shadow=True, would_have=nothing)  # fmt: skip


@pytest.mark.parametrize(
    "server",
    [httpx.ConnectError("refused"), httpx.Response(500, json={"detail": "boom"})],
    ids=["unreachable", "500"],
)
def test_a_shadow_run_stays_full_when_siftwise_fails(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter, server: Any
) -> None:
    api.post(f"{SERVER}/select").mock(side_effect=[server])
    no_comments(api)

    result = gh.run("select", "pull_request", pr_event(repo), SIFTWISE_SHADOW="always")

    assert result.exit_code == 0, result.output
    outputs = gh.outputs()
    assert (outputs["mode"], outputs["command"], outputs["shadow"]) == ("full", "", "true")
    assert (outputs["shadow-mode"], outputs["shadow-selected"]) == ("", "")
    assert re.search(r"::warning::siftwise: shadow run not counted: (could not reach|siftwise "
                     r"returned 500)", result.output)  # fmt: skip
    assert "this run won't be counted" in gh.summary.read_text()


def test_an_unstored_shadow_selection_warns_with_the_reason(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    api.post(f"{SERVER}/select").respond(
        json=selection(shadow_stored=False, shadow_reason="commit_conflict"))  # fmt: skip
    no_comments(api)

    result = gh.run("select", "pull_request", pr_event(repo), SIFTWISE_SHADOW="always")

    assert result.exit_code == 0
    assert ("::warning::siftwise: shadow run not counted: the selection wasn't stored "
            "(commit_conflict)") in result.output  # fmt: skip
    assert (gh.outputs()["mode"], gh.outputs()["shadow-mode"]) == ("full", "selective")


@pytest.mark.parametrize(
    ("env", "warning"),
    [({"SIFTWISE_SHADOW": "sometimes"}, True), ({"SIFTWISE_SHADOW": "off"}, False),
     ({}, False), ({"SIFTWISE_SHADOW": "always", "GITHUB_RUN_ID": ""}, False)],
    ids=["invalid", "off", "default", "no-run-id"],
)  # fmt: skip
def test_no_shadow_run(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter, env: dict[str, str], warning: bool
) -> None:
    select = api.post(f"{SERVER}/select").respond(json=selection())
    no_comments(api)

    result = gh.run("select", "pull_request", pr_event(repo), **env)

    assert result.exit_code == 0, result.output
    assert "shadow" not in json.loads(select.calls.last.request.content)
    outputs = gh.outputs()
    assert (outputs["mode"], outputs["shadow"], outputs["shadow-mode"]) == (
        "selective", "false", "")  # fmt: skip
    assert ("::warning::siftwise: invalid shadow value 'sometimes'" in result.output) is warning


def test_no_shadow_run_without_a_token(gh: Runner, repo: dict[str, str]) -> None:
    result = gh.run("select", "pull_request", pr_event(repo, fork=True), SIFTWISE_API_TOKEN="",
                    SIFTWISE_SHADOW="always")  # fmt: skip

    assert result.exit_code == 0
    assert (gh.outputs()["mode"], gh.outputs()["shadow"]) == ("full", "false")


def compare_response(**overrides: Any) -> dict[str, Any]:
    """A ``POST /repos/{repo}/shadow`` response: a counted selective shadow run."""
    return {
        "repo": REPO, "ci_run_id": "555", "run_attempt": 2, "shadow": True, "counted": True,
        "mode": "selective",
        "selections": [{"variant": None, "commit_sha": "a" * 40, "mode": "selective",
                        "reason": "r", "selected_count": 120, "total_known": 692,
                        "estimated_ms": {"total": 1.0, "would_run": 0.5}, "status": "compared"}],
        "variants": [],
        "failing_tests": 4, "caught": 2, "missed": 1, "flaky_excluded": 1,
        "missed_by_verdict": {"new_regression": 0, "unknown": 1, "pre_existing": 0},
        "missed_tests": [{"test_id": "t::new[a|b]_x", "verdict": "unknown", "variants": [None]}],
        "time": {"total_ms": 1000.0, "would_run_ms": 300.0, "saved": 0.7},
        **overrides,
    }  # fmt: skip


def test_shadow_markdown() -> None:
    line = gha.shadow_markdown(compare_response())
    assert line.splitlines()[0] == (
        "**Shadow run:** would have run 120 of 692 tests (70% time saved) and caught 2 of 3 "
        "failures (1 flaky not counted).")  # fmt: skip
    assert "- ` t::new[a|b]_x ` (unknown)" in line

    full = gha.shadow_markdown(compare_response(mode="full", caught=3, missed=0, missed_tests=[],
                                                flaky_excluded=0))  # fmt: skip
    assert full == ("**Shadow run:** would have run all 692 tests (the full suite) and caught "
                    "3 of 3 failures.\n")  # fmt: skip

    quiet = gha.shadow_markdown(compare_response(caught=0, missed=0, missed_tests=[],
                                                 flaky_excluded=0))  # fmt: skip
    assert quiet.endswith("(70% time saved) and had no failures to catch.\n")

    many = [{"test_id": f"t::m{i}", "verdict": "pre_existing", "variants": [None]}
            for i in range(25)]  # fmt: skip
    capped = gha.shadow_markdown(compare_response(missed=25, missed_tests=many))
    assert capped.count("- ` t::") == 20 and "- +5 more" in capped
    assert "(pre-existing)" in capped

    mismatch = compare_response(counted=False, mode=None, selections=[
        {**compare_response()["selections"][0], "status": "commit_mismatch"}])  # fmt: skip
    assert gha.shadow_markdown(mismatch) == (
        "**Shadow run:** not counted: the recorded results are for another commit.\n"
    )


def test_rollup_adds_the_shadow_line_to_the_comment_summary_and_outputs(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    shadow = api.post(f"{SERVER}/repos/{REPO}/shadow").respond(json=compare_response())
    api.post(f"{SERVER}/repos/{REPO}/triage").respond(json=triage_response(
        failure("t::cart", "new_regression")))  # fmt: skip
    api.post(f"{SERVER}/repos/{REPO}/rollup").respond(json={})
    created = pr_routes(api, repo["head"])

    result = gh.run("rollup", "pull_request", pr_event(repo))

    assert result.exit_code == 0, result.output
    assert json.loads(shadow.calls.last.request.content) == {"ci_run_id": "555", "run_attempt": 2}
    line = "**Shadow run:** would have run 120 of 692 tests (70% time saved) and caught 2 of 3"
    body = sent_body(created)
    assert line in body and body.index("t::cart") < body.index(line)  # in the failures section
    assert line in gh.summary.read_text(encoding="utf-8")
    assert line in Path(gh.outputs()["report"]).read_text(encoding="utf-8")
    outputs = gh.outputs()
    assert {k: outputs[k] for k in ("shadow", "shadow-mode", "shadow-caught", "shadow-missed")} \
        == {"shadow": "true", "shadow-mode": "selective", "shadow-caught": "2",
            "shadow-missed": "1"}  # fmt: skip


def test_rollup_of_a_run_that_was_not_a_shadow_run(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    api.post(f"{SERVER}/repos/{REPO}/triage").respond(json=triage_response())
    api.post(f"{SERVER}/repos/{REPO}/rollup").respond(json={})

    assert gh.run("rollup", "push", {}).exit_code == 0

    assert "Shadow run" not in gh.summary.read_text(encoding="utf-8")
    assert (gh.outputs()["shadow"], gh.outputs()["shadow-caught"]) == ("false", "")


@pytest.mark.parametrize("fail_on", ["", "new_regression"])
def test_a_failed_shadow_comparison_only_warns(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter, fail_on: str
) -> None:
    api.post(f"{SERVER}/repos/{REPO}/shadow").respond(500, json={"detail": "boom"})
    api.post(f"{SERVER}/repos/{REPO}/triage").respond(json=triage_response())
    api.post(f"{SERVER}/repos/{REPO}/rollup").respond(json={})

    result = gh.run("rollup", "push", {}, SIFTWISE_FAIL_ON=fail_on)

    assert result.exit_code == 0, result.output  # no verdict matched: the comparison can't fail it
    assert ("::warning::siftwise: shadow comparison failed: siftwise returned 500: boom; this "
            "run won't be counted in recall") in result.output  # fmt: skip
    assert gh.outputs()["shadow"] == "false"


def test_rollup_without_triage_puts_the_shadow_line_in_the_summary(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    api.post(f"{SERVER}/repos/{REPO}/shadow").respond(json=compare_response())
    api.post(f"{SERVER}/repos/{REPO}/rollup").respond(json={})

    assert gh.run("rollup", "push", {}, SIFTWISE_TRIAGE="false").exit_code == 0

    assert "**Shadow run:** would have run 120 of 692" in gh.summary.read_text(encoding="utf-8")


def test_record_in_a_single_job_compares_a_shadow_run(
    gh: Runner, repo: dict[str, str], api: respx.MockRouter
) -> None:
    Path("junit.xml").write_text("<testsuite/>")
    api.get(f"{GH}/repos/{REPO}/actions/runs/555").respond(404)
    api.post(f"{SERVER}/runs").respond(201, json={"run_id": 9, "created": True})
    api.post(f"{SERVER}/repos/{REPO}/triage").respond(json=triage_response(variants=(None,)))
    api.post(f"{SERVER}/repos/{REPO}/shadow").respond(json=compare_response())
    created = pr_routes(api, repo["head"])

    result = gh.run("record", "pull_request", pr_event(repo))

    assert result.exit_code == 0, result.output
    assert "**Shadow run:** would have run 120 of 692" in sent_body(created)
    assert gh.outputs()["shadow-missed"] == "1"


def test_shadow_markdown_shows_each_missed_tests_cause() -> None:
    missed = [
        {"test_id": "t::new_x", "verdict": "unknown", "variants": [None], "cause": "new_test",
         "cause_detail": "no result for this test before this run"},
        {"test_id": "t::db[a|b]", "verdict": "new_regression", "variants": [None],
         "cause": "config_gap", "cause_detail": "…"},
        {"test_id": "t::q", "verdict": "pre_existing", "variants": [None], "cause": "no_link",
         "cause_detail": "…"},
        {"test_id": "t::old", "verdict": "unknown", "variants": [None]},  # an older server
    ]  # fmt: skip
    lines = gha.shadow_markdown(compare_response(missed=4, missed_tests=missed)).splitlines()

    assert lines[-4:] == [
        "- ` t::new_x `: new test (verdict: unknown)",
        "- ` t::db[a|b] `: config gap (verdict: new regression)",
        "- ` t::q `: no link to the change (verdict: pre-existing)",
        "- ` t::old ` (unknown)",
    ]
