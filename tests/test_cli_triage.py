"""`siftwise triage`: failures from JUnit, the change from git, verdicts out, --fail-on."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner, Result

from siftwise.cli.main import EXIT_API_ERROR, EXIT_FAIL_ON, EXIT_USAGE, app
from tests.test_cli_select import git, repo  # noqa: F401  (fixture)

SERVER = "http://siftwise.test"
TRIAGE_URL = f"{SERVER}/triage"

JUNIT = """<testsuite name="pytest">
  <testcase classname="tests.test_cart" name="test_total" file="tests/test_cart.py">
    <failure message="assert 2 == 1">trace</failure>
  </testcase>
  <testcase classname="tests.test_cart" name="test_retried"><failure message="x"/></testcase>
  <testcase classname="tests.test_cart" name="test_retried"/>
  <testcase classname="tests.test_db" name="test_query"><error message="db down"/></testcase>
  <testcase classname="tests.test_db" name="test_ok"/>
  <testcase classname="tests.test_db" name="test_skip"><skipped/></testcase>
</testsuite>
"""


@pytest.fixture(autouse=True)
def token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIFTWISE_API_TOKEN", "cli-token")


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


@pytest.fixture
def junit(repo: Path) -> Path:  # noqa: F811
    path = repo / "junit.xml"
    path.write_text(JUNIT)
    return path


def result(test_id: str, verdict: str, confidence: str = "high") -> dict[str, Any]:
    return {"test_id": test_id, "verdict": verdict, "confidence": confidence,
            "reasons": [f"why {verdict}"], "evidence": {}}  # fmt: skip


def response(*results: dict[str, Any]) -> dict[str, Any]:
    counts = {v: 0 for v in ("new_regression", "pre_existing", "flaky", "unknown")}
    for r in results:
        counts[r["verdict"]] += 1
    return {"repo": "acme/shop", "counts": counts, "results": list(results)}


def run(*args: str) -> Result:
    return CliRunner().invoke(
        app, ["triage", "junit.xml", "--repo", "acme/shop", "--server", SERVER, *args]
    )


@pytest.mark.usefixtures("junit")
def test_sends_final_failures_and_the_change(api: respx.MockRouter) -> None:
    route = api.post(TRIAGE_URL).respond(json=response(
        result("tests.test_cart::test_total", "new_regression"),
        result("tests.test_db::test_query", "unknown", "low"),
    ))  # fmt: skip

    out = run("--variant", "linux", "--ci-run-id", "42", "--run-attempt", "2")

    assert out.exit_code == 0, out.output
    body = json.loads(route.calls.last.request.content)
    assert body["failures"] == [
        {"test_id": "tests.test_cart::test_total", "message": "assert 2 == 1",
         "file_path": "tests/test_cart.py"},
        {"test_id": "tests.test_db::test_query", "message": "db down", "file_path": None},
    ]  # fmt: skip  # test_retried passed on its final attempt
    assert len(body["commit_sha"]) == 40
    assert (body["variant"], body["ci_run_id"], body["run_attempt"]) == ("linux", "42", 2)
    assert body["changed_files_known"] is True
    assert "src/cart.py" in body["changed_files"]
    assert "always_run" not in body and "junit_dir" not in body
    assert out.stdout.splitlines() == [
        "new_regression  high   tests.test_cart::test_total",
        "    why new_regression",
        "unknown         low    tests.test_db::test_query",
        "    why unknown",
    ]
    assert "2 failing test(s): 1 new regression, 0 pre existing, 0 flaky, 1 unknown" in out.stderr


def commit_config(repo: Path, branch: str, content: str) -> None:  # noqa: F811
    git(repo, "checkout", "-q", branch)
    (repo / ".siftwise.toml").write_text(content)
    git(repo, "add", ".siftwise.toml")
    git(repo, "commit", "-q", "-m", "config")
    git(repo, "checkout", "-q", "feature")


@pytest.mark.usefixtures("junit")
def test_known_flaky_comes_from_the_base_by_default(
    repo: Path,  # noqa: F811
    api: respx.MockRouter,
) -> None:
    commit_config(repo, "main", 'known_flaky = ["tests.test_db::*"]\n')
    # The change tries to excuse its own failure: it applies only after merge.
    (repo / ".siftwise.toml").write_text('known_flaky = ["tests.test_cart::*"]\n')
    route = api.post(TRIAGE_URL).respond(json=response())

    base = run()
    head = run("--known-flaky-source", "head")

    assert base.exit_code == head.exit_code == 0, base.output
    first, second = (json.loads(call.request.content) for call in route.calls)
    assert first["known_flaky"] == ["tests.test_db::*"]
    assert "known_flaky changes in this change apply after merge" in base.stderr
    assert second["known_flaky"] == ["tests.test_cart::*"]
    assert "apply after merge" not in head.stderr


@pytest.mark.usefixtures("junit")
def test_known_flaky_unchanged_or_unreadable_at_the_base(
    repo: Path,  # noqa: F811
    api: respx.MockRouter,
) -> None:
    route = api.post(TRIAGE_URL).respond(json=response())

    neither = run()  # no .siftwise.toml anywhere: nothing listed, nothing changed
    commit_config(repo, "main", "known_flaky = 3\n")
    invalid = run()

    assert neither.exit_code == invalid.exit_code == 0
    assert [json.loads(c.request.content)["known_flaky"] for c in route.calls] == [[], []]
    assert "apply after merge" not in neither.stderr
    assert "ignoring known_flaky at main" in invalid.stderr


@pytest.mark.usefixtures("junit")
def test_json_output(api: respx.MockRouter) -> None:
    payload = response(result("tests.test_cart::test_total", "flaky"))
    api.post(TRIAGE_URL).respond(json=payload)

    out = run("--json")

    assert out.exit_code == 0
    assert json.loads(out.stdout) == payload


@pytest.mark.usefixtures("junit")
@pytest.mark.parametrize(
    ("fail_on", "code"),
    [("new_regression", EXIT_FAIL_ON), ("new_regression,unknown", EXIT_FAIL_ON),
     ("pre_existing", 0), ("", 0)],
)  # fmt: skip
def test_fail_on(api: respx.MockRouter, fail_on: str, code: int) -> None:
    api.post(TRIAGE_URL).respond(json=response(
        result("tests.test_cart::test_total", "new_regression"),
        result("tests.test_db::test_query", "flaky"),
    ))  # fmt: skip

    out = run("--fail-on", fail_on)

    assert out.exit_code == code, out.output
    assert ("--fail-on matched" in out.stderr) is (code == EXIT_FAIL_ON)


@pytest.mark.usefixtures("junit")
def test_usage_errors(api: respx.MockRouter) -> None:
    bad_verdict = run("--fail-on", "new_regression,broken")
    assert bad_verdict.exit_code == EXIT_USAGE
    assert "unknown --fail-on verdict(s): broken" in bad_verdict.stderr

    Path("junit.xml").write_text("<html/>")
    assert run().exit_code == EXIT_USAGE
    missing = CliRunner().invoke(app, ["triage", "nope.xml", "--repo", "r", "--server", SERVER])
    assert missing.exit_code == EXIT_USAGE


def test_no_failures_never_calls_the_api(repo: Path, api: respx.MockRouter) -> None:  # noqa: F811
    (repo / "junit.xml").write_text('<testsuite><testcase classname="a" name="b"/></testsuite>')
    route = api.post(TRIAGE_URL)

    out = run("--json")

    assert out.exit_code == 0
    assert json.loads(out.stdout)["results"] == []
    assert "no failing tests" in out.stderr
    assert not route.called


@pytest.mark.usefixtures("junit")
def test_api_errors_exit_1(api: respx.MockRouter) -> None:
    api.post(TRIAGE_URL).mock(side_effect=[httpx.Response(404, json={"detail": "unknown repo"})])

    out = run()

    assert out.exit_code == EXIT_API_ERROR
    assert "unknown repo" in out.stderr
