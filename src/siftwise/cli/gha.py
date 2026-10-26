"""`siftwise gha select|record|rollup`: the GitHub Actions glue behind actions/*/action.yml.

Each command reads the run's context from the environment GitHub provides (``GITHUB_*`` and the
event payload), calls the siftwise API through the same functions as ``siftwise select`` /
``record`` / ``rollup``, and reports back through ``$GITHUB_OUTPUT``, ``$GITHUB_STEP_SUMMARY``
and workflow-command annotations.

Fail open: siftwise must never block CI. If the server is unreachable, times out, answers with
an error, or no token is available (fork PRs get no secrets), ``select`` outputs ``mode=full``
and ``record``/``rollup`` log a warning; all three exit 0. The deliberate failures: a shallow
checkout in ``select`` (a diff against the PR base is impossible there, and the fix is a
one-line workflow change), and ``fail-on``, which the user opts into: a failure with one of its
verdicts fails the step, and so do unavailable verdicts (fail closed, since the user made
siftwise the gate), except without a siftwise token (fork PRs): a warning, not gated.

Failure verdicts come after the run's last upload: from ``rollup`` after a matrix, or from
``record`` in a single job. The server triages what the uploads stored (``POST
/repos/{repo}/triage``), so that step needs no checkout. The sticky PR comment has two
sections, each written by its own step: the selection (``select``) and the failures.

Shadow mode (``shadow`` on ``select``: off, always, or a percentage of CI runs, sampled by a
hash of the run ID so every leg agrees): the full suite runs (``mode=full``, decided before any
server call), while the server stores what siftwise would have selected. Where triage runs,
``POST /repos/{repo}/shadow`` then counts the failures it would have caught or missed: live
recall. Shadow mode never fails or shortens a run; problems are warnings.

Flaky handling in ``record``: ``known_flaky`` from ``.siftwise.toml`` is recorded with the run
(a PR's from its BASE commit by default, so a PR can't mark its own failures flaky and get past
``fail-on``). ``rerun-unknown: N`` re-runs this leg's failures with an ``unknown`` verdict (with
their own runner: pytest, Go, Jest, Maven or Gradle, see ``cli/reruns.py``; no shell) up to N
times, capped in tests and time, and uploads each round as a re-run
of the run (``rerun = n``); triage then calls a test that passed on a re-run flaky. Fail open:
any problem is a warning, and the verdicts stay what they were.

The select/record/rollup flows (``run_select``, ``run_record``, ``run_rollup``) are shared with
GitLab CI (``cli/gitlab.py``): how a step reports back (outputs, logs, summaries, the comment,
Markdown escaping) goes through a ``Provider``, GitHub's by default.
"""

from __future__ import annotations

import contextvars
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer

from siftwise.cli import reruns
from siftwise.cli.repoconfig import RepoConfig, RepoConfigError
from siftwise.core.junit import JUnitParseError, Status, parse_junit

app = typer.Typer(help="GitHub Actions steps (used by actions/select, record and rollup).")

COMMENT_MARKER = "<!-- siftwise -->"
# The comment's sections. A comment with only COMMENT_MARKER (the format before sections) is
# all selection, and is upgraded in place.
SELECTION_MARKER = "<!-- siftwise:selection -->"
FAILURES_MARKER = re.compile(r"<!-- siftwise:failures sha=([0-9a-fA-F]*) attempt=(\d+) -->")
COMMENT_FOOTER = "<sub>Updated on each push.</sub>"
PULL_REQUEST_EVENTS = frozenset({"pull_request", "pull_request_target"})
MERGE_REQUEST_EVENT = "merge_request_event"  # GitLab's CI_PIPELINE_SOURCE for MR pipelines
ZERO_SHA = "0" * 40
# Directories a recursive JUnit glob never needs to look into.
SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", ".tox", "__pycache__"})
SELECT_TIMEOUT = 30.0
RECORD_TIMEOUT = 120.0
TRIAGE_TIMEOUT = 120.0

# Failure verdicts, worst first (the server's order). The last two are collapsed in the comment.
VERDICTS = ("new_regression", "unknown", "flaky", "pre_existing")
COLLAPSED = frozenset({"flaky", "pre_existing"})
CONFIDENCES = ("high", "medium", "low")
_LABELS = {
    "new_regression": ("new regression", "new regressions"),
    "unknown": ("unknown", "unknown"),
    "flaky": ("flaky", "flaky"),
    "pre_existing": ("pre-existing", "pre-existing"),
}
TRIAGE_LIMIT = 500  # results the server returns: the job summary's full list
COMMENT_ROWS = 20  # rows per comment table (the visible one and the collapsed one)
REASON_CHARS = 120
NOTHING_RECORDED = "nothing recorded for this attempt"
NO_TOKEN = "no siftwise token (secrets aren't available to pull requests from forks)"
SHADOW_REASON = "shadow run: the full suite runs, to check what siftwise would have caught"
KNOWN_FLAKY_AFTER_MERGE = "known_flaky changes in this PR apply after merge"
# Re-runs (record's rerun-* inputs).
RERUN_VERDICTS = frozenset({"unknown", "new_regression"})
DEFAULT_RERUN_VERDICTS = frozenset({"unknown"})
MAX_RERUN_ROUNDS = 3
DEFAULT_RERUN_MAX_TESTS = 20
MAX_RERUN_TESTS = 100
DEFAULT_RERUN_MINUTES = 10
MAX_RERUN_MINUTES = 60
_NOT_COUNTED = {
    "commit_mismatch": "the recorded results are for another commit",
    "no_upload": "no results were recorded for its variant",
    "compare_error": "the server failed to compare it",
    "pending": "it was never compared",
}

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
    server_url: str = "https://github.com"
    # GitLab: the merge request's (target) project, for the API, and the pipeline's page.
    project_id: str = ""
    pipeline_url: str = ""

    @property
    def run_url(self) -> str:
        """This run attempt's page, where the job summaries are."""
        if self.pipeline_url:
            return self.pipeline_url
        run = f"{self.server_url}/{self.repo}/actions/runs/{self.run_id}"
        return f"{run}/attempts/{self.run_attempt}"

    @property
    def change_request(self) -> bool:
        """A pull request (GitHub) or merge request (GitLab) run."""
        return self.event_name in PULL_REQUEST_EVENTS or self.event_name == MERGE_REQUEST_EVENT


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
        server_url=env.get("GITHUB_SERVER_URL", "https://github.com"),
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
    shadow: Annotated[str, typer.Option(envvar="SIFTWISE_SHADOW")] = "off",
    variant: Annotated[str, typer.Option(envvar="SIFTWISE_VARIANT")] = "",
) -> None:
    """Select tests for this run and set the step's outputs. In a shadow run the outputs say
    run the full suite, and the server stores the selection to measure recall."""
    if git_is_shallow():
        _annotate("error", "siftwise needs the full git history to diff against the base: "
                  f"{_ci().shallow_fix}")  # fmt: skip
        raise typer.Exit(1)
    ctx = load_context(os.environ, base_ref)
    if ctx.event_name == "pull_request_target":
        _annotate("warning", "siftwise doesn't support pull_request_target: full suite.")
    run_select(ctx, server=server, token=token, repo=repo, junit_dir=junit_dir,
               comment=comment, scm_token=github_token, shadow=shadow, variant=variant)  # fmt: skip


def run_select(
    ctx: Context,
    *,
    server: str,
    token: str,
    repo: str,
    junit_dir: str,
    comment: bool,
    scm_token: str,
    shadow: str,
    variant: str,
) -> None:
    """The select step, for any CI provider (``scm_token``: the one that writes the comment)."""
    from siftwise.cli import main as cli  # main registers this app: import it lazily

    env = os.environ
    ci = _ci()
    repo = repo or ctx.repo
    junit_dir = junit_dir or ci.default_junit_dir(env)
    Path(junit_dir).mkdir(parents=True, exist_ok=True)
    # For the record step later in the job: where the reports are, and when the job started.
    ci.export(env, {"SIFTWISE_JUNIT_DIR": junit_dir, "SIFTWISE_SELECT_STARTED_AT": _now()})
    rate = parse_shadow(shadow)
    if rate is None:
        _annotate("warning", f"siftwise: invalid shadow value {shadow!r} (use off, always or a "
                  "percentage such as 10%); shadow mode is off")  # fmt: skip
        rate = 0.0
    # A shadow run needs somewhere to store the selection: a token, a run ID and the commit.
    in_shadow = bool(token and ctx.run_id and ctx.head_sha) and sampled(repo, ctx.run_id, rate)
    if in_shadow:
        # Decided before any server call can fail: whatever happens next, the full suite runs.
        ci.outputs(env, {"mode": "full", "command": "", "shadow": "true"})

    result: dict[str, Any] | None = None
    if not token:
        problem = ci.no_token
    else:
        try:
            body = cli.selection_request(repo, ctx.base, ctx.head_sha or "HEAD",
                                         junit_dir=junit_dir)  # fmt: skip
            if in_shadow:
                body["shadow"] = {
                    "ci_run_id": ctx.run_id,
                    "run_attempt": ctx.run_attempt,
                    "variant": variant or None,
                    "commit_sha": ctx.head_sha,
                }
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

    would_have: dict[str, Any] | None = None  # in a shadow run: what siftwise would have run
    not_counted = ""
    if in_shadow:
        if problem:
            not_counted = problem
        else:
            would_have = result
            if result.get("shadow_stored") is not True:
                not_counted = f"the selection wasn't stored ({result.get('shadow_reason')})"
        if not_counted:
            _annotate("warning", f"siftwise: shadow run not counted: {not_counted}")
        result = shadow_result(result)

    estimated = result.get("estimated_ms") or {}
    ci.outputs(env, {
        "mode": result["mode"],
        "command": result["command"],
        "selected": str(result["selected_count"]),
        "total": str(result["total_known"]),
        "reason": result["reason"],
        "junit-dir": junit_dir,
        "estimated-ms-total": str(round(estimated.get("total", 0))),
        "estimated-ms-would-run": str(round(estimated.get("would_run", 0))),
        "shadow": "true" if in_shadow else "false",
        "shadow-mode": would_have["mode"] if would_have else "",
        "shadow-selected": str(would_have["selected_count"]) if would_have else "",
        "shadow-total": str(would_have["total_known"]) if would_have else "",
    })  # fmt: skip
    table = selection_markdown(result, ctx.head_sha, shadow=in_shadow, would_have=would_have,
                               not_counted=not_counted)  # fmt: skip
    notes = []
    if comment and ctx.pr_number is not None:
        notes.extend(ci.comment(ctx, scm_token, selection=table))
    # Leading newline: several steps append to the same summary.
    _append_summary(env, "\n" + table + "".join(f"\n> {note}\n" for note in notes))
    ci.echo(f"siftwise: {result['mode']} ({result['selected_count']} of "
            f"{result['total_known']} known tests): {result['reason']}")  # fmt: skip
    if would_have:
        ci.echo(f"siftwise: shadow run: would have run {would_have['selected_count']} of "
                f"{would_have['total_known']} ({would_have['mode']})")  # fmt: skip


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
    triage: Annotated[str, typer.Option(envvar="SIFTWISE_TRIAGE")] = "auto",
    comment: Annotated[bool, typer.Option(envvar="SIFTWISE_COMMENT")] = True,
    fail_on: Annotated[str, typer.Option(envvar="SIFTWISE_FAIL_ON")] = "",
    known_flaky_source: Annotated[str, typer.Option(envvar="SIFTWISE_KNOWN_FLAKY_SOURCE")] = "base",
    rerun_unknown: Annotated[str, typer.Option(envvar="SIFTWISE_RERUN_UNKNOWN")] = "0",
    rerun_verdicts: Annotated[str, typer.Option(envvar="SIFTWISE_RERUN_VERDICTS")] = "unknown",
    rerun_max_tests: Annotated[str, typer.Option(envvar="SIFTWISE_RERUN_MAX_TESTS")] = "20",
    rerun_timeout_minutes: Annotated[
        str, typer.Option(envvar="SIFTWISE_RERUN_TIMEOUT_MINUTES")
    ] = "10",
    rerun_prefix: Annotated[str, typer.Option(envvar="SIFTWISE_RERUN_PREFIX")] = "",
) -> None:
    """Upload this run's JUnit reports, re-run its unknown failures if asked, then (in a
    single job) triage its failures. Never fails the job, unless ``fail_on`` is set."""
    gate = parse_fail_on(fail_on)
    source = parse_known_flaky_source(known_flaky_source)
    rerun = parse_rerun(rerun_unknown, rerun_verdicts, rerun_max_tests, rerun_timeout_minutes,
                        rerun_prefix)  # fmt: skip
    ctx = load_context(os.environ, base_ref)
    run_record(ctx, server=server, token=token, repo=repo, junit=junit, junit_dir=junit_dir,
               variant=variant, defer_rollup=defer_rollup, scm_token=github_token,
               triage=triage, comment=comment, gate=gate, source=source, rerun=rerun)  # fmt: skip


def run_record(
    ctx: Context,
    *,
    server: str,
    token: str,
    repo: str,
    junit: str,
    junit_dir: str,
    variant: str,
    defer_rollup: str,
    scm_token: str,
    triage: str,
    comment: bool,
    gate: frozenset[str],
    source: str,
    rerun: Rerun,
) -> None:
    """The record step, for any CI provider. Raises typer.Exit for ``fail-on``."""
    env = os.environ
    ci = _ci()
    repo = repo or ctx.repo
    # A matrix leg defers the rollup to one `actions/rollup` job: the per-upload rollup costs
    # about as much as the whole-repo one on a big repo (~75 s on rdflib), once per leg.
    defer = bool(variant) if defer_rollup == "auto" else defer_rollup == "true"
    # Triage after the run's last upload: here in a single job, in the rollup job after a
    # matrix (a leg's verdicts would be one of N comment writers racing).
    do_triage = not defer if triage == "auto" else triage == "true"
    if gate and not do_triage:
        _annotate("warning", "siftwise: fail-on has no effect where verdicts aren't computed "
                  f"(a matrix leg): set it on {ci.rollup_step}")  # fmt: skip
    problem, metadata = _record(ctx, server, token, repo, junit, junit_dir, variant, defer,
                                scm_token, source)  # fmt: skip
    if metadata is not None and rerun.rounds:
        report = rerun_failures(ctx, server, token, repo, variant, metadata, defer, rerun,
                                find_junit(junit, junit_dir))  # fmt: skip
        ci.outputs(env, {"rerun-tests": str(len(report.tests)),
                         "rerun-passed": str(len(report.passed))})  # fmt: skip
        line = report.line(rerun)
        if line:
            ci.echo(line)
            _append_summary(env, f"\n{line}\n")
    if do_triage:
        response = None
        if not problem:
            response, problem = fetch_triage(server, token, repo, ctx)
        shadow = fetch_shadow(server, token, repo, ctx)
        _shadow_outputs(env, shadow)
        code = report_failures(ctx, response, problem, comment=comment,
                               github_token=scm_token, fail_on=gate, shadow=shadow)  # fmt: skip
        if code:
            raise typer.Exit(code)


def _record(
    ctx: Context,
    server: str,
    token: str,
    repo: str,
    junit: str,
    junit_dir: str,
    variant: str,
    defer: bool,
    github_token: str,
    known_flaky_source: str = "base",
) -> tuple[str, dict[str, Any] | None]:
    """Upload the reports: ("", the run's metadata) on success, else (why nothing was
    recorded, None)."""
    from siftwise.cli import main as cli

    env = os.environ
    ci = _ci()
    if not token:
        _annotate("warning", f"siftwise: no token ({ci.no_token_hint}); results not recorded")
        return ci.no_token, None
    files = find_junit(junit, junit_dir)
    if not files:
        _annotate("warning", f"siftwise: no JUnit files match {junit!r}"
                  f"{f' or {junit_dir}/*.xml' if junit_dir else ''}; nothing recorded")  # fmt: skip
        return f"{NOTHING_RECORDED} (no JUnit files)", None
    known_flaky, changed = recorded_known_flaky(ctx, known_flaky_source)
    # Same diff as the select step, so a recorded run's changed files are what was selected on,
    # and its import graphs, so triage can later tell if a failure was related to the change.
    metadata = {
        "repo": repo,
        "commit_sha": ctx.head_sha,
        "branch": ctx.branch,
        "is_main": ctx.is_main,
        "ci_run_id": ctx.run_id or None,
        "run_attempt": ctx.run_attempt,
        "variant": variant or None,
        "started_at": ci.started_at(ctx, github_token, env),
        **cli.recorded_change(ctx.base, ctx.head_sha or "HEAD"),
        "known_flaky": known_flaky,
        "known_flaky_changed": changed,
    }
    try:
        result = cli.upload_run(server, token, files, metadata, defer_rollup=defer,
                                timeout=RECORD_TIMEOUT)  # fmt: skip
    except cli.SiftwiseError as exc:
        _annotate("warning", f"siftwise: results not recorded: {exc}")
        return f"{NOTHING_RECORDED} ({exc})", None
    state = "stored" if result.get("created") else "already stored"
    line = (f"siftwise: run {result.get('run_id')} {state}: {len(files)} file(s), "
            f"{result.get('counts', {}).get('total', '?')} results"
            f"{' (stats update deferred to the rollup job)' if defer else ''}")  # fmt: skip
    ci.echo(line)
    _append_summary(env, f"\n{line}\n")
    if changed:
        ci.echo(f"siftwise: {KNOWN_FLAKY_AFTER_MERGE}")
    return "", metadata


@app.command("rollup")
def rollup_step(
    server: Annotated[str, typer.Option(envvar="SIFTWISE_SERVER")],
    token: Annotated[str, typer.Option(envvar="SIFTWISE_API_TOKEN")] = "",
    repo: Annotated[str, typer.Option(envvar="SIFTWISE_REPO")] = "",
    github_token: Annotated[str, typer.Option(envvar="SIFTWISE_GITHUB_TOKEN")] = "",
    triage: Annotated[bool, typer.Option(envvar="SIFTWISE_TRIAGE")] = True,
    comment: Annotated[bool, typer.Option(envvar="SIFTWISE_COMMENT")] = True,
    fail_on: Annotated[str, typer.Option(envvar="SIFTWISE_FAIL_ON")] = "",
) -> None:
    """After every matrix leg recorded: triage the run's failures (all variants, one PR
    comment section), then recompute the repo's test stats once. Never fails, unless
    ``fail_on`` is set."""
    gate = parse_fail_on(fail_on)
    # No checkout needed: the event payload says which run and PR, the server the rest.
    ctx = load_context(os.environ, has_commit=lambda _ref: False)
    run_rollup(ctx, server=server, token=token, repo=repo, scm_token=github_token,
               triage=triage, comment=comment, gate=gate)  # fmt: skip


def run_rollup(
    ctx: Context,
    *,
    server: str,
    token: str,
    repo: str,
    scm_token: str,
    triage: bool,
    comment: bool,
    gate: frozenset[str],
) -> None:
    """The rollup step, for any CI provider. Raises typer.Exit for ``fail-on``."""
    from siftwise.cli import main as cli

    env = os.environ
    ci = _ci()
    repo = repo or ctx.repo
    code = 0
    # A shadow run's comparison, after every leg recorded (a no-op answer otherwise).
    shadow = fetch_shadow(server, token, repo, ctx)
    _shadow_outputs(env, shadow)
    if triage:
        response, problem = fetch_triage(server, token, repo, ctx)
        code = report_failures(ctx, response, problem, comment=comment,
                               github_token=scm_token, fail_on=gate, shadow=shadow)  # fmt: skip
    else:
        if gate:
            _annotate("warning", "siftwise: fail-on has no effect with triage turned off")
        if shadow is not None:
            _append_summary(env, "\n" + shadow_markdown(shadow))
    if not token:
        _annotate("warning", f"siftwise: no token ({ci.no_token_hint}); stats not updated")
    else:
        try:
            result = cli.rollup_repo(server, token, repo)
            line = (f"siftwise: {result.get('tests_updated')} test stats updated in "
                    f"{result.get('seconds')}s")  # fmt: skip
            ci.echo(line)
            _append_summary(env, f"\n{line}\n")
        except cli.SiftwiseError as exc:
            _annotate("warning", f"siftwise: stats not updated: {exc}")
    if code:
        raise typer.Exit(code)


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


def shadow_result(result: dict[str, Any]) -> dict[str, Any]:
    """A shadow run's outputs: the full suite, whatever siftwise would have selected."""
    total = (result.get("estimated_ms") or {}).get("total") or 0.0
    return {
        **result,
        "mode": "full",
        "command": "",
        "reason": SHADOW_REASON,
        "selected_count": result["total_known"],
        "estimated_ms": {"total": total, "would_run": total},
    }


def selection_markdown(
    result: dict[str, Any],
    head_sha: str,
    *,
    shadow: bool = False,
    would_have: dict[str, Any] | None = None,
    not_counted: str = "",
) -> str:
    """The selection as a short Markdown list (step summary and PR comment). A list, not a
    two-column table: a table needs a header row, and this one had nothing to put in it.

    In a shadow run ``result`` is the full suite that runs; ``would_have`` is the selection
    siftwise made (None if it couldn't), and ``not_counted`` why recall won't count the run."""
    if shadow:
        return _shadow_selection_markdown(result, head_sha, would_have, not_counted)
    mode, total_known = result["mode"], result["total_known"]
    if mode == "full":
        tests = f"all ({total_known:,} known)" if total_known else "all"
    elif result["command"]:
        tests = f"{result['selected_count']:,} of {total_known:,} known tests"
    else:
        tests = f"none of {total_known:,} known tests are affected"
    rows = [("Mode", mode), ("Tests", tests)]
    estimated = result.get("estimated_ms") or {}
    total, would_run = estimated.get("total") or 0.0, estimated.get("would_run") or 0.0
    if total > 0:
        saved = total - would_run
        rows.append(("Estimated test time", f"{_duration(would_run)} of {_duration(total)} "
                     f"({100 * saved / total:.0f}% saved)"))  # fmt: skip
    if mode == "full":
        rows.append(("Why the full suite", _prose(result["reason"])))
    return _selection_list(rows, head_sha)


def _shadow_selection_markdown(
    result: dict[str, Any], head_sha: str, would_have: dict[str, Any] | None, not_counted: str
) -> str:
    total_known = result["total_known"]
    rows = [
        ("Mode", "full (shadow run)"),
        ("Tests", f"all ({total_known:,} known)" if total_known else "all"),
    ]
    if would_have is not None:
        rows.append(("Would have run", _would_have_run(would_have)))
    rows.append(("Why the full suite", SHADOW_REASON))
    if not_counted:
        rows.append(("Recall", f"this run won't be counted: {_prose(not_counted)}"))
    return _selection_list(rows, head_sha)


def _would_have_run(selection: dict[str, Any]) -> str:
    total_known = selection["total_known"]
    if selection["mode"] == "full":
        return f"all ({_prose(selection['reason'])})"
    if not selection["command"]:
        return f"none of {total_known:,} known tests (none affected)"
    ran = f"{selection['selected_count']:,} of {total_known:,} known tests"
    estimated = selection.get("estimated_ms") or {}
    total, would_run = estimated.get("total") or 0.0, estimated.get("would_run") or 0.0
    if total > 0:
        ran += f" (~{100 * (total - would_run) / total:.0f}% time saved, estimated)"
    return ran


def _selection_list(rows: list[tuple[str, str]], head_sha: str) -> str:
    items = "\n".join(f"- **{k}:** {v}" for k, v in rows)
    commit = f" for `{head_sha[:7]}`" if head_sha else ""
    return f"### siftwise test selection{commit}\n\n{items}\n"


def split_comment(body: str) -> tuple[str | None, str | None]:
    """The comment's (selection, failures) sections; the failures one keeps its marker line.
    A comment from before sections (only COMMENT_MARKER) is all selection."""
    text = body.replace(COMMENT_MARKER, "", 1).replace(COMMENT_FOOTER, "")
    failures = None
    match = FAILURES_MARKER.search(text)
    if match:  # always the last section
        failures, text = text[match.start() :].strip(), text[: match.start()]
    if SELECTION_MARKER in text:
        text = text.split(SELECTION_MARKER, 1)[1]
    return text.strip() or None, failures


def compose_comment(selection: str | None, failures: str | None) -> str:
    parts = [COMMENT_MARKER]
    if selection:
        parts.append(f"{SELECTION_MARKER}\n{selection.strip()}")
    if failures:
        parts.append(failures.strip())
    parts.append(COMMENT_FOOTER)
    # Blank lines between sections: a table runs on into the next line otherwise.
    return "\n\n".join(parts) + "\n"


def failures_marker(ctx: Context) -> str:
    return f"<!-- siftwise:failures sha={ctx.head_sha} attempt={ctx.run_attempt} -->"


def merged_comment(
    old_body: str, ctx: Context, selection: str | None, failures: str | None
) -> str | None:
    """The comment with only the given section replaced (``old_body``: the current one, or
    ""). ``selection`` also drops a failures section from another commit (stale after a
    push). None: skip, a newer attempt of this commit already wrote the failures."""
    old_selection, old_failures = split_comment(old_body)
    old = FAILURES_MARKER.search(old_failures or "")
    same_commit = old is not None and old[1].lower() == ctx.head_sha.lower()
    if failures is not None and same_commit and old and int(old[2]) > ctx.run_attempt:
        return None
    if selection is None:
        selection = old_selection
    if failures is None:
        failures = old_failures if same_commit else None
    return compose_comment(selection, failures)


def upsert_comment(
    ctx: Context,
    github_token: str,
    *,
    selection: str | None = None,
    failures: str | None = None,
) -> list[str]:
    """Create or update this PR's one siftwise comment, replacing only the given section.

    ``selection`` also drops a failures section from another commit (stale after a push).
    ``failures`` (marker included) is skipped if a newer attempt of this commit already wrote
    one. Returns notes for the summary; never raises (a missing `pull-requests: write`
    permission is only a warning)."""
    issue = f"{ctx.api_url}/repos/{ctx.repo}/issues/{ctx.pr_number}"
    try:
        with httpx.Client(headers=_github_headers(github_token), timeout=30) as gh:
            existing = None
            url: str | None = f"{issue}/comments?per_page=100"
            while url and existing is None:  # paginate: busy PRs have more than 100 comments
                page = gh.get(url)
                page.raise_for_status()
                existing = next((c for c in page.json() if COMMENT_MARKER in c.get("body", "")),
                                None)  # fmt: skip
                url = page.links.get("next", {}).get("url")
            text = merged_comment(existing["body"] if existing else "", ctx, selection, failures)
            if text is None:
                return ["A newer attempt of this run already updated the PR comment."]
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


def pr_head_sha(ctx: Context, github_token: str) -> str | None:
    """The PR's current head commit, or None if GitHub can't tell us."""
    try:
        response = httpx.get(f"{ctx.api_url}/repos/{ctx.repo}/pulls/{ctx.pr_number}",
                             headers=_github_headers(github_token), timeout=15)  # fmt: skip
        if response.is_success:
            head = (response.json().get("head") or {}).get("sha")
            return str(head) if head else None
    except (httpx.HTTPError, ValueError):
        pass
    return None


# --- failure triage -------------------------------------------------------------------------


def parse_fail_on(value: str) -> frozenset[str]:
    """The ``fail-on`` verdicts. An unknown one fails the step: a typo must not silently turn
    the gate off."""
    gate = frozenset(v.strip() for v in value.replace(",", " ").split() if v.strip())
    unknown = sorted(gate - set(VERDICTS))
    if unknown:
        _annotate("error", f"siftwise: unknown fail-on verdict(s): {', '.join(unknown)} "
                  f"(choose from {', '.join(VERDICTS)})")  # fmt: skip
        raise typer.Exit(1)
    return gate


def fetch_triage(
    server: str, token: str, repo: str, ctx: Context
) -> tuple[dict[str, Any] | None, str]:
    """``POST /repos/{repo}/triage`` for this run attempt: (response, "") or (None, why not)."""
    from siftwise.cli import main as cli

    if not token:
        return None, _ci().no_token
    if not ctx.run_id:
        return None, f"no CI run ID ({_ci().run_id_name} is not set)"
    body = {"ci_run_id": ctx.run_id, "run_attempt": ctx.run_attempt, "limit": TRIAGE_LIMIT}
    try:
        return cli.post_json(server, token, f"/repos/{repo}/triage", body,
                             timeout=TRIAGE_TIMEOUT), ""  # fmt: skip
    except cli.SiftwiseError as exc:
        if exc.status == httpx.codes.NOT_FOUND:  # an unknown repo, or no runs: same thing here
            return None, NOTHING_RECORDED
        return None, str(exc)


def report_failures(
    ctx: Context,
    response: dict[str, Any] | None,
    problem: str,
    *,
    comment: bool,
    github_token: str,
    fail_on: frozenset[str],
    shadow: dict[str, Any] | None = None,
) -> int:
    """Write the failures (and a shadow run's recall line) to the job summary, a report file
    and the PR comment, and set the outputs. Returns the exit code: 1 only for ``fail_on`` (a
    match, or no verdicts); the shadow comparison never affects it."""
    env = os.environ
    ci = _ci()
    if response is None:
        _annotate("warning", f"siftwise: failure verdicts unavailable: {problem}")
        in_comment = in_summary = unavailable_markdown(problem)
    else:
        in_comment = failures_markdown(response, ci.full_list_url(ctx), max_rows=COMMENT_ROWS,
                                       full_list=ci.full_list)  # fmt: skip
        in_summary = failures_markdown(response, ctx.run_url)
    if shadow is not None:
        in_comment += "\n" + shadow_markdown(shadow)
        in_summary += "\n" + shadow_markdown(shadow)
    notes = []
    if comment and ctx.pr_number is not None:
        section = f"{failures_marker(ctx)}\n{in_comment}"
        notes.extend(ci.comment(ctx, github_token, failures=section))
    markdown = "\n" + in_summary + "".join(f"\n> {note}\n" for note in notes)
    _append_summary(env, markdown)
    # The summary file is per step; this copy is for later steps (and the e2e check).
    report = ci.report_file(env)
    report.write_text(ci.redact(markdown), encoding="utf-8")
    ci.outputs(env, {
        "verdicts-available": "true" if response is not None else "false",
        "counts": json.dumps(response["counts"] if response is not None else {}),
        "report": str(report),
    })  # fmt: skip
    if response is not None:
        failing = response["failing_tests"]
        detail = f": {counts_line(response['counts'])}" if failing else ""
        ci.echo(f"siftwise: {failing} failing test(s){detail}")

    if not fail_on:
        return 0
    if response is None and ci.gate_skipped(ctx, problem):
        # Fork PRs get no secrets: failing them all would block every outside contribution.
        # Only a missing token skips the gate; server errors, timeouts and 404s fail closed.
        _annotate("warning", ci.gate_skip_warning)
        return 0
    if response is None:
        _annotate("error", f"siftwise verdicts unavailable ({problem}); failing because "
                  "fail-on is set")  # fmt: skip
        return 1
    # Any variant's verdict counts: a test that's a new regression on Linux and flaky on macOS
    # matches fail-on flaky too. Variant counts are exact even when results were cut off.
    hit = [
        verdict
        for verdict in VERDICTS
        if verdict in fail_on and any(v["counts"].get(verdict) for v in response["variants"])
    ]
    if not hit:
        return 0
    tests = sorted({r["test_id"] for r in response["results"] if r["verdict"] in fail_on})
    listed = ", ".join(tests[:10]) + (f" and {len(tests) - 10} more" if len(tests) > 10 else "")
    _annotate("error", f"siftwise: fail-on matched {', '.join(hit)}: {listed}")
    return 1


# --- flaky handling -------------------------------------------------------------------------


def parse_known_flaky_source(value: str) -> str:
    """``known-flaky-source``: 'base' (default) or 'head'; anything else warns, then base."""
    text = value.strip().lower() or "base"
    if text not in ("base", "head"):
        _annotate("warning", f"siftwise: invalid known-flaky-source {value!r} (use base or "
                  "head); using base")  # fmt: skip
        return "base"
    return text


def recorded_known_flaky(ctx: Context, source: str = "base") -> tuple[list[str], bool]:
    """(``known_flaky`` globs triage applies to this run, whether the PR's own list differs).
    A PR uses its base commit's list unless ``source`` is 'head', so a PR can't mark its own
    failures flaky and get past fail-on; a push uses its own. An invalid checkout file has no
    list (record warns about it with the change links)."""
    from siftwise.cli import main as cli

    try:
        config = cli.local_config()
    except RepoConfigError:
        config = RepoConfig()
    if not ctx.change_request:
        return list(config.known_flaky), False
    flaky = cli.known_flaky(config, ctx.base, source)
    return list(flaky.globs), flaky.changed


@dataclass(frozen=True)
class Rerun:
    """record's re-run settings."""

    rounds: int = 0  # rerun-unknown: 0 = off
    verdicts: frozenset[str] = DEFAULT_RERUN_VERDICTS
    max_tests: int = DEFAULT_RERUN_MAX_TESTS
    timeout_s: float = DEFAULT_RERUN_MINUTES * 60
    prefix: tuple[str, ...] = ()  # e.g. ("uv", "run")


def parse_rerun(
    rounds: str, verdicts: str, max_tests: str, timeout_minutes: str, prefix: str
) -> Rerun:
    """The rerun-* inputs. An invalid value warns and falls back to its default (an
    unparsable prefix turns re-runs off: what to run is unknown)."""
    n = _int_input("rerun-unknown", rounds, 0, 0, MAX_RERUN_ROUNDS)
    chosen = frozenset(v.strip() for v in verdicts.replace(",", " ").split())
    if chosen - RERUN_VERDICTS or (verdicts.strip() and not chosen):
        _annotate("warning", f"siftwise: invalid rerun-verdicts {verdicts!r} (use unknown or "
                  "unknown,new_regression); using unknown")  # fmt: skip
        chosen = DEFAULT_RERUN_VERDICTS
    tests = _int_input("rerun-max-tests", max_tests, DEFAULT_RERUN_MAX_TESTS, 1, MAX_RERUN_TESTS)
    minutes = _int_input("rerun-timeout-minutes", timeout_minutes, DEFAULT_RERUN_MINUTES, 1,
                         MAX_RERUN_MINUTES)  # fmt: skip
    try:
        argv = tuple(shlex.split(prefix))
    except ValueError as exc:
        _annotate("warning", f"siftwise: invalid rerun-prefix {prefix!r} ({exc}); no re-runs")
        n, argv = 0, ()
    return Rerun(n, chosen or DEFAULT_RERUN_VERDICTS, tests, minutes * 60, argv)


def _int_input(name: str, value: str, default: int, low: int, high: int) -> int:
    text = value.strip()
    if not text:
        return default
    if not text.isdigit() or not low <= int(text) <= high:
        _annotate("warning", f"siftwise: invalid {name} {value!r} (a whole number from {low} to "
                  f"{high}); using {default}")  # fmt: skip
        return default
    return int(text)


@dataclass
class RerunReport:
    tests: list[str] = field(default_factory=list)  # re-run
    passed: list[str] = field(default_factory=list)  # passed on a re-run: flaky
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (test, why not re-run)
    over_cap: int = 0  # candidates left out by rerun-max-tests
    unmatched: list[str] = field(default_factory=list)  # re-run, but not reported by its ID
    rounds: int = 0  # re-runs done (uploaded)
    problem: str = ""

    def line(self, rerun: Rerun) -> str:
        """One line for the log and job summary ("" if there was nothing to re-run)."""
        if not (self.tests or self.skipped or self.problem):
            return ""
        verdicts = " or ".join(sorted(rerun.verdicts, reverse=True))
        line = (f"siftwise: re-ran {_plural(len(self.tests), 'failure')} ({verdicts}) in "
                f"{_plural(self.rounds, 'round')}")  # fmt: skip
        if self.tests:
            line += f": {len(self.passed)} passed on a re-run (now flaky)"
        if self.unmatched:
            line += f", {len(self.unmatched)} not in the re-run's report under the same test ID"
        skipped = [why for _, why in self.skipped]
        if self.over_cap:
            skipped += [f"over rerun-max-tests ({rerun.max_tests})"] * self.over_cap
        if skipped:
            why = "; ".join(sorted(set(skipped)))
            line += f"; {len(skipped)} not re-run ({why})"
        if self.problem:
            line += f"; stopped: {self.problem}"
        return line


def rerun_failures(
    ctx: Context,
    server: str,
    token: str,
    repo: str,
    variant: str,
    metadata: Mapping[str, Any],
    defer: bool,
    rerun: Rerun,
    reported: Sequence[Path] = (),
) -> RerunReport:
    """Re-run this leg's failures whose verdict is in ``rerun.verdicts``: triage only this
    variant (the other legs may not be recorded yet), run the failing tests again with their
    runners (``reruns.classify`` / ``reruns.invocations``; no shell; ``reported``: this leg's
    JUnit files, for the test files Jest needs), tests still failing go round again, up to
    ``rerun.rounds`` times, within ``rerun.timeout_s`` in all, and upload each round's reports
    together as ``rerun = n`` of this run. Fail open: a problem stops the re-runs with a
    warning, and what was uploaded stands."""
    from siftwise.cli import main as cli

    report = RerunReport()
    body = {"ci_run_id": ctx.run_id, "run_attempt": ctx.run_attempt, "limit": TRIAGE_LIMIT,
            "variants": [variant or None]}  # fmt: skip
    try:
        response = cli.post_json(server, token, f"/repos/{repo}/triage", body,
                                 timeout=TRIAGE_TIMEOUT)  # fmt: skip
    except cli.SiftwiseError as exc:
        report.problem = f"verdicts unavailable ({exc})"
        _annotate("warning", f"siftwise: re-runs skipped: {report.problem}")
        return report
    checkout = reruns.Checkout(Path.cwd())
    paths = _reported_paths(reported)
    targets: dict[str, reruns.Target] = {}
    no_runner: list[str] = []
    for result in response["results"]:
        test_id = result["test_id"]
        if result["verdict"] not in rerun.verdicts or test_id in targets:
            continue
        if any(test_id == skipped for skipped, _ in report.skipped):
            continue
        target = reruns.classify(test_id, paths().get(test_id), checkout)
        if isinstance(target, str):
            report.skipped.append((test_id, target))
            if target.startswith("no runner found"):
                no_runner.append(test_id)
        else:
            targets[test_id] = target
    if no_runner:
        _annotate("warning", f"siftwise: not re-run, no runner found for: {', '.join(no_runner)}")
    report.tests = list(targets)[: rerun.max_tests]
    report.over_cap = len(targets) - len(report.tests)

    out = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir())
    out = out / f"siftwise-rerun-{uuid.uuid4().hex[:8]}"  # outside the checkout's JUnit globs
    deadline = time.monotonic() + rerun.timeout_s
    remaining = report.tests
    for n in range(1, rerun.rounds + 1):
        if not remaining:
            break
        if deadline - time.monotonic() <= 0:
            report.problem = "the rerun-timeout-minutes limit was reached"
            break
        try:
            out.mkdir(parents=True, exist_ok=True)
            commands = reruns.invocations([targets[t] for t in remaining], out, n,
                                          rerun.prefix, checkout.root)  # fmt: skip
        except OSError as exc:  # backing up Maven/Gradle's reports
            report.problem = f"re-run {n} couldn't start ({exc})"
            break
        files, report.problem = _run_round(n, commands, deadline)
        if report.problem:
            break
        try:
            final: dict[str, Status] = {}
            for xml in files:  # last attempt, each runner's tests in its own report
                final.update((r.test_id, r.status) for r in parse_junit(xml.read_bytes()))
        except (OSError, JUnitParseError) as exc:
            report.problem = f"re-run {n} wrote no JUnit report ({exc})"
            break
        try:
            cli.upload_run(server, token, files, {**metadata, "rerun": n, "started_at": _now()},
                           defer_rollup=defer, timeout=RECORD_TIMEOUT)  # fmt: skip
        except cli.SiftwiseError as exc:
            report.problem = f"re-run {n} not recorded ({exc})"
            break
        report.rounds = n
        report.passed += [t for t in remaining if final.get(t) is Status.PASSED]
        # A test the report doesn't name (a different rootdir, other options) stays unknown.
        report.unmatched += [t for t in remaining if t not in final]
        remaining = [t for t in remaining if final.get(t) in (Status.FAILED, Status.ERROR)]
    if report.problem:
        _annotate("warning", f"siftwise: re-runs stopped: {report.problem}")
    return report


def _run_round(
    n: int, commands: list[reruns.Invocation], deadline: float
) -> tuple[list[Path], str]:
    """Run round ``n``'s commands in turn, within the deadline: (their JUnit reports, "") or
    ([], what went wrong). Then, whatever happened, Maven/Gradle's reports in the checkout are
    put back as the CI run left them (a failure to is only a warning)."""
    try:
        return _run_commands(n, commands, deadline)
    finally:
        for command in commands:
            try:
                command.restore()
            except OSError as exc:
                _annotate("warning", f"siftwise: re-run {n} changed {command.tool}'s reports "
                                     f"in the checkout ({exc})")  # fmt: skip


def _run_commands(
    n: int, commands: list[reruns.Invocation], deadline: float
) -> tuple[list[Path], str]:
    files: list[Path] = []
    for command in commands:
        left = deadline - time.monotonic()
        if left <= 0:
            return [], f"re-run {n} hit the rerun-timeout-minutes limit (results discarded)"
        _ci().echo(f"siftwise: re-run {n}: {shlex.join(command.argv)}")
        try:
            run_command(command.argv, left, **command.options())
        except subprocess.TimeoutExpired:
            return [], f"re-run {n} hit the rerun-timeout-minutes limit (results discarded)"
        except OSError as exc:
            return [], f"couldn't run {command.tool} ({exc})"
        try:
            files += command.reports()
        except OSError as exc:
            return [], f"re-run {n} wrote no JUnit report ({exc})"
    return files, ""


def _reported_paths(reports: Sequence[Path]) -> Callable[[], dict[str, str]]:
    """Test ID -> file from this leg's own JUnit reports (Jest's re-runs need the file; the
    triage response doesn't carry it). Read once, when first needed."""
    cache: dict[str, str] = {}
    done = False

    def paths() -> dict[str, str]:
        nonlocal done
        if not done:
            done = True
            for path in reports:
                try:
                    results = parse_junit(path.read_bytes())
                except (OSError, JUnitParseError):
                    continue
                for r in results:
                    if r.file_path:
                        cache.setdefault(r.test_id, r.file_path)
        return cache

    return paths


def run_command(
    argv: list[str],
    timeout: float,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    stdout: Path | None = None,
) -> None:
    """Run a re-run's command (no shell); its output goes to the step's log (or ``stdout``, a
    file: ``go test -json``). ``env`` adds to the environment. The program is looked up on
    PATH first, so ``npx``/``mvn``'s Windows ``.cmd`` shims are found. Raises TimeoutExpired or
    OSError."""
    program = shutil.which(argv[0]) or argv[0]
    full_env = {**os.environ, **env} if env else None
    if stdout is None:
        subprocess.run([program, *argv[1:]], timeout=timeout, check=False, cwd=cwd, env=full_env)
        return
    with stdout.open("wb") as sink:
        subprocess.run([program, *argv[1:]], timeout=timeout, check=False, cwd=cwd, env=full_env,
                       stdout=sink)  # fmt: skip


# --- shadow mode ----------------------------------------------------------------------------


def parse_shadow(value: str) -> float | None:
    """The ``shadow`` input as a sampling rate (0 = off, 1 = always), or None if invalid."""
    text = value.strip().lower()
    if text in ("", "off"):
        return 0.0
    if text == "always":
        return 1.0
    match = re.fullmatch(r"(\d+(?:\.\d+)?)%", text)
    if match and float(match[1]) <= 100:
        return float(match[1]) / 100
    return None


def sampled(repo: str, run_id: str, rate: float) -> bool:
    """Whether this CI run is a shadow run. Deterministic per run ID, so every matrix leg (and
    every re-run attempt) of the run agrees without talking to the others."""
    if rate <= 0 or not run_id:
        return False
    if rate >= 1:
        return True
    digest = hashlib.sha256(f"{repo}\n{run_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < rate


def fetch_shadow(server: str, token: str, repo: str, ctx: Context) -> dict[str, Any] | None:
    """``POST /repos/{repo}/shadow`` for this run attempt: the comparison if this was a
    shadow run, else None. A failure only warns: the run just isn't counted."""
    from siftwise.cli import main as cli

    if not token or not ctx.run_id:
        return None
    body = {"ci_run_id": ctx.run_id, "run_attempt": ctx.run_attempt}
    try:
        result = cli.post_json(server, token, f"/repos/{repo}/shadow", body,
                               timeout=TRIAGE_TIMEOUT)  # fmt: skip
    except cli.SiftwiseError as exc:
        if exc.status != httpx.codes.NOT_FOUND:  # 404: a repo siftwise doesn't know, no shadow
            _annotate("warning", f"siftwise: shadow comparison failed: {exc}; this run won't "
                      "be counted in recall")  # fmt: skip
        return None
    return result if result.get("shadow") else None


def shadow_markdown(response: dict[str, Any], *, max_rows: int = COMMENT_ROWS) -> str:
    """The shadow run's line: what siftwise would have run, and the failures it would have
    caught; then the missed tests (at most ``max_rows``)."""
    if not response["counted"]:
        reasons = sorted({_NOT_COUNTED.get(s["status"], s["status"])
                          for s in response["selections"]})  # fmt: skip
        return f"**Shadow run:** not counted: {'; '.join(reasons) or 'nothing compared'}.\n"
    compared = [s for s in response["selections"] if s["status"] == "compared"]
    total = max(s["total_known"] for s in compared)
    if response["mode"] == "full":
        ran = f"all {total:,} tests (the full suite)"
    else:
        counts = {s["selected_count"] for s in compared}
        ran = f"{'up to ' if len(counts) > 1 else ''}{max(counts):,} of {total:,} tests"
        saved = response["time"]["saved"]
        if saved is not None:
            ran += f" ({saved:.0%} time saved)"
    caught, missed = response["caught"], response["missed"]
    if caught + missed:
        outcome = f"caught {caught} of {caught + missed} failures"
    else:
        outcome = "had no failures to catch"
    flaky = response["flaky_excluded"]
    if flaky:
        outcome += f" ({flaky} flaky not counted)"
    lines = [f"**Shadow run:** would have run {ran} and {outcome}."]
    listed = response["missed_tests"][:max_rows]
    if listed:
        lines += ["", "Missed:"]
        lines += [_missed_line(m) for m in listed]
        if missed > len(listed):
            lines.append(f"- +{missed - len(listed)} more")
    return "\n".join(lines) + "\n"


# Why a shadow run's selection missed a test (core/shadow.py CAUSES).
_CAUSE_LABELS = {
    "new_test": "new test",
    "flaky_history": "flaky history",
    "config_gap": "config gap",
    "no_link": "no link to the change",
    "unknown": "unknown cause",
}


def _missed_line(miss: Mapping[str, Any]) -> str:
    """A missed test with its cause and verdict (just the verdict from an older server)."""
    # Our own labels as they are; a value this client doesn't know, as untrusted text.
    verdict = _LABELS[miss["verdict"]][0] if miss["verdict"] in _LABELS else _text(miss["verdict"])
    test = _code(miss["test_id"])
    cause = miss.get("cause")
    if not cause:
        return f"- {test} ({verdict})"
    label = _CAUSE_LABELS[cause] if cause in _CAUSE_LABELS else _text(cause)
    return f"- {test}: {label} (verdict: {verdict})"


def _shadow_outputs(env: Env, response: dict[str, Any] | None) -> None:
    counted = response is not None and response["counted"]
    _ci().outputs(env, {
        "shadow": "true" if response is not None else "false",
        "shadow-mode": (response or {}).get("mode") or "",
        "shadow-caught": str(response["caught"]) if counted and response else "",
        "shadow-missed": str(response["missed"]) if counted and response else "",
    })  # fmt: skip


@dataclass
class FailureRow:
    """One table row: a test with one verdict, on the variants where it got that verdict."""

    test_id: str
    verdict: str
    confidence: str
    variants: list[str | None]
    reason: str


def failure_rows(response: dict[str, Any]) -> list[FailureRow]:
    """The response's results grouped by (test, verdict), worst first. A test with different
    verdicts on different variants gets a row per verdict."""
    rows: dict[tuple[str, str], FailureRow] = {}
    for result in response["results"]:
        key = (result["test_id"], result["verdict"])
        if key in rows:
            rows[key].variants.append(result.get("variant"))
        else:
            reasons = result.get("reasons") or [""]
            rows[key] = FailureRow(result["test_id"], result["verdict"], result["confidence"],
                                   [result.get("variant")], reasons[0])  # fmt: skip
    # Stable: the server's order (worst test first, then test ID) within a verdict/confidence.
    return sorted(rows.values(), key=lambda row: (
        _rank(VERDICTS, row.verdict), _rank(CONFIDENCES, row.confidence)))  # fmt: skip


def failures_markdown(
    response: dict[str, Any],
    run_url: str,
    *,
    max_rows: int | None = None,
    full_list: str = "full list in the job summary",
) -> str:
    """The Failures section: a summary line, the new regressions and unknowns in a table, and
    the flaky and pre-existing ones collapsed. ``max_rows`` caps each table (the comment); the
    job summary gets everything the server returned (``run_url`` links to it, as
    ``full_list``)."""
    variants = [v.get("variant") for v in response["variants"]]
    total = response["failing_tests"]
    # The PR edits known_flaky: this run used the base branch's list.
    note = (
        [f"> {KNOWN_FLAKY_AFTER_MERGE}.", ""]
        if any(v.get("known_flaky_changed") for v in response["variants"])
        else []
    )
    if not total:
        recorded = _plural(len(variants), "recorded variant")
        return "\n".join(["### Failures", "", *note, f"No failing tests in {recorded}.", ""])
    rows = failure_rows(response)
    shown = [r for r in rows if r.verdict not in COLLAPSED][:max_rows]
    collapsed = [r for r in rows if r.verdict in COLLAPSED][:max_rows]
    lines = [f"### Failures: {counts_line(response['counts'])}", "", *note]
    for v in response["variants"]:
        if v.get("failing") and not v.get("links_known"):
            where = _code(v["variant"]) if v.get("variant") else "this run"
            lines += [
                f"> Change links weren't recorded for {where} (an import graph failed or was "
                "skipped), so its failures can't be tied to this change.",
                "",
            ]
    if shown:
        lines += [*_failure_table(shown, variants), ""]
    if collapsed:
        quiet = counts_line({v: response["counts"].get(v, 0) for v in VERDICTS if v in COLLAPSED})
        lines += [f"<details><summary>{quiet}: likely not caused by this change</summary>", "",
                  *_failure_table(collapsed, variants), "", "</details>", ""]  # fmt: skip
    more = total - len({r.test_id for r in shown + collapsed})
    if more > 0:
        if max_rows is not None:
            lines.append(f"+{more} more: [{full_list}]({run_url})")
        else:
            lines.append(f"+{more} more not listed (siftwise returns at most {TRIAGE_LIMIT}).")
    return "\n".join(lines).rstrip() + "\n"


def unavailable_markdown(problem: str) -> str:
    return f"### Failures\n\nFailure verdicts unavailable: {_text(problem)}.\n"


def counts_line(counts: Mapping[str, int]) -> str:
    """'2 new regressions, 1 unknown, 1 flaky': worst first, zeros left out."""
    parts = []
    for verdict in VERDICTS:
        n = counts.get(verdict, 0)
        if n:
            parts.append(f"{n} {_LABELS[verdict][0 if n == 1 else 1]}")
    return ", ".join(parts)


def _failure_table(rows: list[FailureRow], variants: list[str | None]) -> list[str]:
    with_variants = len(variants) > 1
    header = ["Test", "Verdict", "Confidence", *(["Variants"] if with_variants else []), "Why"]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for row in rows:
        label = _LABELS[row.verdict][0]
        if row.verdict == "new_regression":
            label = f"**{label}**"
        cells = [_code(row.test_id, table=True), label, row.confidence]
        if with_variants:
            cells.append(_variants_cell(row.variants, variants))
        reason = row.reason if len(row.reason) <= REASON_CHARS else \
            row.reason[: REASON_CHARS - 1] + "…"  # fmt: skip
        cells.append(_text(reason, table=True))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _variants_cell(failed: list[str | None], recorded: list[str | None]) -> str:
    """'all' only when the test got this verdict on every recorded variant; a test that failed
    on some legs and passed on others lists just the legs where it failed."""
    if set(failed) == set(recorded):
        return "all"
    return ", ".join(_text(v, table=True) if v else "(no variant)" for v in failed)


# Control characters (newlines, ANSI escapes): they'd end a table row, or fake a log line.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def code_span(text: str, *, table: bool = False) -> str:
    """Untrusted text (test IDs, file paths, reasons, variants: they come from the change's
    code) as a Markdown code span, which GitHub and GitLab show literally: no formatting,
    HTML, links or autolinks, images, ``@`` mentions or references
    (https://github.github.com/gfm/#code-spans, https://docs.gitlab.com/user/markdown/).
    The fence is one backtick longer than any run of backticks inside, with a space on each
    side (one is stripped). Newlines and control characters become spaces; in a table, every
    ``|`` is escaped (``\\|``, which GFM tables unescape even inside code) so it can't end the
    cell; ``<!--`` is broken up so the comment's own section markers can't be forged."""
    text = _CONTROL.sub(" ", text).replace("<!--", "<! --")
    if table:
        text = text.replace("|", "\\|")
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    return f"{fence} {text} {fence}"


def _rank(order: tuple[str, ...], value: str) -> int:
    return order.index(value) if value in order else len(order)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _github_headers(github_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


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
    """Files matching any of the (whitespace-separated) globs, plus ``junit_dir/*.xml``. Never
    looks inside SKIP_DIRS (node_modules, virtualenvs, .git...)."""
    found: set[Path] = set()
    for pattern in patterns.split():
        found.update(path for path in _glob_pruned(pattern)
                     if path.is_file() and not SKIP_DIRS.intersection(path.parts))  # fmt: skip
    if junit_dir and Path(junit_dir).is_dir():
        found.update(p for p in Path(junit_dir).glob("*.xml") if p.is_file())
    return sorted(found)


_GLOB_CHARS = re.compile(r"[*?\[]")


def _glob_pruned(pattern: str) -> Iterator[Path]:
    """``glob.glob(pattern, recursive=True)``, except that ``**`` never descends into SKIP_DIRS.

    glob walks a whole ``node_modules`` or ``.venv`` before a filter can drop its matches: on a
    big checkout that's most of the step's time. Like glob, ``**`` and ``*`` skip hidden
    directories and files (unless the pattern's own segment starts with a dot).
    """
    from siftwise.core.selector import glob_match

    segments = pattern.replace("\\", "/").split("/")
    first = next((i for i, s in enumerate(segments) if _GLOB_CHARS.search(s)), len(segments))
    rest = "/".join(segments[first:])
    if "**" not in rest:  # no recursion: glob only reads the directories the pattern names
        yield from (Path(name) for name in glob.glob(pattern))
        return
    base = "/".join(segments[:first]) or "."
    if segments[:first] == [""]:  # an absolute pattern: "/**/x.xml"
        base = "/"
    hidden_ok = segments[-1].startswith(".")
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        rel_dir = os.path.relpath(dirpath, base).replace(os.sep, "/")
        for name in filenames:
            if name.startswith(".") and not hidden_ok:
                continue
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if glob_match(rel, rest):
                yield Path(name) if dirpath == "." else Path(dirpath) / name


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
    _ci().summary(env, markdown)


def _annotate(level: str, message: str) -> None:
    _ci().annotate(level, message)


def _code(text: str, *, table: bool = False) -> str:
    """Untrusted text (a test ID, a variant) shown as code in the comment and summaries."""
    return _ci().code(text, table=table)


def _text(text: str, *, table: bool = False) -> str:
    """Untrusted text (a reason, a problem) in the comment and summaries."""
    return _ci().text(text, table=table)


def _prose(text: str) -> str:
    """A selection reason in the comment's list."""
    return _ci().prose(text)


def _duration(ms: float) -> str:
    seconds = round(ms / 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- CI providers ---------------------------------------------------------------------------


class Provider:
    """How the select/record/rollup steps report back to the CI system: GitHub Actions here.
    GitLab CI's (``cli/gitlab.py``) overrides what differs; the flows themselves are shared.
    The active one is ``_PROVIDER``'s (``use_provider``)."""

    no_token = NO_TOKEN
    no_token_hint = "fork pull request?"
    gate_skip_warning = "fail-on skipped: no siftwise token (fork PR?); this run is not gated"
    shallow_fix = "set `fetch-depth: 0` on actions/checkout."
    run_id_name = "GITHUB_RUN_ID"
    rollup_step = "actions/rollup"
    full_list = "full list in the job summary"

    def annotate(self, level: str, message: str) -> None:
        # Workflow commands are line-based: escape what would end or alter the command.
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        typer.echo(f"::{level}::{escaped}")

    def echo(self, line: str) -> None:
        typer.echo(line)

    def outputs(self, env: Env, values: Mapping[str, str]) -> None:
        """The step's outputs (``outputs.mode`` ...)."""
        _append(env, "GITHUB_OUTPUT", values)

    def export(self, env: Env, values: Mapping[str, str]) -> None:
        """Environment variables for the job's later steps."""
        _append(env, "GITHUB_ENV", values)

    def summary(self, env: Env, markdown: str) -> None:
        path = env.get("GITHUB_STEP_SUMMARY")
        if path:
            with open(path, "a", encoding="utf-8") as out:
                out.write(markdown)

    def report_file(self, env: Env) -> Path:
        """Where the failures report goes (the summary file is per step)."""
        return Path(env.get("RUNNER_TEMP", ".")) / f"siftwise-failures-{uuid.uuid4().hex[:8]}.md"

    def default_junit_dir(self, env: Env) -> str:
        return str(Path(env.get("RUNNER_TEMP", ".")) / "siftwise-junit")

    def full_list_url(self, ctx: Context) -> str:
        """The comment's "+N more" link: where the full failures list is."""
        return ctx.run_url

    def redact(self, text: str) -> str:
        """Secrets out of text written to a file (GitHub masks secrets in its own logs)."""
        return text

    # Untrusted text: code spans only (both providers).

    def code(self, text: str, *, table: bool = False) -> str:
        return code_span(text, table=table)

    def text(self, text: str, *, table: bool = False) -> str:
        return code_span(text, table=table)

    def prose(self, text: str) -> str:
        return code_span(text)

    def started_at(self, ctx: Context, scm_token: str, env: Env) -> str:
        return run_started_at(ctx, scm_token, env)

    def comment(
        self,
        ctx: Context,
        scm_token: str,
        *,
        selection: str | None = None,
        failures: str | None = None,
    ) -> list[str]:
        """Replace one section of the PR's sticky comment. Returns notes for the summary;
        never raises."""
        if ctx.fork:
            return ["Pull request from a fork: no PR comment (its token is read-only)."]
        if not scm_token:
            return ["No GitHub token: no PR comment."]
        if failures is not None:
            head = pr_head_sha(ctx, scm_token)
            if head and head.lower() != ctx.head_sha.lower():
                # A push since this run started: its own run owns the comment now.
                return ["The pull request has newer commits: PR comment not updated."]
        return upsert_comment(ctx, scm_token, selection=selection, failures=failures)

    def gate_skipped(self, ctx: Context, problem: str) -> bool:
        """Whether unavailable verdicts skip fail-on instead of failing closed."""
        return problem == self.no_token


GITHUB = Provider()
_PROVIDER: contextvars.ContextVar[Provider] = contextvars.ContextVar("siftwise_ci",
                                                                     default=GITHUB)  # fmt: skip


def _ci() -> Provider:
    return _PROVIDER.get()


@contextmanager
def use_provider(provider: Provider) -> Iterator[None]:
    """Run the shared flows with ``provider`` reporting."""
    reset = _PROVIDER.set(provider)
    try:
        yield
    finally:
        _PROVIDER.reset(reset)
