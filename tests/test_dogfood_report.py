"""scripts/dogfood_report.py (summaries of the dogfood workflow's shadow runs, and the report over
them) and .github/workflows/dogfood.yml's shape. No network: GitHub and siftwise are mocked."""

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml  # type: ignore[import-untyped]  # PyYAML, through uvicorn[standard]

from scripts import dogfood_report as dogfood
from scripts.eval._github import client
from siftwise.core.shadow import LINKED_NOT_SELECTED

ROOT = Path(__file__).resolve().parents[1]
REPO = "acme/shop"
SINCE = datetime(2026, 10, 26, tzinfo=UTC)  # a Monday
UNTIL = datetime(2026, 11, 9, tzinfo=UTC)
TOKEN = "dogfood-SECRET-0123456789abcdef"


def compare(
    run_id: str,
    *,
    mode: str = "selective",
    caught: int = 0,
    missed: list[dict[str, Any]] | None = None,
    flaky: int = 0,
    ms: tuple[float, float] = (1000.0, 400.0),
    counted: bool = True,
    status: str = "compared",
) -> dict[str, Any]:
    """A POST /repos/{repo}/shadow response, as the server sends it."""
    missed = missed or []
    by_verdict: dict[str, int] = {}
    for miss in missed:
        by_verdict[miss["verdict"]] = by_verdict.get(miss["verdict"], 0) + 1
    return {
        "repo": REPO,
        "ci_run_id": run_id,
        "run_attempt": 1,
        "shadow": True,
        "counted": counted,
        "mode": mode if counted else None,
        "selections": [
            {
                "variant": "py3.12",
                "commit_sha": "a" * 40,
                "mode": mode,
                "reason": "",
                "selected_count": 3,
                "total_known": 10,
                "estimated_ms": {"total": 1.0, "would_run": 1.0},
                "status": status,
            }
        ],
        "variants": [
            {
                "variant": "py3.12",
                "run_id": 1,
                "status": "compared",
                "failing": caught + len(missed) + flaky,
                "caught": caught,
                "missed": len(missed),
                "flaky": flaky,
                "tests_total": 10,
                "tests_would_run": 3,
                "ms_total": ms[0],
                "ms_would_run": ms[1],
            }
        ]
        if counted
        else [],
        "failing_tests": caught + len(missed) + flaky,
        "caught": caught,
        "missed": len(missed),
        "flaky_excluded": flaky,
        "missed_by_verdict": by_verdict,
        "missed_tests": missed,
        "time": {"total_ms": ms[0], "would_run_ms": ms[1], "saved": 1 - ms[1] / ms[0]},
    }


def miss(
    test_id: str, cause: str = "new_test", verdict: str = "unknown", detail: str = ""
) -> dict[str, Any]:
    return {
        "test_id": test_id,
        "verdict": verdict,
        "variants": ["py3.12"],
        "cause": cause,
        "cause_detail": detail,
    }


def summary(
    run_id: str,
    at: str = "2026-10-27T10:00:00Z",
    *,
    attempt: int = 1,
    shadow: dict[str, Any] | None = None,
    fork: bool = False,
    counts: dict[str, int] | None = None,
    problems: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema": dogfood.SCHEMA,
        "repo": REPO,
        "run_id": run_id,
        "run_attempt": attempt,
        "event": "pull_request",
        "pr_number": 7,
        "fork": fork,
        "branch": "feature",
        "is_main": False,
        "commit_sha": "b" * 40,
        "at": at,
        "shadow": shadow,
        "triage": {"failing_tests": 1, "counts": counts or {}, "results": []},
        "problems": problems or [],
    }


def report(summaries: list[dict[str, Any]], **kwargs: Any) -> tuple[dict[str, Any], str]:
    result = dogfood.aggregate(summaries, REPO, SINCE, UNTIL, **kwargs)
    return result, "\n".join(dogfood.report_lines(result, by_week=kwargs.get("by_week", False)))


# --- the report -----------------------------------------------------------------------------


def test_recall_time_saved_and_misses_add_up_like_the_servers_recall() -> None:
    runs = [
        summary("1", shadow=compare("1", caught=2, missed=[miss("t::new")])),
        summary(
            "2",
            "2026-10-28T10:00:00Z",
            shadow=compare("2", mode="full", caught=1, ms=(2000.0, 2000.0)),
        ),
        summary(
            "3",
            "2026-10-29T10:00:00Z",
            shadow=compare("3", flaky=1),
            counts={"flaky": 1, "new_regression": 2},
        ),
    ]

    result, text = report(runs)

    every, selective = result["all"], result["selective"]
    assert (result["shadow_runs"], result["selective_runs"], result["full_runs"]) == (3, 2, 1)
    assert (every["caught"], every["missed"], every["flaky_excluded"], every["failures"]) == (
        3,
        1,
        1,
        5,
    )
    assert every["recall"] == 0.75 and selective["recall"] == pytest.approx(2 / 3)
    assert every["missed_by_cause"] == {
        "new_test": 1,
        "flaky_history": 0,
        "config_gap": 0,
        "no_link": 0,
        "unknown": 0,
    }
    assert every["missed_by_verdict"] == {"new_regression": 0, "unknown": 1, "pre_existing": 0}
    assert result["time"] == {
        "total_ms": 4000.0,
        "would_run_ms": 2800.0,
        "saved": pytest.approx(0.3),
    }
    [missed] = result["misses"]
    assert (missed["test_id"], missed["ci_run_id"], missed["cause"]) == ("t::new", "1", "new_test")
    # siftwise report's own text, both recalls on the first line.
    assert (
        "Recall 75.0% (3 of 4) over 3 shadow runs; selective runs only: 66.7% (2 of 3) over 2"
        in text
    )
    assert "Time saved (measured on the shadow runs): 30.0% of 0.1 min" in text
    assert "t::new" in text and "new test" in text
    assert "Failure verdicts (distinct failing tests per run): 2 new regression, 1 flaky" in text


def test_one_summary_per_run_its_first_attempt() -> None:
    first = summary("9", attempt=1, shadow=compare("9", caught=1))
    rerun = summary("9", attempt=2, shadow=compare("9", caught=5))

    for runs in ([rerun, first], [first, rerun, first]):  # any order; duplicate downloads too
        result, _ = report(runs)
        assert result["shadow_runs"] == 1 and result["all"]["caught"] == 1


def test_no_data_says_so() -> None:
    result, text = report([])

    assert result["shadow_runs"] == 0 and result["all"]["recall"] is None
    assert "0 runs" in text and "No dogfood runs in this range." in text


def test_the_date_range_forks_and_malformed_summaries() -> None:
    runs = [
        summary("1", shadow=compare("1", caught=1)),
        summary("2", "2026-10-01T00:00:00Z", shadow=compare("2", caught=1)),  # before since
        summary("3", shadow=compare("3", caught=1), fork=True),
        {**summary("4"), "schema": 99},
        {**summary("5"), "at": "not a time"},
        {"run_id": "6"},
        {**summary("7", shadow=compare("7")), "repo": "someone/else"},
        summary("8", shadow={"counted": True}),  # a fork could upload anything
    ]

    result, text = report(runs)
    assert result["dogfood"]["outside_range"] == 1 and result["dogfood"]["malformed"] == 5
    assert result["all"]["caught"] == 2 and result["dogfood"]["forks"] == 1
    assert "1 from forks (untrusted" in text

    result, text = report(runs, exclude_forks=True)
    assert result["all"]["caught"] == 1 and "(excluded)" in text


def test_runs_without_a_comparison_are_not_counted_with_their_reason() -> None:
    runs = [
        summary("1", problems=["server step: failure"]),  # no shadow comparison at all
        summary("2", shadow=compare("2", counted=False, status="no_upload")),
        summary("3", shadow={**compare("3", counted=False), "selections": []}),
    ]

    result, text = report(runs)

    assert result["shadow_runs"] == 0
    assert result["not_counted"] == {"no_comparison": 1, "no_upload": 1, "not_stored": 1}
    assert "Not counted: 1 no comparison, 1 no upload, 1 not stored" in text
    assert "Runs with problems (fail-open): 1 server step" in text


def test_linked_but_not_selected_misses_warn() -> None:
    runs = [
        summary(
            "1", shadow=compare("1", missed=[miss("t::a", "unknown", detail=LINKED_NOT_SELECTED)])
        )
    ]
    result, text = report(runs)

    assert result["linked_not_selected"] == 1 and "may be a selector bug" in text


def test_by_week() -> None:
    runs = [
        summary("1", "2026-10-27T10:00:00Z", shadow=compare("1", caught=1)),
        summary("2", "2026-11-03T10:00:00Z", shadow=compare("2", missed=[miss("t::b")])),
    ]

    result, text = report(runs, by_week=True)

    weeks = result["periods"]
    assert [w["start"][:10] for w in weeks] == ["2026-10-26", "2026-11-02"]
    assert [(w["all"]["caught"], w["all"]["missed"]) for w in weeks] == [(1, 0), (0, 1)]
    assert "2026-10-26" in text and "2026-11-02" in text and "Week of" in text


# --- fetching -------------------------------------------------------------------------------


def zipped(content: dict[str, Any] | bytes) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        raw = content if isinstance(content, bytes) else json.dumps(content).encode()
        archive.writestr(dogfood.SUMMARY_FILE, raw)
    return data.getvalue()


def artifact(n: int, name: str, created: str, *, expired: bool = False) -> dict[str, Any]:
    return {
        "id": n,
        "name": name,
        "created_at": created,
        "expired": expired,
        "archive_download_url": f"https://api.github.com/artifacts/{n}/zip",
    }


@respx.mock
def test_summaries_are_downloaded_since_a_date() -> None:
    listing = [
        artifact(1, "dogfood-summary-1", "2026-11-02T00:00:00Z"),
        artifact(2, "coverage", "2026-11-01T00:00:00Z"),
        artifact(3, "dogfood-summary-2", "2026-10-31T00:00:00Z", expired=True),
        artifact(4, "dogfood-summary-1", "2026-10-30T00:00:00Z"),
        artifact(5, "dogfood-summary-1", "2026-10-29T00:00:00Z"),  # not a zip of ours
        artifact(6, "dogfood-summary-1", "2026-10-01T00:00:00Z"),  # before since: stops here
    ]
    respx.get(f"https://api.github.com/repos/{REPO}/actions/artifacts").respond(
        json={"total_count": len(listing), "artifacts": listing}
    )
    one = respx.get("https://api.github.com/artifacts/1/zip").respond(content=zipped(summary("1")))
    four = respx.get("https://api.github.com/artifacts/4/zip").respond(content=zipped(summary("4")))
    respx.get("https://api.github.com/artifacts/5/zip").respond(content=b"not a zip")
    others = respx.get(url__regex=r"/artifacts/(2|3|6)/zip").respond(content=b"")

    with client(TOKEN) as gh:
        found = dogfood.download_summaries(gh, REPO, SINCE)

    assert [s["run_id"] for s in found] == ["1", "4"]
    assert one.called and four.called and not others.called


def test_the_command_line_reads_and_saves_folders(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, saved = tmp_path / "in", tmp_path / "out"
    source.mkdir()
    for run in (summary("1", shadow=compare("1", caught=1)), summary("2", shadow=compare("2"))):
        (source / f"{run['run_id']}.json").write_text(json.dumps(run))
    (source / "junk.json").write_text("{not json")

    code = dogfood.main(
        [
            "report",
            "--repo",
            REPO,
            "--since",
            "2026-10-26",
            "--until",
            "2026-11-09",
            "--from",
            str(source),
            "--save",
            str(saved),
        ]
    )

    assert code == 0
    assert "2 runs" in capsys.readouterr().out
    assert sorted(p.name for p in saved.iterdir()) == ["1-1.json", "2-1.json"]

    dogfood.main(
        [
            "report",
            "--repo",
            REPO,
            "--since",
            "2026-10-26",
            "--until",
            "2026-11-09",
            "--from",
            str(saved),
            "--json",
        ]
    )
    assert json.loads(capsys.readouterr().out)["all"]["caught"] == 1


# --- the summary (in the dogfood job) -------------------------------------------------------


def job_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    event = {
        "pull_request": {
            "number": 7,
            "head": {"sha": "c" * 40, "ref": "feature", "repo": {"full_name": REPO}},
            "base": {"sha": "d" * 40},
        },
        "repository": {"default_branch": "main"},
    }
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event))
    return {
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_EVENT_PATH": str(path),
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_RUN_ID": "555",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_HEAD_REF": "feature",
        "SIFTWISE_API_TOKEN": TOKEN,
        "DOGFOOD_STARTED_AT": "2026-10-27T10:00:00Z",
        "DOGFOOD_SHADOW": "true",
        "DOGFOOD_SHADOW_MODE": "selective",
        "DOGFOOD_SHADOW_SELECTED": "12",
        "DOGFOOD_SHADOW_TOTAL": "700",
        "DOGFOOD_TESTS_OUTCOME": "failure",
        "DOGFOOD_HISTORY_FROM": "siftwise-dogfood-db-v1-100-1",
        "DOGFOOD_STEPS": "cache=success history=success server=success select=success "
        "record=success rollup=success",
        **extra,
    }


@respx.mock
def test_the_summary_holds_the_servers_comparison_and_verdicts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shadow = respx.post(f"http://localhost:8000/repos/{REPO}/shadow").respond(
        json=compare("555", caught=1)
    )
    respx.post(f"http://localhost:8000/repos/{REPO}/triage").respond(
        json={
            "failing_tests": 1,
            "counts": {"new_regression": 1},
            "results": [
                {
                    "test_id": "t::x",
                    "variant": "py3.12",
                    "verdict": "new_regression",
                    "confidence": "high",
                    "reasons": ["..."],
                }
            ],
        }
    )
    out = tmp_path / "s" / "dogfood-summary.json"

    code = dogfood.main(
        ["summary", "--out", str(out), "--server", "http://localhost:8000"], env=job_env(tmp_path)
    )

    assert code == 0
    written = json.loads(out.read_text())
    assert json.loads(shadow.calls.last.request.content) == {"ci_run_id": "555", "run_attempt": 2}
    assert shadow.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"
    assert (written["run_id"], written["run_attempt"], written["pr_number"], written["fork"]) == (
        "555",
        2,
        7,
        False,
    )
    assert written["commit_sha"] == "c" * 40  # the PR head, as record stored it
    assert written["select"] == {
        "shadow": "true",
        "shadow_mode": "selective",
        "selected": 12,
        "total": 700,
    }
    assert written["history_from"] == "siftwise-dogfood-db-v1-100-1"
    assert written["shadow"]["caught"] == 1 and written["problems"] == []
    assert written["triage"]["results"] == [
        {"test_id": "t::x", "variant": "py3.12", "verdict": "new_regression", "confidence": "high"}
    ]
    # It reads back as one counted run.
    assert dogfood.aggregate([written], REPO, SINCE, UNTIL)["all"]["caught"] == 1
    assert TOKEN not in out.read_text() + capsys.readouterr().out


@respx.mock
def test_a_failing_server_or_step_is_a_problem_never_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.post(url__regex=r"/shadow$").mock(side_effect=httpx.ConnectError("refused"))
    respx.post(url__regex=r"/triage$").respond(500, text=f"boom {TOKEN}")
    out = tmp_path / "dogfood-summary.json"
    env = job_env(
        tmp_path,
        DOGFOOD_STEPS="cache=success history=failure server=success "
        "select=success record= rollup=skipped",
    )

    assert dogfood.main(["summary", "--out", str(out)], env=env) == 0

    written = json.loads(out.read_text())
    assert written["shadow"] is None and written["triage"] is None
    assert written["problems"][:2] == ["history step: failure", "record step: did not run"]
    assert any(p.startswith("shadow compare:") for p in written["problems"])
    printed = capsys.readouterr().out
    assert "::warning::siftwise dogfood: history step: failure" in printed
    assert TOKEN not in printed + out.read_text()


# --- the workflow ---------------------------------------------------------------------------


def workflow() -> dict[Any, Any]:
    data: dict[Any, Any] = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "dogfood.yml").read_text(encoding="utf-8")
    )
    return data


def test_the_workflow_never_gates_comments_or_writes_mains_cache_from_a_pr() -> None:
    flow = workflow()
    text = (ROOT / ".github" / "workflows" / "dogfood.yml").read_text(encoding="utf-8")
    triggers: dict[str, Any] = flow[True]  # YAML 1.1 reads a bare `on` as true

    assert set(triggers) == {"push", "pull_request", "schedule"}
    assert triggers["push"] == {"branches": ["main"]}
    assert "pull_request_target" not in text and "workflow_run" not in text
    assert flow["permissions"] == {"contents": "read", "actions": "read"}
    assert "${{ secrets." not in text  # needs none: fork PRs run it too
    steps = flow["jobs"]["dogfood"]["steps"]
    siftwise = [s for s in steps if str(s.get("uses", "")).startswith("./actions/")]
    assert [s["uses"] for s in siftwise] == [
        "./actions/select",
        "./actions/record",
        "./actions/rollup",
    ]
    for step in siftwise:
        assert step["with"]["comment"] is False
        assert "fail-on" not in step["with"]  # never gates
        assert step["continue-on-error"] is True  # fail open
    assert next(s for s in siftwise if s["uses"] == "./actions/select")["with"]["shadow"] == (
        "always"
    )
    # Everything after the checkout fails open (setup steps excepted: no siftwise in them).
    setup = {"Start time", "Start PostgreSQL", "Install dependencies", "Token"}
    for step in steps:
        uses = str(step.get("uses", ""))
        if step.get("name") not in setup and not uses.startswith(("actions/checkout", "astral")):
            assert step.get("continue-on-error") is True, step


def test_the_workflow_keeps_history_and_summaries() -> None:
    steps = {s.get("name"): s for s in workflow()["jobs"]["dogfood"]["steps"]}

    restore, save = (
        steps["Restore the history (newest cache entry this run may read)"],
        steps["Save the history"],
    )
    assert save["with"]["key"] == restore["with"]["key"]  # a new key per run attempt
    assert "github.run_id" in save["with"]["key"] and "github.run_attempt" in save["with"]["key"]
    assert restore["with"]["restore-keys"] == "${{ env.CACHE_PREFIX }}"
    # A history that failed to load is never saved over the one it came from.
    assert "steps.history.outcome == 'success'" in steps["Dump the history"]["if"]
    assert "steps.dump.outcome == 'success'" in save["if"]
    assert "examples/gitlab/ci/history.py restore" in steps["Load the history"]["run"]
    assert "examples/gitlab/ci/history.py dump" in steps["Dump the history"]["run"]
    upload = steps["Upload the summary"]["with"]
    assert upload["name"].startswith(dogfood.ARTIFACT_PREFIX)
    assert upload["path"].endswith(dogfood.SUMMARY_FILE) and upload["retention-days"] == 90
    assert dogfood.SUMMARY_FILE in steps["Summary"]["run"]
    # record uploads this run's report only, never tests/fixtures' sample files.
    assert steps["Record"]["with"]["junit"].endswith("/*.xml")


# --- against the real server: the report adds up exactly what /recall does ------------------


def junit(results: dict[str, bool]) -> bytes:
    cases = "".join(
        f'<testcase classname="{test.split("::")[0]}" name="{test.split("::")[1]}" time="2.0">'
        + ("" if passed else '<failure message="boom">boom</failure>')
        + "</testcase>"
        for test, passed in results.items()
    )
    return f'<testsuite name="pytest">{cases}</testsuite>'.encode()


def test_a_summary_reports_what_the_servers_recall_reports(db_session: Any) -> None:
    from fastapi.testclient import TestClient

    from siftwise.api.main import create_app
    from siftwise.config import Settings
    from siftwise.db import get_session

    api = create_app(Settings(_env_file=None, api_token=TOKEN))
    api.dependency_overrides[get_session] = lambda: db_session
    auth = {"Authorization": f"Bearer {TOKEN}"}
    a, b = "tests.test_a::test_a", "tests.test_b::test_b"

    with TestClient(api) as server, respx.mock:

        def forward(request: httpx.Request) -> httpx.Response:
            answer = server.post(
                request.url.raw_path.decode(),
                content=request.content,
                headers={**auth, "content-type": "application/json"},
            )
            return httpx.Response(answer.status_code, content=answer.content)

        respx.post(url__startswith="http://siftwise").mock(side_effect=forward)

        def upload(run: str, sha: str, main: bool, results: dict[str, bool]) -> None:
            meta = {
                "repo": REPO,
                "commit_sha": sha,
                "branch": "main" if main else "feature",
                "is_main": main,
                "ci_run_id": run,
                "run_attempt": 1,
                "variant": "py3.12",
                "changed_files": ["tests/test_a.py"],
            }
            files = [("files", ("junit.xml", junit(results), "application/xml"))]
            response = server.post(
                "/runs", files=files, data={"metadata": json.dumps(meta)}, headers=auth
            )
            assert response.status_code == 201, response.text

        upload("100", "a" * 40, True, {a: True, b: True})  # main's history
        # The PR's shadow run: siftwise would run test_a.py only; test_b fails too (a miss).
        shadow = {"ci_run_id": "555", "run_attempt": 2, "variant": "py3.12", "commit_sha": "c" * 40}
        selected = server.post(
            "/select",
            headers=auth,
            json={"repo": REPO, "changed_files": ["tests/test_a.py"], "shadow": shadow},
        )
        assert selected.json()["mode"] == "selective" and selected.json()["shadow_stored"]
        response = server.post(
            "/runs",
            headers=auth,
            files=[("files", ("j.xml", junit({a: False, b: False}), "application/xml"))],
            data={
                "metadata": json.dumps(
                    {
                        "repo": REPO,
                        "commit_sha": "c" * 40,
                        "branch": "feature",
                        "is_main": False,
                        "ci_run_id": "555",
                        "run_attempt": 2,
                        "variant": "py3.12",
                        "changed_files": ["tests/test_a.py"],
                    }
                )
            },
        )
        assert response.status_code == 201, response.text

        built = dogfood.build_summary(
            {
                "GITHUB_REPOSITORY": REPO,
                "GITHUB_RUN_ID": "555",
                "GITHUB_RUN_ATTEMPT": "2",
                "GITHUB_EVENT_NAME": "pull_request",
            },
            "http://siftwise:8000",
            TOKEN,
        )
        recall = server.get(f"/repos/{REPO}/recall", params={"days": 30}, headers=auth).json()

    assert built["problems"] == [] and built["shadow"]["counted"]
    now = datetime.now(UTC)
    mine = dogfood.aggregate([built], REPO, now - timedelta(days=30), now + timedelta(days=1))
    for part in ("all", "selective"):
        assert mine[part] == recall[part], part
    assert mine["time"] == recall["time"]
    assert (mine["shadow_runs"], mine["selective_runs"], mine["full_runs"]) == (
        recall["shadow_runs"],
        recall["selective_runs"],
        recall["full_runs"],
    )
    assert [(m["test_id"], m["cause"]) for m in mine["misses"]] == [
        (m["test_id"], m["cause"]) for m in recall["misses"]
    ]
    assert [m["test_id"] for m in mine["misses"]] == [b]
    assert mine["all"]["caught"] == 1 and mine["all"]["missed"] == 1
