"""`siftwise mcp`: test selection, failure triage and explanations for coding agents.

An MCP server over stdio (the official ``mcp`` SDK, ``siftwise[mcp]``). It holds no state:
each tool computes the change in the agent's checkout with the same code as ``siftwise
select`` (git diff, import graphs, .siftwise.toml) and calls the siftwise API. It never
records runs.

Configuration comes from the environment, read on every call: ``SIFTWISE_SERVER`` (default
``http://localhost:8000``), ``SIFTWISE_API_TOKEN``, and optionally ``SIFTWISE_REPO`` (else
``owner/name`` from the ``origin`` remote) and ``SIFTWISE_BASE_REF`` (else the branch
``origin/HEAD`` points to, else ``main``).

By default the change includes uncommitted work (``include_uncommitted``): the diff runs from
the merge base of the base branch and HEAD to the working tree, untracked files included.
Triage of a dirty tree sends no commit, since no earlier run tested that code.

Fail open: when the server is unreachable, errors or no token is set, ``select_tests`` says to
run the full suite and ``triage_failure`` says verdicts are unavailable. Outputs are compact
and capped at ``MAX_OUTPUT_CHARS``; the API token never appears in them.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

from siftwise.cli import main as cli
from siftwise.cli.repoconfig import RepoConfigError
from siftwise.core.junit import JUnitParseError

# Every tool result, as JSON text, stays within this many characters.
MAX_OUTPUT_CHARS = 8000
MAX_REASON_CHARS = 200
DEFAULT_SERVER = "http://localhost:8000"
TIMEOUT = 60.0

# Work first: what the agent should fix, then what to look at, then what to leave alone.
VERDICT_ORDER = ("new_regression", "unknown", "flaky", "pre_existing")
MAX_TRIAGE_RESULTS = 30

INSTRUCTIONS = """\
siftwise knows this repository's test history. Before running tests after a change, call
select_tests and run the command it returns instead of the full suite. When tests fail, call
triage_failure before trying to fix anything: fix new_regression failures; flaky ones need a
re-run, not a fix; pre_existing ones were already failing on main. Use explain when a
selection or a verdict surprises you."""

SELECT_DESCRIPTION = """\
Call this before running tests after changing code. It returns the command that runs only the
tests your change (committed and uncommitted) can affect, with a reason per test. Run that
command instead of the full test suite. If mode is "full", run the project's full test suite;
if the command is empty in selective mode, no tests are affected."""

TRIAGE_DESCRIPTION = """\
Call this when tests fail, before trying to fix them. For each failing test it says whether
your change caused it (new_regression: fix these), it was already failing on main
(pre_existing: not yours), it is flaky (flaky: re-run it, don't fix it), or siftwise can't
tell (unknown). Pass the JUnit XML report paths from the test run, or the failing test IDs
(pytest node IDs like tests/test_x.py::test_y work as classname::name, e.g.
tests.test_x::test_y) with optional failure messages."""

EXPLAIN_DESCRIPTION = """\
Call this to understand one test: its recent results, whether it is flaky or broken on main,
and why siftwise would or wouldn't select it for your current change. Use it when a selection
or a triage verdict surprises you, before overriding it."""


@dataclass(frozen=True)
class Config:
    server: str
    token: str
    repo: str | None
    base: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        return cls(
            server=env.get("SIFTWISE_SERVER") or DEFAULT_SERVER,
            token=env.get("SIFTWISE_API_TOKEN") or "",
            repo=env.get("SIFTWISE_REPO") or None,
            base=env.get("SIFTWISE_BASE_REF") or None,
        )


class Unavailable(Exception):
    """siftwise can't answer: no token, no repo, the server is down or errors."""


def build_server(env: Mapping[str, str] | None = None) -> MCPServer:
    """The MCP server. ``env`` (default: the process environment, read on every call)."""
    server = MCPServer("siftwise", instructions=INSTRUCTIONS)

    def config() -> Config:
        return Config.from_env(os.environ if env is None else env)

    @server.tool(description=SELECT_DESCRIPTION)
    def select_tests(
        base: str | None = None, include_uncommitted: bool = True, max_tests: int = 50
    ) -> dict[str, Any]:
        """base: branch or ref to diff against (default: the repo's main branch).
        include_uncommitted: include staged, unstaged and untracked changes.
        max_tests: at most this many tests listed (the command still runs all of them)."""
        return _guarded(config(), lambda c: _select(c, base, include_uncommitted, max_tests))

    @server.tool(description=TRIAGE_DESCRIPTION)
    def triage_failure(
        junit_paths: list[str] | None = None,
        failures: list[dict[str, str]] | None = None,
        base: str | None = None,
        variant: str | None = None,
        include_uncommitted: bool = True,
    ) -> dict[str, Any]:
        """junit_paths: JUnit XML files written by the test run.
        failures: failing tests as {"test_id": ..., "message": ...} (message optional).
        base: branch or ref the change is diffed against (default: the repo's main branch).
        variant: CI matrix leg to compare with (e.g. ubuntu-py3.12), if the repo has several."""
        return _guarded(
            config(),
            lambda c: _triage(c, junit_paths, failures, base, variant, include_uncommitted),
        )

    @server.tool(description=EXPLAIN_DESCRIPTION)
    def explain(
        test_id: str,
        base: str | None = None,
        variant: str | None = None,
        include_uncommitted: bool = True,
    ) -> dict[str, Any]:
        """test_id: the test, as siftwise names it (classname::name).
        base: branch or ref the change is diffed against (default: the repo's main branch).
        variant: CI matrix leg for the broken-on-main evidence."""
        return _guarded(
            config(), lambda c: _explain(c, test_id, base, variant, include_uncommitted)
        )

    return server


# --- tools --------------------------------------------------------------------------------


def _select(config: Config, base: str | None, uncommitted: bool, max_tests: int) -> dict[str, Any]:
    base = base or config.base or cli.default_base()
    try:
        repo = _repo(config)
        change = cli.local_change(base, "HEAD", working_tree=uncommitted)
    except Unavailable as exc:
        return _run_full(str(exc))
    except RepoConfigError as exc:
        return _run_full(f"invalid .siftwise.toml ({exc})")
    body = {"repo": repo, **change.fields, "always_run": list(change.config.always_run)}
    try:
        result = _post(config, "/select", body)
    except Unavailable as exc:
        return _run_full(str(exc))
    estimated = result.get("estimated_ms") or {}
    total, would_run = estimated.get("total") or 0.0, estimated.get("would_run") or 0.0
    tests = result.get("tests") or []
    limit = max(0, max_tests)
    out: dict[str, Any] = {
        "mode": result["mode"],
        "reason": result["reason"],
        "command": result["command"],
        "next_step": _select_next_step(result["mode"], result["command"]),
        "selected": result["selected_count"],
        "total_known": result["total_known"],
        "base": base,
        "uncommitted_included": uncommitted,
    }
    if total > 0:
        out["estimated_test_time_skipped"] = f"{100 * (total - would_run) / total:.0f}%"
    out["tests"] = [
        {"test_id": t["test_id"], "reason": _short(t["reasons"][0] if t["reasons"] else "")}
        for t in tests[:limit]
    ]
    if len(tests) > limit:
        out["more_tests"] = len(tests) - limit
    return out


def _triage(
    config: Config,
    junit_paths: list[str] | None,
    failures: list[dict[str, str]] | None,
    base: str | None,
    variant: str | None,
    uncommitted: bool,
) -> dict[str, Any]:
    found = []
    try:
        if junit_paths:
            found.extend(cli.junit_failures([Path(p) for p in junit_paths]))
    except (JUnitParseError, OSError) as exc:
        return {"error": f"could not read the JUnit reports: {exc}"}
    for item in failures or []:
        if not isinstance(item, dict) or not str(item.get("test_id") or "").strip():
            return {"error": 'each failure needs a "test_id"'}
        found.append({"test_id": str(item["test_id"]).strip(), "message": item.get("message")})
    if not found:
        return {
            "verdicts_available": True,
            "message": "no failing tests given (or none in the JUnit reports)",
            "results": [],
        }
    ids = list(dict.fromkeys(f["test_id"] for f in found))

    try:
        repo, base = _repo(config), base or config.base or cli.default_base()
        try:
            change = cli.local_change(base, "HEAD", working_tree=uncommitted)
        except RepoConfigError as exc:
            raise Unavailable(f"invalid .siftwise.toml ({exc})") from exc
        dirty = cli.git_is_dirty()
        # Tests ran on the working tree: with uncommitted changes, no earlier run tested it.
        commit = None if dirty or dirty is None else cli.git_head()
        body = {
            "repo": repo,
            **change.fields,
            "commit_sha": commit,
            "variant": variant,
            "failures": found,
        }
        result = _post(config, "/triage", body)
    except Unavailable as exc:
        return {
            "verdicts_available": False,
            "message": f"siftwise unavailable ({exc}): verdicts unavailable; treat every failure "
            "as possibly caused by your change",
            "failures": ids[:MAX_TRIAGE_RESULTS],
            "more_failures": max(0, len(ids) - MAX_TRIAGE_RESULTS),
        }

    results = sorted(result["results"], key=lambda r: VERDICT_ORDER.index(r["verdict"]))
    out: dict[str, Any] = {
        "verdicts_available": True,
        "counts": result["counts"],
        "next_step": _triage_next_step(result["counts"]),
        "tested_code": "uncommitted changes" if commit is None else f"commit {commit[:7]}",
        "results": [_compact_result(r) for r in results[:MAX_TRIAGE_RESULTS]],
    }
    if len(results) > MAX_TRIAGE_RESULTS:
        out["more_results"] = len(results) - MAX_TRIAGE_RESULTS
    return out


def _explain(
    config: Config, test_id: str, base: str | None, variant: str | None, uncommitted: bool
) -> dict[str, Any]:
    try:
        repo, base = _repo(config), base or config.base or cli.default_base()
        try:
            change = cli.local_change(base, "HEAD", working_tree=uncommitted)
        except RepoConfigError as exc:
            raise Unavailable(f"invalid .siftwise.toml ({exc})") from exc
        body = {
            "repo": repo,
            **change.fields,
            "always_run": list(change.config.always_run),
            "test_id": test_id,
            "variant": variant,
        }
        result = _post(config, "/explain", body)
    except Unavailable as exc:
        return {"available": False, "message": f"siftwise unavailable ({exc})"}

    stats = result.get("stats")
    selection = result["selection"]
    out: dict[str, Any] = {
        "available": True,
        "test_id": result["test_id"],
        "known": result["known"],
        "file_path": result["file_path"],
        "selection": {
            "selected": selection["selected"],
            "mode": selection["mode"],
            "why": [_short(r) for r in selection["reasons"]]
            or ["not selected: nothing in your change links to it"],
            "links": [_short(link["reason"]) for link in selection["links"]],
        },
    }
    if stats:
        out["stats"] = {
            "window_days": result["window_days"],
            "runs": stats["runs"],
            "failures": stats["failures"],
            "last_failed_at": stats["last_failed_at"],
            "flaky_score": round(stats["flaky_score"], 3),
            "avg_duration_ms": stats["avg_duration_ms"] and round(stats["avg_duration_ms"]),
        }
        out["broken_on_main"] = [
            f"{v['variant'] or 'default variant'} since {v['since_sha'][:7]}"
            for v in stats["broken_on_main_variants"]
        ] or "no"
    out["flaky"] = {
        "counts": result["flaky_counts"],
        "events": [_describe_event(e) for e in result["flaky"][:5]],
    }
    out["recent"] = [
        f"{r['occurred_at'][:16]} {'main' if r['is_main'] else r['branch']} "
        f"{r['commit_sha'][:7]}{_on(r['variant'])}: {r['status']}"
        + (f" ({r['attempts']} attempts)" if r["attempts"] > 1 else "")
        for r in result["recent"]
    ]
    return out


# --- pieces -------------------------------------------------------------------------------


def _guarded(config: Config, tool: Callable[[Config], dict[str, Any]]) -> dict[str, Any]:
    """Run a tool; whatever happens, the result fits the cap and never contains the token."""
    try:
        result = tool(config)
    except Unavailable as exc:  # raised before a tool's own fallback (e.g. no repo)
        result = {"available": False, "message": f"siftwise unavailable ({exc})"}
    except Exception as exc:  # noqa: BLE001 - an agent tool must answer, not crash
        result = {"error": f"siftwise failed: {type(exc).__name__}: {exc}"}
    return _fit(_redact(result, config.token))


def _repo(config: Config) -> str:
    repo = config.repo or cli.origin_repo()
    if not repo:
        raise Unavailable("can't tell the repository: set SIFTWISE_REPO, e.g. owner/name")
    return repo


def _post(config: Config, path: str, body: dict[str, Any]) -> dict[str, Any]:
    if not config.token:
        raise Unavailable("SIFTWISE_API_TOKEN is not set")
    try:
        return cli.post_json(config.server, config.token, path, body, timeout=TIMEOUT)
    except cli.SiftwiseError as exc:
        if exc.status == httpx.codes.NOT_FOUND:
            raise Unavailable("no test history for this repo yet") from exc
        raise Unavailable(str(exc)) from exc


def _run_full(problem: str) -> dict[str, Any]:
    return {
        "mode": "full",
        "reason": problem,
        "command": "",
        "next_step": f"siftwise can't select tests ({problem}): run the full suite",
    }


def _select_next_step(mode: str, command: str) -> str:
    if mode == "full":
        return f"Run the full suite: {command}" if command else "Run the project's full test suite."
    if command:
        return "Run this command instead of the full suite: " + command
    return "No tests are affected by your change: there is nothing to run."


def _triage_next_step(counts: Mapping[str, int]) -> str:
    steps = []
    if counts.get("new_regression"):
        steps.append(f"fix the {counts['new_regression']} new_regression failure(s) first")
    if counts.get("unknown"):
        steps.append(f"look into the {counts['unknown']} unknown one(s): they may be yours")
    if counts.get("flaky"):
        steps.append(f"re-run the {counts['flaky']} flaky one(s) instead of fixing them")
    if counts.get("pre_existing"):
        steps.append(
            f"leave the {counts['pre_existing']} pre_existing one(s): already failing on main"
        )
    return "; ".join(steps).capitalize() + "." if steps else "Nothing to do."


def _compact_result(result: Mapping[str, Any]) -> dict[str, Any]:
    evidence = result.get("evidence") or {}
    compact: dict[str, Any] = {
        "test_id": result["test_id"],
        "verdict": result["verdict"],
        "confidence": result["confidence"],
        "reasons": [_short(r) for r in result["reasons"][:3]],
    }
    links = evidence.get("links") or []
    if links:
        compact["linked_by"] = _short(links[0]["reason"])
    streak = evidence.get("pre_existing")
    if streak:
        compact["failing_on_main"] = (
            f"since {streak['since_sha'][:7]}, {streak['main_runs_failing']} main run(s)"
        )
    history = evidence.get("history") or {}
    flaky = history.get("flaky_counts") or {}
    if any(flaky.values()):
        compact["flaky_counts"] = flaky
        if evidence.get("flaky"):
            compact["newest_flaky_event"] = _describe_event(evidence["flaky"][0])
    return compact


def _describe_event(event: Mapping[str, Any]) -> str:
    failed = ", ".join(f"run {r['run_id']}" for r in event["failed"][:3])
    passed = ", ".join(f"run {r['run_id']}" for r in event["passed"][:3])
    if event["kind"] == "same_commit":
        what = f"failed ({failed}) and passed ({passed}) on commit {event['commit_sha'][:7]}"
    else:
        what = (
            f"failed on {event['commit_sha'][:7]} ({failed}) with an unrelated change, then "
            f"passed on the branch's next run ({passed})"
        )
    return f"{event['kind']}: {what}{_on(event.get('variant'))}"


def _on(variant: str | None) -> str:
    return f" on {variant}" if variant else ""


def _short(text: str, limit: int = MAX_REASON_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _redact(value: Any, token: str) -> Any:
    """``value`` with every occurrence of the token replaced: it must never reach the agent."""
    if not token:
        return value
    if isinstance(value, str):
        return value.replace(token, "***")
    if isinstance(value, list):
        return [_redact(v, token) for v in value]
    if isinstance(value, dict):
        return {_redact(k, token): _redact(v, token) for k, v in value.items()}
    return value


def _size(value: Any) -> int:
    # As the SDK renders a structured result's text content.
    return len(json.dumps(value, indent=2))


def _fit(result: dict[str, Any], limit: int = MAX_OUTPUT_CHARS) -> dict[str, Any]:
    """Shrink ``result`` to at most ``limit`` characters of JSON: drop list items from the
    end (counted in ``truncated``), then shorten long strings."""
    if _size(result) <= limit:
        return result
    result = dict(result)
    result["truncated"] = True
    lists = [k for k, v in result.items() if isinstance(v, list)]
    while _size(result) > limit and any(result[k] for k in lists):
        key = max((k for k in lists if result[k]), key=lambda k: _size(result[k]))
        result[key] = result[key][:-1]
        more = f"more_{key}"
        result[more] = int(result.get(more, 0)) + 1
    for cut in (MAX_REASON_CHARS, 80, 30):
        if _size(result) <= limit:
            break
        result = _shorten(result, cut)
    return result


def _shorten(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return _short(value, limit)
    if isinstance(value, list):
        return [_shorten(v, limit) for v in value]
    if isinstance(value, dict):
        return {k: _shorten(v, limit) for k, v in value.items()}
    return value


def main() -> None:
    """Serve over stdio until the client disconnects."""
    # The SDK logs at INFO to stderr; one httpx line per API call is noise in agent logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    build_server().run("stdio")
