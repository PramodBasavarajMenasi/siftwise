"""core/ci_stats.py: every CI number, and the API, the dashboard and the report agreeing."""

import dataclasses
import html
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from siftwise.api.main import create_app
from siftwise.cli import gha, gitlab
from siftwise.cli import main as cli
from siftwise.config import Settings
from siftwise.core import tokens
from siftwise.core.ci_stats import (
    BASELINE_DAYS,
    BASELINE_MIN_RUNS,
    BRANCHES,
    LOW_CONFIDENCE_RUNS,
    Baseline,
    Saving,
    baseline,
    ci_stats,
    ci_stats_by_branch,
    full_suite_legs,
    leg_baselines,
    saving,
)
from siftwise.core.history import recompute_repo_stats
from siftwise.core.models import Repo, Run
from siftwise.core.selector import FullReason, Mode, Selection
from siftwise.core.shadow import ShadowTarget, compare_ci_run, store_selection
from siftwise.core.tokens import Scope
from siftwise.db import get_session
from tests.test_ci_selections import ranked
from tests.test_dashboard import signed_in
from tests.test_shadow import selection
from tests.test_triage_db import OTHER, REPO, TEST, F, P, at, record, sha

NOW = at(1000)  # about 41 days after the first run
THIRD = "tests.test_ship::test_rate"
ADMIN = "test-token"
AUTH = {"Authorization": f"Bearer {ADMIN}"}


def run(session: Session, ci: str, *, hour: float = 900, branch: str = "pr", attempt: int = 1,
        variant: str | None = None, tests: dict[str, Any] | None = None, commit: int = 50,
        rerun: int = 0, is_main: bool | None = None) -> Run:  # fmt: skip
    return record(session, commit=commit, hour=hour, branch=branch, variant=variant,
                  ci_run_id=ci, run_attempt=attempt, rerun=rerun, is_main=is_main,
                  tests=tests or {TEST: P, OTHER: P})  # fmt: skip


def stored(session: Session, ci: str, sel: Selection, *, attempt: int = 1,
           variant: str | None = None, commit: int = 50, shadow: bool = False) -> None:  # fmt: skip
    target = ShadowTarget(ci, attempt, variant, sha(commit))
    assert store_selection(session, REPO, target, sel, shadow=shadow) is None


def full(code: FullReason) -> Selection:
    return dataclasses.replace(selection(mode=Mode.FULL), reason_code=code)


def stats(session: Session, days: int | None = None, branch: Any = "all") -> Any:
    found = ci_stats(session, REPO, days, branch, NOW)
    assert found is not None
    return found


# --- unit: every number -----------------------------------------------------------------------


def test_an_unknown_repo_and_an_empty_window(db_session: Session) -> None:
    assert ci_stats(db_session, "nobody/nothing", 30, "all", NOW) is None
    run(db_session, "old", hour=1)  # 41 days before NOW
    s = stats(db_session, days=30)
    assert (s.runs, s.recorded, s.counted) == (0, 0, 0)
    assert (s.median_share, s.saved_ms, s.median_saved_ms) == (None, None, None)
    assert s.failures.runs == 0 and s.full_reasons == {}
    assert s.range_text == "2026-10-12 to 2026-11-11 (UTC)"
    all_s = stats(db_session)
    assert all_s.runs == 1  # all history
    assert all_s.range_text == "2026-10-01 to 2026-11-11 (UTC)"


def test_runs_without_a_selection_count_in_coverage_only(db_session: Session) -> None:
    run(db_session, "a")
    run(db_session, "b", commit=51)
    record(db_session, commit=52, hour=900, tests={TEST: P})  # no CI run ID: a run of its own
    stored(db_session, "a", ranked(TEST))
    s = stats(db_session)
    assert (s.runs, s.recorded, s.selective_runs, s.full_runs) == (3, 1, 1, 0)


def test_only_full_suite_runs(db_session: Session) -> None:
    run(db_session, "a")
    run(db_session, "b", commit=51)
    stored(db_session, "a", full(FullReason.BUILD_FILE))
    stored(db_session, "b", full(FullReason.UNMAPPED_FILE), commit=51)
    s = stats(db_session)
    assert (s.recorded, s.selective_runs, s.full_runs) == (2, 0, 2)
    assert dict(s.full_reasons) == {"build_file": 1, "unmapped_file": 1}
    assert (s.median_share, s.saved_ms) == (None, None)  # never 0% or 0 min


def test_main_and_pull_requests_apart(db_session: Session) -> None:
    run(db_session, "m", branch="main", commit=60)
    run(db_session, "p", commit=61)
    run(db_session, "q", commit=62)
    stored(db_session, "m", ranked(TEST), commit=60)
    stored(db_session, "p", full(FullReason.BUILD_FILE), commit=61)
    by = ci_stats_by_branch(db_session, REPO, None, now=NOW)
    assert by is not None
    counts = [(by[b].runs, by[b].recorded, by[b].selective_runs, by[b].full_runs)
              for b in ("main", "prs", "all")]  # fmt: skip
    assert counts == [(1, 1, 1, 0), (2, 1, 0, 1), (3, 2, 1, 1)]


def test_main_is_the_default_branch_the_ci_sent_not_the_name_main(
    db_session: Session, tmp_path: Path
) -> None:
    # A repo whose default branch is master: the uploaders decide is_main from it.
    event = {"repository": {"default_branch": "master"}}
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event))
    github = {"GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(path),
              "GITHUB_REPOSITORY": REPO, "GITHUB_SHA": "f" * 40}  # fmt: skip
    no_parent = lambda _: False  # noqa: E731
    assert gha.load_context({**github, "GITHUB_REF_NAME": "master"}, None, no_parent).is_main
    assert not gha.load_context({**github, "GITHUB_REF_NAME": "main"}, None, no_parent).is_main
    gl = {"CI_PIPELINE_SOURCE": "push", "CI_COMMIT_SHA": "f" * 40, "CI_DEFAULT_BRANCH": "master"}
    assert gitlab.load_context({**gl, "CI_COMMIT_BRANCH": "master"}, None, no_parent).is_main
    assert not gitlab.load_context({**gl, "CI_COMMIT_BRANCH": "main"}, None, no_parent).is_main
    # ci_stats counts by the stored is_main, never by the branch's name.
    run(db_session, "m", branch="master", is_main=True, commit=60)
    run(db_session, "n", branch="main", is_main=False, commit=61)
    for ci, commit in (("m", 60), ("n", 61)):
        stored(db_session, ci, ranked(TEST), commit=commit)
    by = ci_stats_by_branch(db_session, REPO, None, now=NOW)
    assert by is not None
    assert (by["main"].runs, by["prs"].runs) == (1, 1)
    main_run = db_session.scalars(select(Run).where(Run.ci_run_id == "m")).one()
    assert main_run.branch == "master" and main_run.is_main


def many(n: int) -> dict[str, Any]:
    """``n`` passing tests: a leg measuring ``n`` x 5 ms."""
    return {f"tests.test_many::test_{i}": P for i in range(n)}


def full_leg(session: Session, ci: str, hour: float, tests: int, variant: str | None = None,
             *, shadow: bool = False) -> None:  # fmt: skip
    """A full-suite leg (a full selection, or a shadow run's) measuring ``tests`` x 5 ms."""
    commit = 100 + int(hour)
    run(session, ci, hour=hour, variant=variant, commit=commit, tests=many(tests))
    sel = ranked("tests.test_many::test_0") if shadow else full(FullReason.BUILD_FILE)
    stored(session, ci, sel, variant=variant, commit=commit, shadow=shadow)


def test_time_saved_against_each_legs_own_baseline(db_session: Session) -> None:
    # Baselines (medians of the variant's full-suite legs in the 30 days before):
    # linux 50/100/150 ms -> 100 (one of them a shadow run's); mac 200/200/200 -> 200.
    for n, (hour, tests) in enumerate([(800, 10), (810, 20), (820, 30)]):
        full_leg(db_session, f"lin{n}", hour, tests, "linux", shadow=n == 1)
        full_leg(db_session, f"mac{n}", hour, 40, "mac")
    # a: two legs under one NULL-variant selection, measured 2 tests x 5 ms each.
    run(db_session, "a", variant="linux")
    run(db_session, "a", variant="mac")
    stored(db_session, "a", ranked(TEST))  # 1 of 10 known
    # b: a variant with no full-suite runs: no baseline, left out (and counted).
    run(db_session, "b", variant="win", commit=51)
    stored(db_session, "b", ranked(TEST, OTHER), variant="win", commit=51)  # 2 of 10
    # c: mixed legs: linux counted, win left out.
    run(db_session, "c", variant="linux", commit=52)
    run(db_session, "c", variant="win", commit=52)
    stored(db_session, "c", ranked(TEST), commit=52)
    s = stats(db_session)
    assert (s.selective_runs, s.full_runs, s.shadow_runs) == (3, 5, 1)
    assert sorted(s.shares) == [0.1, 0.1, 0.2] and s.median_share == pytest.approx(0.1)
    assert sorted(s.savings) == [90.0, 280.0]  # c: 100 - 10; a: (100 - 10) + (200 - 10)
    assert (s.saved_ms, s.median_saved_ms, s.baseline_ms) == (370.0, 185.0, 400.0)
    assert (s.legs_counted, s.legs_left_out) == (3, 2)


def test_a_baseline_needs_3_full_suite_runs_before_the_leg_within_30_days(
    db_session: Session,
) -> None:
    full_leg(db_session, "old0", 100, 20)  # 33 days before the selective run: too old
    full_leg(db_session, "old1", 170, 20)
    full_leg(db_session, "new0", 600, 20)  # within 30 days, but only two
    full_leg(db_session, "new1", 700, 20)
    for n, hour in enumerate((905, 910, 920)):  # after it: never used
        full_leg(db_session, f"after{n}", hour, 20)
    run(db_session, "sel", hour=900)
    stored(db_session, "sel", ranked(TEST))
    s = stats(db_session)
    assert (s.selective_runs, s.savings, s.saved_ms) == (1, [], None)
    assert (s.legs_counted, s.legs_left_out) == (0, 1)
    repo_id = db_session.scalars(select(Repo.id).where(Repo.name == REPO)).one()
    assert leg_baselines(db_session, repo_id, [(None, at(900))]) == [None]
    # A third run within the 30 days gives it one.
    full_leg(db_session, "new2", 800, 40)
    assert leg_baselines(db_session, repo_id, [(None, at(900))]) == [Baseline(100.0, 3)]
    assert stats(db_session).savings == [90.0]


def test_a_full_suite_leg_scores_about_0_against_its_baseline(db_session: Session) -> None:
    for n, hour in enumerate((800, 810, 820, 830)):
        full_leg(db_session, f"f{n}", hour, 20, "linux")
    repo_id = db_session.scalars(select(Repo.id).where(Repo.name == REPO)).one()
    full = full_suite_legs(db_session, repo_id, None, NOW)
    base = baseline(full, "linux", at(830))  # from the three before it, not itself
    assert base == Baseline(100.0, 3)
    found = saving([(base, 100.0)])
    assert found is not None and found.saved_ms == 0
    assert baseline(full, "mac", at(830)) is None  # per variant


def test_a_selective_leg_slower_than_its_baseline_saves_negative_time(
    db_session: Session,
) -> None:
    for n, hour in enumerate((800, 810, 820)):
        full_leg(db_session, f"f{n}", hour, 20)  # baseline 100 ms
    run(db_session, "fast")  # 2 tests: 10 ms, saved 90
    run(db_session, "slow", commit=51, tests=many(30))  # 150 ms: saved -50, kept
    stored(db_session, "fast", ranked(TEST))
    stored(db_session, "slow", ranked(TEST), commit=51)
    s = stats(db_session)
    assert sorted(s.savings) == [-50.0, 90.0]
    assert (s.saved_ms, s.median_saved_ms) == (40.0, 20.0)
    assert saving([(Baseline(100.0, 3), 150.0)]) == Saving(100.0, 150.0, 1, 0)
    assert saving([(None, 5.0), (Baseline(100.0, 3), None)]) is None
    assert cli.ci_minutes(-60_000) == "-1.0 min"


def test_legs_without_a_baseline_are_left_out_and_counted_everywhere(
    db_session: Session, client: TestClient
) -> None:
    for n, hour in enumerate((800, 810, 820)):
        full_leg(db_session, f"f{n}", hour, 20, "linux")
    run(db_session, "ok", variant="linux")
    run(db_session, "ok", variant="win")  # no baseline
    run(db_session, "none", variant="win", commit=51)
    run(db_session, "nomeasure", variant="linux", commit=52)
    for ci, commit in (("ok", 50), ("none", 51), ("nomeasure", 52)):
        stored(db_session, ci, ranked(TEST), commit=commit)
    for r in db_session.scalars(select(Run).where(Run.ci_run_id == "nomeasure")):
        r.measured_ms = None
    db_session.flush()
    s = stats(db_session)
    assert (s.selective_runs, len(s.savings), s.saved_ms) == (3, 1, 90.0)
    assert (s.legs_counted, s.legs_left_out) == (1, 3)
    body = client.get(f"/repos/{REPO}/ci", params={"days": 365}, headers=AUTH).json()
    saved = body["time_saved"]
    assert (saved["runs"], saved["left_out"], saved["legs"], saved["legs_left_out"]) == (1, 2, 1, 3)
    assert saved["baseline_ms"] == 100.0
    line = next(x for x in cli.ci_detail_lines(body) if x.startswith("test time saved"))
    assert ("based on 1 of 3 selective runs (3 legs left out: no baseline yet or nothing "
            "measured)") in line  # fmt: skip
    signed_in(client, ADMIN)
    page = html.unescape(client.get(f"/ui/repos/{REPO}", params={"days": "all"}).text)
    assert "based on 1 of 3 selective runs (3 legs left out: no baseline yet" in page
    # The run page: the leg's measured baseline, never the summed per-test estimate.
    run_page = html.unescape(client.get(f"/ui/repos/{REPO}/-/runs/ok/1").text)
    assert "full-suite baseline" in run_page and "on 1 of 2 variants" in run_page
    assert "for the full suite" not in run_page
    none_page = html.unescape(client.get(f"/ui/repos/{REPO}/-/runs/none/1").text)
    assert "no baseline yet (3 full-suite runs of this variant in the 30 days before)" in none_page


def test_a_leg_measuring_0_counts_as_no_measured_time(db_session: Session) -> None:
    # 0 ms = the upload reported no durations: out of the baseline and of time saved.
    for n, hour in enumerate((800, 810, 820, 830)):
        full_leg(db_session, f"f{n}", hour, 20)  # 100 ms each
    run(db_session, "sel")
    run(db_session, "zero", commit=51)
    for ci, commit in (("sel", 50), ("zero", 51)):
        stored(db_session, ci, ranked(TEST), commit=commit)
    db_session.execute(text("UPDATE runs SET measured_ms = 0 WHERE ci_run_id IN ('f3', 'zero')"))
    repo_id = db_session.scalars(select(Repo.id).where(Repo.name == REPO)).one()
    # Three 100 ms legs, not four with a 0 among them.
    assert leg_baselines(db_session, repo_id, [(None, at(900))]) == [Baseline(100.0, 3)]
    s = stats(db_session)
    assert (s.selective_runs, s.savings, s.legs_counted, s.legs_left_out) == (2, [90.0], 1, 1)
    assert saving([(Baseline(100.0, 3), 0.0)]) is None


def test_time_saved_from_fewer_than_5_runs_is_flagged(
    db_session: Session, client: TestClient
) -> None:
    for n, hour in enumerate((800, 810, 820)):
        full_leg(db_session, f"f{n}", hour, 20)
    for n in range(4):
        run(db_session, f"s{n}", commit=60 + n)
        stored(db_session, f"s{n}", ranked(TEST), commit=60 + n)
    s = stats(db_session)
    assert (len(s.savings), s.low_confidence) == (4, True)
    body = client.get(f"/repos/{REPO}/ci", params={"days": 365}, headers=AUTH).json()
    assert body["time_saved"]["low_confidence"] is True
    line = next(x for x in cli.ci_detail_lines(body) if x.startswith("test time saved"))
    assert line.endswith("; too few runs for a reliable figure (4 runs)")
    signed_in(client, ADMIN)
    page = html.unescape(client.get(f"/ui/repos/{REPO}", params={"days": "all"}).text)
    assert "too few runs for a reliable figure (4 runs)" in page
    # A fifth counted run: no flag anywhere.
    run(db_session, "s4", commit=64)
    stored(db_session, "s4", ranked(TEST), commit=64)
    assert stats(db_session).low_confidence is False
    body = client.get(f"/repos/{REPO}/ci", params={"days": 365}, headers=AUTH).json()
    assert body["time_saved"]["low_confidence"] is False
    assert not any("too few runs" in x for x in cli.ci_detail_lines(body))
    page = html.unescape(client.get(f"/ui/repos/{REPO}", params={"days": "all"}).text)
    assert "too few runs" not in page
    # No counted run: nothing to flag.
    assert ci_stats(db_session, REPO, 1, "all", NOW) is not None
    assert stats(db_session, days=1).low_confidence is False


def test_the_report_states_the_baseline_rule_the_server_uses() -> None:
    assert (BASELINE_DAYS, BASELINE_MIN_RUNS, LOW_CONFIDENCE_RUNS) == (30, 3, 5)
    assert "in the 30 days before it, at least 3" in cli.CI_BASELINE_NOTE


def test_shadow_runs_are_counted_apart(db_session: Session) -> None:
    run(db_session, "seed", hour=1, tests={TEST: P, OTHER: P, THIRD: P}, commit=1)
    repo_id = db_session.scalars(select(Repo.id).where(Repo.name == REPO)).one()
    recompute_repo_stats(db_session, repo_id, 90)
    run(db_session, "sh", tests={TEST: F, OTHER: P, THIRD: P})
    stored(db_session, "sh", ranked(TEST), shadow=True)
    assert compare_ci_run(db_session, REPO, "sh", 1) is not None
    s = stats(db_session)
    assert (s.shadow_runs, s.selective_runs, s.full_runs, s.shadow_compared) == (1, 0, 0, 1)
    assert s.saved_ms is None and s.savings == []  # never added to the time saved
    # Measured 3 x 5 ms; it would have run TEST alone (5 ms): it would have saved 10.
    assert (s.shadow_measured_ms, s.shadow_would_save_ms) == (15.0, 10.0)


def test_each_attempt_counts_once_and_re_run_uploads_never(db_session: Session) -> None:
    for n, hour in enumerate((800, 810, 820)):
        full_leg(db_session, f"f{n}", hour, 20)  # baseline 100 ms
    run(db_session, "a", attempt=1)
    run(db_session, "a", attempt=2)
    run(db_session, "a", attempt=2, rerun=1, tests={TEST: P})  # a re-run of failing tests
    stored(db_session, "a", ranked(TEST), attempt=2)
    s = stats(db_session)
    assert (s.runs - 3, s.recorded - 3) == (2, 1)  # the three baseline runs aside
    assert s.savings == [90.0] and s.legs_counted == 1  # attempt 2's own upload only


def test_failures_in_selective_runs_with_their_rank(db_session: Session) -> None:
    run(db_session, "seed", hour=1, tests={TEST: P, OTHER: P, THIRD: P}, commit=1)
    run(db_session, "a", tests={TEST: F, OTHER: P, THIRD: F})  # THIRD ran outside
    stored(db_session, "a", ranked(OTHER, TEST))  # TEST ranks 2
    run(db_session, "b", commit=51, tests={TEST: P, OTHER: P})  # passes: no failures
    stored(db_session, "b", ranked(TEST), commit=51)
    run(db_session, "c", commit=52)  # selective, but its outcome was never stored
    stored(db_session, "c", ranked(TEST), commit=52)
    for ci in ("a", "b"):
        assert compare_ci_run(db_session, REPO, ci, 1) is not None
    f = stats(db_session).failures
    assert (f.runs, f.runs_failing, f.failing, f.in_selection, f.outside) == (2, 1, 2, 1, 1)
    assert (f.ranks, f.first_ranks, f.median_rank, f.median_first_rank) == ([2], [2], 2, 2)


# --- the API ----------------------------------------------------------------------------------


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app = create_app(Settings(_env_file=None, api_token=ADMIN, dashboard=True))
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as test_client:
        yield test_client


def test_the_api_validates_its_parameters(client: TestClient, db_session: Session) -> None:
    run(db_session, "a")
    ok = client.get(f"/repos/{REPO}/ci", headers=AUTH)
    assert ok.status_code == 200 and (ok.json()["days"], ok.json()["branch"]) == (30, "all")
    for params in ({"days": 0}, {"days": 366}, {"days": "x"}, {"branch": "feature"}):
        assert client.get(f"/repos/{REPO}/ci", params=params, headers=AUTH).status_code == 422
    assert client.get("/repos/nobody/nothing/ci", headers=AUTH).status_code == 404


def test_a_read_token_works_and_another_repos_token_is_refused(
    client: TestClient, db_session: Session
) -> None:
    run(db_session, "a")
    _, mine = tokens.create_token(db_session, REPO, Scope.READ, "dash")
    _, other = tokens.create_token(db_session, "acme/other", Scope.WRITE, "x")
    assert client.get(f"/repos/{REPO}/ci", headers={"Authorization": f"Bearer {mine}"}
                      ).status_code == 200  # fmt: skip
    refused = client.get(f"/repos/{REPO}/ci", headers={"Authorization": f"Bearer {other}"})
    assert refused.status_code == 403
    assert client.get(f"/repos/{REPO}/ci").status_code == 401


def test_the_dashboard_the_api_and_the_report_give_the_same_numbers(
    client: TestClient, db_session: Session
) -> None:
    run(db_session, "seed", hour=1, tests={TEST: P, OTHER: P, THIRD: P}, commit=1)
    for n, hour in enumerate((800, 810, 820)):  # baselines for both variants
        full_leg(db_session, f"bl{n}", hour, 20)
        full_leg(db_session, f"bl-linux{n}", hour, 30, "linux")
    run(db_session, "m", branch="main", commit=60, tests={TEST: F, OTHER: P})
    stored(db_session, "m", ranked(TEST), commit=60)
    run(db_session, "p", commit=61)
    stored(db_session, "p", full(FullReason.BUILD_FILE), commit=61)
    run(db_session, "q", commit=62, variant="linux")
    run(db_session, "q", commit=62, variant="mac")
    stored(db_session, "q", ranked(TEST, OTHER), commit=62)
    run(db_session, "r", commit=63)  # no selection
    assert compare_ci_run(db_session, REPO, "m", 1) is not None
    days = 90  # the last 90 days from now, on every surface
    api = {b: client.get(f"/repos/{REPO}/ci", params={"days": days, "branch": b},
                         headers=AUTH).json() for b in ("main", "prs", "all")}  # fmt: skip
    core = ci_stats_by_branch(db_session, REPO, days)
    assert core is not None
    for b in BRANCHES:
        r, s = api[b], core[b]
        assert (r["runs"], r["recorded"], r["selective_runs"], r["full_runs"]) == (
            s.runs, s.recorded, s.selective_runs, s.full_runs)  # fmt: skip
        assert (r["time_saved"]["sum_ms"], r["share_selected"]["median"]) == (
            s.saved_ms, s.median_share)  # fmt: skip
        assert r["failures"]["failing"] == s.failures.failing
    report = "\n".join(cli.ci_lines({b: api[b] for b in cli.CI_BRANCHES}))
    signed_in(client, ADMIN)
    page = client.get(f"/ui/repos/{REPO}", params={"days": days})
    assert page.status_code == 200
    text = html.unescape(re.sub(r"<[^>]+>", "", page.text))
    # The same values, formatted by the same helpers, on both.
    for b in ("main", "prs"):
        r = api[b]
        counted = r["selective_runs"] + r["full_runs"]
        values = [cli.ci_pct(r["selective_runs"], counted)]
        if r["share_selected"]["median"] is not None:
            values.append(cli.ci_share(r["share_selected"]["median"]))
        if r["time_saved"]["sum_ms"] is not None:
            values.append(cli.ci_minutes(r["time_saved"]["sum_ms"]))
        for value in values:
            assert value in report, (b, value)
            assert value in text, (b, value)
    assert (api["main"]["recorded"], api["prs"]["recorded"]) == (1, 8)
    assert None not in (api["main"]["time_saved"]["sum_ms"], api["prs"]["time_saved"]["sum_ms"])
    # Neither claims a recall from selective runs.
    assert (
        "Recall comes only from shadow runs" in report
        and "Recall comes only from shadow runs" in text
    )
