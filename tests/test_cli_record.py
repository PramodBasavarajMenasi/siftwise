"""`siftwise record` and `siftwise rollup` (generic CLI, no GitHub)."""

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from siftwise.cli.main import EXIT_API_ERROR, EXIT_USAGE, app

SERVER = "http://siftwise.test"


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "a.py").write_text("x = 1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "one")
    (tmp_path / "b.py").write_text("y = 1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "two")
    (tmp_path / "junit.xml").write_text("<testsuite/>")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIFTWISE_API_TOKEN", "cli-token")
    return git(tmp_path, "rev-parse", "HEAD")


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


def test_record_uploads_junit_with_metadata(checkout: str, api: respx.MockRouter) -> None:
    upload = api.post(f"{SERVER}/runs").respond(
        201, json={"run_id": 4, "created": True, "counts": {"total": 0}})  # fmt: skip

    result = CliRunner().invoke(app, [
        "record", "junit.xml", "--repo", "acme/shop", "--branch", "main", "--main",
        "--ci-run-id", "77", "--variant", "linux", "--base", "HEAD~1", "--defer-rollup",
        "--server", SERVER,
    ])  # fmt: skip

    assert result.exit_code == 0, result.output
    request = upload.calls.last.request
    assert request.url.params["defer_rollup"] == "true"
    assert request.headers["authorization"] == "Bearer cli-token"
    meta = json.loads(request.content.split(b'name="metadata"\r\n\r\n')[1].split(b"\r\n--")[0])
    assert meta == {
        "repo": "acme/shop", "commit_sha": checkout, "branch": "main", "is_main": True,
        "ci_run_id": "77", "run_attempt": 1, "variant": "linux", "started_at": None,
        "changed_files": ["b.py"], "changed_files_known": True,
        # b.py is imported by no test file: an empty but complete graph
        "affected": {"affected_packages": {}, "affected_files": {}, "depends": [],
                     "go_module": None, "generated": []},
    }  # fmt: skip


def test_record_without_a_complete_graph_sends_no_links(
    checkout: str, api: respx.MockRouter
) -> None:
    upload = api.post(f"{SERVER}/runs").respond(
        201, json={"run_id": 4, "created": True, "counts": {"total": 0}})  # fmt: skip
    args = ["record", "junit.xml", "--repo", "acme/shop", "--branch", "b", "--server", SERVER,
            "--base", "HEAD~1", "--no-py-imports"]  # fmt: skip

    assert CliRunner().invoke(app, args).exit_code == 0

    content = upload.calls.last.request.content
    meta = json.loads(content.split(b'name="metadata"\r\n\r\n')[1].split(b"\r\n--")[0])
    # Python changed but its graph was skipped: the run's relatedness is unknown.
    assert (meta["changed_files"], meta["affected"]) == (["b.py"], None)


def test_record_with_an_invalid_repo_config_still_uploads(
    checkout: str, api: respx.MockRouter
) -> None:
    Path(".siftwise.toml").write_text("[[depends]]\ntests = 1\n")
    upload = api.post(f"{SERVER}/runs").respond(
        201, json={"run_id": 4, "created": True, "counts": {"total": 0}})  # fmt: skip
    args = ["record", "junit.xml", "--repo", "acme/shop", "--branch", "b", "--server", SERVER,
            "--base", "HEAD~1"]  # fmt: skip

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, result.output
    assert "recording the run without its change links" in result.output
    assert b'"affected": null' in upload.calls.last.request.content
    assert b'"changed_files": ["b.py"]' in upload.calls.last.request.content


def test_record_without_base_has_unknown_changed_files(
    checkout: str, api: respx.MockRouter
) -> None:
    upload = api.post(f"{SERVER}/runs").respond(
        201, json={"run_id": 4, "created": True, "counts": {"total": 0}})  # fmt: skip

    args = ["record", "junit.xml", "--repo", "acme/shop", "--branch", "b", "--server", SERVER]
    assert CliRunner().invoke(app, args).exit_code == 0

    request = upload.calls.last.request
    assert b'"changed_files_known": false' in request.content
    assert b'"affected": null' in request.content
    assert "defer_rollup" not in request.url.params


def test_record_errors(checkout: str, api: respx.MockRouter) -> None:
    args = ["record", "--repo", "acme/shop", "--branch", "b", "--server", SERVER]
    assert CliRunner().invoke(app, [*args[:1], "missing.xml", *args[1:]]).exit_code == EXIT_USAGE

    api.post(f"{SERVER}/runs").mock(side_effect=[httpx.ConnectError("refused")])
    down = CliRunner().invoke(app, [*args[:1], "junit.xml", *args[1:]])
    assert down.exit_code == EXIT_API_ERROR
    assert "could not reach siftwise" in down.output


def test_rollup(checkout: str, api: respx.MockRouter) -> None:
    stats = {"repo": "acme/shop", "tests_updated": 3, "window_days": 90, "seconds": 0.5}
    api.post(f"{SERVER}/repos/acme/shop/rollup").respond(json=stats)

    result = CliRunner().invoke(app, ["rollup", "--repo", "acme/shop", "--server", SERVER])

    assert result.exit_code == 0, result.output
    assert "3 tests updated" in result.output
