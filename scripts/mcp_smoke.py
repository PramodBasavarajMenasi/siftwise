"""Smoke-test `siftwise mcp` the way an MCP client runs it, against a real siftwise server.

    uv run --no-sync python scripts/mcp_smoke.py [--server URL] [--command CMD ...] [--keep]

Client-agnostic: it launches the server over stdio with the MCP Python SDK's client, which
(like the official TypeScript SDK's) passes only a minimal environment plus the variables it
sets, and does the classic ``initialize`` handshake. Every check prints PASS or FAIL; the
exit code is the number of failures (2 if the setup failed).

Setup: the Quickstart's demo project (examples/quickstart) is copied into a temp git repo,
its tests run once on ``main`` and are recorded as repo ``mcp-smoke/shop`` (each run adds one
passing main run to that repo's history); a ``feature`` branch then changes shop/cart.py and a
JUnit report with one failing cart test is written.

Checks: the handshake (server name and version, startup time vs Codex's 10 s default), the
tool list and its schemas, select_tests (selects tests/test_cart.py), triage_failure (the
JUnit report: one new_regression), explain; then fail-open: server down, no token, started
outside a repository (and fixed by SIFTWISE_REPO_PATH, the repo_path argument and the
client's MCP roots).

Needs a running server (the dev stack: ``docker compose -f docker-compose.yml -f
docker-compose.dev.yml up -d``) and its token: ``SIFTWISE_API_TOKEN`` in the environment, else
the repo's ``.env``. The token is never printed. ``--command`` tests another launcher, e.g. an
installed ``siftwise`` (``--command siftwise mcp``); the default runs this checkout's code.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from siftwise.cli import main as cli

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "quickstart"
REPO = "mcp-smoke/shop"
TOOLS = {"select_tests", "triage_failure", "explain"}
FAILING_TEST = "tests.test_cart::test_total"
EXPECTED_SELECTION = "pytest tests/test_cart.py"
CODEX_STARTUP_TIMEOUT = 10.0  # seconds, Codex's default startup_timeout_sec
DEAD_SERVER = "http://127.0.0.1:9"  # nothing listens on the discard port
THIS_CHECKOUT = [sys.executable, "-c", "from siftwise.cli.main import app; app(['mcp'])"]
FAILING_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="2" failures="1">
  <testcase classname="tests.test_cart" name="test_total" time="0.01">
    <failure message="assert 180 == 170">assert 180 == 170</failure>
  </testcase>
  <testcase classname="tests.test_cart" name="test_bulk_discount" time="0.01"/>
</testsuite></testsuites>
"""


class Report:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}{f': {detail}' if detail else ''}", flush=True)
        return ok


def token_from_env_file() -> str:
    path = ROOT / ".env"
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "SIFTWISE_API_TOKEN":
            return value.strip().strip("\"'")
    return ""


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def make_demo(workdir: Path, server: str, token: str) -> Path:
    """The demo repo: main recorded as passing, then a feature branch changing the cart."""
    demo = workdir / "shop"
    shutil.copytree(EXAMPLE, demo, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    git(demo, "init", "-q", "-b", "main")
    for key, value in (("user.name", "mcp smoke"), ("user.email", "smoke@example.com"),
                       ("commit.gpgsign", "false")):  # fmt: skip
        git(demo, "config", key, value)
    git(demo, "add", "-A")
    git(demo, "commit", "-q", "-m", "A shop")
    report = workdir / "main.xml"
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}"],
        cwd=demo,
        check=True,
        capture_output=True,
    )
    cli.upload_run(server, token, [report], {
        "repo": REPO, "commit_sha": git(demo, "rev-parse", "HEAD"), "branch": "main",
        "is_main": True, "changed_files": [], "changed_files_known": False,
    })  # fmt: skip
    git(demo, "checkout", "-q", "-b", "feature")
    cart = demo / "shop" / "cart.py"
    cart.write_text(cart.read_text(encoding="utf-8") + "# smoke: changed\n", encoding="utf-8")
    git(demo, "commit", "-q", "-am", "Change the cart")
    (demo / "junit-failing.xml").write_text(FAILING_JUNIT, encoding="utf-8")
    git(demo, "add", "junit-failing.xml")  # tracked: a committed tree, a commit to triage
    git(demo, "commit", "-q", "-m", "The failing run's report")
    return demo


async def session(
    command: list[str],
    env: dict[str, str],
    cwd: Path,
    log: Any,
    body: Callable[[ClientSession, Any, float], Any],
    roots: list[Path] | None = None,
) -> Any:
    """Start the server, do the handshake, run ``body``. ``roots``: the client declares the
    roots capability and answers roots/list with these folders."""
    params = StdioServerParameters(command=command[0], args=command[1:], env=env, cwd=str(cwd))

    async def list_roots(context: Any) -> types.ListRootsResult:
        return types.ListRootsResult(roots=[types.Root(uri=p.as_uri()) for p in roots or []])

    started = time.monotonic()
    async with (
        stdio_client(params, errlog=log) as (read, write),
        ClientSession(
            read, write, list_roots_callback=list_roots if roots is not None else None
        ) as client,
    ):
        init = await client.initialize()
        return await body(client, init, time.monotonic() - started)


async def call(client: ClientSession, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(tool, args)
    if result.is_error:
        return {"tool_error": str(result.content)}
    text = result.content[0].text if result.content else ""  # type: ignore[union-attr]
    data: dict[str, Any] = result.structured_content or json.loads(text)
    return data


def run(args: argparse.Namespace) -> int:
    report = Report()
    token = os.environ.get("SIFTWISE_API_TOKEN") or token_from_env_file()
    try:
        httpx.get(f"{args.server.rstrip('/')}/healthz", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        print(f"setup: siftwise isn't reachable at {args.server} ({exc}): start the dev stack")
        return 2
    if not token:
        print("setup: no SIFTWISE_API_TOKEN (environment or .env)")
        return 2
    command = args.command or THIS_CHECKOUT

    workdir = Path(tempfile.mkdtemp(prefix="siftwise-mcp-smoke-"))
    log_path = workdir / "server-stderr.log"
    try:
        try:
            demo = make_demo(workdir, args.server, token)
        except (subprocess.CalledProcessError, cli.SiftwiseError) as exc:
            print(f"setup: could not build and record the demo repo: {exc}")
            return 2
        outside = workdir / "not-a-repo"
        outside.mkdir()
        env = {"SIFTWISE_SERVER": args.server, "SIFTWISE_API_TOKEN": token, "SIFTWISE_REPO": REPO,
               "SIFTWISE_BASE_REF": "main",
               # Never find a parent repository from `outside` (e.g. a temp dir in a checkout).
               "GIT_CEILING_DIRECTORIES": str(workdir.parent)}  # fmt: skip

        with log_path.open("w", encoding="utf-8") as log:

            async def happy(client: ClientSession, init: Any, startup: float) -> None:
                info = init.server_info
                report.check(
                    "handshake",
                    info.name == "siftwise" and bool(info.version),
                    f"{info.name} {info.version}, protocol {init.protocol_version}",
                )
                report.check(
                    "startup within Codex's 10 s default",
                    startup < CODEX_STARTUP_TIMEOUT,
                    f"{startup:.1f} s to initialize",
                )
                tools = {t.name: t for t in (await client.list_tools()).tools}
                report.check("tools listed", set(tools) == TOOLS, ", ".join(sorted(tools)))
                plain = all(
                    "anyOf" not in json.dumps(t.input_schema)
                    and all(p.get("description") for p in t.input_schema["properties"].values())
                    for t in tools.values()
                )
                report.check("schemas: every parameter described, no anyOf", plain)

                out = await call(client, "select_tests", {})
                report.check(
                    "select_tests selects the cart's tests",
                    out.get("mode") == "selective" and out.get("command") == EXPECTED_SELECTION,
                    f"mode={out.get('mode')} command={out.get('command')!r}",
                )
                out = await call(client, "triage_failure", {"junit_paths": ["junit-failing.xml"]})
                results = out.get("results") or []
                verdicts = [(r["test_id"], r["verdict"]) for r in results]
                report.check(
                    "triage_failure: one new_regression from the JUnit report",
                    out.get("verdicts_available") is True
                    and verdicts == [(FAILING_TEST, "new_regression")],
                    f"{verdicts or out}",
                )
                out = await call(client, "explain", {"test_id": FAILING_TEST})
                selection = out.get("selection") or {}
                report.check(
                    "explain: known and selected",
                    out.get("available") is True
                    and out.get("known") is True
                    and selection.get("selected") is True,
                    f"known={out.get('known')} selected={selection.get('selected')}",
                )

            async def fail_open(client: ClientSession, init: Any, startup: float) -> Any:
                return (
                    await call(client, "select_tests", {}),
                    await call(client, "triage_failure", {"failures": [{"test_id": "a::b"}]}),
                    await call(client, "explain", {"test_id": FAILING_TEST}),
                )

            async def scenario(scenario_env: dict[str, str], cwd: Path) -> Any:
                return await session(command, scenario_env, cwd, log, fail_open)

            async def all_checks() -> None:
                await session(command, env, demo, log, happy)

                select, triage, explained = await scenario(
                    {**env, "SIFTWISE_SERVER": DEAD_SERVER}, demo
                )
                report.check(
                    "server down: full suite, no verdicts, explain unavailable",
                    select.get("mode") == "full"
                    and select.get("command") == ""
                    and triage.get("verdicts_available") is False
                    and explained.get("available") is False,
                    select.get("reason", ""),
                )
                no_token = {k: v for k, v in env.items() if k != "SIFTWISE_API_TOKEN"}
                select, triage, explained = await scenario(no_token, demo)
                report.check(
                    "no token: full suite, no verdicts",
                    select.get("mode") == "full"
                    and "SIFTWISE_API_TOKEN is not set" in select.get("reason", "")
                    and triage.get("verdicts_available") is False,
                    select.get("reason", ""),
                )
                select, triage, explained = await scenario(env, outside)
                message = select.get("reason", "")
                report.check(
                    "outside a repository: a clear message on every tool",
                    select.get("mode") == "full"
                    and "not inside a git repository" in message
                    and "SIFTWISE_REPO_PATH" in message
                    and triage.get("verdicts_available") is False
                    and explained.get("available") is False,
                    message[:160],
                )
                select, _, _ = await scenario({**env, "SIFTWISE_REPO_PATH": str(demo)}, outside)
                report.check(
                    "outside a repository + SIFTWISE_REPO_PATH: selects as usual",
                    select.get("command") == EXPECTED_SELECTION,
                    f"command={select.get('command')!r}",
                )

                async def with_argument(client: ClientSession, init: Any, startup: float) -> None:
                    out = await call(client, "select_tests", {"repo_path": str(demo)})
                    report.check(
                        "outside a repository + the repo_path argument: selects as usual",
                        out.get("command") == EXPECTED_SELECTION,
                        f"command={out.get('command')!r}",
                    )

                await session(command, env, outside, log, with_argument)

                async def with_roots(client: ClientSession, init: Any, startup: float) -> None:
                    out = await call(client, "select_tests", {})
                    report.check(
                        "outside a repository + the client's roots: selects as usual",
                        out.get("command") == EXPECTED_SELECTION,
                        f"command={out.get('command')!r}",
                    )

                await session(command, env, outside, log, with_roots, roots=[demo])

            try:
                anyio.run(all_checks)
            except Exception as exc:  # noqa: BLE001 - a crash is a failed check, with the log
                report.check("the server ran", False, f"{type(exc).__name__}: {exc}")
        if report.failures and log_path.is_file():
            print(f"\nserver stderr ({log_path}):")
            print(log_path.read_text(encoding="utf-8")[-4000:].replace(token, "***"))
    finally:
        if args.keep:
            print(f"kept {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)
    print(f"\nmcp_smoke: {report.failures} failure(s)")
    return report.failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--server",
        default=os.environ.get("SIFTWISE_SERVER") or "http://127.0.0.1:8000",
        help="the siftwise API (default: %(default)s)",
    )
    parser.add_argument(
        "--command",
        nargs="+",
        help="how to launch the MCP server (default: this checkout's code, `siftwise mcp`)",
    )
    parser.add_argument("--keep", action="store_true", help="keep the temp demo repo and log")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
