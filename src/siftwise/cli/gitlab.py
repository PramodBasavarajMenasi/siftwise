"""`siftwise gitlab select|record|rollup`: the GitLab CI glue behind gitlab/siftwise.gitlab-ci.yml.

The flows are `siftwise gha`'s (``gha.run_select``, ``run_record``, ``run_rollup``): fail open,
``fail-on``, shadow mode, ``known_flaky`` from the merge request's base commit, re-runs. This
module reads the pipeline from GitLab's predefined CI/CD variables (``load_context``) and
reports through ``GitLabProvider``: outputs as a dotenv file for later jobs
(``artifacts:reports:dotenv``), reports under ``.siftwise-ci/`` for artifacts, plain log lines,
and the sticky merge request note through the Notes API.

Security, as requirements:

* Secrets come from the environment only, never from options: the siftwise token
  (``SIFTWISE_API_TOKEN``, or ``SIFTWISE_TOKEN``) and the GitLab token
  (``SIFTWISE_GITLAB_TOKEN``). Every log line, file and note goes through
  ``GitLabProvider.redact``, so neither can appear even if an error message echoes one.
* Untrusted text (test IDs, file paths, reasons, variants: they come from the merge request's
  code) is written into notes as code spans only, which GitLab shows literally: no Markdown,
  HTML, links, images, references, ``@`` mentions or placeholders. No line of a note can
  start with ``/``, so it can't run a quick action. Raw failure messages are never shown.
* Fork merge requests run in the fork without this project's variables: no token, so the full
  suite runs, no note, no upload, and ``fail-on`` is skipped with a warning. Unlike GitHub, a
  missing token on a non-fork pipeline (a protected variable on an unprotected branch) fails
  ``fail-on`` closed: only forks are expected to lack it.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
import typer

from siftwise.cli import gha
from siftwise.cli.gha import Context, Env

app = typer.Typer(help="GitLab CI jobs (used by gitlab/siftwise.gitlab-ci.yml).")

# Everything siftwise writes in the job, relative to the project directory (artifact paths
# must be inside it). A dot-directory: JUnit globs and the import graphs skip it.
OUT_DIR = Path(".siftwise-ci")
JUNIT_DIR = (OUT_DIR / "junit").as_posix()
FAILURES_REPORT = OUT_DIR / "failures.md"
# Secrets, from the environment only. CI_JOB_TOKEN isn't used, but it's a secret in every job.
SIFTWISE_TOKEN_VARS = ("SIFTWISE_API_TOKEN", "SIFTWISE_TOKEN")
GITLAB_TOKEN_VAR = "SIFTWISE_GITLAB_TOKEN"
SECRET_VARS = (*SIFTWISE_TOKEN_VARS, GITLAB_TOKEN_VAR, "CI_JOB_TOKEN")
MASKED = "[MASKED]"
API_TIMEOUT = 30
MAX_NOTE_PAGES = 50  # 100 notes a page
# dotenv values: no quoting, no escapes, no newlines; ``$`` and quotes stay out too.
# https://docs.gitlab.com/ci/variables/dotenv_variables/
_DOTENV_UNSAFE = re.compile(r"[^A-Za-z0-9 _.,:;/()+=-]")
_DOTENV_MAX = 200
# Control characters (newlines, ANSI escapes): a log line can't fake another one, or a section.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_OUTPUT_NAMES = {"shadow": "SIFTWISE_SHADOW_RUN"}  # SIFTWISE_SHADOW is select's own input
_EXPORTED = frozenset({"SIFTWISE_JUNIT_DIR"})  # what select passes on besides its outputs


def load_context(
    env: Env, base_ref: str | None = None, has_commit: Callable[[str], bool] | None = None
) -> Context:
    """The pipeline, from GitLab's predefined variables
    (https://docs.gitlab.com/ci/variables/predefined_variables/). ``base_ref`` overrides the
    diff base.

    * Merge request pipelines (``CI_PIPELINE_SOURCE=merge_request_event``): the diff is the
      whole MR, from ``CI_MERGE_REQUEST_DIFF_BASE_SHA``. The recorded commit is the MR's source
      commit: in merged results pipelines ``CI_COMMIT_SHA`` is the merge commit GitLab made, and
      ``CI_MERGE_REQUEST_SOURCE_BRANCH_SHA`` (set only there) is the MR head, as on GitHub. The
      repo and the note go to the target project: a fork's pipeline runs in the fork
      (https://docs.gitlab.com/ci/pipelines/merge_request_pipelines/#use-with-forked-projects).
      Merge trains test the MR on top of the others in the train, so their diff is unknown.
    * Branch pipelines from a push: ``CI_COMMIT_BEFORE_SHA`` (zeros for a new branch: then the
      first parent), like a GitHub push. Other sources (schedules, the web, the API, triggers,
      tags) have no diff base: the full suite.
    * A default-branch pipeline that isn't for a merge request is a main run.
    """
    has_commit = has_commit or gha.git_has_commit
    source = env.get("CI_PIPELINE_SOURCE", "")
    iid = env.get("CI_MERGE_REQUEST_IID", "")
    base = base_ref or None
    pr_number: int | None = None
    if source == gha.MERGE_REQUEST_EVENT and iid.isdigit():
        pr_number = int(iid)
        head = env.get("CI_MERGE_REQUEST_SOURCE_BRANCH_SHA") or env.get("CI_COMMIT_SHA", "")
        branch = env.get("CI_MERGE_REQUEST_SOURCE_BRANCH_NAME") or "unknown"
        repo = env.get("CI_MERGE_REQUEST_PROJECT_PATH") or env.get("CI_PROJECT_PATH", "")
        project_id = env.get("CI_MERGE_REQUEST_PROJECT_ID") or env.get("CI_PROJECT_ID", "")
        source_project = env.get("CI_MERGE_REQUEST_SOURCE_PROJECT_ID", "")
        fork = bool(source_project) and source_project != project_id
        if base is None and env.get("CI_MERGE_REQUEST_EVENT_TYPE") != "merge_train":
            base = env.get("CI_MERGE_REQUEST_DIFF_BASE_SHA") or None
    else:
        head = env.get("CI_COMMIT_SHA", "")
        branch = (env.get("CI_COMMIT_BRANCH") or env.get("CI_COMMIT_TAG")
                  or env.get("CI_COMMIT_REF_NAME") or "unknown")  # fmt: skip
        repo = env.get("CI_PROJECT_PATH", "")
        project_id = env.get("CI_PROJECT_ID", "")
        fork = False
        if base is None and source == "push":
            before = env.get("CI_COMMIT_BEFORE_SHA", "")
            if before and before != gha.ZERO_SHA and has_commit(before):
                base = before
            elif head and has_commit(f"{head}^"):
                base = f"{head}^"  # a new branch: diff against the first parent
    default_branch = env.get("CI_DEFAULT_BRANCH", "")
    return Context(
        event_name=source,
        repo=repo,
        head_sha=head,
        branch=branch,
        # CI_COMMIT_BRANCH is unset in merge request and tag pipelines.
        is_main=pr_number is None
        and bool(default_branch)
        and env.get("CI_COMMIT_BRANCH") == default_branch,
        base=base,
        run_id=env.get("CI_PIPELINE_ID", ""),
        # A retried job keeps its pipeline ID: only the pipeline's first upload of each leg
        # counts (record says so). A new pipeline is a new run.
        run_attempt=1,
        pr_number=pr_number,
        fork=fork,
        api_url=env.get("CI_API_V4_URL", "https://gitlab.com/api/v4"),
        server_url=env.get("CI_SERVER_URL", "https://gitlab.com"),
        project_id=project_id,
        pipeline_url=env.get("CI_PIPELINE_URL", ""),
    )


def runs_in_parent(env: Env) -> bool:
    """A fork's merge request pipeline that a member ran in this project: it has this
    project's variables, with the fork's code."""
    project = env.get("CI_MERGE_REQUEST_PROJECT_ID", "")
    return bool(project) and env.get("CI_PROJECT_ID") == project


def siftwise_token(env: Env) -> str:
    return next((env[name] for name in SIFTWISE_TOKEN_VARS if env.get(name)), "")


def insecure_server(url: str) -> bool:
    """An ``http://`` server outside the job: the token would cross the network in clear.
    ``localhost`` and service hostnames (one label, e.g. ``siftwise``) stay inside it."""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() != "http":
        return False
    host = (parts.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return False
    return "." in host or ":" in host  # a domain name or an IP address


class GitLabProvider(gha.Provider):
    """GitLab CI's reporting for one ``siftwise gitlab`` job (``step``: select, record or
    rollup)."""

    no_token = (
        "no siftwise server or token in this pipeline (merge requests from forks get none of "
        "this project's variables, and protected ones only reach protected branches)"
    )
    no_token_hint = "a fork, or a protected variable on an unprotected branch?"
    gate_skip_warning = (
        "siftwise: fail-on skipped: no siftwise token in a merge request from a fork; this "
        "pipeline is not gated"
    )
    shallow_fix = 'set GIT_DEPTH: "0" on the job (new GitLab projects clone 20 commits).'
    run_id_name = "CI_PIPELINE_ID"
    rollup_step = "the rollup job (.siftwise-rollup)"
    full_list = f"full list: {FAILURES_REPORT.as_posix()} in this job's artifacts"

    def __init__(self, step: str, env: Env) -> None:
        self.step = step
        self.env = env
        # Longest first, so a secret containing another is masked whole.
        values = {env.get(name, "") for name in SECRET_VARS}
        self.secrets = sorted((v for v in values if len(v) >= 4), key=len, reverse=True)

    # --- everything written goes through redact ---

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, MASKED)
        return text

    def _log(self, line: str) -> str:
        return _CONTROL.sub(" ", self.redact(line))

    def annotate(self, level: str, message: str) -> None:
        typer.echo(f"{level.upper()}: {self._log(message)}")

    def echo(self, line: str) -> None:
        typer.echo(self._log(line))

    def outputs(self, env: Env, values: Mapping[str, str]) -> None:
        """The job's outputs, as ``SIFTWISE_<NAME>`` in ``.siftwise-ci/<step>.env``: GitLab
        passes them to later jobs (``artifacts:reports:dotenv``; the last value of a name
        wins). The command goes to a file (it can exceed dotenv's 5 KB and needs quotes), the
        counts to one variable per verdict (no JSON in dotenv)."""
        lines: dict[str, str] = {}
        for name, value in values.items():
            if name == "command":
                path = OUT_DIR / "command.sh"
                _write(path, self.redact(value) + "\n" if value else "")
                lines["SIFTWISE_COMMAND_FILE"] = path.as_posix()
            elif name == "report":  # relative to the project directory, like artifact paths
                lines["SIFTWISE_REPORT"] = Path(value).as_posix()
            elif name == "counts":
                counts = json.loads(value or "{}")
                for verdict in gha.VERDICTS:
                    lines[f"SIFTWISE_COUNT_{verdict.upper()}"] = str(counts.get(verdict, 0))
            else:
                lines[_OUTPUT_NAMES.get(name) or _dotenv_name(name)] = value
        self._dotenv(lines)

    def export(self, env: Env, values: Mapping[str, str]) -> None:
        self._dotenv({k: v for k, v in values.items() if k in _EXPORTED})

    def _dotenv(self, values: Mapping[str, str]) -> None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(OUT_DIR / f"{self.step}.env", "a", encoding="utf-8") as out:
            for name, value in values.items():
                out.write(f"{name}={dotenv_value(self.redact(value))}\n")

    def summary(self, env: Env, markdown: str) -> None:
        """GitLab has no job summary: ``.siftwise-ci/<step>.md``, an artifact."""
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(OUT_DIR / f"{self.step}.md", "a", encoding="utf-8") as out:
            out.write(self.redact(markdown))

    def report_file(self, env: Env) -> Path:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        return FAILURES_REPORT

    def default_junit_dir(self, env: Env) -> str:
        return JUNIT_DIR

    def full_list_url(self, ctx: Context) -> str:
        return self.env.get("CI_JOB_URL") or ctx.run_url

    def started_at(self, ctx: Context, scm_token: str, env: Env) -> str:
        """The pipeline's creation time: every job of the pipeline records the same."""
        return env.get("CI_PIPELINE_CREATED_AT") or env.get("CI_JOB_STARTED_AT") or gha._now()

    def comment(
        self,
        ctx: Context,
        scm_token: str,
        *,
        selection: str | None = None,
        failures: str | None = None,
    ) -> list[str]:
        if ctx.fork and not runs_in_parent(self.env):
            gha._annotate("warning", "siftwise: merge request from a fork: no comment (its "
                          "pipeline gets none of this project's variables)")  # fmt: skip
            return ["Merge request from a fork: no comment."]
        if not scm_token:
            gha._annotate("warning", f"siftwise: no {GITLAB_TOKEN_VAR}: no merge request "
                          "comment")  # fmt: skip
            return [f"No {GITLAB_TOKEN_VAR}: no merge request comment."]
        if failures is not None:
            head = mr_head_sha(ctx, scm_token)
            if head and head.lower() != ctx.head_sha.lower():
                return ["The merge request has newer commits: comment not updated."]
        if selection is not None:
            selection = self.redact(selection)
        if failures is not None:
            failures = self.redact(failures)
        notes = upsert_note(ctx, scm_token, selection=selection, failures=failures)
        return [self.redact(note) for note in notes]

    def gate_skipped(self, ctx: Context, problem: str) -> bool:
        """Only a fork's pipeline is expected to have no token; anywhere else, ``fail-on``
        fails closed (a protected variable that doesn't reach the branch must not turn the gate
        off)."""
        return problem == self.no_token and ctx.fork


# Untrusted text: gha.code_span, shared with GitHub (GitLab shows code spans literally too:
# no references or placeholders, https://docs.gitlab.com/user/markdown/#code-spans-and-blocks).
code_span = gha.code_span


def note_safe(body: str) -> str:
    """No line may start a quick action: GitLab runs them from notes created through the API
    (https://docs.gitlab.com/api/notes/), and a command starts with ``/`` on its own line
    (https://docs.gitlab.com/user/project/quick_actions/). The comment's own lines never do;
    this escapes any that would (``\\/``, which renders as ``/``)."""
    return re.sub(r"^(\s*)/", r"\1\\/", body, flags=re.MULTILINE)


def dotenv_value(value: str) -> str:
    """A value GitLab's dotenv report accepts as is: one line, no quotes, no ``$``, no
    characters that need escaping, at most 200 characters."""
    return _DOTENV_UNSAFE.sub("_", _CONTROL.sub(" ", value)).strip()[:_DOTENV_MAX]


def _dotenv_name(name: str) -> str:
    return "SIFTWISE_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --- the merge request note (https://docs.gitlab.com/api/notes/) --------------------------------


def _headers(gitlab_token: str) -> dict[str, str]:
    return {"PRIVATE-TOKEN": gitlab_token}


def _mr_url(ctx: Context) -> str:
    return f"{ctx.api_url}/projects/{ctx.project_id}/merge_requests/{ctx.pr_number}"


def upsert_note(
    ctx: Context,
    gitlab_token: str,
    *,
    selection: str | None = None,
    failures: str | None = None,
) -> list[str]:
    """Create or update this merge request's one siftwise note, replacing only the given
    section (the same Markdown and rules as the GitHub PR comment). Only a note written by the
    token's own user counts as ours (``GET /user``): anyone can post one with the marker.
    Returns notes for the report; never raises (a token without access is a warning)."""
    mr = _mr_url(ctx)
    try:
        with httpx.Client(headers=_headers(gitlab_token), timeout=API_TIMEOUT) as gl:
            me = gl.get(f"{ctx.api_url}/user")
            me.raise_for_status()
            user_id = me.json().get("id")
            existing: dict[str, Any] | None = None
            params: dict[str, str | int] = {"sort": "asc", "order_by": "created_at",
                                            "per_page": 100, "page": "1"}  # fmt: skip
            for _ in range(MAX_NOTE_PAGES):  # paginated: x-next-page is empty on the last
                response = gl.get(f"{mr}/notes", params=params)
                response.raise_for_status()
                existing = next((n for n in response.json() if _ours(n, user_id)), None)
                params["page"] = response.headers.get("x-next-page", "")
                if existing is not None or not params["page"]:
                    break
            text = gha.merged_comment(str(existing["body"]) if existing else "", ctx, selection,
                                      failures)  # fmt: skip
            if text is None:
                return ["A newer attempt of this pipeline already updated the comment."]
            body = {"body": note_safe(text)}
            if existing is None:
                gl.post(f"{mr}/notes", json=body).raise_for_status()
            else:
                gl.put(f"{mr}/notes/{existing['id']}", json=body).raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        hint = (f" (does {GITLAB_TOKEN_VAR} have the api scope and at least the Reporter role "
                "on the project?)" if status in (401, 403, 404) else "")  # fmt: skip
        gha._annotate("warning", f"siftwise: merge request comment failed: HTTP {status}{hint}")
        return [f"Merge request comment failed: HTTP {status}{hint}."]
    except (httpx.HTTPError, ValueError) as exc:
        gha._annotate("warning", f"siftwise: merge request comment failed: {exc}")
        return [f"Merge request comment failed: {exc}."]
    return []


def _ours(note: Mapping[str, Any], user_id: object) -> bool:
    return (not note.get("system") and user_id is not None
            and (note.get("author") or {}).get("id") == user_id
            and gha.COMMENT_MARKER in str(note.get("body") or ""))  # fmt: skip


def mr_head_sha(ctx: Context, gitlab_token: str) -> str | None:
    """The merge request's current head commit (its ``sha``,
    https://docs.gitlab.com/api/merge_requests/), or None if GitLab can't tell us."""
    try:
        response = httpx.get(_mr_url(ctx), headers=_headers(gitlab_token), timeout=API_TIMEOUT)
        if response.is_success:
            head = response.json().get("sha")
            return str(head) if head else None
    except (httpx.HTTPError, ValueError):
        pass
    return None


# --- jobs ---------------------------------------------------------------------------------------


def _preflight(provider: GitLabProvider, ctx: Context, server: str) -> str:
    """Warnings every job gives; the siftwise token, or "" when siftwise can't be used."""
    env = provider.env
    if ctx.fork and runs_in_parent(env):
        gha._annotate("warning", "siftwise: this merge request comes from a fork and runs with "
                      "this project's CI/CD variables: its code can read the siftwise and GitLab "
                      "tokens. Run fork pipelines in the parent project only for code you "
                      "trust.")  # fmt: skip
    if not server:
        gha._annotate("warning", "siftwise: SIFTWISE_SERVER is not set")
        return ""
    if insecure_server(server):
        gha._annotate("warning", f"siftwise: SIFTWISE_SERVER ({server}) is plain http: the "
                      "token crosses the network unencrypted. Use https outside the job's own "
                      "network.")  # fmt: skip
    return siftwise_token(env)


@app.command("select")
def select_job(
    server: Annotated[str, typer.Option(envvar="SIFTWISE_SERVER")] = "",
    repo: Annotated[str, typer.Option(envvar="SIFTWISE_REPO")] = "",
    base_ref: Annotated[str, typer.Option(envvar="SIFTWISE_BASE_REF")] = "",
    junit_dir: Annotated[str, typer.Option(envvar="SIFTWISE_JUNIT_DIR")] = "",
    comment: Annotated[bool, typer.Option(envvar="SIFTWISE_COMMENT")] = True,
    shadow: Annotated[str, typer.Option(envvar="SIFTWISE_SHADOW")] = "off",
    variant: Annotated[str, typer.Option(envvar="SIFTWISE_VARIANT")] = "",
) -> None:
    """Select tests for this pipeline: ``SIFTWISE_MODE`` and ``SIFTWISE_COMMAND_FILE`` (and
    the rest) in .siftwise-ci/select.env for later jobs. Tokens: SIFTWISE_TOKEN (or
    SIFTWISE_API_TOKEN) and SIFTWISE_GITLAB_TOKEN, from the environment only."""
    env = os.environ
    provider = GitLabProvider("select", env)
    with gha.use_provider(provider):
        # First: if anything below fails, later jobs still read "run the full suite".
        provider.outputs(env, {"mode": "full", "command": "", "junit-dir": JUNIT_DIR})
        if gha.git_is_shallow():
            gha._annotate("error", "siftwise needs the full git history to diff against the "
                          f"base: {provider.shallow_fix}")  # fmt: skip
            raise typer.Exit(1)
        ctx = load_context(env, base_ref)
        token = _preflight(provider, ctx, server)
        gha.run_select(ctx, server=server, token=token, repo=repo, junit_dir=junit_dir,
                       comment=comment, scm_token=env.get(GITLAB_TOKEN_VAR, ""), shadow=shadow,
                       variant=variant)  # fmt: skip


@app.command("record")
def record_job(
    server: Annotated[str, typer.Option(envvar="SIFTWISE_SERVER")] = "",
    repo: Annotated[str, typer.Option(envvar="SIFTWISE_REPO")] = "",
    base_ref: Annotated[str, typer.Option(envvar="SIFTWISE_BASE_REF")] = "",
    junit: Annotated[str, typer.Option(envvar="SIFTWISE_JUNIT_GLOB")] = "**/junit*.xml",
    junit_dir: Annotated[str, typer.Option(envvar="SIFTWISE_JUNIT_DIR")] = "",
    variant: Annotated[str, typer.Option(envvar="SIFTWISE_VARIANT")] = "",
    defer_rollup: Annotated[str, typer.Option(envvar="SIFTWISE_DEFER_ROLLUP")] = "auto",
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
    """Upload this job's JUnit reports (``**/junit*.xml`` and .siftwise-ci/junit/*.xml),
    re-run unknown failures if asked, then (unless deferred to the rollup job) triage them.
    Never fails the job, unless ``fail_on`` is set."""
    env = os.environ
    provider = GitLabProvider("record", env)
    with gha.use_provider(provider):
        gate = gha.parse_fail_on(fail_on)
        source = gha.parse_known_flaky_source(known_flaky_source)
        rerun = gha.parse_rerun(rerun_unknown, rerun_verdicts, rerun_max_tests,
                                rerun_timeout_minutes, rerun_prefix)  # fmt: skip
        ctx = load_context(env, base_ref)
        token = _preflight(provider, ctx, server)
        if (env.get("CI_JOB_RETRY_COUNT") or "0") not in ("", "0"):
            gha._annotate("warning", "siftwise: a retried job keeps its pipeline ID, and only "
                          "the pipeline's first upload of each leg is stored: run a new "
                          "pipeline to record again")  # fmt: skip
        gha.run_record(ctx, server=server, token=token, repo=repo, junit=junit,
                       junit_dir=junit_dir or JUNIT_DIR, variant=variant,
                       defer_rollup=defer_rollup, scm_token=env.get(GITLAB_TOKEN_VAR, ""),
                       triage=triage, comment=comment, gate=gate, source=source,
                       rerun=rerun)  # fmt: skip


@app.command("rollup")
def rollup_job(
    server: Annotated[str, typer.Option(envvar="SIFTWISE_SERVER")] = "",
    repo: Annotated[str, typer.Option(envvar="SIFTWISE_REPO")] = "",
    triage: Annotated[bool, typer.Option(envvar="SIFTWISE_TRIAGE")] = True,
    comment: Annotated[bool, typer.Option(envvar="SIFTWISE_COMMENT")] = True,
    fail_on: Annotated[str, typer.Option(envvar="SIFTWISE_FAIL_ON")] = "",
) -> None:
    """After every test job recorded: triage the pipeline's failures (all variants, one
    merge request note section), then recompute the repo's test stats once. Never fails,
    unless ``fail_on`` is set."""
    env = os.environ
    provider = GitLabProvider("rollup", env)
    with gha.use_provider(provider):
        gate = gha.parse_fail_on(fail_on)
        # No checkout needed: the variables say which pipeline and MR, the server the rest.
        ctx = load_context(env, has_commit=lambda _ref: False)
        token = _preflight(provider, ctx, server)
        gha.run_rollup(ctx, server=server, token=token, repo=repo,
                       scm_token=env.get(GITLAB_TOKEN_VAR, ""), triage=triage, comment=comment,
                       gate=gate)  # fmt: skip
