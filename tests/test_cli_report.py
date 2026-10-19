"""`siftwise report`: live recall from shadow runs."""

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from siftwise.cli.main import EXIT_API_ERROR, EXIT_USAGE, app, recall_lines

SERVER = "http://siftwise.test"
URL = f"{SERVER}/repos/acme/shop/recall"
ARGS = ["report", "--repo", "acme/shop", "--server", SERVER]


def stats(runs: int, caught: int, missed: int, flaky: int = 0, **by: int) -> dict[str, Any]:
    counted = caught + missed
    return {
        "runs": runs,
        "failures": counted + flaky,
        "flaky_excluded": flaky,
        "caught": caught,
        "missed": missed,
        "recall": caught / counted if counted else None,
        "missed_by_verdict": {"new_regression": 0, "unknown": 0, "pre_existing": 0, **by},
    }


def recall(**overrides: Any) -> dict[str, Any]:
    return {
        "repo": "acme/shop",
        "days": 30,
        "since": "2026-09-19T00:00:00Z",
        "shadow_runs": 40,
        "selective_runs": 25,
        "full_runs": 15,
        "not_counted": {"cancelled": 2, "no_upload": 1},
        "in_progress": 1,
        "all": stats(40, 148, 2, flaky=3, unknown=1, pre_existing=1),
        "selective": stats(25, 48, 2, flaky=1, unknown=1, pre_existing=1),
        "time": {"total_ms": 3_600_000.0, "would_run_ms": 1_200_000.0, "saved": 2 / 3},
        "misses": [
            {
                "test_id": "tests.test_db::test_query",
                "ci_run_id": "123",
                "run_attempt": 1,
                "commit_sha": "a" * 40,
                "variants": ["linux", None],
                "verdict": "unknown",
                "selection_mode": "selective",
                "at": "2026-10-18T10:00:00Z",
            }
        ],
        "misses_truncated": False,
        **overrides,
    }


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    monkeypatch.setenv("SIFTWISE_API_TOKEN", "cli-token")
    with respx.mock(assert_all_mocked=True) as router:
        yield router


def test_report_first_line_shows_both_recalls(api: respx.MockRouter) -> None:
    route = api.get(URL).respond(json=recall())

    result = CliRunner().invoke(app, [*ARGS, "--days", "30"])

    assert result.exit_code == 0, result.output
    assert route.calls.last.request.url.params["days"] == "30"
    assert route.calls.last.request.headers["Authorization"] == "Bearer cli-token"
    lines = result.stdout.splitlines()
    assert lines[0] == (
        "Recall 98.7% (148 of 150) over 40 shadow runs; selective runs only: 96.0% (48 of 50) "
        "over 25"
    )
    assert "Flaky failures not counted: 3 (all runs), 1 (selective)" in lines
    assert ("Missed by verdict: 0 new regression, 1 unknown, 1 pre-existing (all runs); "
            "0 new regression, 1 unknown, 1 pre-existing (selective)") in lines  # fmt: skip
    assert "Time saved (measured on the shadow runs): 66.7% of 60.0 min" in lines
    assert "Full-suite shadow runs (siftwise would have run everything): 15" in lines
    assert "Not counted: 2 cancelled, 1 no upload, 1 in progress" in lines
    assert lines[-1] == ("  2026-10-18  unknown        tests.test_db::test_query  "
                         "(run 123 attempt 1, aaaaaaa, linux, -)")  # fmt: skip


def test_report_without_failures_or_shadow_runs() -> None:
    lines = recall_lines(recall(
        shadow_runs=0, selective_runs=0, full_runs=0, not_counted={}, in_progress=0,
        all=stats(0, 0, 0), selective=stats(0, 0, 0), misses=[],
        time={"total_ms": 0.0, "would_run_ms": 0.0, "saved": None},
    ))  # fmt: skip

    assert lines == [
        "Recall n/a (no failures) over 0 shadow runs; selective runs only: n/a (no failures) "
        "over 0",
        "Full-suite shadow runs (siftwise would have run everything): 0",
    ]


def test_report_json(api: respx.MockRouter) -> None:
    api.get(URL).respond(json=recall())

    result = CliRunner().invoke(app, [*ARGS, "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == recall()


def test_report_errors(api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    api.get(URL).respond(404, json={"detail": "unknown repo 'acme/shop'"})
    missing = CliRunner().invoke(app, ARGS)
    assert missing.exit_code == EXIT_API_ERROR
    assert "unknown repo" in missing.output

    api.get(URL).mock(side_effect=httpx.ConnectError("refused"))
    assert CliRunner().invoke(app, ARGS).exit_code == EXIT_API_ERROR

    assert CliRunner().invoke(app, [*ARGS, "--days", "0"]).exit_code == EXIT_USAGE
    monkeypatch.delenv("SIFTWISE_API_TOKEN")
    assert CliRunner().invoke(app, ARGS).exit_code == EXIT_USAGE
