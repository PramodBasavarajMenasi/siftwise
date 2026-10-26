"""examples/gitlab: history within one pipeline (ci/history.py), and the merge request pipeline's
siftwise-base job, which records the MR's base commit as a main run because the example keeps
no history between pipelines. The flow test runs the example's own project and tests."""

import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from scripts import e2e_report
from siftwise.cli import gitlab
from siftwise.cli.main import app
from tests.test_gha import git, recorded_runs
from tests.test_gitlab import JOB_TOKEN, SERVER, load_yaml, mr_env

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "gitlab"
TOKEN = "sift-EXAMPLE-SECRET-0123456789"
SECRETS = (TOKEN, JOB_TOKEN)
BULK = "tests.test_cart::test_bulk_discount"


def load_history() -> ModuleType:
    path = EXAMPLE / "ci" / "history.py"
    spec = importlib.util.spec_from_file_location("gitlab_example_history", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    writes, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no __pycache__ in examples/
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = writes
    return module


history = load_history()


def base_env(**overrides: str) -> dict[str, str]:
    return {
        "CI_MERGE_REQUEST_DIFF_BASE_SHA": "b" * 40,
        "CI_MERGE_REQUEST_PROJECT_PATH": "acme/shop",
        "CI_PROJECT_PATH": "someone/shop",
        "CI_DEFAULT_BRANCH": "main",
        "CI_PIPELINE_ID": "555",
        "CI_PIPELINE_CREATED_AT": "2026-10-26T10:00:00Z",
        **overrides,
    }


# --- record-base ---------------------------------------------------------------------------


def test_the_base_run_starts_at_its_commit_and_always_before_the_pipeline() -> None:
    created = "2026-10-26T10:00:00Z"
    assert history.base_started_at("2026-10-25T08:00:00+02:00", created) == (
        "2026-10-25T08:00:00+02:00")  # fmt: skip
    # A committer clock ahead of GitLab's: still before this pipeline's run, or triage
    # (which never reads runs after the one it triages) wouldn't see it.
    assert history.base_started_at("2026-10-26T12:00:00+00:00", created) == (
        "2026-10-26T09:59:59+00:00")  # fmt: skip
    assert history.base_started_at("2026-10-26T12:00:00+00:00", None) == (
        "2026-10-26T12:00:00+00:00")  # fmt: skip


def test_the_base_is_recorded_as_a_main_run_of_its_own() -> None:
    args = history.base_record_args(base_env(), "base.xml", "2026-10-25T08:00:00+00:00")

    assert args == [
        "record", "base.xml",
        "--repo", "acme/shop",  # the MR's target project, as the gitlab commands name it
        "--branch", "main",
        "--commit", "b" * 40,
        "--main",
        "--ci-run-id", "base-of-555",  # never the pipeline's own ID: the test job records that
        "--started-at", "2026-10-25T08:00:00+00:00",
    ]  # fmt: skip
    env = base_env(CI_MERGE_REQUEST_PROJECT_PATH="")
    plain = history.base_record_args(env, "b.xml", "2026-10-25T08:00:00+00:00")
    assert plain[plain.index("--repo") + 1] == "someone/shop"


def test_record_base_needs_a_base_and_a_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(history.subprocess, "run", lambda args, **_: calls.append(args))

    history.record_base(str(tmp_path / "full.xml"))  # no CI_MERGE_REQUEST_DIFF_BASE_SHA
    assert "no CI_MERGE_REQUEST_DIFF_BASE_SHA" in capsys.readouterr().out

    monkeypatch.setenv("CI_MERGE_REQUEST_DIFF_BASE_SHA", "b" * 40)
    history.record_base(str(tmp_path / "full.xml"))
    assert "wrote no" in capsys.readouterr().out
    assert calls == []


def test_record_base_records_the_base_commit_with_its_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "Test")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-10-20T08:00:00+00:00")
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    monkeypatch.chdir(tmp_path)
    for name, value in base_env(CI_MERGE_REQUEST_DIFF_BASE_SHA=base).items():
        monkeypatch.setenv(name, value)
    Path("full.xml").write_text("<testsuite/>")
    real_run, siftwise_calls = subprocess.run, list[list[str]]()

    def run(args: list[str], **kwargs: Any) -> Any:
        if args[0] == "siftwise":
            siftwise_calls.append(args)
            return None
        return real_run(args, **kwargs)

    monkeypatch.setattr(history.subprocess, "run", run)

    history.record_base("full.xml")

    [args] = siftwise_calls
    assert args[1:] == history.base_record_args(os.environ, "full.xml", "2026-10-20T08:00:00+00:00")
    assert f"recording {base[:12]} (main) as a main run" in capsys.readouterr().out


# --- the example's pipeline ----------------------------------------------------------------


def test_merge_request_pipelines_seed_their_history_from_the_base_commit() -> None:
    pipeline = load_yaml(EXAMPLE / ".gitlab-ci.yml")
    stages = pipeline["stages"]
    job = pipeline["siftwise-base"]

    assert stages.index(job["stage"]) < stages.index(pipeline["siftwise-select"]["stage"])
    assert stages.index(pipeline["siftwise-select"]["stage"]) < stages.index("test")
    assert job["rules"] == [{"if": '$CI_PIPELINE_SOURCE == "merge_request_event" '
                                  "&& $CI_MERGE_REQUEST_DIFF_BASE_SHA"}]  # fmt: skip
    assert job["allow_failure"] is True and job["variables"]["GIT_DEPTH"] == "0"
    script = "\n".join(job["script"])
    assert '"$CI_MERGE_REQUEST_DIFF_BASE_SHA"' in script
    [report] = re.findall(r"--junitxml=(\S+?)\)", script)
    assert f"python ci/history.py record-base {report}" in script
    assert script.index("record-base") < script.index("history.py dump")
    # Only the history goes on: the base's JUnit must not be uploaded as this pipeline's run.
    assert job["artifacts"]["paths"] == [".siftwise-history/"]
    assert not report.startswith(".siftwise-ci")


def test_no_history_comes_from_another_pipeline() -> None:
    """The cross-pipeline artifact download is gone: on gitlab.com Free, CI_JOB_TOKEN got
    "404 Project Not Found" from jobs/artifacts/:ref/download (2026-10-26)."""
    for path in (EXAMPLE / "ci" / "history.py", EXAMPLE / ".gitlab-ci.yml"):
        text = path.read_text(encoding="utf-8")
        assert "CI_JOB_TOKEN" not in text and "jobs/artifacts" not in text, path


# --- the merge request flow, for real ------------------------------------------------------


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    from siftwise.api.main import create_app
    from siftwise.config import Settings
    from siftwise.db import get_session

    api = create_app(Settings(_env_file=None, api_token=TOKEN, stats_window_days=30))
    api.dependency_overrides[get_session] = lambda: db_session
    with TestClient(api) as test_client:
        yield test_client


def forward_to(client: TestClient) -> Callable[[httpx.Request], httpx.Response]:
    """The CLI's requests to SERVER, answered by the real API."""

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() in ("authorization", "content-type")}  # fmt: skip
        answer = client.request(request.method, request.url.raw_path.decode(),
                                content=request.content, headers=headers)  # fmt: skip
        content_type = answer.headers.get("content-type", "")
        return httpx.Response(answer.status_code, content=answer.content,
                              headers={"content-type": content_type})  # fmt: skip

    return handler


def pytest_run(cwd: Path, *args: str) -> None:
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
                   cwd=cwd, capture_output=True, check=False)  # fmt: skip


@pytest.fixture
def example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The example's project pushed as main, then the README's merge request: a bigger bulk
    discount, which breaks test_bulk_discount."""
    root = tmp_path / "shop"
    root.mkdir()
    for name in ("shop", "tests"):
        shutil.copytree(EXAMPLE / name, root / name, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(EXAMPLE / "pyproject.toml", root)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "siftwise GitLab example")
    base = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", "-b", "break-discount")
    cart = root / "shop" / "cart.py"
    cart.write_text(cart.read_text().replace("cents * 9 // 10", "cents * 8 // 10"))
    git(root, "commit", "-q", "-am", "Bigger bulk discount")
    monkeypatch.chdir(root)
    return {"base": base, "head": git(root, "rev-parse", "HEAD")}


@pytest.mark.parametrize("seeded", [True, False])
def test_a_merge_request_pipeline_with_and_without_the_base_run(
    example: dict[str, str], client: TestClient, tmp_path: Path, seeded: bool
) -> None:
    """With siftwise-base, the README's merge request runs only test_cart.py and its failure is
    a new regression. Without it (what gitlab.com showed when the download failed): the full
    suite, and unknown."""
    root = Path.cwd()
    env: dict[str, str | None] = {
        **mr_env(example, SIFTWISE_TOKEN=TOKEN, SIFTWISE_GITLAB_TOKEN=None),
        "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME": "break-discount",
        "SIFTWISE_API_TOKEN": TOKEN,
    }
    outputs = []
    with respx.mock(assert_all_called=False) as api:
        uploads = api.post(f"{SERVER}/runs").mock(side_effect=forward_to(client))
        api.route(host="siftwise").mock(side_effect=forward_to(client))

        if seeded:  # siftwise-base: the full suite on a worktree of the base, recorded as main
            worktree = tmp_path / "siftwise-base"
            git(root, "worktree", "add", "-q", "--detach", str(worktree), example["base"])
            report = tmp_path / "siftwise-base-junit" / "full.xml"
            pytest_run(worktree, f"--junitxml={report}")
            committed = git(root, "show", "-s", "--format=%cI", example["base"])
            args = history.base_record_args({k: v for k, v in env.items() if v}, str(report),
                                            committed)  # fmt: skip
            result = CliRunner().invoke(app, args, env=env)
            assert result.exit_code == 0, result.output
            outputs.append(result.output)

        result = CliRunner().invoke(app, ["gitlab", "select"], env=env)
        assert result.exit_code == 0, result.output
        outputs.append(result.output)
        selected = dict(line.split("=", 1) for line in
                        (gitlab.OUT_DIR / "select.env").read_text().splitlines())  # fmt: skip
        if selected["SIFTWISE_MODE"] == "selective":
            command = Path(selected["SIFTWISE_COMMAND_FILE"]).read_text().strip()
            assert shlex.split(command)[0] == "pytest"
            pytest_run(root, *shlex.split(command)[1:])
        else:
            pytest_run(root, f"--junitxml={gitlab.JUNIT_DIR}/full.xml")

        record_env = {**env, "SIFTWISE_DEFER_ROLLUP": "true", "SIFTWISE_TRIAGE": "false"}
        result = CliRunner().invoke(app, ["gitlab", "record"], env=record_env)
        assert result.exit_code == 0, result.output
        outputs.append(result.output)
        rollup_env = {**env, "SIFTWISE_FAIL_ON": "new_regression,unknown"}
        result = CliRunner().invoke(app, ["gitlab", "rollup"], env=rollup_env)
        outputs.append(result.output)

    report_md = (gitlab.OUT_DIR / "failures.md").read_text(encoding="utf-8")
    runs = recorded_runs(uploads)
    if seeded:
        assert (runs[0]["commit_sha"], runs[0]["branch"], runs[0]["is_main"]) == (
            example["base"], "main", True)  # fmt: skip
        assert runs[0]["ci_run_id"] != runs[1]["ci_run_id"] == "555"
        assert selected["SIFTWISE_MODE"] == "selective"
        assert "tests/test_cart.py" in command and "test_prices" not in command
        # High: test_cart.py maps to the changed shop/cart.py, and main passed it.
        verdict = ("new_regression", "high")
    else:
        assert selected["SIFTWISE_MODE"] == "full"
        verdict = ("unknown", "low")
    assert e2e_report.has_verdict(report_md, BULK, *verdict), e2e_report.rows_for(report_md, BULK)
    assert result.exit_code == 1  # fail-on either way: the gate works
    for text in (*outputs, report_md):
        for secret in SECRETS:
            assert secret not in text
