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
from mcp import Client, StdioServerParameters, types
from typer.testing import CliRunner

import siftwise
from siftwise.cli.main import EXIT_USAGE, app
from siftwise.mcp import server as mcp_server
from siftwise.mcp.server import MAX_OUTPUT_CHARS, TIMEOUT, build_server

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


def test_known_flaky_is_the_bases_never_the_agents_edit(
    checkout: Path, api: respx.MockRouter
) -> None:
    git(checkout, "stash", "-u")
    git(checkout, "checkout", "-q", "main")
    (checkout / ".siftwise.toml").write_text('known_flaky = ["tests.test_db::*"]\n')
    git(checkout, "add", ".siftwise.toml")
    git(checkout, "commit", "-q", "-m", "config")
    git(checkout, "checkout", "-q", "feature")
    git(checkout, "stash", "pop")
    (checkout / ".siftwise.toml").write_text('known_flaky = ["**"]\n')  # the agent's edit
    triage = api.post(f"{SERVER}/triage").respond(json=triage_response(verdict("a::b", "flaky")))
    explain = api.post(f"{SERVER}/explain").respond(json=explanation())

    call("triage_failure", {"failures": [{"test_id": "a::b"}]})
    call("explain", {"test_id": "a::b"})

    assert sent(triage)["known_flaky"] == sent(explain)["known_flaky"] == ["tests.test_db::*"]


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


# --- clients: schemas, where the server runs ----------------------------------------------


def test_the_server_reports_its_version() -> None:
    # Clients show it in their server lists; bug reports need it.
    async def go() -> Any:
        async with Client(build_server(ENV)) as client:
            return client.server_info

    info = asyncio.run(go())

    assert (info.name, info.version) == ("siftwise", siftwise.__version__)


def test_tool_schemas_are_plain_for_every_client() -> None:
    # Every parameter described (clients show and send only the schema); no anyOf/null, which
    # some clients' schema converters (Codex, Gemini for Vertex AI) mishandle.
    async def go() -> Any:
        async with Client(build_server(ENV)) as client:
            return {t.name: t.input_schema for t in (await client.list_tools()).tools}

    schemas = asyncio.run(go())

    for name, schema in schemas.items():
        text = json.dumps(schema)
        assert "anyOf" not in text and "null" not in text, name
        for param, prop in schema["properties"].items():
            assert prop.get("description") and prop.get("type"), (name, param)
        assert "repo_path" in schema["properties"], name
    assert schemas["explain"]["required"] == ["test_id"]
    failure = schemas["triage_failure"]["properties"]["failures"]["items"]
    assert (failure["type"], failure["required"]) == ("object", ["test_id"])


@pytest.mark.usefixtures("checkout")
def test_optional_arguments_accept_null(api: respx.MockRouter) -> None:
    # Some clients send null for every optional argument they don't fill in.
    route = api.post(f"{SERVER}/triage").respond(json=triage_response())
    args = {"junit_paths": None, "base": None, "variant": None, "repo_path": None}

    call("triage_failure", {**args, "failures": [{"test_id": "a::b", "message": None}]})

    assert sent(route)["failures"] == [{"test_id": "a::b", "message": None}]


@pytest.fixture
def elsewhere(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory outside any git repository (as when a client starts the server in
    the home directory)."""
    outside = tmp_path_factory.mktemp("home")
    monkeypatch.chdir(outside)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(outside.parent))  # no parent repo either
    return outside


@pytest.mark.usefixtures("elsewhere")
def test_outside_a_repository_every_tool_says_how_to_fix_it(api: respx.MockRouter) -> None:
    select = call("select_tests")
    triage = call("triage_failure", {"failures": [{"test_id": "a::b"}]})
    explained = call("explain", {"test_id": "a::b"})
    reports = call("triage_failure", {"junit_paths": ["junit.xml"]})

    assert (select["mode"], select["command"]) == ("full", "")
    for message in (select["reason"], triage["message"], explained["message"], reports["message"]):
        assert "is not inside a git repository" in message
        assert "repo_path" in message and "SIFTWISE_REPO_PATH" in message
    assert triage["verdicts_available"] is False and triage["failures"] == ["a::b"]
    assert explained["available"] is False
    assert reports["verdicts_available"] is False  # not a confusing "file not found"
    assert not api.calls  # nothing sent


def test_repo_path_from_the_environment(
    checkout: Path, elsewhere: Path, api: respx.MockRouter
) -> None:
    select_route = api.post(f"{SERVER}/select").respond(json=selection())
    triage_route = api.post(f"{SERVER}/triage").respond(json=triage_response())
    env = {**ENV, "SIFTWISE_REPO_PATH": str(checkout)}

    out = call("select_tests", env=env)
    call("triage_failure", {"junit_paths": ["junit.xml"]}, env=env)  # relative to the repo

    body = sent(select_route)
    assert out["mode"] == "selective"
    assert body["repo"] == "acme/shop"
    assert body["changed_files"] == ["src/cart.py", "src/db.py", "junit.xml", "src/new.py"]
    assert [f["test_id"] for f in sent(triage_route)["failures"]] == ["tests.test_cart::test_total"]


def test_the_repo_path_argument_wins(
    checkout: Path, elsewhere: Path, api: respx.MockRouter
) -> None:
    route = api.post(f"{SERVER}/explain").mock(side_effect=[httpx.ConnectError("refused")])
    env = {**ENV, "SIFTWISE_REPO_PATH": str(elsewhere)}  # not a repository

    out = call("explain", {"test_id": "a::b", "repo_path": str(checkout / "src")}, env=env)

    assert route.called  # got as far as the API: the checkout was found from a subdirectory
    assert "could not reach siftwise" in out["message"]


@pytest.mark.usefixtures("elsewhere")
def test_a_repo_path_that_is_not_a_directory(tmp_path: Path, api: respx.MockRouter) -> None:
    out = call("select_tests", {"repo_path": str(tmp_path / "nope")})

    assert out["mode"] == "full"
    assert f"repo_path {tmp_path / 'nope'} is not a directory" in out["reason"]


# The order: repo_path, SIFTWISE_REPO_PATH, CLAUDE_PROJECT_DIR, the client's roots, the working
# directory. Each repo below has its own origin, so the repo sent to /select tells which won.


def make_repo(path: Path, name: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "remote", "add", "origin", f"git@github.com:acme/{name}.git")
    return path


@pytest.fixture
def repos(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Four repos, the working directory in the "cwd" one."""
    base = tmp_path_factory.mktemp("repos")
    made = {name: make_repo(base / name, name) for name in ("arg", "env", "claude", "root", "cwd")}
    monkeypatch.chdir(made["cwd"])
    return made


class Roots:
    """A client's roots/list handler that records whether it was asked."""

    def __init__(self, *paths: Path | str, error: bool = False, delay: float = 0) -> None:
        self.uris = [p if isinstance(p, str) else p.as_uri() for p in paths]
        self.error, self.delay, self.asked = error, delay, 0

    async def __call__(self, context: Any) -> Any:
        self.asked += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            return types.ErrorData(code=-32601, message="no roots here")
        return types.ListRootsResult(roots=[types.Root(uri=u) for u in self.uris])


def call_client(
    tool: str, args: dict[str, Any], env: dict[str, str], roots: Roots | None = None
) -> Any:
    """Like ``call``, over the handshake protocol real clients use, optionally with roots."""

    async def go() -> Any:
        # No callback: the client doesn't declare the roots capability.
        async with Client(build_server(env), mode="legacy", list_roots_callback=roots) as client:
            result = await client.call_tool(tool, args)
            assert not result.is_error, result.content
            return result.structured_content

    return asyncio.run(go())


def selected_repo(api: respx.MockRouter, env: dict[str, str], roots: Roots | None = None,
                  args: dict[str, Any] | None = None) -> str:  # fmt: skip
    route = api.post(f"{SERVER}/select").respond(json=selection())
    out = call_client("select_tests", args or {}, env, roots)
    assert route.called, out
    return str(sent(route)["repo"])


def test_order_the_repo_path_argument_first(repos: dict[str, Path], api: respx.MockRouter) -> None:
    env = {
        **ENV,
        "SIFTWISE_REPO_PATH": str(repos["env"]),
        "CLAUDE_PROJECT_DIR": str(repos["claude"]),
    }
    roots = Roots(repos["root"])

    repo = selected_repo(api, env, roots, {"repo_path": str(repos["arg"])})

    assert repo == "acme/arg"
    assert roots.asked == 0


def test_order_then_siftwise_repo_path(repos: dict[str, Path], api: respx.MockRouter) -> None:
    env = {
        **ENV,
        "SIFTWISE_REPO_PATH": str(repos["env"]),
        "CLAUDE_PROJECT_DIR": str(repos["claude"]),
    }
    roots = Roots(repos["root"])

    assert selected_repo(api, env, roots) == "acme/env"
    assert roots.asked == 0


def test_order_then_claude_project_dir(repos: dict[str, Path], api: respx.MockRouter) -> None:
    roots = Roots(repos["root"])

    assert selected_repo(api, {**ENV, "CLAUDE_PROJECT_DIR": str(repos["claude"])}, roots) == (
        "acme/claude"
    )
    assert roots.asked == 0  # an explicit location: the client is never asked


def test_order_then_the_first_root_in_a_git_repository(
    repos: dict[str, Path], tmp_path: Path, api: respx.MockRouter
) -> None:
    not_a_repo = tmp_path / "docs"
    not_a_repo.mkdir()
    roots = Roots(not_a_repo, repos["root"] / "sub", repos["env"])  # (the spec: file:// only)
    (repos["root"] / "sub").mkdir()

    assert selected_repo(api, ENV, roots) == "acme/root"  # a subdirectory finds its repo
    assert roots.asked == 1


def test_order_then_the_working_directory(repos: dict[str, Path], api: respx.MockRouter) -> None:
    assert selected_repo(api, ENV) == "acme/cwd"  # a client without roots: as before
    assert selected_repo(api, ENV, Roots()) == "acme/cwd"  # roots, but none


@pytest.mark.parametrize("roots", [Roots(error=True), Roots(delay=1.0)], ids=["error", "timeout"])
def test_roots_that_fail_fall_back_to_the_working_directory(
    repos: dict[str, Path], api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch, roots: Roots
) -> None:
    monkeypatch.setattr(mcp_server, "ROOTS_TIMEOUT", 0.2)

    assert selected_repo(api, ENV, roots) == "acme/cwd"
    assert roots.asked == 1


@pytest.mark.usefixtures("elsewhere")
def test_roots_outside_any_repository_are_named(tmp_path: Path, api: respx.MockRouter) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()

    out = call_client("select_tests", {}, ENV, Roots(docs))

    assert out["mode"] == "full"
    assert f"none of the client's roots ({docs}) is in a git repository" in out["reason"]
    assert "repo_path" in out["reason"] and not api.calls


def test_the_handshake_without_roots_never_asks(
    repos: dict[str, Path], api: respx.MockRouter
) -> None:
    # The SDK refuses a server request the client didn't declare; it must never get that far.
    route = api.post(f"{SERVER}/explain").respond(json={"detail": "x"}, status_code=404)

    out = call_client("explain", {"test_id": "a::b"}, ENV)

    assert route.called and out["available"] is False


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("file:///home/me/my%20repo", "/home/me/my repo"),
        ("file:///C:/Users/me/repo", "C:/Users/me/repo" if sys.platform == "win32" else None),
        ("https://example.com/repo", None),
    ],
)
def test_root_uris_become_local_paths(uri: str, expected: str | None) -> None:
    path = mcp_server._root_path(uri)

    if expected is None and uri.startswith("file:"):
        assert path is not None  # a POSIX reading of a Windows URI: some path, no crash
    elif expected is None:
        assert path is None
    else:
        assert path == Path(expected)


def test_api_calls_end_before_the_clients_tool_timeouts() -> None:
    # Codex gives a tool call 60 s by default: a hung server must still get the agent the
    # fail-open answer, not a client-side timeout.
    assert TIMEOUT <= 30


# --- the command --------------------------------------------------------------------------


def test_mcp_command_without_the_extra_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "siftwise.mcp.server", raising=False)
    for name in ("mcp", "mcp.server", "mcp.server.mcpserver"):  # as if the extra weren't there
        monkeypatch.setitem(sys.modules, name, None)

    result = CliRunner().invoke(app, ["mcp"])

    assert result.exit_code == EXIT_USAGE
    assert "pip install 'siftwise[mcp]'" in result.output
    assert "uv tool install 'siftwise[mcp]'" in result.output


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
