"""``siftwise report``: live recall from shadow runs."""

import csv
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
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        # A server older than 0.4 by default (/healthz without a version): no CI numbers.
        router.get(url__regex=r"/healthz$").respond(json={"status": "ok"})
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
    assert (
        "Missed by verdict: 0 new regression, 1 unknown, 1 pre-existing (all runs); "
        "0 new regression, 1 unknown, 1 pre-existing (selective)"
    ) in lines
    assert "Time saved (measured on the shadow runs): 66.7% of 60.0 min" in lines
    assert "Full-suite shadow runs (siftwise would have run everything): 15" in lines
    assert "Not counted: 2 cancelled, 1 no upload, 1 in progress" in lines


def test_report_shows_causes_a_selector_warning_and_suggestions() -> None:
    lines = recall_lines(recall())

    assert (
        "Missed by cause: 1 config gap, 1 unknown cause (all runs); 1 config gap, "
        "1 unknown cause (selective)"
    ) in lines
    assert (
        "Warning: 1 miss linked to the change but not selected: the selector may not have "
        "known the test at select time. This may be a selector bug."
    ) in lines
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
    rules = lines.index(
        "Suggested .siftwise.toml rules (review before adding; siftwise never applies them):"
    )
    assert lines[rules + 1 :] == [
        "",
        "# would have caught 1 miss: tests.test_db::test_query",
        "[[depends]]",
        'tests = "tests/test_db.py"',
        'on = ["tests/data/**"]',
    ]


def test_report_without_causes_or_suggestions() -> None:
    # Misses compared before causes existed, from a server without suggestions.
    old = recall(
        all=stats(1, 0, 1, causes={"unclassified": 1}),
        selective=stats(1, 0, 1, causes={"unclassified": 1}),
        misses=[miss(cause=None, cause_detail=None, suggestion=None)],
        suggestions=[],
        linked_not_selected=0,
    )
    lines = recall_lines(old)

    assert any(line.startswith("Missed by cause: 1 unclassified") for line in lines)
    assert lines[-1] == "      unclassified (compared before causes existed)"
    assert not any(line.startswith(("Warning", "Suggested")) for line in lines)


def test_report_without_failures_or_shadow_runs() -> None:
    lines = recall_lines(
        recall(
            shadow_runs=0,
            selective_runs=0,
            full_runs=0,
            not_counted={},
            in_progress=0,
            all=stats(0, 0, 0),
            selective=stats(0, 0, 0),
            misses=[],
            suggestions=[],
            linked_not_selected=0,
            time={"total_ms": 0.0, "would_run_ms": 0.0, "saved": None},
        )
    )

    assert lines == [
        "Recall n/a (no failures) over 0 shadow runs; selective runs only: n/a (no failures) "
        "over 0",
        "Full-suite shadow runs (siftwise would have run everything): 0",
    ]


def period(
    start: str,
    every: dict[str, Any],
    selective: dict[str, Any],
    saved: float | None,
    full: int = 0,
) -> dict[str, Any]:
    return {
        "start": f"{start}T00:00:00Z",
        "end": "…",
        "shadow_runs": every["runs"],
        "selective_runs": selective["runs"],
        "full_runs": full,
        "all": every,
        "selective": selective,
        "time": {"total_ms": 1.0, "would_run_ms": 1.0, "saved": saved},
    }


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


def test_report_csv(api: respx.MockRouter) -> None:
    api.get(URL).respond(json=recall())

    result = CliRunner().invoke(app, [*ARGS, "--csv"])

    assert result.exit_code == 0, result.output
    assert list(csv.reader(result.stdout.splitlines())) == [
        ["runs", "selective_runs", "caught", "missed", "time_saved_min"],
        ["40", "25", "148", "2", "40.0"],
    ]


def test_report_csv_by_week(api: respx.MockRouter) -> None:
    first_week = {
        **WEEKS[0],
        "time": {
            "total_ms": 3_600_000.0,
            "would_run_ms": 1_200_000.0,
            "saved": 2 / 3,
        },
    }
    api.get(URL).respond(json=recall(periods=[first_week, WEEKS[1]]))

    result = CliRunner().invoke(app, [*ARGS, "--csv", "--by", "week"])

    assert result.exit_code == 0, result.output
    assert list(csv.reader(result.stdout.splitlines())) == [
        ["week", "runs", "selective_runs", "caught", "missed", "time_saved_min"],
        ["2026-10-05", "3", "2", "49", "1", "40.0"],
        ["2026-10-12", "0", "0", "0", "0", ""],
    ]


def test_report_csv_rounds_time_saved_to_one_decimal(
    api: respx.MockRouter,
) -> None:
    data = recall(
        time={
            "total_ms": 7_000.0,
            "would_run_ms": 0.0,
            "saved": 1.0,
        }
    )
    api.get(URL).respond(json=data)

    result = CliRunner().invoke(app, [*ARGS, "--csv"])

    assert result.exit_code == 0, result.output
    rows = list(csv.reader(result.stdout.splitlines()))
    assert rows[1][-1] == "0.1"


def test_report_csv_and_json_are_mutually_exclusive() -> None:
    result = CliRunner().invoke(app, [*ARGS, "--csv", "--json"])

    assert result.exit_code == EXIT_USAGE
    assert "--csv and --json cannot be used together" in result.output


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


def test_server_comes_from_siftwise_server(
    api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Like the MCP server and the Action's steps: --server, else SIFTWISE_SERVER, else
    # http://localhost:8000.
    args = ["report", "--repo", "acme/shop"]
    default = api.get("http://localhost:8000/repos/acme/shop/recall").respond(json=recall())
    from_env = api.get("http://siftwise.internal:9000/repos/acme/shop/recall").respond(
        json=recall()
    )
    flag = api.get(URL).respond(json=recall())

    assert CliRunner().invoke(app, args).exit_code == 0
    monkeypatch.setenv("SIFTWISE_SERVER", "http://siftwise.internal:9000")
    assert CliRunner().invoke(app, args).exit_code == 0
    assert CliRunner().invoke(app, [*args, "--server", SERVER]).exit_code == 0

    assert default.call_count == 1
    assert from_env.call_count == 1
    assert flag.call_count == 1  # the flag wins over SIFTWISE_SERVER


def test_budget_misses_are_counted_apart_never_hidden() -> None:
    every = {
        **stats(10, 8, 2, causes={"budget": 1, "no_link": 1}),
        "budget_limited_runs": 3,
        "missed_by_budget": 1,
    }
    lines = recall_lines(
        recall(
            all=every,
            misses=[
                miss(cause="budget", cause_detail="left out by the 5m time budget", suggestion=None)
            ],
        )
    )
    assert lines[0].startswith("Recall 80.0% (8 of 10) over 10 shadow runs")
    assert (
        "Budget-limited runs: 3; misses a time budget left out: 1 (counted in the recall above)"
    ) in lines
    assert any("1 left out by a time budget" in line for line in lines)
    assert "      left out by a time budget: left out by the 5m time budget" in lines


def test_no_budget_line_without_budget_runs() -> None:
    assert not any(line.startswith("Budget-limited") for line in recall_lines(recall()))


def ci_result(branch: str, **overrides: Any) -> dict[str, Any]:
    """A GET /repos/{repo}/ci response."""
    return {
        "repo": "acme/shop",
        "days": 30,
        "branch": branch,
        "since": "2026-10-22T00:00:00Z",
        "until": "2026-11-21T00:00:00Z",
        "counting": "each CI run attempt counts once",
        "runs": 23,
        "recorded": 20,
        "shadow_runs": 2,
        "selective_runs": 9,
        "full_runs": 9,
        "full_reasons": {"build_file": 6, "unmapped_file": 3},
        "share_selected": {"median": 0.125, "runs": 9},
        "time_saved": {
            "sum_ms": 7_200_000.0,
            "median_ms": 600_000.0,
            "runs": 8,
            "left_out": 1,
            "legs": 10,
            "legs_left_out": 2,
            "baseline_ms": 9_000_000.0,
        },
        "shadow": {
            "runs": 2,
            "compared": 2,
            "measured_ms": 1_200_000.0,
            "would_save_ms": 900_000.0,
        },
        "failures": {
            "runs": 7,
            "runs_failing": 3,
            "failing": 5,
            "in_selection": 4,
            "outside": 1,
            "ranked": 4,
            "median_rank": 2.5,
            "first_rank_runs": 3,
            "median_first_rank": 1,
        },
        "recall_note": "...",
        **overrides,
    }


def test_report_adds_the_ci_numbers_from_a_04_server(api: respx.MockRouter) -> None:
    api.get(url__regex=r"/healthz$").respond(json={"status": "ok", "version": "0.4.0"})
    api.get(URL).respond(json=recall())
    ci = api.get(f"{SERVER}/repos/acme/shop/ci")
    ci.side_effect = [
        httpx.Response(200, json=ci_result("main")),
        httpx.Response(200, json=ci_result("prs", runs=4, recorded=0)),
    ]

    result = CliRunner().invoke(app, [*ARGS, "--days", "30"])

    assert result.exit_code == 0, result.output
    assert [dict(c.request.url.params) for c in ci.calls] == [
        {"days": "30", "branch": "main"},
        {"days": "30", "branch": "prs"},
    ]
    out = result.stdout
    assert "  main: selections recorded for 20 of 23 CI runs" in out
    assert (
        "selective 50% (9 of 18) runs; full suite 9 (build_file 6, unmapped_file 3); "
        "shadow runs 2 (ran the full suite, counted apart)"
    ) in out
    assert "median share of tests selected: 12.5% (9 selective runs)" in out
    assert (
        "test time saved (estimated, sums of test durations, not wall-clock CI time): "
        "2.0 h based on 8 of 9 selective runs (2 legs left out: no baseline yet or nothing "
        "measured), median 10.0 min a run"
    ) in out
    assert "shadow runs would have saved 15.0 min of 20.0 min measured (2 runs; not in" in out
    assert (
        "5 failing tests, 4 in the selection, 1 ran outside it; median rank 2.5 (4 "
        "ranked), first failure median rank 1 (3 runs)"
    ) in out
    # An empty window says so: never 0% or 0 min.
    lines = out.splitlines()
    empty = lines.index(
        "  pull requests and other branches: selections recorded for 0 of 4 CI runs"
    )
    assert lines[empty + 1] == "    no recorded selections in this window"
    assert "Recall comes only from shadow runs" in out


def test_report_against_an_older_server_says_there_are_no_ci_numbers(
    api: respx.MockRouter,
) -> None:
    api.get(URL).respond(json=recall())
    result = CliRunner().invoke(app, ARGS)
    assert result.exit_code == 0
    assert "CI numbers: the server is older than 0.4" in result.stdout


@pytest.mark.parametrize(
    ("n", "of", "expected"),
    [
        (9, 23, "39% (9 of 23)"),
        (2043, 2044, "99% (2,043 of 2,044)"),
        (0, 5, "0% (0 of 5)"),
        (1, 0, "n/a"),
    ],
)
def test_a_share_shows_its_counts_and_never_rounds_up_to_all(
    n: int, of: int, expected: str
) -> None:
    from siftwise.cli.main import ci_pct

    assert ci_pct(n, of) == expected
