"""`siftwise report`: live recall from shadow runs."""

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from siftwise.cli.main import EXIT_API_ERROR, EXIT_USAGE, app, recall_lines, weekly_lines

SERVER = "http://siftwise.test"
URL = f"{SERVER}/repos/acme/shop/recall"
ARGS = ["report", "--repo", "acme/shop", "--server", SERVER]
CAUSES = ("new_test", "flaky_history", "config_gap", "no_link", "unknown")
RULE = '[[depends]]\ntests = "tests/test_db.py"\non = ["tests/data/**"]'


def stats(
    runs: int,
    caught: int,
    missed: int,
    flaky: int = 0,
    causes: dict[str, int] | None = None,
    **by: int,
) -> dict[str, Any]:
    counted = caught + missed
    return {
        "runs": runs,
        "failures": counted + flaky,
        "flaky_excluded": flaky,
        "caught": caught,
        "missed": missed,
        "recall": caught / counted if counted else None,
        "missed_by_verdict": {"new_regression": 0, "unknown": 0, "pre_existing": 0, **by},
        "missed_by_cause": {**dict.fromkeys(CAUSES, 0), **(causes or {})},
    }


def miss(**overrides: Any) -> dict[str, Any]:
    return {
        "test_id": "tests.test_db::test_query",
        "ci_run_id": "123",
        "run_attempt": 1,
        "commit_sha": "a" * 40,
        "variants": ["linux", None],
        "verdict": "unknown",
        "selection_mode": "selective",
        "at": "2026-10-18T10:00:00Z",
        "cause": "config_gap",
        "cause_detail": "non-code file(s) changed in the test's directory, which no import "
        "graph follows: tests/data/db.json",
        "suggestion": RULE,
        **overrides,
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
        "all": stats(
            40, 148, 2, flaky=3, causes={"config_gap": 1, "unknown": 1}, unknown=1, pre_existing=1
        ),
        "selective": stats(
            25, 48, 2, flaky=1, causes={"config_gap": 1, "unknown": 1}, unknown=1, pre_existing=1
        ),
        "time": {"total_ms": 3_600_000.0, "would_run_ms": 1_200_000.0, "saved": 2 / 3},
        "misses": [
            miss(),
            miss(
                test_id="tests.test_cart::test_total",
                verdict="pre_existing",
                cause="unknown",
                cause_detail="linked to the change but not selected (selector didn't know the "
                "test at select time?)",
                suggestion=None,
                at="2026-10-17T09:00:00Z",
            ),
        ],
        "misses_truncated": False,
        "suggestions": [{"rule": RULE, "misses": 1, "test_ids": ["tests.test_db::test_query"]}],
        "linked_not_selected": 1,
        "periods": None,
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
    assert "by" not in route.calls.last.request.url.params
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


def test_report_shows_causes_a_selector_warning_and_suggestions() -> None:
    lines = recall_lines(recall())

    assert ("Missed by cause: 1 config gap, 1 unknown cause (all runs); 1 config gap, "
            "1 unknown cause (selective)") in lines  # fmt: skip
    assert ("Warning: 1 miss linked to the change but not selected: the selector may not have "
            "known the test at select time. This may be a selector bug.") in lines  # fmt: skip
    first = lines.index("Misses, newest first:")
    assert lines[first + 1 : first + 5] == [
        "  2026-10-18  unknown        tests.test_db::test_query  (run 123 attempt 1, aaaaaaa, "
        "linux, -)",
        "      config gap: non-code file(s) changed in the test's directory, which no import "
        "graph follows: tests/data/db.json",
        "  2026-10-17  pre_existing   tests.test_cart::test_total  (run 123 attempt 1, "
        "aaaaaaa, linux, -)",
        "      unknown cause: linked to the change but not selected (selector didn't know the "
        "test at select time?)",
    ]
    rules = lines.index("Suggested .siftwise.toml rules (review before adding; siftwise never "
                        "applies them):")  # fmt: skip
    assert lines[rules + 1 :] == [
        "",
        "# would have caught 1 miss: tests.test_db::test_query",
        "[[depends]]",
        'tests = "tests/test_db.py"',
        'on = ["tests/data/**"]',
    ]


def test_report_without_causes_or_suggestions() -> None:
    # Misses compared before causes existed, from a server without suggestions.
    old = recall(all=stats(1, 0, 1, causes={"unclassified": 1}),
                 selective=stats(1, 0, 1, causes={"unclassified": 1}),
                 misses=[miss(cause=None, cause_detail=None, suggestion=None)], suggestions=[],
                 linked_not_selected=0)  # fmt: skip
    lines = recall_lines(old)

    assert any(line.startswith("Missed by cause: 1 unclassified") for line in lines)
    assert lines[-1] == "      unclassified (compared before causes existed)"
    assert not any(line.startswith(("Warning", "Suggested")) for line in lines)


def test_report_without_failures_or_shadow_runs() -> None:
    lines = recall_lines(recall(
        shadow_runs=0, selective_runs=0, full_runs=0, not_counted={}, in_progress=0,
        all=stats(0, 0, 0), selective=stats(0, 0, 0), misses=[], suggestions=[],
        linked_not_selected=0, time={"total_ms": 0.0, "would_run_ms": 0.0, "saved": None},
    ))  # fmt: skip

    assert lines == [
        "Recall n/a (no failures) over 0 shadow runs; selective runs only: n/a (no failures) "
        "over 0",
        "Full-suite shadow runs (siftwise would have run everything): 0",
    ]


def period(start: str, every: dict[str, Any], selective: dict[str, Any], saved: float | None,
           full: int = 0) -> dict[str, Any]:  # fmt: skip
    return {"start": f"{start}T00:00:00Z", "end": "…", "shadow_runs": every["runs"],
            "selective_runs": selective["runs"], "full_runs": full, "all": every,
            "selective": selective,
            "time": {"total_ms": 1.0, "would_run_ms": 1.0, "saved": saved}}  # fmt: skip


WEEKS = [
    period("2026-10-05", stats(3, 49, 1), stats(2, 19, 1), 0.41, full=1),
    period("2026-10-12", stats(0, 0, 0), stats(0, 0, 0), None),
]


def test_report_by_week(api: respx.MockRouter) -> None:
    route = api.get(URL).respond(json=recall(periods=WEEKS))

    result = CliRunner().invoke(app, [*ARGS, "--by", "week"])

    assert result.exit_code == 0, result.output
    assert route.calls.last.request.url.params["by"] == "week"
    assert result.stdout.splitlines() == [
        "Recall 98.7% (148 of 150) over 40 shadow runs; selective runs only: 96.0% (48 of 50) "
        "over 25",
        "",
        "Week of     Runs  Recall (all)        Selective  Recall (selective)  Caught  Missed  "
        "Time saved",
        "2026-10-05     3  98.0% (49 of 50)            2  95.0% (19 of 20)        49       1  "
        "       41%",
        "2026-10-12     0  n/a (no failures)           0  n/a (no failures)        0       0  "
        "         -",
    ]
    assert weekly_lines(recall(periods=WEEKS)) == result.stdout.splitlines()


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
    by_month = CliRunner().invoke(app, [*ARGS, "--by", "month"])
    assert by_month.exit_code == EXIT_USAGE and "--by must be 'week'" in by_month.output
    monkeypatch.delenv("SIFTWISE_API_TOKEN")
    assert CliRunner().invoke(app, ARGS).exit_code == EXIT_USAGE
