"""`siftwise mcp`: test selection, failure triage and explanations for coding agents.

An MCP server over stdio (the official ``mcp`` SDK, ``siftwise[mcp]``). It holds no state:
each tool computes the change in the agent's checkout with the same code as ``siftwise
select`` (git diff, import graphs, .siftwise.toml) and calls the siftwise API. It never
records runs.

Configuration comes from the environment, read on every call: ``SIFTWISE_SERVER`` (default
``http://localhost:8000``), ``SIFTWISE_API_TOKEN``, and optionally ``SIFTWISE_REPO`` (else
``owner/name`` from the ``origin`` remote), ``SIFTWISE_BASE_REF`` (else the branch
``origin/HEAD`` points to, else ``main``) and ``SIFTWISE_REPO_PATH``.

The repository, first that applies: the tool's ``repo_path`` argument, ``SIFTWISE_REPO_PATH``,
``CLAUDE_PROJECT_DIR`` (set by Claude Code), the first of the client's MCP roots that is in a
git repository (asked only when the client declared the roots capability), else the server's
working directory. Clients start stdio servers where they like (VS Code in the workspace,
others in the home directory), so outside a git repository every tool says how to point
siftwise at one. Git runs in that directory; nothing changes the process's directory (the
blocking work runs in worker threads, in parallel).

Schemas are kept plain for every client: each parameter has a description, and optional ones
are plain JSON types (no ``anyOf`` with null), which some clients' converters mishandle.

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
import urllib.parse
import urllib.request
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import anyio
import anyio.to_thread
import httpx
from mcp import MCPDeprecationWarning
from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from siftwise import __version__
from siftwise.cli import main as cli
from siftwise.cli.repoconfig import RepoConfigError
from siftwise.core import selector
from siftwise.core.junit import JUnitParseError

# Every tool result, as JSON text, stays within this many characters.
MAX_OUTPUT_CHARS = 8000
MAX_REASON_CHARS = 200
DEFAULT_SERVER = "http://localhost:8000"
# API calls: under the clients' own tool-call timeouts (Codex: 60 s by default), so a
# hung server still gets the fail-open answer back to the agent.
TIMEOUT = 30.0
# Asking the client for its roots: a client that declares roots but never answers must not
# hold up the tool.
ROOTS_TIMEOUT = 5.0
# Repositories one level down listed in the "not a repository" error.
MAX_FOUND = 10
NOT_A_REPO = (
    "is not inside a git repository: call again with repo_path set to the repository's "
    "absolute path, or set SIFTWISE_REPO_PATH (or the client's working directory for the "
    "server) to it"
)

logger = logging.getLogger(__name__)

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
    # The checkout, when the client starts the server elsewhere (SIFTWISE_REPO_PATH).
    repo_path: str | None = None
    # The project Claude Code started the server for (it sets CLAUDE_PROJECT_DIR).
    project_dir: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        return cls(
            server=env.get("SIFTWISE_SERVER") or DEFAULT_SERVER,
            token=env.get("SIFTWISE_API_TOKEN") or "",
            repo=env.get("SIFTWISE_REPO") or None,
            base=env.get("SIFTWISE_BASE_REF") or None,
            repo_path=env.get("SIFTWISE_REPO_PATH") or None,
            project_dir=env.get("CLAUDE_PROJECT_DIR") or None,
        )


@dataclass(frozen=True)
class Where:
    """What a tool call knows about where the repository is: the ``repo_path`` argument and
    the client's roots (MCP roots, asked only when nothing before them in the order applies)."""

    repo_path: str | None = None
    roots: tuple[Path, ...] = ()


class Unavailable(Exception):
    """siftwise can't answer: no token, no repo, the server is down or errors."""


def _plain(**schema: Any) -> Callable[[dict[str, Any]], None]:
    """``json_schema_extra`` for an optional parameter: the plain JSON type, no ``anyOf`` with
    null and no null default. Codex's converter gives a schema without ``type`` the type
    string (openai/codex#1975, test_mcp_tool_anyof_defaults_to_string); Gemini CLI edits
    ``anyOf`` entries for Vertex AI (its MCP docs). The Python side still accepts null."""

    def edit(field: dict[str, Any]) -> None:
        field.pop("anyOf", None)
        if field.get("default", "") is None:
            del field["default"]
        field.update(schema)

    return edit


STRING = _plain(type="string")
# Parameters every tool takes, described in the schema: clients show and send only that.
Base = Annotated[
    str | None,
    Field(
        description="Branch or ref the change is diffed against (default: SIFTWISE_BASE_REF, "
        "else the branch origin/HEAD points to, else main).",
        json_schema_extra=STRING,
    ),
]
Uncommitted = Annotated[
    bool, Field(description="Include staged, unstaged and untracked changes in the change.")
]
Variant = Annotated[
    str | None,
    Field(
        description="CI matrix leg to compare with (e.g. ubuntu-py3.12), if the repo has several.",
        json_schema_extra=STRING,
    ),
]
RepoPath = Annotated[
    str | None,
    Field(
        description="Absolute path of the repository. Only needed when siftwise says it isn't "
        "running in a git repository (default: SIFTWISE_REPO_PATH, CLAUDE_PROJECT_DIR, the "
        "client's workspace roots, else the server's working directory).",
        json_schema_extra=STRING,
    ),
]
FAILURE_ITEM = {
    "type": "object",
    "properties": {
        "test_id": {"type": "string", "description": "classname::name, e.g. tests.test_x::test_y"},
        "message": {"type": "string", "description": "the failure message (optional)"},
    },
    "required": ["test_id"],
}


def build_server(env: Mapping[str, str] | None = None) -> MCPServer:
    """The MCP server. ``env`` (default: the process environment, read on every call)."""
    # The version shows in clients' server lists (and in bug reports).
    server = MCPServer("siftwise", instructions=INSTRUCTIONS, version=__version__)

    def config() -> Config:
        return Config.from_env(os.environ if env is None else env)

    @server.tool(description=SELECT_DESCRIPTION)
    async def select_tests(
        ctx: Context,
        base: Base = None,
        include_uncommitted: Uncommitted = True,
        max_tests: Annotated[
            int,
            Field(
                description="List at most this many tests (the command still runs all of them).",
                ge=0,
            ),
        ] = 50,
        repo_path: RepoPath = None,
    ) -> dict[str, Any]:
        c = config()
        where = await _where(c, repo_path, ctx)
        return await _run(c, lambda c: _select(c, where, base, include_uncommitted, max_tests))

    @server.tool(description=TRIAGE_DESCRIPTION)
    async def triage_failure(
        ctx: Context,
        junit_paths: Annotated[
            list[str] | None,
            Field(
                description="JUnit XML reports written by the test run: absolute paths, or "
                "relative to the repository root.",
                json_schema_extra=_plain(type="array", items={"type": "string"}),
            ),
        ] = None,
        failures: Annotated[
            list[dict[str, str | None]] | None,
            Field(
                description='Failing tests, if there is no JUnit report: [{"test_id": ..., '
                '"message": ...}], message optional.',
                json_schema_extra=_plain(type="array", items=FAILURE_ITEM),
            ),
        ] = None,
        base: Base = None,
        variant: Variant = None,
        include_uncommitted: Uncommitted = True,
        repo_path: RepoPath = None,
    ) -> dict[str, Any]:
        c = config()
        where = await _where(c, repo_path, ctx)
        return await _run(
            c,
            lambda c: _triage(c, where, junit_paths, failures, base, variant, include_uncommitted),
        )

    @server.tool(description=EXPLAIN_DESCRIPTION)
    async def explain(
        ctx: Context,
        test_id: Annotated[
            str, Field(description="The test, as siftwise names it: classname::name.")
        ],
        base: Base = None,
        variant: Variant = None,
        include_uncommitted: Uncommitted = True,
        repo_path: RepoPath = None,
    ) -> dict[str, Any]:
        c = config()
        where = await _where(c, repo_path, ctx)
        return await _run(
            c, lambda c: _explain(c, where, test_id, base, variant, include_uncommitted)
        )

    return server


# --- tools --------------------------------------------------------------------------------


def _select(
    config: Config, where: Where, base: str | None, uncommitted: bool, max_tests: int
) -> dict[str, Any]:
    try:
        checkout = _checkout(config, where)
        root = checkout.root
        base = base or config.base or cli.default_base(root)
        repo = _repo(config, root)
        change = cli.local_change(base, "HEAD", working_tree=uncommitted, cwd=root)
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
        **checkout.noted(),
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
    where: Where,
    junit_paths: list[str] | None,
    failures: list[dict[str, str | None]] | None,
    base: str | None,
    variant: str | None,
    uncommitted: bool,
) -> dict[str, Any]:
    given = []
    for item in failures or []:
        if not isinstance(item, dict) or not str(item.get("test_id") or "").strip():
            return {"error": 'each failure needs a "test_id"'}
        given.append({"test_id": str(item["test_id"]).strip(), "message": item.get("message")})
    found: list[dict[str, Any]] = []
    root: Path | None = None
    problem: Unavailable | None = None
    noted: dict[str, str] = {}
    try:
        checkout = _checkout(config, where)
        root, noted = checkout.root, checkout.noted()
    except Unavailable as exc:
        problem = exc
    try:
        if junit_paths:
            # Relative paths are the repository's (the server may run elsewhere).
            paths = [root / p if root else Path(p) for p in junit_paths]
            found.extend(cli.junit_failures(paths))
    except (JUnitParseError, OSError) as exc:
        if problem is not None:  # most likely why: say how to point siftwise at the repo
            return {"verdicts_available": False, "message": f"siftwise unavailable ({problem})"}
        return {"error": f"could not read the JUnit reports: {exc}"}
    found.extend(given)  # after the reports' failures, as before
    if not found:
        return {
            "verdicts_available": True,
            "message": "no failing tests given (or none in the JUnit reports)",
            "results": [],
        }
    ids = list(dict.fromkeys(f["test_id"] for f in found))

    try:
        if root is None:
            raise problem or Unavailable("no repository")
        repo, base = _repo(config, root), base or config.base or cli.default_base(root)
        try:
            change = cli.local_change(base, "HEAD", working_tree=uncommitted, cwd=root)
        except RepoConfigError as exc:
            raise Unavailable(f"invalid .siftwise.toml ({exc})") from exc
        dirty = cli.git_is_dirty(root)
        # Tests ran on the working tree: with uncommitted changes, no earlier run tested it.
        commit = None if dirty or dirty is None else cli.git_head(root)
        body = {
            "repo": repo,
            **change.fields,
            "commit_sha": commit,
            "variant": variant,
            # The base's list: an agent's own edit to known_flaky must not excuse its failures.
            "known_flaky": list(cli.known_flaky(change.config, base, cwd=root).globs),
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
        **noted,
        "results": [_compact_result(r) for r in results[:MAX_TRIAGE_RESULTS]],
    }
    if len(results) > MAX_TRIAGE_RESULTS:
        out["more_results"] = len(results) - MAX_TRIAGE_RESULTS
    return out


def _explain(
    config: Config,
    where: Where,
    test_id: str,
    base: str | None,
    variant: str | None,
    uncommitted: bool,
) -> dict[str, Any]:
    try:
        checkout = _checkout(config, where)
        root = checkout.root
        repo, base = _repo(config, root), base or config.base or cli.default_base(root)
        try:
            change = cli.local_change(base, "HEAD", working_tree=uncommitted, cwd=root)
        except RepoConfigError as exc:
            raise Unavailable(f"invalid .siftwise.toml ({exc})") from exc
        body = {
            "repo": repo,
            **change.fields,
            "always_run": list(change.config.always_run),
            "known_flaky": list(cli.known_flaky(change.config, base, cwd=root).globs),
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
        "file_path": result["file_path"] or _derived_file(result["test_id"], root),
        **checkout.noted(),
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


async def _run(config: Config, tool: Callable[[Config], dict[str, Any]]) -> dict[str, Any]:
    """``_guarded`` in a worker thread: git, the graphs and the API call block."""
    return await anyio.to_thread.run_sync(_guarded, config, tool)


@dataclass(frozen=True)
class Checkout:
    """The repository a tool works on, and a note when siftwise had to look inside the folder
    the client gave (the agent should know which repository it got)."""

    root: Path
    note: str | None = None

    def noted(self) -> dict[str, str]:
        return {"repository_note": self.note} if self.note else {}


def _user_given(config: Config, where: Where) -> tuple[str, str] | None:
    """The location the user set, as (its name, its value): the ``repo_path`` argument, else
    SIFTWISE_REPO_PATH. Wrong is an error: the user meant it."""
    for source, value in (("repo_path", where.repo_path), ("SIFTWISE_REPO_PATH", config.repo_path)):
        if value:
            return source, value
    return None


async def _where(config: Config, repo_path: str | None, ctx: Context) -> Where:
    """The call's ``Where``: the client's roots are asked for only when no user-set location
    applies and CLAUDE_PROJECT_DIR doesn't give a repository (so a client that doesn't
    support roots is never asked anything)."""
    if _user_given(config, Where(repo_path)):
        return Where(repo_path)
    if config.project_dir:
        found, _ = await anyio.to_thread.run_sync(
            _client_folder, "CLAUDE_PROJECT_DIR", Path(config.project_dir).expanduser()
        )
        if found is not None:
            return Where(repo_path)
    return Where(repo_path, await _client_roots(ctx))


async def _client_roots(ctx: Context) -> tuple[Path, ...]:
    """The client's roots (MCP roots) as local paths, or () when the client didn't declare the
    roots capability, the connection can't carry a server request (the 2026-07-28 protocol
    has none), or asking fails or takes longer than ``ROOTS_TIMEOUT``."""
    session = ctx.session
    capabilities = session.client_capabilities
    if capabilities is None or capabilities.roots is None or not session.can_send_request:
        return ()
    try:
        with anyio.fail_after(ROOTS_TIMEOUT), warnings.catch_warnings():
            # Deprecated in 2026-07-28 (SEP-2577); handshake-era clients still use roots.
            warnings.simplefilter("ignore", MCPDeprecationWarning)
            result = await session.list_roots()
    except Exception as exc:  # noqa: BLE001 - no roots: the working directory, as before
        logger.warning("siftwise: could not get the client's roots (%s)", exc)
        return ()
    paths = (_root_path(str(root.uri)) for root in result.roots)
    return tuple(path for path in paths if path is not None)


def _root_path(uri: str) -> Path | None:
    """A ``file://`` root as a local path (``file:///C:/x`` -> ``C:\\x`` on Windows)."""
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "file":
        return None
    path = urllib.request.url2pathname(parsed.path)
    if parsed.netloc and parsed.netloc != "localhost":  # a UNC share: //server/share/...
        path = f"//{parsed.netloc}{path}"
    return Path(path)


def _repos_inside(folder: Path) -> list[Path]:
    """Git repositories directly inside ``folder`` (a subdirectory with a ``.git`` entry: a
    directory, or a file for worktrees and submodules), sorted."""
    try:
        return sorted(p for p in folder.iterdir() if p.is_dir() and (p / ".git").exists())
    except OSError:
        return []


def _client_folder(source: str, folder: Path) -> tuple[Checkout | None, list[Path]]:
    """A folder the client set (CLAUDE_PROJECT_DIR, a root): its repository, else the only
    repository directly inside it (with a note), else None. Also the repositories found
    inside, for the error when nothing works."""
    if not folder.is_dir():
        return None, []
    root = cli.git_toplevel(folder)
    if root is not None:
        return Checkout(root), []
    inside = _repos_inside(folder)
    if len(inside) == 1:
        root = cli.git_toplevel(inside[0])
        if root is not None:
            note = (
                f"{source} {folder} isn't in a git repository; using the only one directly "
                f"inside it, {root} (pass repo_path for another)"
            )
            return Checkout(root, note), inside
    return None, inside


def _checkout(config: Config, where: Where) -> Checkout:
    """The git checkout the tools work on, first that applies:

    1. the ``repo_path`` argument, 2. SIFTWISE_REPO_PATH: set by the user, so a folder outside
       a git repository is an error;
    3. CLAUDE_PROJECT_DIR (Claude Code), 4. the client's roots (the first one in a git
       repository, else the first holding exactly one repository): set by the client, so a
       folder that isn't a repository but holds exactly one directly inside means that one,
       and anything else falls through;
    5. the server's working directory.

    Clients start stdio servers wherever they like (some in the home directory), so the error
    says how to point siftwise at a repository and lists those found one level down."""
    given = _user_given(config, where)
    if given:
        source, value = given
        start = Path(value).expanduser()
        if not start.is_dir():
            raise Unavailable(f"{source} {start} is not a directory")
        root = cli.git_toplevel(start)
        if root is None:
            raise Unavailable(f"{source} {start} {NOT_A_REPO}{_found(_repos_inside(start))}")
        return Checkout(root)

    tried: list[str] = []
    found: list[Path] = []
    if config.project_dir:
        project = Path(config.project_dir).expanduser()
        checkout, inside = _client_folder("CLAUDE_PROJECT_DIR", project)
        if checkout is not None:
            return checkout
        tried.append(f"CLAUDE_PROJECT_DIR {project}")
        found += inside
    # A root inside a repository wins over one that only holds a repository.
    for path in where.roots:
        root = cli.git_toplevel(path) if path.is_dir() else None
        if root is not None:
            return Checkout(root)
    for path in where.roots:
        checkout, inside = _client_folder("the client's root", path)
        if checkout is not None:
            return checkout
        found += inside
    if where.roots:
        tried.append(f"the client's roots ({', '.join(str(path) for path in where.roots)})")

    start = Path.cwd()
    root = cli.git_toplevel(start)
    if root is not None:
        return Checkout(root)
    found += _repos_inside(start)
    where_ = f"the server's working directory, {start}, {NOT_A_REPO}"
    if tried:
        where_ = f"{' and '.join(tried)} gave no git repository, and {where_}"
    raise Unavailable(where_ + _found(found))


def _found(repos: list[Path]) -> str:
    """The repositories found one level down, for an error message."""
    unique = list(dict.fromkeys(repos))
    if not unique:
        return ""
    listed = ", ".join(str(path) for path in unique[:MAX_FOUND])
    more = f" and {len(unique) - MAX_FOUND} more" if len(unique) > MAX_FOUND else ""
    return f". Git repositories one level down: {listed}{more}"


def _derived_file(test_id: str, root: Path) -> str | None:
    """A pytest test's file from its ID (the selector's mapping: ``tests.test_cart::test_x`` ->
    ``tests/test_cart.py``), when nothing reported one; only if it exists in the checkout."""
    path = selector.pytest_file(test_id)
    return path if path and (root / path).is_file() else None


def _repo(config: Config, root: Path) -> str:
    repo = config.repo or cli.origin_repo(root)
    if not repo:
        raise Unavailable(
            "can't tell the repository's name (no origin remote): set SIFTWISE_REPO, "
            "e.g. owner/name"
        )
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
