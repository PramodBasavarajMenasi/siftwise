"""#85 part 3: the CI page (ci_stats per UTC day/week, slowest tests) and the outcomes backfill
(no peeking, labels, idempotent, resumable, dry run, scopes, a live outcome always wins)."""

import html
import json
import re
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from siftwise.api.main import create_app
from siftwise.cli import main as cli
from siftwise.config import Settings
from siftwise.core import tokens
from siftwise.core.ci_stats import (
    MAX_DAY_PERIODS,
    TooManyPeriods,
    ci_stats,
    ci_stats_periods,
    period_start,
    slowest_tests,
)
from siftwise.core.history import recompute_repo_stats
from siftwise.core.models import Repo
from siftwise.core.outcomes_backfill import LIVE_UPLOAD, backfill_outcomes, pending_attempts
from siftwise.core.selector import FullReason
from siftwise.core.shadow import OUTCOME_BACKFILL, OUTCOME_LIVE, compare_ci_run
from siftwise.core.tokens import Scope
from siftwise.core.triage import Verdict, triage_ci_run
from siftwise.db import get_session
from tests.test_ci_selections import ranked
from tests.test_ci_stats import full, full_leg, run, stored
from tests.test_dashboard import signed_in
from tests.test_triage_db import OTHER, REPO, TEST, F, P, S, at, record

NOW = at(1000)  # 2026-11-11 16:00 UTC, a Wednesday
ADMIN = "test-token"
AUTH = {"Authorization": f"Bearer {ADMIN}"}


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app = create_app(Settings(_env_file=None, api_token=ADMIN, dashboard=True))
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as test_client:
        yield test_client


def periods(session: Session, days: int | None, by: Any, **kw: Any) -> Any:
    found = ci_stats_periods(session, REPO, days, by, now=NOW, **kw)
    assert found is not None
    return found


def page_text(client: TestClient, path: str, **params: Any) -> str:
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return html.unescape(re.sub(r"<[^>]+>", " ", response.text))


def squash(value: str) -> str:
    return re.sub(r"\s+", " ", value)


# --- periods ------------------------------------------------------------------------------


def test_periods_add_up_to_the_window_and_include_empty_ones(db_session: Session) -> None:
    run(db_session, "a", hour=1000 - 2)  # today
    run(db_session, "b", hour=1000 - 30, commit=51)  # yesterday
    run(db_session, "c", hour=1000 - 31, commit=52)
    run(db_session, "old", hour=1000 - 24 * 9, commit=53)  # before the 7-day window
    stored(db_session, "a", ranked(TEST))
    stored(db_session, "b", full(FullReason.BUILD_FILE), commit=51)
    by_day = periods(db_session, 7, "day")
    assert len(by_day.periods) == 8  # the partial first day through today, empty ones too
    first = period_start(NOW - timedelta(days=7), "day")
    assert [p.since for p in by_day.periods] == [first + timedelta(days=n) for n in range(8)]
    assert all(p.since.hour == 0 and p.since.tzinfo is not None for p in by_day.periods)
    assert by_day.periods[-1].until == NOW  # today, so far
    runs = [p.runs for p in by_day.periods]
    assert runs[-2:] == [2, 1] and sum(runs) == by_day.total.runs == 3
    for field in ("recorded", "selective_runs", "full_runs"):
        assert sum(getattr(p, field) for p in by_day.periods) == getattr(by_day.total, field)
    assert by_day.total.runs == ci_stats(db_session, REPO, 7, "all", NOW).runs  # type: ignore[union-attr]
    by_week = periods(db_session, 30, "week")
    assert all(p.since.weekday() == 0 and p.since.hour == 0 for p in by_week.periods)  # Monday
    assert sum(p.runs for p in by_week.periods) == by_week.total.runs == 4
    assert periods(db_session, 7, "day", branch="main").total.runs == 0  # branch filter


def test_a_run_counts_in_the_period_of_its_newest_upload(db_session: Session) -> None:
    midnight = period_start(NOW, "day")
    hours = (midnight - at(0)) / timedelta(hours=1)
    run(db_session, "x", hour=hours - 0.5, variant="linux")  # 23:30 the day before
    run(db_session, "x", hour=hours + 0.5, variant="mac")  # 00:30 today
    by_day = periods(db_session, 2, "day")
    assert [p.runs for p in by_day.periods][-2:] == [0, 1]


def test_by_day_over_too_long_a_span_is_refused(db_session: Session) -> None:
    run(db_session, "ancient", hour=-24 * (MAX_DAY_PERIODS + 5))
    with pytest.raises(TooManyPeriods):
        periods(db_session, None, "day")
    assert periods(db_session, None, "week").total.runs == 1


def test_the_api_gives_the_same_periods(client: TestClient, db_session: Session) -> None:
    now = datetime.now(UTC)
    hour = (now - at(0)) / timedelta(hours=1)
    run(db_session, "a", hour=hour - 1)
    run(db_session, "b", hour=hour - 49, commit=51)
    stored(db_session, "a", ranked(TEST))
    body = client.get(f"/repos/{REPO}/ci", params={"days": 7, "by": "day"}, headers=AUTH).json()
    assert body["by"] == "day" and len(body["periods"]) == 8
    assert sum(p["runs"] for p in body["periods"]) == body["runs"] == 2
    assert sum(p["recorded"] for p in body["periods"]) == body["recorded"] == 1
    assert body["periods"][-1]["runs"] == 1 and body["periods"][-1]["recorded"] == 1
    plain = client.get(f"/repos/{REPO}/ci", params={"days": 7}, headers=AUTH).json()
    assert plain["periods"] is None and plain["runs"] == 2
    bad = client.get(f"/repos/{REPO}/ci", params={"days": 7, "by": "month"}, headers=AUTH)
    assert bad.status_code == 422
    weeks = client.get(f"/repos/{REPO}/ci", params={"days": 365, "by": "week"}, headers=AUTH)
    assert weeks.status_code == 200 and len(weeks.json()["periods"]) in (53, 54)


# --- the CI page --------------------------------------------------------------------------


def _ci_page_data(session: Session) -> None:
    """Today: a selective run with a ranked failure, a full one, a shadow one, one without a
    selection, one with its outcome computed later; nothing yesterday. The seed (history) is
    older than the 7-day window."""
    now = datetime.now(UTC)
    hour = (now - at(0)) / timedelta(hours=1) - 1
    run(session, "seed", hour=hour - 24 * 8, tests={TEST: P, OTHER: P}, commit=1)
    run(session, "sel", hour=hour, tests={TEST: F, OTHER: P}, commit=60)
    stored(session, "sel", ranked(TEST), commit=60)
    assert compare_ci_run(session, REPO, "sel", 1) is not None
    run(session, "full", hour=hour, commit=61)
    stored(session, "full", full(FullReason.UNMAPPED_FILE), commit=61)
    run(session, "shadow", hour=hour, commit=62)
    stored(session, "shadow", ranked(TEST), commit=62, shadow=True)
    run(session, "none", hour=hour, commit=63)  # no selection, no outcome
    run(session, "later", hour=hour, tests={TEST: F, OTHER: P}, commit=64)  # backfilled below
    found = backfill_outcomes(session, REPO, window_days=90)
    assert found is not None and found.computed >= 1


def test_the_ci_page_shows_ci_stats_numbers_by_utc_day(
    client: TestClient, db_session: Session
) -> None:
    _ci_page_data(db_session)
    signed_in(client, ADMIN)
    text_ = squash(page_text(client, f"/ui/repos/{REPO}/-/ci", days=7))
    found = ci_stats_periods(db_session, REPO, 7, "day")
    assert found is not None
    today, t = found.periods[-1], found.total
    assert "per UTC day" in text_ and "UTC day" in text_ and "(so far)" in text_
    assert today.runs == t.runs == 5
    # Every cell from ci_stats, formatted by the report's helpers.
    expected = [
        cli.ci_pct(today.recorded, today.runs),
        cli.ci_pct(today.selective_runs, today.counted),
        f"{today.failures.failing} failing",
        f"{today.failures.in_selection} in the selection",
        f"median {cli.ci_rank(today.failures.median_first_rank or 0)}",
        "changed file with no known tests: 1",
    ]
    for value in expected:
        assert value in text_, value
    assert (today.recorded, today.selective_runs, today.full_runs, today.shadow_runs) == (
        3,
        1,
        1,
        1,
    )
    # The backfill computed every attempt without an outcome: full, shadow, none, later.
    assert (today.backfilled, today.backfilled_without_selection) == (4, 2)  # 2: none, later
    assert (
        "verdicts computed later for 4 (2: verdicts computed later; no selection recorded)" in text_
    )
    assert "no CI runs" in text_  # the empty days are listed
    assert "No recall from these runs" in text_ and "Recall comes only from shadow runs" in text_
    by_week = squash(page_text(client, f"/ui/repos/{REPO}/-/ci", days=90))
    assert "per UTC week (Monday 00:00 UTC)" in by_week and "week of" in by_week
    main = squash(page_text(client, f"/ui/repos/{REPO}/-/ci", days=7, branch="main"))
    assert "no recorded selections in this window" in main


def test_the_ci_page_flags_time_saved_from_too_few_runs(
    client: TestClient, db_session: Session
) -> None:
    now = datetime.now(UTC)
    hour = (now - at(0)) / timedelta(hours=1) - 1
    for n, h in enumerate((hour - 30, hour - 20, hour - 10)):
        full_leg(db_session, f"f{n}", h, 20)
    run(db_session, "s", hour=hour)
    stored(db_session, "s", ranked(TEST))
    signed_in(client, ADMIN)
    text_ = squash(page_text(client, f"/ui/repos/{REPO}/-/ci", days=7))
    assert "too few runs for a reliable figure (1 run)" in text_


def test_slowest_tests_only_those_that_ran_in_the_window(
    client: TestClient, db_session: Session
) -> None:
    now = datetime.now(UTC)
    hour = (now - at(0)) / timedelta(hours=1)
    slow, skipped, gone = "t::slow", "t::skipped_now", "t::not_in_window"
    record(db_session, commit=1, hour=hour - 24 * 20, tests={slow: P, skipped: P, gone: P})
    record(db_session, commit=2, hour=hour - 24 * 19, tests={slow: P, skipped: P, gone: P})
    record(db_session, commit=3, hour=hour - 2, tests={slow: P, skipped: S})
    db_session.execute(text(
        "UPDATE test_results tr SET duration_ms = CASE t.test_id WHEN 't::slow' THEN 4000 "
        "ELSE 90000 END FROM tests t WHERE t.id = tr.test_key"))  # fmt: skip
    repo_id = db_session.scalars(select(Repo.id).where(Repo.name == REPO)).one()
    recompute_repo_stats(db_session, repo_id, 90)
    found = slowest_tests(db_session, REPO, 7)
    assert [(s.test_id, s.mean_ms, s.runs) for s in found or ()] == [(slow, 4000.0, 3)]
    everything = slowest_tests(db_session, REPO, None)
    assert [s.test_id for s in everything or ()] == [gone, skipped, slow]
    signed_in(client, ADMIN)
    text_ = squash(page_text(client, f"/ui/repos/{REPO}/-/ci", days=7))
    assert "4.0 s mean over the last 3 runs" in text_ and "not_in_window" not in text_
    assert "p95" not in text_


# --- the outcomes backfill ----------------------------------------------------------------


def outcomes(session: Session) -> list[tuple[str, int, str]]:
    rows = session.execute(
        text("SELECT ci_run_id, run_attempt, source FROM attempt_outcomes ORDER BY 1, 2")
    )
    return [(ci, attempt, source) for ci, attempt, source in rows]


def stored_rows(session: Session) -> list[Any]:
    return [
        *session.execute(
            text("SELECT run_id, selection_id, failing, unknown FROM run_outcomes ORDER BY run_id")
        ).all(),
        *session.execute(
            text(
                "SELECT run_id, test_key, verdict, would_run FROM run_failures "
                "ORDER BY run_id, test_key"
            )
        ).all(),
        *session.execute(
            text(
                "SELECT ci_run_id, run_attempt, source, fail_on FROM attempt_outcomes ORDER BY 1, 2"
            )
        ).all(),
    ]


def test_backfill_computes_only_attempts_without_an_outcome_and_is_idempotent(
    db_session: Session,
) -> None:
    run(db_session, "live", hour=900, tests={TEST: F, OTHER: P})
    stored(db_session, "live", ranked(TEST))
    assert compare_ci_run(db_session, REPO, "live", 1) is not None
    run(db_session, "sel", hour=901, tests={TEST: F, OTHER: P}, commit=51)  # outcome step failed
    stored(db_session, "sel", ranked(TEST), commit=51)
    run(db_session, "old", hour=902, tests={TEST: F, OTHER: P}, commit=52)  # an older client
    run(
        db_session, "old", hour=902, tests={TEST: P}, commit=52, rerun=1
    )  # a re-run: not an attempt
    repo_id = db_session.scalars(select(Repo.id).where(Repo.name == REPO)).one()
    assert [(p.ci_run_id, p.selection) for p in pending_attempts(db_session, repo_id)] == [
        ("sel", True), ("old", False)]  # fmt: skip
    before = ci_stats(db_session, REPO, None, "all", NOW)
    found = backfill_outcomes(db_session, REPO, window_days=90)
    assert found is not None
    assert (found.pending, found.computed_with_selection, found.computed_without_selection,
            found.skipped_live, found.failed, found.next, found.remaining) == (
        2, 1, 1, 0, [], None, 0)  # fmt: skip
    assert outcomes(db_session) == [("live", 1, OUTCOME_LIVE), ("old", 1, OUTCOME_BACKFILL),
                                    ("sel", 1, OUTCOME_BACKFILL)]  # fmt: skip
    after = ci_stats(db_session, REPO, None, "all", NOW)
    assert before is not None and after is not None
    # Backfilled outcomes never make a selection: coverage and modes are unchanged.
    assert (after.runs, after.recorded, after.selective_runs) == (
        before.runs, before.recorded, before.selective_runs)  # fmt: skip
    assert (after.backfilled, after.backfilled_without_selection) == (2, 1)
    assert after.failures.runs == before.failures.runs + 1  # "sel" had a selection
    # Twice: nothing changes.
    rows = stored_rows(db_session)
    again = backfill_outcomes(db_session, REPO, window_days=90)
    assert again is not None and (again.pending, again.computed, again.remaining) == (0, 0, 0)
    assert stored_rows(db_session) == rows


def test_backfill_resumes_after_a_batch_and_a_dry_run_writes_nothing(
    db_session: Session,
) -> None:
    for n in range(3):
        run(db_session, f"r{n}", hour=900 + n, tests={TEST: F}, commit=50 + n)
    dry = backfill_outcomes(db_session, REPO, window_days=90, dry_run=True)
    assert dry is not None and (dry.pending, dry.computed, dry.remaining) == (3, 0, 3)
    assert outcomes(db_session) == []
    first = backfill_outcomes(db_session, REPO, window_days=90, limit=1)
    assert first is not None and (first.computed, first.remaining) == (1, 2)
    assert first.next is not None and first.next.ci_run_id == "r0"
    second = backfill_outcomes(db_session, REPO, window_days=90, limit=1, after=first.next)
    assert second is not None and (second.pending, second.computed, second.remaining) == (2, 1, 1)
    # Stopped there: a fresh run (no cursor) finds what's left.
    rest = backfill_outcomes(db_session, REPO, window_days=90)
    assert rest is not None and (rest.pending, rest.computed, rest.next) == (1, 1, None)
    assert [ci for ci, _, _ in outcomes(db_session)] == ["r0", "r1", "r2"]
    # The time limit: no new attempt after max_seconds (the first one always runs).
    run(db_session, "r3", hour=903, tests={TEST: F}, commit=53)
    run(db_session, "r4", hour=904, tests={TEST: F}, commit=54)
    capped = backfill_outcomes(db_session, REPO, window_days=90, max_seconds=0)
    assert capped is not None and (capped.computed, capped.remaining) == (1, 1)


def _no_peeking_data(session: Session, *, uploaded_late_by: timedelta) -> None:
    """``pr`` fails TEST at hour 900. Main passed TEST at 800; a main run at 899 failed it,
    but was uploaded only at hour 950 (after ``pr``'s last upload, at 900 + 10 min, when it
    was uploaded by CI): with it, TEST is pre-existing; without it, unknown."""
    record(session, commit=1, hour=800, tests={TEST: P, OTHER: P}, ci_run_id="m1")
    record(session, commit=2, hour=899, tests={TEST: F, OTHER: P}, ci_run_id="m2")
    record(session, commit=3, hour=900, branch="pr", tests={TEST: F, OTHER: P}, ci_run_id="pr")
    session.execute(text("UPDATE runs SET created_at = started_at + interval '10 minutes'"))
    session.execute(text("UPDATE runs SET created_at = :late WHERE ci_run_id = 'm2'"),
                    {"late": at(950)})  # fmt: skip
    session.execute(text("UPDATE runs SET created_at = started_at + :late WHERE ci_run_id = 'pr'"),
                    {"late": uploaded_late_by})  # fmt: skip


def _verdict(session: Session) -> str:
    query = text(
        "SELECT f.verdict FROM run_failures f JOIN runs r ON r.id = f.run_id "
        "WHERE r.ci_run_id = 'pr'"
    )
    return str(session.execute(query).scalar_one())


def test_backfill_uses_only_what_was_uploaded_by_the_attempts_last_upload(
    db_session: Session,
) -> None:
    _no_peeking_data(db_session, uploaded_late_by=timedelta(minutes=10))
    # Today's history would call it pre-existing (the late main run is there now).
    today = triage_ci_run(db_session, REPO, "pr", 1)
    assert today and today[0].report and today[0].report.results[0].verdict is Verdict.PRE_EXISTING
    found = backfill_outcomes(db_session, REPO, window_days=90)
    assert found is not None and found.computed >= 1
    assert _verdict(db_session) == Verdict.UNKNOWN.value  # what a live call could have known


def test_backfill_of_an_imported_attempt_reads_everything_that_ran_before_it(
    db_session: Session,
) -> None:
    # Uploaded days after it ran (scripts/backfill.py): there was no live moment.
    _no_peeking_data(db_session, uploaded_late_by=LIVE_UPLOAD + timedelta(days=2))
    found = backfill_outcomes(db_session, REPO, window_days=90)
    assert found is not None and found.computed >= 1
    assert _verdict(db_session) == Verdict.PRE_EXISTING.value


def test_a_live_outcome_wins_over_the_backfill(db_session: Session) -> None:
    run(db_session, "a", hour=900, tests={TEST: F, OTHER: P})
    stored(db_session, "a", ranked(TEST))
    # Backfill first, then the live call: the live one replaces it, one row per leg.
    assert backfill_outcomes(db_session, REPO, window_days=90) is not None
    assert outcomes(db_session) == [("a", 1, OUTCOME_BACKFILL)]
    assert compare_ci_run(db_session, REPO, "a", 1, fail_on=["new_regression"]) is not None
    assert outcomes(db_session) == [("a", 1, OUTCOME_LIVE)]
    assert db_session.scalar(text("SELECT count(*) FROM run_outcomes")) == 1
    # Live first: the backfill doesn't touch it.
    rows = stored_rows(db_session)
    again = backfill_outcomes(db_session, REPO, window_days=90)
    assert again is not None and again.pending == 0 and stored_rows(db_session) == rows


def _committed_attempt(engine: Engine, repo: str) -> int:
    with Session(engine) as session:
        session.execute(text("DELETE FROM repos WHERE name IN (:r, :shared)"),
                        {"r": repo, "shared": REPO})  # fmt: skip
        # record() writes to REPO; renamed so it can't meet the rolled-back tests' data.
        run_ = record(session, commit=7, hour=900, tests={TEST: F, OTHER: P}, ci_run_id="race")
        session.execute(text("UPDATE repos SET name = :r WHERE id = :id"),
                        {"r": repo, "id": run_.repo_id})  # fmt: skip
        session.commit()
        return run_.repo_id


def _wait_for_lock_waiters(engine: Engine, n: int) -> None:
    deadline = time.monotonic() + 20
    with engine.connect() as connection:
        while time.monotonic() < deadline:
            waiting = connection.execute(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
            )).scalar_one()  # fmt: skip
            if waiting >= n:
                return
            time.sleep(0.05)
    raise AssertionError(f"expected {n} waiting for the attempt's lock")


def test_a_live_outcome_racing_the_backfill_wins_with_no_error_or_duplicate(
    db_engine: Engine,
) -> None:
    repo = "race/live-wins"
    repo_id = _committed_attempt(db_engine, repo)
    key = f"{repo_id}:race:1"
    try:
        # 1. The live call holds the attempt (emulated: its lock, then its committed row);
        #    the backfill picked the attempt before and now waits for the lock.
        with db_engine.connect() as live:
            live.execute(text("SELECT pg_advisory_xact_lock(3, hashtext(:k))"), {"k": key})
            results: dict[str, Any] = {}

            def backfill() -> None:
                with Session(db_engine) as session:
                    results["backfill"] = backfill_outcomes(session, repo, window_days=90)

            thread = threading.Thread(target=backfill)
            thread.start()
            _wait_for_lock_waiters(db_engine, 1)
            live.execute(text("INSERT INTO attempt_outcomes (repo_id, ci_run_id, run_attempt) "
                              "VALUES (:r, 'race', 1)"), {"r": repo_id})  # fmt: skip
            live.commit()
            thread.join(30)
        found = results["backfill"]
        assert (found.skipped_live, found.computed, found.failed) == (1, 0, [])
        with Session(db_engine) as session:
            assert session.execute(text(
                "SELECT source FROM attempt_outcomes WHERE repo_id = :r"), {"r": repo_id}
            ).scalars().all() == [OUTCOME_LIVE]  # fmt: skip
            session.execute(text("DELETE FROM attempt_outcomes WHERE repo_id = :r"),
                            {"r": repo_id})  # fmt: skip
            session.commit()
        # 2. Both really at once, whoever gets the lock first: the live outcome stands, one
        #    row per leg, no error.
        errors: list[BaseException] = []

        def call(fn: Any) -> None:
            try:
                with Session(db_engine) as session:
                    fn(session)
            except BaseException as exc:  # noqa: BLE001 (reported below)
                errors.append(exc)

        with db_engine.connect() as gate:
            gate.execute(text("SELECT pg_advisory_xact_lock(3, hashtext(:k))"), {"k": key})
            threads = [
                threading.Thread(
                    target=call, args=(lambda s: backfill_outcomes(s, repo, window_days=90),)
                ),
                threading.Thread(target=call, args=(lambda s: compare_ci_run(s, repo, "race", 1),)),
            ]
            for thread in threads:
                thread.start()
            _wait_for_lock_waiters(db_engine, 2)
            gate.rollback()
        for thread in threads:
            thread.join(60)
        assert errors == []
        with Session(db_engine) as session:
            assert session.execute(text(
                "SELECT source FROM attempt_outcomes WHERE repo_id = :r"), {"r": repo_id}
            ).scalars().all() == [OUTCOME_LIVE]  # fmt: skip
            assert session.scalar(text(
                "SELECT count(*) FROM run_outcomes o JOIN runs r ON r.id = o.run_id "
                "WHERE r.repo_id = :r"), {"r": repo_id}) == 1  # fmt: skip
    finally:
        with Session(db_engine) as session:
            session.execute(text("DELETE FROM repos WHERE id = :r"), {"r": repo_id})
            session.commit()


# --- the API, its scopes, and the labels ----------------------------------------------------


def test_backfill_needs_a_write_token_for_its_repo(client: TestClient, db_session: Session) -> None:
    run(db_session, "a", hour=900, tests={TEST: F})
    _, read = tokens.create_token(db_session, REPO, Scope.READ, "r")
    _, write = tokens.create_token(db_session, REPO, Scope.WRITE, "w")
    _, other = tokens.create_token(db_session, "acme/other", Scope.WRITE, "o")
    path = f"/repos/{REPO}/outcomes/backfill"
    body = {"dry_run": True}
    assert (
        client.post(path, json=body, headers={"Authorization": f"Bearer {read}"}).status_code == 403
    )
    assert (
        client.post(path, json=body, headers={"Authorization": f"Bearer {other}"}).status_code
        == 403
    )
    ok = client.post(path, json=body, headers={"Authorization": f"Bearer {write}"})
    assert ok.status_code == 200 and ok.json()["pending"] == 1 and outcomes(db_session) == []
    done = client.post(path, json={}, headers=AUTH).json()  # the admin token
    assert (done["computed_without_selection"], done["remaining"], done["next"]) == (1, 0, None)
    for bad in ({"limit": 101}, {"max_seconds": 61}, {"max_seconds": 0}, {"extra": 1}):
        assert client.post(path, json=bad, headers=AUTH).status_code == 422, bad
    assert (
        client.post("/repos/nobody/x/outcomes/backfill", json={}, headers=AUTH).status_code == 404
    )


def test_backfilled_outcomes_are_labelled_everywhere(
    client: TestClient, db_session: Session
) -> None:
    now = datetime.now(UTC)
    hour = (now - at(0)) / timedelta(hours=1) - 1
    run(db_session, "with", hour=hour, tests={TEST: F, OTHER: P})
    stored(db_session, "with", ranked(TEST))  # a 0.4 run whose outcome step failed
    run(db_session, "without", hour=hour, tests={TEST: F}, commit=51)  # an older client
    found = backfill_outcomes(db_session, REPO, window_days=90)
    assert found is not None and (found.computed, found.failed) == (2, []), found
    assert outcomes(db_session) == [("with", 1, "backfill"), ("without", 1, "backfill")]
    signed_in(client, ADMIN)
    with_page = squash(page_text(client, f"/ui/repos/{REPO}/-/runs/with/1"))
    assert "Outcome: verdicts computed later ." in with_page  # tags became spaces
    assert "no selection recorded" not in with_page
    without_page = squash(page_text(client, f"/ui/repos/{REPO}/-/runs/without/1"))
    assert "Outcome: verdicts computed later; no selection recorded ." in without_page
    runs = client.get(f"/ui/repos/{REPO}/-/runs").text
    row_with = re.search(r"runs/with/1.*?</tr>", runs, re.S)
    row_without = re.search(r"runs/without/1.*?</tr>", runs, re.S)
    assert row_with and row_without
    assert ">verdicts computed later<" in row_with.group(0)
    assert ">verdicts computed later; no selection recorded<" in row_without.group(0)
    ci = squash(page_text(client, f"/ui/repos/{REPO}/-/ci", days=7))
    assert "verdicts computed later for 2 (1: verdicts computed later; no selection recorded)" in ci


# --- the CLI loop ---------------------------------------------------------------------------

SERVER = "http://siftwise.test"
BACKFILL_URL = f"{SERVER}/repos/acme/shop/outcomes/backfill"


def batch(**overrides: Any) -> dict[str, Any]:
    return {"repo": "acme/shop", "dry_run": False, "pending": 3, "with_selection": 1,
            "computed_with_selection": 0, "computed_without_selection": 0, "skipped_live": 0,
            "failed": [], "next": None, "remaining": 0, **overrides}  # fmt: skip


@respx.mock
def test_the_cli_loops_over_batches_until_nothing_is_left(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIFTWISE_API_TOKEN", "t")
    cursor = {"run_at": "2026-10-01T00:00:00Z", "ci_run_id": "a", "run_attempt": 1}
    route = respx.post(BACKFILL_URL)
    route.side_effect = [
        httpx.Response(200, json=batch(computed_with_selection=1, next=cursor, remaining=2)),
        httpx.Response(200, json=batch(pending=2, computed_without_selection=1, skipped_live=1)),
    ]
    result = CliRunner().invoke(cli.app, ["outcomes", "backfill", "--repo", "acme/shop",
                                          "--server", SERVER, "--batch", "5"])  # fmt: skip
    assert result.exit_code == 0, result.output
    bodies = [json.loads(c.request.content) for c in route.calls]
    assert bodies[0] == {"dry_run": False, "limit": 5, "max_seconds": 30}
    assert bodies[1]["after"] == cursor
    out = result.stdout
    assert "3 CI run attempts without an outcome (1 with a recorded selection)." in out
    assert ('Computed 2: 1 "verdicts computed later", 1 "verdicts computed later; no selection '
            'recorded"; skipped 1 (a live outcome got there first); failed 0.') in out  # fmt: skip


@respx.mock
def test_the_cli_dry_run_and_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIFTWISE_API_TOKEN", "t")
    respx.post(BACKFILL_URL).respond(json=batch(dry_run=True, remaining=3))
    args = ["outcomes", "backfill", "--repo", "acme/shop", "--server", SERVER]
    dry = CliRunner().invoke(cli.app, [*args, "--dry-run"])
    assert dry.exit_code == 0 and "Dry run, nothing written: 1 would be labelled" in dry.stdout
    respx.post(BACKFILL_URL).respond(json=batch(
        failed=[{"ci_run_id": "x", "run_attempt": 2, "error": "OperationalError"}]))  # fmt: skip
    failed = CliRunner().invoke(cli.app, args)
    assert failed.exit_code == 1
    assert "failed: x attempt 2 (OperationalError); run it again to retry" in failed.stdout
    assert CliRunner().invoke(cli.app, [*args, "--batch", "101"]).exit_code == 2
