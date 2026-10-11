"""`siftwise gha select|record|rollup`: the GitHub Actions glue behind actions/*/action.yml.

Each command reads the run's context from the environment GitHub provides (``GITHUB_*`` and the
event payload), calls the siftwise API through the same functions as ``siftwise select`` /
``record`` / ``rollup``, and reports back through ``$GITHUB_OUTPUT``, ``$GITHUB_STEP_SUMMARY``
and workflow-command annotations.

Fail open: siftwise must never block CI. If the server is unreachable, times out, answers with
an error, or no token is available (fork PRs get no secrets), ``select`` outputs ``mode=full``
and ``record``/``rollup`` log a warning; all three exit 0. The one deliberate failure is a
shallow checkout in ``select``: a diff against the PR base is impossible there, and the fix is
a one-line workflow change.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer

from siftwise.cli.repoconfig import RepoConfigError

app = typer.Typer(help="GitHub Actions steps (used by actions/select, record and rollup).")

COMMENT_MARKER = "<!-- siftwise -->"
PULL_REQUEST_EVENTS = frozenset({"pull_request", "pull_request_target"})
ZERO_SHA = "0" * 40
# Directories a recursive JUnit glob never needs to look into.
SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", ".tox", "__pycache__"})
SELECT_TIMEOUT = 30.0
RECORD_TIMEOUT = 120.0

Env = Mapping[str, str]


@dataclass(frozen=True)
class Context:
    """What a workflow run is testing, from the GitHub environment and event payload."""

    event_name: str
    repo: str
    # The commit the run tests: a PR's head, never the merge commit GitHub checks out.
    head_sha: str
    branch: str
    is_main: bool
    # Diff base (a SHA or ref); None means the changed files are unknown.
    base: str | None
    run_id: str
    run_attempt: int
    pr_number: int | None
    # A PR from a fork: no secrets, and GITHUB_TOKEN is read-only.
    fork: bool
    api_url: str


def load_context(
    env: Env, base_ref: str | None = None, has_commit: Callable[[str], bool] | None = None
) -> Context:
    """Build the context. ``base_ref`` overrides the event's diff base.

    The diff base matches what backfill records for the same CI run: a PR's base SHA (the
    whole PR), a push's ``before`` (every pushed commit), falling back to the first parent
    for a new branch. Other events have no base, so the server runs the full suite.
    """
    has_commit = has_commit or git_has_commit
    event_name = env.get("GITHUB_EVENT_NAME", "")
    event: dict[str, Any] = {}
    if env.get("GITHUB_EVENT_PATH"):
        try:
            event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            event = {}
    pr: dict[str, Any] = event.get("pull_request") or {}
    default_branch = (event.get("repository") or {}).get("default_branch")
    repo = env.get("GITHUB_REPOSITORY", "")

    if event_name in PULL_REQUEST_EVENTS and pr:
        head_sha = (pr.get("head") or {}).get("sha") or env.get("GITHUB_SHA", "")
        branch = env.get("GITHUB_HEAD_REF") or (pr.get("head") or {}).get("ref") or "unknown"
        head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
        fork = head_repo != repo  # also True when the fork was deleted (head.repo is null)
    else:
        head_sha = env.get("GITHUB_SHA", "")
        branch = env.get("GITHUB_REF_NAME") or "unknown"
        fork = False

    base = base_ref or None
    if base is None and event_name == "pull_request":
        base = (pr.get("base") or {}).get("sha")
    elif base is None and event_name == "push":
        before = event.get("before")
        if before and before != ZERO_SHA and has_commit(before):
            base = before
        elif head_sha and has_commit(f"{head_sha}^"):
            base = f"{head_sha}^"  # a new branch: like backfill, diff against the first parent
    # pull_request_target runs with base-branch code and secrets: unsupported (see the README).

    return Context(
        event_name=event_name,
        repo=repo,
        head_sha=head_sha,
        branch=branch,
        # Same rule as backfill: a non-PR run on the default branch.
        is_main=bool(default_branch)
        and branch == default_branch
        and event_name not in PULL_REQUEST_EVENTS,
        base=base,
        run_id=env.get("GITHUB_RUN_ID", ""),
        run_attempt=int(env.get("GITHUB_RUN_ATTEMPT") or 1),
        pr_number=pr.get("number") if event_name == "pull_request" else None,
        fork=fork,
        api_url=env.get("GITHUB_API_URL", "https://api.github.com"),
    )


# --- commands -----------------------------------------------------------------------------


@app.command("select")
def select_step(
    server: Annotated[str, typer.Option(envvar="SIFTWISE_SERVER")],
    token: Annotated[str, typer.Option(envvar="SIFTWISE_API_TOKEN")] = "",
    repo: Annotated[str, typer.Option(envvar="SIFTWISE_REPO")] = "",
    base_ref: Annotated[str, typer.Option(envvar="SIFTWISE_BASE_REF")] = "",
    junit_dir: Annotated[str, typer.Option(envvar="SIFTWISE_JUNIT_DIR")] = "",
    comment: Annotated[bool, typer.Option(envvar="SIFTWISE_COMMENT")] = True,
    github_token: Annotated[str, typer.Option(envvar="SIFTWISE_GITHUB_TOKEN")] = "",
) -> None:
    """Select tests for this run and set the step's outputs."""
    from siftwise.cli import main as cli  # main registers this app: import it lazily

    env = os.environ
    if git_is_shallow():
        _annotate("error", "siftwise needs the full git history to diff against the base: set "
                  "`fetch-depth: 0` on actions/checkout.")  # fmt: skip
        raise typer.Exit(1)
    ctx = load_context(env, base_ref)
    repo = repo or ctx.repo
    if ctx.event_name == "pull_request_target":
        _annotate("warning", "siftwise doesn't support pull_request_target: full suite.")
    junit_dir = junit_dir or str(Path(env.get("RUNNER_TEMP", ".")) / "siftwise-junit")
    Path(junit_dir).mkdir(parents=True, exist_ok=True)
    # For the record step later in the job: where the reports are, and when the job started.
    _append(env, "GITHUB_ENV", {"SIFTWISE_JUNIT_DIR": junit_dir,
                                "SIFTWISE_SELECT_STARTED_AT": _now()})  # fmt: skip

    result: dict[str, Any] | None = None
    if not token:
        problem = "no siftwise token (secrets aren't available to pull requests from forks)"
    else:
        try:
            body = cli.selection_request(repo, ctx.base, ctx.head_sha or "HEAD",
                                         junit_dir=junit_dir)  # fmt: skip
            result = cli.post_json(server, token, "/select", body, timeout=SELECT_TIMEOUT)
            problem = ""
        except cli.SiftwiseError as exc:
            if exc.status == httpx.codes.NOT_FOUND:
                # A repo siftwise has never seen: the normal first run, not an outage.
                result = fail_open_result("")
                result["reason"] = "no test history for this repo yet"
            problem = str(exc)
        except RepoConfigError as exc:
            problem = str(exc)
    if result is None:
        _annotate("warning", f"siftwise: {problem}; running the full suite")
        result = fail_open_result(problem)

    estimated = result.get("estimated_ms") or {}
    _append(env, "GITHUB_OUTPUT", {
        "mode": result["mode"],
        "command": result["command"],
        "selected": str(result["selected_count"]),
        "total": str(result["total_known"]),
        "reason": result["reason"],
        "junit-dir": junit_dir,
        "estimated-ms-total": str(round(estimated.get("total", 0))),
        "estimated-ms-would-run": str(round(estimated.get("would_run", 0))),
    })  # fmt: skip
    table = selection_markdown(result, ctx.head_sha)
    notes = []
    if comment and ctx.pr_number is not None:
        if ctx.fork:
            notes.append("Pull request from a fork: no PR comment (its token is read-only).")
        elif not github_token:
            notes.append("No GitHub token: no PR comment.")
        else:
            notes.extend(upsert_comment(ctx, github_token, table))
    # Leading newline: several steps append to the same summary.
    _append_summary(env, "\n" + table + "".join(f"\n> {note}\n" for note in notes))
    typer.echo(f"siftwise: {result['mode']} ({result['selected_count']} of "
               f"{result['total_known']} known tests): {result['reason']}")  # fmt: skip


@app.command("record")
def record_step(
    server: Annotated[str, typer.Option(envvar="SIFTWISE_SERVER")],
    token: Annotated[str, typer.Option(envvar="SIFTWISE_API_TOKEN")] = "",
    repo: Annotated[str, typer.Option(envvar="SIFTWISE_REPO")] = "",
    base_ref: Annotated[str, typer.Option(envvar="SIFTWISE_BASE_REF")] = "",
    junit: Annotated[str, typer.Option(envvar="SIFTWISE_JUNIT_GLOB")] = "**/junit*.xml",
    junit_dir: Annotated[str, typer.Option(envvar="SIFTWISE_JUNIT_DIR")] = "",
    variant: Annotated[str, typer.Option(envvar="SIFTWISE_VARIANT")] = "",
    defer_rollup: Annotated[str, typer.Option(envvar="SIFTWISE_DEFER_ROLLUP")] = "auto",
    github_token: Annotated[str, typer.Option(envvar="SIFTWISE_GITHUB_TOKEN")] = "",
) -> None:
    """Upload this run's JUnit reports. Never fails the job."""
    from siftwise.cli import main as cli

    env = os.environ
    ctx = load_context(env, base_ref)
    repo = repo or ctx.repo
    if not token:
        _annotate("warning", "siftwise: no token (fork pull request?); results not recorded")
        return
    files = find_junit(junit, junit_dir)
    if not files:
        _annotate("warning", f"siftwise: no JUnit files match {junit!r}"
                  f"{f' or {junit_dir}/*.xml' if junit_dir else ''}; nothing recorded")  # fmt: skip
        return
    # Same diff as the select step, so a recorded run's changed files are what was selected on.
    diff = cli.git_diff(ctx.base, ctx.head_sha or "HEAD") if ctx.base else None
    metadata = {
        "repo": repo,
        "commit_sha": ctx.head_sha,
        "branch": ctx.branch,
        "is_main": ctx.is_main,
        "ci_run_id": ctx.run_id or None,
        "run_attempt": ctx.run_attempt,
        "variant": variant or None,
        "started_at": run_started_at(ctx, github_token, env),
        "changed_files": diff[0] if diff else [],
        "changed_files_known": diff is not None,
    }
    # A matrix leg defers the rollup to one `actions/rollup` job: the per-upload rollup costs
    # about as much as the whole-repo one on a big repo (~75 s on rdflib), once per leg.
    defer = bool(variant) if defer_rollup == "auto" else defer_rollup == "true"
    try:
        result = cli.upload_run(server, token, files, metadata, defer_rollup=defer,
                                timeout=RECORD_TIMEOUT)  # fmt: skip
    except cli.SiftwiseError as exc:
        _annotate("warning", f"siftwise: results not recorded: {exc}")
        return
    state = "stored" if result.get("created") else "already stored"
    line = (f"siftwise: run {result.get('run_id')} {state}: {len(files)} file(s), "
            f"{result.get('counts', {}).get('total', '?')} results"
            f"{' (stats update deferred to the rollup job)' if defer else ''}")  # fmt: skip
    typer.echo(line)
    _append_summary(env, f"\n{line}\n")


@app.command("rollup")
def rollup_step(
    server: Annotated[str, typer.Option(envvar="SIFTWISE_SERVER")],
    token: Annotated[str, typer.Option(envvar="SIFTWISE_API_TOKEN")] = "",
    repo: Annotated[str, typer.Option(envvar="SIFTWISE_REPO")] = "",
) -> None:
    """Recompute the repo's test stats once, after every matrix leg recorded. Never fails."""
    from siftwise.cli import main as cli

    repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
    if not token:
        _annotate("warning", "siftwise: no token (fork pull request?); stats not updated")
        return
    try:
        result = cli.rollup_repo(server, token, repo)
    except cli.SiftwiseError as exc:
        _annotate("warning", f"siftwise: stats not updated: {exc}")
        return
    line = f"siftwise: {result.get('tests_updated')} test stats updated in {result.get('seconds')}s"
    typer.echo(line)
    _append_summary(os.environ, f"\n{line}\n")


# --- pieces ---------------------------------------------------------------------------------


def fail_open_result(problem: str) -> dict[str, Any]:
    """A /select response that means "run your full suite": no command, since only the
    workflow knows its full-suite command (flags, JUnit output) when siftwise is down."""
    return {
        "mode": "full",
        "command": "",
        "selected_count": 0,
        "total_known": 0,
        "reason": f"siftwise unavailable: {problem}",
        "estimated_ms": {"total": 0.0, "would_run": 0.0},
    }


def selection_markdown(result: dict[str, Any], head_sha: str) -> str:
    """The selection as a small Markdown table (step summary and PR comment)."""
    mode, total_known = result["mode"], result["total_known"]
    if mode == "full":
        tests = f"all ({total_known:,} known)" if total_known else "all"
    elif result["command"]:
        tests = f"{result['selected_count']:,} of {total_known:,} known tests"
    else:
        tests = f"none of {total_known:,} known tests are affected"
    rows = [("Mode", f"**{mode}**"), ("Tests", tests)]
    estimated = result.get("estimated_ms") or {}
    total, would_run = estimated.get("total") or 0.0, estimated.get("would_run") or 0.0
    if total > 0:
        saved = total - would_run
        rows.append(("Estimated test time", f"{_duration(would_run)} of {_duration(total)} "
                     f"({100 * saved / total:.0f}% saved)"))  # fmt: skip
    if mode == "full":
        rows.append(("Why the full suite", _cell(result["reason"])))
    table = "\n".join(f"| {k} | {v} |" for k, v in rows)
    commit = f" for `{head_sha[:7]}`" if head_sha else ""
    return f"### siftwise test selection{commit}\n\n| | |\n|---|---|\n{table}\n"


def upsert_comment(ctx: Context, github_token: str, body: str) -> list[str]:
    """Create or update this PR's one siftwise comment. Returns notes for the summary; never
    raises (a missing `pull-requests: write` permission is only a warning)."""
    text = f"{COMMENT_MARKER}\n{body}\n<sub>Updated on each push.</sub>\n"
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    issue = f"{ctx.api_url}/repos/{ctx.repo}/issues/{ctx.pr_number}"
    try:
        with httpx.Client(headers=headers, timeout=30) as gh:
            existing = None
            url: str | None = f"{issue}/comments?per_page=100"
            while url and existing is None:  # paginate: busy PRs have more than 100 comments
                page = gh.get(url)
                page.raise_for_status()
                existing = next((c for c in page.json() if COMMENT_MARKER in c.get("body", "")),
                                None)  # fmt: skip
                url = page.links.get("next", {}).get("url")
            if existing is None:
                gh.post(f"{issue}/comments", json={"body": text}).raise_for_status()
            else:
                gh.patch(existing["url"], json={"body": text}).raise_for_status()
    except httpx.HTTPStatusError as exc:
        hint = " (does the job have `pull-requests: write`?)" if exc.response.status_code in (
            403, 404) else ""  # fmt: skip
        _annotate("warning", f"siftwise: PR comment failed: {exc.response.status_code}{hint}")
        return [f"PR comment failed: HTTP {exc.response.status_code}{hint}."]
    except httpx.HTTPError as exc:
        _annotate("warning", f"siftwise: PR comment failed: {exc}")
        return [f"PR comment failed: {exc}."]
    return []


def run_started_at(ctx: Context, github_token: str, env: Env) -> str:
    """When the CI run started: the Actions API's ``run_started_at``, as backfill records it
    (needs `actions: read`), else when the select step ran, else now."""
    if github_token and ctx.run_id:
        try:
            response = httpx.get(
                f"{ctx.api_url}/repos/{ctx.repo}/actions/runs/{ctx.run_id}",
                headers={"Authorization": f"Bearer {github_token}",
                         "Accept": "application/vnd.github+json"},
                timeout=15,
            )  # fmt: skip
            if response.is_success and response.json().get("run_started_at"):
                return str(response.json()["run_started_at"])
        except (httpx.HTTPError, ValueError):
            pass
    return env.get("SIFTWISE_SELECT_STARTED_AT") or _now()


def find_junit(patterns: str, junit_dir: str) -> list[Path]:
    """Files matching any of the (whitespace-separated) globs, plus ``junit_dir/*.xml``."""
    found: set[Path] = set()
    for pattern in patterns.split():
        for name in glob.glob(pattern, recursive=True):
            path = Path(name)
            if path.is_file() and not SKIP_DIRS.intersection(path.parts):
                found.add(path)
    if junit_dir and Path(junit_dir).is_dir():
        found.update(p for p in Path(junit_dir).glob("*.xml") if p.is_file())
    return sorted(found)


def git_is_shallow() -> bool:
    proc = subprocess.run(["git", "rev-parse", "--is-shallow-repository"], capture_output=True,
                          timeout=30, check=False)  # fmt: skip
    return proc.returncode == 0 and proc.stdout.decode().strip() == "true"


def git_has_commit(ref: str) -> bool:
    proc = subprocess.run(["git", "cat-file", "-e", f"{ref}^{{commit}}"], capture_output=True,
                          timeout=30, check=False)  # fmt: skip
    return proc.returncode == 0


def _append(env: Env, file_var: str, values: Mapping[str, str]) -> None:
    """Append ``name=value`` pairs to a GitHub file ($GITHUB_OUTPUT, $GITHUB_ENV). The heredoc
    form with a random delimiter keeps a value from injecting more variables."""
    path = env.get(file_var)
    if not path:
        return
    with open(path, "a", encoding="utf-8") as out:
        for name, value in values.items():
            delimiter = f"siftwise_{uuid.uuid4().hex}"
            out.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")


def _append_summary(env: Env, markdown: str) -> None:
    path = env.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as out:
            out.write(markdown)


def _annotate(level: str, message: str) -> None:
    # Workflow commands are line-based: escape what would end or alter the command.
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    typer.echo(f"::{level}::{escaped}")


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _duration(ms: float) -> str:
    seconds = round(ms / 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
