"""`siftwise` command-line interface."""

import json
import os
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer

from siftwise import __version__, extras
from siftwise.cli import gha, gitlab, server
from siftwise.cli.gograph import build_graph, run_go_list
from siftwise.cli.pygraph import build_py_graph
from siftwise.cli.repoconfig import (
    FILE_NAME,
    RepoConfig,
    RepoConfigError,
    load_repo_config,
    loads_repo_config,
    read_go_module,
)
from siftwise.core.junit import JUnitParseError, Status, parse_junit

app = typer.Typer(help="Siftwise: test impact analysis for CI.", no_args_is_help=True)
app.add_typer(gha.app, name="gha")
app.add_typer(gitlab.app, name="gitlab")
app.add_typer(server.app, name="server")

# `siftwise select` exit codes, besides 0 (success, including "no tests affected").
EXIT_API_ERROR = 1
EXIT_USAGE = 2
EXIT_FULL_SUITE_NO_COMMAND = 3
# `siftwise triage --fail-on`: a failure got one of the given verdicts.
EXIT_FAIL_ON = 4

VERDICTS = ("new_regression", "pre_existing", "flaky", "unknown")

# --server, else SIFTWISE_SERVER (as for MCP and the Action's steps), else localhost.
DEFAULT_SERVER = "http://localhost:8000"
ServerOption = Annotated[
    str,
    typer.Option(
        help="siftwise server URL (default: SIFTWISE_SERVER, else http://localhost:8000).",
        envvar="SIFTWISE_SERVER",
    ),
]


class KnownFlakySource(StrEnum):
    BASE = "base"
    HEAD = "head"


class SiftwiseError(Exception):
    """The server couldn't be reached, timed out, or answered with an error."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status  # the HTTP status, if the server answered


def _print_version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    show_version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_print_version,
            is_eager=True,
            help="Print the siftwise version and exit (same as `siftwise version`).",
        ),
    ] = False,
) -> None:
    """Siftwise: test impact analysis for CI."""


@app.command()
def version() -> None:
    """Print the siftwise version."""
    typer.echo(__version__)


@app.command("select")
def select_command(
    repo: Annotated[str, typer.Option(help="Repository name in siftwise, e.g. acme/shop.")],
    base: Annotated[str, typer.Option(help="Base ref to diff against.")] = "main",
    head: Annotated[str, typer.Option(help="Head ref.")] = "HEAD",
    server: ServerOption = DEFAULT_SERVER,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the full API response as JSON.")
    ] = False,
    go_list: Annotated[
        bool,
        typer.Option(help="For Go repos, run `go list` to select tests of dependent packages."),
    ] = True,
    py_imports: Annotated[
        bool,
        typer.Option(
            help="When Python files changed, build a static import graph to select test files "
            "that import changed modules."
        ),
    ] = True,
) -> None:
    """Print the test command for the changes in BASE...HEAD.

    The command goes to stdout (so `eval "$(siftwise select ...)"` works); the mode and reason go
    to stderr. Needs SIFTWISE_API_TOKEN. Exit codes: 0 ok (stdout is empty if no tests are
    affected), 1 API error, 2 usage error, 3 full suite needed but no command is known.
    """
    token = _token()
    try:
        body = selection_request(repo, base, head, go_list=go_list, py_imports=py_imports)
    except RepoConfigError as exc:
        _err(f"error: {exc}")
        raise typer.Exit(EXIT_USAGE) from exc
    try:
        result = post_json(server, token, "/select", body)
    except SiftwiseError as exc:
        _err(f"error: {exc}")
        raise typer.Exit(EXIT_API_ERROR) from exc

    if json_output:
        typer.echo(json.dumps(result, indent=2))
        return

    _err(
        f"siftwise: {result['mode']} ({result['selected_count']} of {result['total_known']} "
        f"known tests): {result['reason']}"
    )
    if result["command"]:
        typer.echo(result["command"])
    elif result["mode"] == "full":
        # Never let "run nothing" be mistaken for "run everything".
        _err("error: the full suite is needed, but siftwise knows no command for this repo's tests")
        raise typer.Exit(EXIT_FULL_SUITE_NO_COMMAND)


@app.command("record")
def record_command(
    junit: Annotated[list[Path], typer.Argument(help="JUnit XML files to upload.")],
    repo: Annotated[str, typer.Option(help="Repository name in siftwise, e.g. acme/shop.")],
    branch: Annotated[str, typer.Option(help="Branch the run tested.")],
    commit: Annotated[str | None, typer.Option(help="Full commit SHA (default: HEAD).")] = None,
    main: Annotated[bool, typer.Option("--main", help="The run is on the main branch.")] = False,
    ci_run_id: Annotated[str | None, typer.Option(help="CI run ID (makes re-uploads no-ops).")] = (
        None
    ),
    run_attempt: Annotated[int, typer.Option(min=1, help="CI run attempt.")] = 1,
    variant: Annotated[str | None, typer.Option(help="Matrix leg, e.g. ubuntu-py3.12.")] = None,
    started_at: Annotated[str | None, typer.Option(help="ISO 8601 run start time.")] = None,
    base: Annotated[
        str | None, typer.Option(help="Base ref: changed files are BASE...HEAD (else unknown).")
    ] = None,
    head: Annotated[str, typer.Option(help="Head ref for the diff.")] = "HEAD",
    defer_rollup: Annotated[
        bool, typer.Option(help="Skip the stats rollup; run `siftwise rollup` after all legs.")
    ] = False,
    server: ServerOption = DEFAULT_SERVER,
    go_list: Annotated[
        bool, typer.Option(help="Record Go import links (`go list`), for triage.")
    ] = True,
    py_imports: Annotated[bool, typer.Option(help="Record Python import links, for triage.")] = (
        True
    ),
) -> None:
    """Upload a CI run's JUnit XML. Exit codes: 0 ok, 1 API error, 2 usage error.

    With --base, the change and its import graphs are recorded too, so triage can later tell
    whether this run's failures were related to its change.
    """
    token = _token()
    missing = [str(path) for path in junit if not path.is_file()]
    if missing:
        _err(f"error: not a file: {', '.join(missing)}")
        raise typer.Exit(EXIT_USAGE)
    metadata: dict[str, Any] = {
        "repo": repo,
        "commit_sha": commit or git_rev_parse("HEAD"),
        "branch": branch,
        "is_main": main,
        "ci_run_id": ci_run_id,
        "run_attempt": run_attempt,
        "variant": variant,
        "started_at": started_at,
        **recorded_change(base, head, go_list=go_list, py_imports=py_imports),
    }
    try:
        result = upload_run(server, token, junit, metadata, defer_rollup=defer_rollup)
    except SiftwiseError as exc:
        _err(f"error: {exc}")
        raise typer.Exit(EXIT_API_ERROR) from exc
    _err(f"siftwise: run {result['run_id']} {'stored' if result['created'] else 'already stored'}"
         f" ({result['counts']['total']} results)")  # fmt: skip


@app.command("rollup")
def rollup_command(
    repo: Annotated[str, typer.Option(help="Repository name in siftwise, e.g. acme/shop.")],
    server: ServerOption = DEFAULT_SERVER,
) -> None:
    """Recompute a repo's test stats, after uploads with --defer-rollup."""
    try:
        result = rollup_repo(server, _token(), repo)
    except SiftwiseError as exc:
        _err(f"error: {exc}")
        raise typer.Exit(EXIT_API_ERROR) from exc
    _err(f"siftwise: {result['tests_updated']} tests updated in {result['seconds']}s")


@app.command("triage")
def triage_command(
    junit: Annotated[list[Path], typer.Argument(help="JUnit XML files of the run.")],
    repo: Annotated[str, typer.Option(help="Repository name in siftwise, e.g. acme/shop.")],
    base: Annotated[str, typer.Option(help="Base ref the change is diffed against.")] = "main",
    head: Annotated[str, typer.Option(help="Head ref: the commit that was tested.")] = "HEAD",
    commit: Annotated[str | None, typer.Option(help="Full commit SHA (default: HEAD's).")] = None,
    variant: Annotated[
        str | None, typer.Option(help="Matrix leg: compare with this variant's history.")
    ] = None,
    ci_run_id: Annotated[
        str | None, typer.Option(help="CI run ID: its uploads are left out of the history.")
    ] = None,
    run_attempt: Annotated[int, typer.Option(min=1, help="CI run attempt.")] = 1,
    fail_on: Annotated[
        str,
        typer.Option(
            help="Comma-separated verdicts that fail the command (exit 4), e.g. new_regression."
        ),
    ] = "",
    server: ServerOption = DEFAULT_SERVER,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the full API response as JSON.")
    ] = False,
    go_list: Annotated[bool, typer.Option(help="For Go repos, link tests through `go list`.")] = (
        True
    ),
    py_imports: Annotated[
        bool, typer.Option(help="Link Python tests through a static import graph.")
    ] = True,
    known_flaky_source: Annotated[
        KnownFlakySource,
        typer.Option(
            help="Whose .siftwise.toml known_flaky applies: BASE's (default: a change can't "
            "mark its own failures flaky) or the checkout's."
        ),
    ] = KnownFlakySource.BASE,
) -> None:
    """Triage a run's failing tests: new regression, pre-existing on main, flaky or unknown.

    The failing tests come from the JUnit files, the change from BASE...HEAD (as in `select`).
    One line per failure on stdout (verdict, confidence, test) with its main reason; counts on
    stderr. Needs SIFTWISE_API_TOKEN. Exit codes: 0 ok, 1 API error, 2 usage error, 4 a
    failure got a --fail-on verdict.
    """
    token = _token()
    gate = {v.strip() for v in fail_on.split(",") if v.strip()}
    if gate - set(VERDICTS):
        _err(f"error: unknown --fail-on verdict(s): {', '.join(sorted(gate - set(VERDICTS)))}"
             f" (choose from {', '.join(VERDICTS)})")  # fmt: skip
        raise typer.Exit(EXIT_USAGE)
    missing = [str(path) for path in junit if not path.is_file()]
    if missing:
        _err(f"error: not a file: {', '.join(missing)}")
        raise typer.Exit(EXIT_USAGE)
    try:
        failures = junit_failures(junit)
    except (JUnitParseError, OSError) as exc:
        _err(f"error: {exc}")
        raise typer.Exit(EXIT_USAGE) from exc
    if not failures:
        _err("siftwise: no failing tests in the JUnit report(s)")
        if json_output:
            typer.echo(json.dumps({"repo": repo, "counts": {}, "results": []}, indent=2))
        return
    try:
        change = local_change(base, head, go_list=go_list, py_imports=py_imports)
    except RepoConfigError as exc:
        _err(f"error: {exc}")
        raise typer.Exit(EXIT_USAGE) from exc
    flaky = known_flaky(change.config, base, known_flaky_source)
    if flaky.changed:
        _err("siftwise: known_flaky changes in this change apply after merge")
    body: dict[str, Any] = {
        "repo": repo,
        **change.fields,
        "commit_sha": commit or git_rev_parse(head),
        "variant": variant,
        "ci_run_id": ci_run_id,
        "run_attempt": run_attempt,
        "known_flaky": list(flaky.globs),
        "failures": failures,
    }
    try:
        result = post_json(server, token, "/triage", body)
    except SiftwiseError as exc:
        _err(f"error: {exc}")
        raise typer.Exit(EXIT_API_ERROR) from exc

    if json_output:
        typer.echo(json.dumps(result, indent=2))
    else:
        for item in result["results"]:
            typer.echo(f"{item['verdict']:<15} {item['confidence']:<6} {item['test_id']}")
            if item["reasons"]:
                typer.echo(f"    {item['reasons'][0]}")
    counts = result.get("counts", {})
    summary = ", ".join(f"{counts.get(v, 0)} {v.replace('_', ' ')}" for v in VERDICTS)
    _err(f"siftwise: {len(result['results'])} failing test(s): {summary}")
    hit = sorted({item["verdict"] for item in result["results"]} & gate)
    if hit:
        _err(f"siftwise: --fail-on matched: {', '.join(hit)}")
        raise typer.Exit(EXIT_FAIL_ON)


@app.command("report")
def report_command(
    repo: Annotated[str, typer.Option(help="Repository name in siftwise, e.g. acme/shop.")],
    days: Annotated[int, typer.Option(min=1, max=365, help="Shadow runs of the last N days.")] = 30,
    server: ServerOption = DEFAULT_SERVER,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the full API response as JSON.")
    ] = False,
    by: Annotated[
        str | None, typer.Option(help="'week': recall, caught, missed and time saved per week.")
    ] = None,
) -> None:
    """Live recall from shadow runs: how many failures the selection would have caught or
    missed (and why), over every shadow run and over selective ones only, the time it would
    have saved, and .siftwise.toml rules to consider. Needs SIFTWISE_API_TOKEN. Exit codes:
    0 ok, 1 API error, 2 usage error."""
    if by not in (None, "week"):
        _err(f"error: --by must be 'week', not {by!r}")
        raise typer.Exit(EXIT_USAGE)
    token = _token()
    params: dict[str, Any] = {"days": days, **({"by": by} if by else {})}
    try:
        result = get_json(server, token, f"/repos/{repo}/recall", params)
    except SiftwiseError as exc:
        _err(f"error: {exc}")
        raise typer.Exit(EXIT_API_ERROR) from exc
    if json_output:
        typer.echo(json.dumps(result, indent=2))
        return
    lines = weekly_lines(result) if by == "week" else recall_lines(result)
    for line in lines:
        typer.echo(line)


def recall_lines(result: dict[str, Any]) -> list[str]:
    """``siftwise report``'s text. The all-runs recall never appears without the selective
    one: full-suite fallbacks catch everything and flatter it."""
    every, selective = result["all"], result["selective"]
    lines = [
        f"Recall {_recall(every)} over {_plural(every['runs'], 'shadow run')}; "
        f"selective runs only: {_recall(selective)} over {selective['runs']}",
    ]
    if every["flaky_excluded"]:
        lines.append(f"Flaky failures not counted: {every['flaky_excluded']} (all runs), "
                     f"{selective['flaky_excluded']} (selective)")  # fmt: skip
    if every["missed"]:
        lines.append(f"Missed by verdict: {_verdicts(every['missed_by_verdict'])} (all runs); "
                     f"{_verdicts(selective['missed_by_verdict'])} (selective)")  # fmt: skip
        lines.append(f"Missed by cause: {_causes(every['missed_by_cause'])} (all runs); "
                     f"{_causes(selective['missed_by_cause'])} (selective)")  # fmt: skip
    linked = result.get("linked_not_selected") or 0
    if linked:
        lines.append(f"Warning: {_plural(linked, 'miss')} linked to the change but not "
                     "selected: the selector may not have known the test at select time. "
                     "This may be a selector bug.")  # fmt: skip
    time = result["time"]
    if time["saved"] is not None:
        lines.append(f"Time saved (measured on the shadow runs): {time['saved']:.1%} of "
                     f"{_minutes(time['total_ms'])}")  # fmt: skip
    lines.append(f"Full-suite shadow runs (siftwise would have run everything): "
                 f"{result['full_runs']}")  # fmt: skip
    not_counted = {reason: n for reason, n in result["not_counted"].items() if n}
    if not_counted or result["in_progress"]:
        parts = [f"{n} {reason.replace('_', ' ')}" for reason, n in not_counted.items()]
        if result["in_progress"]:
            parts.append(f"{result['in_progress']} in progress")
        lines.append(f"Not counted: {', '.join(parts)}")
    if result["misses"]:
        lines.append("")
        lines.append("Misses, newest first:")
        for miss in result["misses"]:
            variants = ", ".join(v or "-" for v in miss["variants"])
            lines.append(f"  {miss['at'][:10]}  {miss['verdict']:<14} {miss['test_id']}  "
                         f"(run {miss['ci_run_id']} attempt {miss['run_attempt']}, "
                         f"{miss['commit_sha'][:7]}, {variants})")  # fmt: skip
            cause = CAUSE_LABELS.get(miss.get("cause") or "unclassified", miss.get("cause"))
            detail = f": {miss['cause_detail']}" if miss.get("cause_detail") else ""
            lines.append(f"      {cause}{detail}")
        if result["misses_truncated"]:
            lines.append("  ... more not shown (--json has the first 100)")
    if result.get("suggestions"):
        lines += ["", "Suggested .siftwise.toml rules (review before adding; siftwise never "
                  "applies them):"]  # fmt: skip
        for suggestion in result["suggestions"]:
            tests = ", ".join(suggestion["test_ids"])
            lines += ["", f"# would have caught {_plural(suggestion['misses'], 'miss')}: "
                      f"{tests}", *suggestion["rule"].splitlines()]  # fmt: skip
    return lines


def weekly_lines(result: dict[str, Any]) -> list[str]:
    """``siftwise report --by week``: the overall line, then one row per week, oldest first.
    Each row has the selective-only recall next to the all-runs one."""
    lines = recall_lines(result)[:1]
    header = (
        f"{'Week of':<10}  {'Runs':>4}  {'Recall (all)':<18}  {'Selective':>9}  "
        f"{'Recall (selective)':<18}  {'Caught':>6}  {'Missed':>6}  {'Time saved':>10}"
    )
    lines += ["", header]
    for period in result.get("periods") or []:
        every, selective = period["all"], period["selective"]
        saved = period["time"]["saved"]
        lines.append(
            f"{period['start'][:10]:<10}  {period['shadow_runs']:>4}  {_recall(every):<18}  "
            f"{period['selective_runs']:>9}  {_recall(selective):<18}  {every['caught']:>6}  "
            f"{every['missed']:>6}  {('-' if saved is None else f'{saved:.0%}'):>10}"
        )
    return lines


CAUSE_LABELS = {
    "new_test": "new test",
    "flaky_history": "flaky history",
    "config_gap": "config gap",
    "no_link": "no link",
    "unknown": "unknown cause",
    "unclassified": "unclassified (compared before causes existed)",
}


def _causes(counts: dict[str, int]) -> str:
    parts = [f"{n} {CAUSE_LABELS.get(cause, cause)}" for cause, n in counts.items() if n]
    return ", ".join(parts) or "none"


def _recall(stats: dict[str, Any]) -> str:
    counted = stats["caught"] + stats["missed"]
    if not counted:
        return "n/a (no failures)"
    return f"{stats['recall']:.1%} ({stats['caught']} of {counted})"


def _verdicts(counts: dict[str, int]) -> str:
    words = {"pre_existing": "pre-existing"}
    return ", ".join(
        f"{n} {words.get(verdict, verdict.replace('_', ' '))}" for verdict, n in counts.items()
    )


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _minutes(ms: float) -> str:
    minutes = ms / 60_000
    return f"{minutes:.1f} min" if minutes < 120 else f"{minutes / 60:.1f} h"


@app.command("mcp")
def mcp_command() -> None:
    """Serve siftwise's tools to coding agents over MCP (stdio): select_tests, triage_failure,
    explain. Reads SIFTWISE_SERVER and SIFTWISE_API_TOKEN (optional: SIFTWISE_REPO,
    SIFTWISE_BASE_REF). Needs the mcp extra; exit 2 without it."""
    try:
        from siftwise.mcp.server import main as serve
    except ImportError as exc:
        if not (exc.name or "").startswith("mcp"):
            raise
        _err(f"error: `siftwise mcp` needs the mcp extra: {extras.install_hint('mcp')}")
        raise typer.Exit(EXIT_USAGE) from exc
    serve()


# --- reusable steps (also used by siftwise.cli.gha) ----------------------------------------


@dataclass(frozen=True)
class LocalChange:
    """BASE...HEAD in the current checkout, as /select and /triage describe a change."""

    # changed_files, changed_files_known, deleted_files, depends, generated, go_module, affected_*
    fields: dict[str, Any]
    config: RepoConfig
    # Every import graph that applies was built: none failed or was turned off.
    graph_complete: bool

    def graph(self) -> dict[str, Any] | None:
        """``runs.affected`` (schemas.ChangeGraph) to record, or None if the change's links are
        unknown: the diff is, or a graph failed or was skipped."""
        if not (self.fields["changed_files_known"] and self.graph_complete):
            return None
        return {
            "affected_packages": self.fields.get("affected_packages", {}),
            "affected_files": self.fields.get("affected_files", {}),
            "depends": self.fields["depends"],
            "go_module": self.fields.get("go_module"),
            "generated": self.fields["generated"],
        }


def local_change(
    base: str | None,
    head: str,
    *,
    go_list: bool = True,
    py_imports: bool = True,
    working_tree: bool = False,
    cwd: Path | None = None,
) -> LocalChange:
    """The change BASE...HEAD with its import graphs and .siftwise.toml. ``base`` None (or a
    failing git diff) means the changed files are unknown. ``working_tree``: the change from
    the merge base of BASE and HEAD to the working tree instead, so uncommitted (staged,
    unstaged and untracked) changes count too; ``head`` is then unused. ``cwd``: the
    checkout (default: the current directory).

    Raises RepoConfigError for an invalid .siftwise.toml.
    """
    root = git_toplevel(cwd) or cwd or Path.cwd()
    config = local_config(root)

    if not base:
        diff = None
    else:
        diff = git_worktree_diff(base, root) if working_tree else git_diff(base, head, cwd=root)
    changed = diff[0] if diff else None
    fields: dict[str, Any] = {
        "changed_files": changed or [],
        "changed_files_known": changed is not None,
        "depends": [{"tests": r.tests, "on": list(r.on)} for r in config.depends],
        # Reports and caches a test run leaves behind (untracked or not) never count as
        # changed source: the server ignores the built-in list plus these.
        "generated": list(config.generated),
    }
    complete = True
    if diff and diff[1]:
        fields["deleted_files"] = diff[1]
    go_module = read_go_module(root)
    if go_module:
        fields["go_module"] = go_module
        if changed:
            packages = go_dependents(root, changed) if go_list else None
            complete = packages is not None
            if go_list:
                fields["affected_packages"] = packages or {}
    if changed and any(p.endswith(".py") for p in changed):
        files = python_dependents(root, changed) if py_imports else None
        complete = complete and files is not None
        if py_imports:
            fields["affected_files"] = files or {}
    return LocalChange(fields, config, complete)


def local_config(root: Path | None = None) -> RepoConfig:
    """The checkout's .siftwise.toml (at the git top level), or an empty config if there is
    none. Raises RepoConfigError for an invalid file."""
    path = (root or git_toplevel() or Path.cwd()) / FILE_NAME
    return load_repo_config(path) if path.is_file() else RepoConfig()


@dataclass(frozen=True)
class KnownFlaky:
    globs: tuple[str, ...]  # what triage applies
    changed: bool  # the checkout's list differs from it (applies after merge)


def known_flaky(
    config: RepoConfig,
    base: str | None,
    source: str = KnownFlakySource.BASE,
    cwd: Path | None = None,
) -> KnownFlaky:
    """The ``known_flaky`` globs triage applies. From BASE's .siftwise.toml by default, so a
    change (a PR, an agent's edit) can't mark its own failures flaky. Without a base the diff
    is unknown and triage never calls a listed test flaky anyway, so the checkout's list is
    used. A missing or invalid file at BASE has no list."""
    head = config.known_flaky
    if source == KnownFlakySource.HEAD or not base:
        return KnownFlaky(head, False)
    content = git_output("show", f"{base}:{FILE_NAME}", cwd=cwd)
    globs: tuple[str, ...] = ()
    if content is not None:
        try:
            globs = loads_repo_config(content.decode("utf-8", "replace"),
                                      f"{base}:{FILE_NAME}").known_flaky  # fmt: skip
        except RepoConfigError as exc:
            _err(f"siftwise: ignoring known_flaky at {base}: {exc}")
    return KnownFlaky(globs, sorted(set(head)) != sorted(set(globs)))


def selection_request(
    repo: str,
    base: str | None,
    head: str,
    *,
    go_list: bool = True,
    py_imports: bool = True,
    junit_dir: str | None = None,
) -> dict[str, Any]:
    """The ``POST /select`` body for BASE...HEAD in the current checkout. ``base`` None (or a
    failing git diff) means the changed files are unknown, so the server picks the full suite.

    Raises RepoConfigError for an invalid .siftwise.toml.
    """
    change = local_change(base, head, go_list=go_list, py_imports=py_imports)
    body: dict[str, Any] = {
        "repo": repo,
        **change.fields,
        "always_run": list(change.config.always_run),
    }
    if junit_dir:
        body["junit_dir"] = junit_dir
    return body


def recorded_change(
    base: str | None, head: str, *, go_list: bool = True, py_imports: bool = True
) -> dict[str, Any]:
    """``changed_files``, ``changed_files_known`` and ``affected`` for ``POST /runs``.

    An invalid .siftwise.toml never blocks an upload: the run is recorded without links.
    """
    if not base:
        return {"changed_files": [], "changed_files_known": False, "affected": None}
    try:
        change = local_change(base, head, go_list=go_list, py_imports=py_imports)
    except RepoConfigError as exc:
        _err(f"warning: {exc}; recording the run without its change links")
        diff = git_diff(base, head)
        return {
            "changed_files": diff[0] if diff else [],
            "changed_files_known": diff is not None,
            "affected": None,
        }
    return {
        "changed_files": change.fields["changed_files"],
        "changed_files_known": change.fields["changed_files_known"],
        "affected": change.graph(),
    }


def junit_failures(paths: Sequence[Path]) -> list[dict[str, Any]]:
    """Failing tests in JUnit reports: tests whose final attempt (the last one reported, files
    in order) failed or errored, with that attempt's message and file.

    Raises JUnitParseError or OSError.
    """
    final: dict[str, Any] = {}
    for path in paths:
        for result in parse_junit(path.read_bytes()):
            final[result.test_id] = result
    return [
        {"test_id": r.test_id, "message": r.message, "file_path": r.file_path}
        for r in final.values()
        if r.status in (Status.FAILED, Status.ERROR)
    ]


def post_json(
    server: str, token: str, path: str, body: dict[str, Any] | None, timeout: float = 60
) -> dict[str, Any]:
    """POST to the siftwise API. Raises SiftwiseError on any transport or HTTP error."""
    try:
        response = httpx.post(
            f"{server.rstrip('/')}{path}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise SiftwiseError(f"could not reach siftwise at {server}: {exc}") from exc
    return _ok(response)


def get_json(
    server: str, token: str, path: str, params: dict[str, Any] | None = None, timeout: float = 60
) -> dict[str, Any]:
    """GET from the siftwise API. Raises SiftwiseError on any transport or HTTP error."""
    try:
        response = httpx.get(
            f"{server.rstrip('/')}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise SiftwiseError(f"could not reach siftwise at {server}: {exc}") from exc
    return _ok(response)


def upload_run(
    server: str,
    token: str,
    junit: list[Path],
    metadata: dict[str, Any],
    *,
    defer_rollup: bool = False,
    timeout: float = 300,
) -> dict[str, Any]:
    """``POST /runs`` with these JUnit files. Raises SiftwiseError."""
    try:
        response = httpx.post(
            f"{server.rstrip('/')}/runs",
            params={"defer_rollup": "true"} if defer_rollup else None,
            files=[("files", (path.name, path.read_bytes(), "application/xml")) for path in junit],
            data={"metadata": json.dumps(metadata)},
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise SiftwiseError(f"could not reach siftwise at {server}: {exc}") from exc
    return _ok(response)


def rollup_repo(server: str, token: str, repo: str, timeout: float = 1800) -> dict[str, Any]:
    """``POST /repos/{repo}/rollup``. Raises SiftwiseError."""
    return post_json(server, token, f"/repos/{repo}/rollup", None, timeout=timeout)


def _ok(response: httpx.Response) -> dict[str, Any]:
    if not response.is_success:
        raise SiftwiseError(
            f"siftwise returned {response.status_code}: {_detail(response)}", response.status_code
        )
    result: dict[str, Any] = response.json()
    return result


def _token() -> str:
    token = os.environ.get("SIFTWISE_API_TOKEN")
    if not token:
        _err("error: set SIFTWISE_API_TOKEN")
        raise typer.Exit(EXIT_USAGE)
    return token


def git_rev_parse(ref: str) -> str:
    """The full SHA of ``ref`` in the current checkout."""
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        _err(f"error: git rev-parse {ref} failed")
        raise typer.Exit(EXIT_USAGE)
    return proc.stdout.decode().strip()


def git_toplevel(cwd: Path | None = None) -> Path | None:
    """The checkout's root, or None outside a git repository (``cwd``: default the current
    directory)."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.decode(errors="replace").strip())


def go_dependents(root: Path, changed: list[str]) -> dict[str, list[str]] | None:
    """Packages whose tests import a changed package; None (fallbacks apply) if go list fails."""
    packages = run_go_list(root)
    if packages is None:
        _err("warning: `go list -deps -test -json ./...` failed; not selecting dependents")
        return None
    affected = build_graph(packages, root).affected(changed)
    if affected:
        _err(f"siftwise: go list: {len(affected)} package(s) depend on changed Go code")
    return affected


def python_dependents(root: Path, changed: list[str]) -> dict[str, list[str]] | None:
    """Test files that import a changed module; None (fallbacks apply) if the graph fails."""
    try:
        graph = build_py_graph(root)
    except (OSError, ValueError, RecursionError) as exc:
        _err(f"warning: could not build the Python import graph ({exc}); not selecting importers")
        return None
    affected = graph.affected(changed)
    if affected:
        _err(f"siftwise: python imports: {len(affected)} test file(s) import changed modules")
    return affected


def git_output(*args: str, cwd: Path | None = None, timeout: float = 30) -> bytes | None:
    """A git command's stdout, or None if it fails or git can't run."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def git_worktree_diff(base: str, root: Path) -> tuple[list[str], list[str]] | None:
    """``(changed, deleted)`` from the merge base of ``base`` and HEAD to the working tree:
    committed, staged, unstaged and untracked changes. None if git fails."""
    merge_base = git_output("merge-base", base, "HEAD", cwd=root)
    if merge_base is None:
        _err(f"warning: no merge base of {base} and HEAD; asking for the full suite")
        return None
    diff = git_output(
        "diff", "--name-status", "-z", "-M", merge_base.decode().strip(), cwd=root, timeout=120
    )
    untracked = git_output(
        "ls-files", "--others", "--exclude-standard", "--full-name", "-z", cwd=root, timeout=120
    )
    if diff is None or untracked is None:
        _err("warning: git diff of the working tree failed; asking for the full suite")
        return None
    added = [path for path in untracked.decode("utf-8", errors="replace").split("\0") if path]
    return list(dict.fromkeys([*parse_name_status(diff), *added])), parse_deleted(diff)


def git_is_dirty(cwd: Path | None = None) -> bool | None:
    """Whether the working tree differs from HEAD (untracked files included); None if git
    fails."""
    status = git_output("status", "--porcelain", "-z", "--untracked-files=normal", cwd=cwd)
    return None if status is None else bool(status.strip(b"\0"))


def git_head(cwd: Path | None = None) -> str | None:
    """HEAD's full SHA, or None."""
    out = git_output("rev-parse", "--verify", "HEAD^{commit}", cwd=cwd)
    return out.decode().strip() if out else None


_REMOTE_REPO = re.compile(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/*$")


def origin_repo(cwd: Path | None = None) -> str | None:
    """``owner/name`` from the ``origin`` remote's URL (https or ssh), or None."""
    out = git_output("remote", "get-url", "origin", cwd=cwd)
    match = _REMOTE_REPO.search(out.decode().strip()) if out else None
    return match.group(1) if match else None


def default_base(cwd: Path | None = None) -> str:
    """The branch ``origin/HEAD`` points to (e.g. ``origin/main``), else ``main``."""
    out = git_output("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", cwd=cwd)
    return out.decode().strip() if out and out.strip() else "main"


def git_diff(base: str, head: str, cwd: Path | None = None) -> tuple[list[str], list[str]] | None:
    """``(changed, deleted)`` files in ``base...head``, or None if git fails.

    Renames give both paths in ``changed``, and the old path in ``deleted``.
    """
    args = ["git", "diff", "--name-status", "-z", "-M", f"{base}...{head}"]
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _err(f"warning: could not run git diff ({exc}); asking for the full suite")
        return None
    if proc.returncode != 0:
        message = proc.stderr.decode(errors="replace").strip().splitlines()
        _err(
            f"warning: git diff {base}...{head} failed ({message[0] if message else 'no output'});"
            " asking for the full suite"
        )
        return None
    return parse_name_status(proc.stdout), parse_deleted(proc.stdout)


def _name_status_entries(output: bytes) -> list[tuple[str, list[str]]]:
    """``(status letter, paths)`` from ``git diff --name-status -z``."""
    fields = output.decode("utf-8", errors="replace").split("\0")
    entries = []
    i = 0
    while i < len(fields) and fields[i]:
        status = fields[i]
        count = 2 if status[0] in "RC" else 1
        entries.append((status[0], fields[i + 1 : i + 1 + count]))
        i += 1 + count
    return entries


def parse_name_status(output: bytes) -> list[str]:
    """Paths from ``git diff --name-status -z``, de-duplicated in order.

    Renames contribute both the old and the new path; copies only the new one (the source
    is unchanged).
    """
    paths: list[str] = []
    for status, entry in _name_status_entries(output):
        paths.extend(entry[1:] if status == "C" else entry)
    return list(dict.fromkeys(paths))


def parse_deleted(output: bytes) -> list[str]:
    """Paths that no longer exist at head: deleted files and the old side of renames."""
    deleted = [entry[0] for status, entry in _name_status_entries(output) if status in "DR"]
    return list(dict.fromkeys(deleted))


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json()["detail"])
    except (ValueError, KeyError, TypeError):
        return response.text[:300]


def _err(message: str) -> None:
    typer.echo(message, err=True)
