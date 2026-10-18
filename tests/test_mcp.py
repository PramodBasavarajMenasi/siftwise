"""`siftwise mcp`: tools through the MCP SDK's in-memory client (siftwise API mocked)."""

import asyncio
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from mcp import Client, StdioServerParameters
from typer.testing import CliRunner

from siftwise.cli.main import EXIT_USAGE, app
from siftwise.mcp.server import MAX_OUTPUT_CHARS, build_server

SERVER = "http://siftwise.test"
TOKEN = "s3cret-token-value"
ENV = {"SIFTWISE_SERVER": SERVER, "SIFTWISE_API_TOKEN": TOKEN}
JUNIT = """<testsuite>
  <testcase classname="tests.test_cart" name="test_total"><failure message="boom"/></testcase>
  <testcase classname="tests.test_cart" name="test_ok"/>
</testsuite>
"""


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """main, a feature branch changing src/cart.py, and uncommitted work on top."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "commit.gpgsign", "false")
    git(tmp_path, "remote", "add", "origin", "git@github.com:acme/shop.git")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cart.py").write_text("x = 1\n")
    (tmp_path / "src" / "db.py").write_text("y = 1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "checkout", "-q", "-b", "feature")
    (tmp_path / "src" / "cart.py").write_text("x = 2\n")
    git(tmp_path, "commit", "-q", "-am", "change cart")
    (tmp_path / "src" / "db.py").write_text("y = 2\n")  # unstaged
    (tmp_path / "src" / "new.py").write_text("z = 1\n")  # untracked
    (tmp_path / "junit.xml").write_text(JUNIT)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def clean(checkout: Path) -> str:
    """Commit everything: a clean tree. Returns HEAD."""
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "-m", "wip")
    return git(checkout, "rev-parse", "HEAD")


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


def call(tool: str, args: dict[str, Any] | None = None, env: dict[str, str] | None = None) -> Any:
    """Call a tool in-process; returns the structured result, checking its text matches."""

    async def go() -> Any:
        async with Client(build_server(ENV if env is None else env)) as client:
            result = await client.call_tool(tool, args or {})
            assert not result.is_error, result.content
            text = result.content[0].text  # type: ignore[union-attr]
            assert len(text) <= MAX_OUTPUT_CHARS
            assert TOKEN not in text
            assert json.loads(text) == result.structured_content
            return result.structured_content

    return asyncio.run(go())


def sent(route: respx.Route) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(route.calls.last.request.content)
    return body


def selection(**overrides: Any) -> dict[str, Any]:
    tests = [
        {"test_id": f"tests.test_cart::test_{i}", "reasons": ["tests/test_cart.py tests changed "
         "src/cart.py"], "reason": "", "runner": "pytest", "file_path": None}
        for i in range(60)
    ]  # fmt: skip
    return {
        "repo": "acme/shop", "mode": "selective", "reason": "60 of 900 known tests selected",
        "selected_count": 60, "total_known": 900, "command": "pytest tests/test_cart.py",
        "commands": ["pytest tests/test_cart.py"], "go_packages_run_whole": [],
        "python_files_run_whole": ["tests/test_cart.py"],
        "estimated_ms": {"total": 1000.0, "would_run": 250.0}, "tests": tests, **overrides,
    }  # fmt: skip


def verdict(test_id: str, kind: str, **evidence: Any) -> dict[str, Any]:
    return {
        "test_id": test_id, "verdict": kind, "confidence": "high", "reasons": [f"why {kind}"],
        "evidence": {"links": [], "pre_existing": None, "other_variants": [], "flaky": [],
                     "history": {"flaky_counts": {"same_commit": 0, "unrelated_fail_then_pass": 0}},
                     **evidence},
    }  # fmt: skip


def triage_response(*results: dict[str, Any]) -> dict[str, Any]:
    counts = {v: 0 for v in ("new_regression", "pre_existing", "flaky", "unknown")}
    for r in results:
        counts[r["verdict"]] += 1
    return {"repo": "acme/shop", "counts": counts, "results": list(results)}


# --- the tool list ------------------------------------------------------------------------


def test_tools_tell_the_agent_when_to_use_them() -> None:
    async def go() -> Any:
        async with Client(build_server(ENV)) as client:
            return {t.name: t.description for t in (await client.list_tools()).tools}

    tools = asyncio.run(go())

    assert set(tools) == {"select_tests", "triage_failure", "explain"}
    assert "before running tests" in tools["select_tests"]
    assert "when tests fail, before trying to fix them" in tools["triage_failure"]
    assert "surprises you" in tools["explain"]


# --- select_tests -------------------------------------------------------------------------


@pytest.mark.usefixtures("checkout")
def test_select_includes_uncommitted_changes(api: respx.MockRouter) -> None:
    route = api.post(f"{SERVER}/select").respond(json=selection())

    out = call("select_tests", {"max_tests": 5})

    body = sent(route)
    assert body["repo"] == "acme/shop"  # from the origin remote
    # committed, unstaged, untracked (the report too: test outputs belong in .gitignore)
    assert body["changed_files"] == ["src/cart.py", "src/db.py", "junit.xml", "src/new.py"]
    assert route.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"
    assert out["mode"] == "selective"
    assert out["command"] == "pytest tests/test_cart.py"
    assert (
        out["next_step"] == "Run this command instead of the full suite: pytest tests/test_cart.py"
    )
    assert (out["selected"], out["total_known"], out["base"]) == (60, 900, "main")
    assert out["estimated_test_time_skipped"] == "75%"
    assert len(out["tests"]) == 5 and out["more_tests"] == 55
    assert out["tests"][0]["reason"] == "tests/test_cart.py tests changed src/cart.py"


@pytest.mark.usefixtures("checkout")
def test_select_committed_only(api: respx.MockRouter) -> None:
    route = api.post(f"{SERVER}/select").respond(json=selection())

    call("select_tests", {"include_uncommitted": False, "base": "main"})

    assert sent(route)["changed_files"] == ["src/cart.py"]


@pytest.mark.usefixtures("checkout")
@pytest.mark.parametrize(
    ("server", "env", "reason"),
    [
        (httpx.ConnectError("refused"), ENV, "could not reach siftwise"),
        (httpx.Response(404, json={"detail": "unknown repo"}), ENV, "no test history"),
        (httpx.Response(500, json={"detail": f"bad token {TOKEN}"}), ENV, "500"),
        (None, {"SIFTWISE_SERVER": SERVER}, "SIFTWISE_API_TOKEN is not set"),
    ],
)
def test_select_fails_open_to_the_full_suite(
    api: respx.MockRouter, server: Any, env: dict[str, str], reason: str
) -> None:
    route = api.post(f"{SERVER}/select").mock(side_effect=[server])

    out = call("select_tests", env=env)

    assert (out["mode"], out["command"]) == ("full", "")
    assert reason in out["reason"]
    assert out["next_step"].endswith("run the full suite")
    assert TOKEN not in json.dumps(out)
    assert route.called is (server is not None)


def test_select_without_a_repo_fails_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api: respx.MockRouter
) -> None:
    git(tmp_path, "init", "-q")  # no origin remote, no SIFTWISE_REPO
    monkeypatch.chdir(tmp_path)

    out = call("select_tests")

    assert out["mode"] == "full"
    assert "set SIFTWISE_REPO" in out["reason"]


# --- triage_failure -----------------------------------------------------------------------


def test_triage_from_junit_on_a_dirty_tree(checkout: Path, api: respx.MockRouter) -> None:
    route = api.post(f"{SERVER}/triage").respond(json=triage_response(
        verdict("a::flaky", "flaky", history={"flaky_counts": {"same_commit": 0,
                "unrelated_fail_then_pass": 2}}),
        verdict("a::main", "pre_existing", pre_existing={"since_sha": "abcdef1234",
                "main_runs_failing": 3}),
        verdict("tests.test_cart::test_total", "new_regression",
                links=[{"reason": "tests/test_cart.py tests changed src/cart.py"}]),
    ))  # fmt: skip

    out = call("triage_failure", {
        "junit_paths": [str(checkout / "junit.xml")],
        "failures": [{"test_id": "a::flaky", "message": "timeout"}, {"test_id": "a::main"}],
        "variant": "linux",
    })  # fmt: skip

    body = sent(route)
    assert body["commit_sha"] is None  # uncommitted changes: no earlier run tested this code
    assert body["variant"] == "linux"
    assert body["failures"] == [
        {"test_id": "tests.test_cart::test_total", "message": "boom", "file_path": None},
        {"test_id": "a::flaky", "message": "timeout"},
        {"test_id": "a::main", "message": None},
    ]
    assert "ci_run_id" not in body  # MCP never records or identifies runs
    assert out["tested_code"] == "uncommitted changes"
    assert [r["verdict"] for r in out["results"]] == ["new_regression", "flaky", "pre_existing"]
    regression, flaky, main = out["results"]
    assert regression["linked_by"] == "tests/test_cart.py tests changed src/cart.py"
    assert flaky["flaky_counts"]["unrelated_fail_then_pass"] == 2
    assert main["failing_on_main"] == "since abcdef1, 3 main run(s)"
    assert out["next_step"].startswith("Fix the 1 new_regression failure(s) first")


def test_triage_on_a_clean_tree_sends_the_commit(checkout: Path, api: respx.MockRouter) -> None:
    head = clean(checkout)
    route = api.post(f"{SERVER}/triage").respond(json=triage_response(verdict("a::b", "unknown")))

    out = call("triage_failure", {"failures": [{"test_id": "a::b"}]})

    assert sent(route)["commit_sha"] == head
    assert out["tested_code"] == f"commit {head[:7]}"


@pytest.mark.usefixtures("checkout")
def test_triage_fails_open_without_verdicts(api: respx.MockRouter) -> None:
    api.post(f"{SERVER}/triage").mock(side_effect=[httpx.ConnectError("refused")])

    out = call("triage_failure", {"failures": [{"test_id": "a::b"}, {"test_id": "a::c"}]})

    assert out["verdicts_available"] is False
    assert "verdicts unavailable" in out["message"]
    assert out["failures"] == ["a::b", "a::c"]


@pytest.mark.usefixtures("checkout")
def test_triage_input_errors_and_no_failures(api: respx.MockRouter) -> None:
    assert "error" in call("triage_failure", {"junit_paths": ["missing.xml"]})
    assert "error" in call("triage_failure", {"failures": [{"message": "no id"}]})
    passing = call("triage_failure", {"failures": []})
    assert (passing["verdicts_available"], passing["results"]) == (True, [])


@pytest.mark.usefixtures("checkout")
def test_huge_triage_results_stay_under_the_cap(api: respx.MockRouter) -> None:
    long_reason = "x" * 3000
    results = [
        {**verdict(f"tests.test_m::test_{i}", "unknown"), "reasons": [long_reason] * 5}
        for i in range(5000)
    ]
    api.post(f"{SERVER}/triage").respond(json=triage_response(*results))

    out = call("triage_failure", {"failures": [{"test_id": f"t::{i}"} for i in range(5000)]})

    assert out["counts"]["unknown"] == 5000
    assert out["truncated"] is True
    assert len(out["results"]) + out["more_results"] == 5000


# --- explain ------------------------------------------------------------------------------


def explanation(**overrides: Any) -> dict[str, Any]:
    run = {"run_id": 7, "commit_sha": "b" * 40, "branch": "main", "variant": "linux",
           "occurred_at": "2026-10-17T10:00:00Z", "ci_run_id": "9"}  # fmt: skip
    return {
        "repo": "acme/shop",
        "test_id": "tests.test_cart::test_total",
        "known": True,
        "file_path": "tests/test_cart.py",
        "stats": {
            "runs": 40,
            "failures": 3,
            "last_failed_at": "2026-10-16T09:00:00Z",
            "flaky_score": 0.05,
            "avg_duration_ms": 12.4,
            "broken_on_main_since_sha": None,
            "broken_on_main_variants": [{"variant": "macos", "since_sha": "c" * 40}],
        },
        "recent": [
            {
                "run_id": 7,
                "commit_sha": "b" * 40,
                "branch": "main",
                "is_main": True,
                "variant": "linux",
                "occurred_at": "2026-10-17T10:00:00Z",
                "status": "failed",
                "attempts": 2,
            },
        ],
        "flaky": [
            {
                "kind": "same_commit",
                "commit_sha": "b" * 40,
                "variant": "linux",
                "failed": [run],
                "passed": [run],
                "message_matches": None,
            }
        ],
        "flaky_counts": {"same_commit": 1, "unrelated_fail_then_pass": 0},
        "window_days": 90,
        "selection": {
            "mode": "selective",
            "selected": False,
            "reasons": [],
            "summary": "1 of 900 known tests selected",
            "links": [],
        },
        **overrides,
    }


@pytest.mark.usefixtures("checkout")
def test_explain(api: respx.MockRouter) -> None:
    route = api.post(f"{SERVER}/explain").respond(json=explanation())

    out = call("explain", {"test_id": "tests.test_cart::test_total", "variant": "linux"})

    body = sent(route)
    assert (body["test_id"], body["variant"], body["repo"]) == (
        "tests.test_cart::test_total", "linux", "acme/shop")  # fmt: skip
    assert "src/new.py" in body["changed_files"]
    assert out["selection"]["why"] == ["not selected: nothing in your change links to it"]
    assert out["stats"]["flaky_score"] == 0.05 and out["stats"]["avg_duration_ms"] == 12
    assert out["broken_on_main"] == [f"macos since {'c' * 7}"]
    assert out["flaky"]["events"][0].startswith("same_commit: failed (run 7) and passed (run 7)")
    assert out["recent"] == [f"2026-10-17T10:00 main {'b' * 7} on linux: failed (2 attempts)"]


@pytest.mark.usefixtures("checkout")
def test_explain_fails_open(api: respx.MockRouter) -> None:
    api.post(f"{SERVER}/explain").mock(side_effect=[httpx.ReadTimeout("slow")])

    out = call("explain", {"test_id": "a::b"})

    assert out["available"] is False
    assert "siftwise unavailable" in out["message"]


# --- the command --------------------------------------------------------------------------


def test_mcp_command_without_the_extra_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "siftwise.mcp.server", raising=False)
    for name in ("mcp", "mcp.server", "mcp.server.mcpserver"):  # as if the extra weren't there
        monkeypatch.setitem(sys.modules, name, None)

    result = CliRunner().invoke(app, ["mcp"])

    assert result.exit_code == EXIT_USAGE
    assert 'uv tool install ".[mcp]"' in result.output


def test_mcp_serves_over_stdio(tmp_path: Path) -> None:
    # A real `siftwise mcp` process: the protocol on stdout, nothing else there (on Windows
    # too, where the stdio transport differs).
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from siftwise.cli.main import app; app(['mcp'])"],
        env={**ENV, "SIFTWISE_SERVER": "http://127.0.0.1:9", "PYTHONUTF8": "1"},
        cwd=str(tmp_path),
    )

    async def go() -> Any:
        async with Client(params) as client:
            names = sorted(t.name for t in (await client.list_tools()).tools)
            result = await client.call_tool("select_tests", {})
            return names, result.structured_content

    names, out = asyncio.run(go())

    assert names == ["explain", "select_tests", "triage_failure"]
    assert out["mode"] == "full"  # not a git checkout, nothing listening: fails open
