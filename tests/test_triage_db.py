"""Triage against the database: evidence from history, no peeking, batched queries."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from siftwise.core.ingest import create_run
from siftwise.core.junit import ParsedTestResult, Status
from siftwise.core.models import Run
from siftwise.core.schemas import ChangeGraph, RunMetadata
from siftwise.core.triage import (
    SAME_COMMIT,
    UNRELATED_FAIL_THEN_PASS,
    Change,
    Failure,
    Target,
    TriageReport,
    TriageResult,
    VariantTriage,
    Verdict,
    load_histories,
    triage_ci_run,
    triage_run,
)

REPO = "acme/shop"
T0 = datetime(2026, 10, 1, tzinfo=UTC)
P, F, S = Status.PASSED, Status.FAILED, Status.SKIPPED
TEST = "tests.test_cart::test_total"  # maps to src/cart.py
OTHER = "tests.test_db::test_query"
NO_LINKS = ChangeGraph()  # a recorded change graph that links nothing


def sha(n: int) -> str:
    return f"{n:040x}"


def at(hour: float) -> datetime:
    return T0 + timedelta(hours=hour)


def record(
    session: Session,
    *,
    commit: int,
    hour: float,
    tests: dict[str, Status | Sequence[Status]],
    branch: str = "main",
    variant: str | None = "linux",
    ci_run_id: str | None = None,
    run_attempt: int = 1,
    changed: Sequence[str] | None = ("docs/notes.txt",),
    graph: ChangeGraph | None = NO_LINKS,
    message: str | None = "AssertionError: assert 2 == 3",
    file_path: str | None = None,
) -> Run:
    """One run. ``changed`` None = unknown diff; ``graph`` None = no stored change links."""
    results = []
    for test_id, statuses in tests.items():
        attempts = [statuses] if isinstance(statuses, Status) else statuses
        for attempt, status in enumerate(attempts, start=1):
            classname, _, name = test_id.partition("::")
            results.append(ParsedTestResult(
                test_id, classname, name, file_path, status, 5, attempt,
                message if status is F else None,
            ))  # fmt: skip
    meta = RunMetadata(
        repo=REPO,
        commit_sha=sha(commit),
        branch=branch,
        is_main=branch == "main",
        ci_run_id=ci_run_id or f"ci-{commit}-{hour}",
        run_attempt=run_attempt,
        variant=variant,
        started_at=at(hour),
        changed_files=list(changed or ()),
        changed_files_known=changed is not None,
        affected=graph,
    )
    run, created = create_run(session, meta, results, rollup=False)
    assert created
    return run


def target(commit: int = 99, **kwargs: Any) -> Target:
    return Target(REPO, sha(commit), **{"variant": "linux", **kwargs})


def run_triage(
    session: Session,
    *test_ids: str,
    change: Change | None = None,
    message: str | None = None,
    window_days: int = 90,
    **kwargs: Any,
) -> TriageReport:
    report = triage_run(
        session,
        target(**kwargs),
        [Failure(t, message) for t in test_ids or (TEST,)],
        change or Change(("docs/unrelated.txt",), True),
        window_days,
    )
    assert report is not None
    return report


def result(session: Session, test_id: str = TEST, **kwargs: Any) -> TriageResult:
    report = run_triage(session, test_id, **kwargs)
    return next(r for r in report.results if r.test_id == test_id)


def test_affected_is_stored_with_the_run(db_session: Session) -> None:
    graph = ChangeGraph(affected_files={"tests/test_cart.py": ["src/cart.py"]})
    run = record(db_session, commit=1, hour=1, tests={TEST: P}, graph=graph)
    no_graph = record(db_session, commit=2, hour=2, tests={TEST: P}, graph=None)

    assert run.affected == {"affected_packages": {}, "affected_files": {
        "tests/test_cart.py": ["src/cart.py"]}, "depends": [], "go_module": None}  # fmt: skip
    assert no_graph.affected is None


def test_unknown_repo(db_session: Session) -> None:
    assert load_histories(db_session, Target("nobody/nothing", sha(1)), [TEST]) is None


# --- pre-existing -------------------------------------------------------------------------


def test_pre_existing_per_variant_with_streak_evidence(db_session: Session) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: P}, variant="linux")
    record(db_session, commit=1, hour=1, tests={TEST: P}, variant="macos")
    record(db_session, commit=2, hour=2, tests={TEST: F}, variant="linux")
    record(db_session, commit=2, hour=2, tests={TEST: P}, variant="macos")
    record(db_session, commit=3, hour=3, tests={TEST: S}, variant="linux")  # skipped: ignored
    record(db_session, commit=4, hour=4, tests={TEST: F}, variant="linux")
    record(db_session, commit=4, hour=4, tests={TEST: P}, variant="macos")

    linux = result(db_session)
    macos = result(db_session, variant="macos")

    assert (linux.verdict, linux.confidence.value) == (Verdict.PRE_EXISTING, "high")
    streak = linux.history.broken_on_main
    assert streak is not None
    assert (streak.since.commit_sha, streak.main_runs_failing) == (sha(2), 2)
    assert streak.last_main_pass is not None and streak.last_main_pass.commit_sha == sha(1)
    # macOS passes on main: the Linux failure there is only a note.
    assert macos.verdict is not Verdict.PRE_EXISTING
    assert [r.variant for r in macos.history.other_variants_failing] == ["linux"]
    assert macos.history.last_main_pass is not None
    assert macos.history.last_main_pass.commit_sha == sha(4)


def test_a_streak_older_than_the_window_is_read_back(db_session: Session) -> None:
    record(db_session, commit=1, hour=0, tests={TEST: P})
    for n, hour in enumerate((48, 72, 96, 120), start=2):
        record(db_session, commit=n, hour=hour, tests={TEST: F})

    found = result(db_session, window_days=1)  # the window holds the last 2 main runs

    streak = found.history.broken_on_main
    assert streak is not None
    assert (streak.since.commit_sha, streak.main_runs_failing) == (sha(2), 4)
    assert streak.last_main_pass is not None and streak.last_main_pass.commit_sha == sha(1)


def test_streak_walk_reads_beyond_the_first_rounds(db_session: Session) -> None:
    # 70 main runs: the pass is in the third round (after 10 and 50 runs).
    record(db_session, commit=1, hour=0, tests={TEST: P})
    for n in range(2, 72):
        record(db_session, commit=n, hour=n, tests={TEST: F, OTHER: P})

    found = result(db_session)

    assert found.history.broken_on_main is not None
    assert found.history.broken_on_main.main_runs_failing == 70
    assert found.history.broken_on_main.since.commit_sha == sha(2)


# --- flaky --------------------------------------------------------------------------------


def test_same_commit_pass_is_flaky_and_retries_are_same_commit_events(
    db_session: Session,
) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: P})
    record(db_session, commit=5, hour=2, tests={TEST: [F, P]}, branch="b")  # a retry
    record(db_session, commit=99, hour=3, tests={TEST: P}, branch="pr", ci_run_id="9")

    rerun = result(db_session, ci_run_id="9", run_attempt=2)

    assert (rerun.verdict, rerun.confidence.value) == (Verdict.FLAKY, "high")
    assert [r.commit_sha for r in rerun.history.same_commit_passes] == [sha(99)]
    [event] = rerun.history.flaky
    assert (event.kind, event.commit_sha) == (SAME_COMMIT, sha(5))
    assert rerun.history.failures_in_window == 0  # the retry passed: not a failed run


def fail_then_pass(
    session: Session,
    first: int,
    hour: float,
    *,
    branch: str = "feature",
    failed_change: Sequence[str] | None = ("src/other.py",),
    passed_change: Sequence[str] | None = ("src/other.py", "src/more.py"),
    failed_graph: ChangeGraph | None = NO_LINKS,
    passed_graph: ChangeGraph | None = NO_LINKS,
) -> None:
    record(session, commit=first, hour=hour, tests={TEST: F}, branch=branch,
           changed=failed_change, graph=failed_graph)  # fmt: skip
    # a re-run of the same commit in between: not "the next run" for this rule
    record(session, commit=first, hour=hour + 0.5, tests={TEST: F}, branch=branch,
           changed=failed_change, graph=failed_graph)  # fmt: skip
    record(session, commit=first + 1, hour=hour + 1, tests={TEST: P}, branch=branch,
           changed=passed_change, graph=passed_graph)  # fmt: skip


def test_unrelated_fail_then_pass_is_flaky_evidence(db_session: Session) -> None:
    record(db_session, commit=1, hour=0, tests={TEST: P})
    fail_then_pass(db_session, 10, 10)

    once = result(db_session)
    fail_then_pass(db_session, 20, 20, branch="other")
    twice = result(db_session, message="AssertionError: assert 7 == 8")

    assert (once.verdict, once.confidence.value) == (Verdict.FLAKY, "medium")
    [event] = once.history.flaky
    assert event.kind == UNRELATED_FAIL_THEN_PASS
    assert [r.commit_sha for r in event.failed] == [sha(10), sha(10)]  # with its re-run
    assert [r.commit_sha for r in event.passed] == [sha(11)]
    assert once.flaky_counts == {SAME_COMMIT: 0, UNRELATED_FAIL_THEN_PASS: 1}
    assert (twice.verdict, twice.confidence.value) == (Verdict.FLAKY, "high")
    assert [e.commit_sha for e in twice.history.flaky] == [sha(20), sha(10)]  # newest first
    assert all(match for _, match in twice.flaky)  # assert N == M, normalised
    assert twice.history.failures_in_window == 4


@pytest.mark.parametrize(
    "kwargs",
    [
        {"failed_graph": None},  # backfilled: relatedness unknown
        {"passed_graph": None},
        {"failed_change": None},  # unknown diff
        {"passed_change": None},
        {"failed_change": ("src/cart.py",)},  # related: path mapping
        {"passed_graph": ChangeGraph(affected_files={"tests/test_cart.py": ["src/more.py"]})},
        {"passed_change": ("pyproject.toml",)},  # a root build file links everything
    ],
)
def test_fail_then_pass_needs_both_runs_known_and_unrelated(
    db_session: Session, kwargs: dict[str, Any]
) -> None:
    record(db_session, commit=1, hour=0, tests={TEST: P})
    fail_then_pass(db_session, 10, 10, **kwargs)

    found = result(db_session)

    assert found.history.flaky == ()
    assert found.verdict is Verdict.UNKNOWN


def test_linked_and_flaky_is_unknown_and_linked_alone_a_regression(db_session: Session) -> None:
    record(db_session, commit=1, hour=0, tests={TEST: P, OTHER: P})
    fail_then_pass(db_session, 10, 10)
    cart = Change(("src/cart.py",), True)

    report = run_triage(db_session, TEST, OTHER, change=cart)
    flaky, other = report.results

    assert (flaky.verdict, flaky.reasons[0]) == (
        Verdict.UNKNOWN, "linked to this change but known flaky: re-run to tell")  # fmt: skip
    assert other.verdict is Verdict.UNKNOWN  # test_db isn't linked to src/cart.py
    regression = result(db_session, OTHER, change=Change(("src/db.py",), True))
    assert (regression.verdict, regression.confidence.value) == (Verdict.NEW_REGRESSION, "high")


def test_unseen_test(db_session: Session) -> None:
    record(db_session, commit=1, hour=0, tests={OTHER: P})

    new = result(db_session, change=Change(("tests/test_cart.py",), True))
    unknown = result(db_session)

    assert (new.verdict, new.history.seen) == (Verdict.NEW_REGRESSION, False)
    assert unknown.verdict is Verdict.UNKNOWN


# --- no peeking ---------------------------------------------------------------------------


def history_before(session: Session) -> None:
    record(session, commit=1, hour=1, tests={TEST: P, OTHER: P})
    fail_then_pass(session, 10, 10)
    record(session, commit=2, hour=20, tests={TEST: P, OTHER: P})


def history_after(session: Session) -> None:
    """Later runs that would change every verdict if triage peeked."""
    record(session, commit=50, hour=200, tests={TEST: F, OTHER: F})  # main breaks
    record(session, commit=51, hour=201, tests={TEST: F, OTHER: F})
    record(session, commit=99, hour=202, tests={TEST: P, OTHER: P}, branch="pr")  # same commit
    record(session, commit=99, hour=203, tests={TEST: P, OTHER: P}, branch="pr", variant="macos")
    fail_then_pass(session, 60, 210)
    fail_then_pass(session, 70, 220)


def summary(report: TriageReport) -> list[tuple[Any, ...]]:
    return [
        (r.test_id, r.verdict, r.confidence, r.reasons, r.history, r.flaky) for r in report.results
    ]


def test_runs_at_and_after_the_cut_off_are_never_used(db_session: Session) -> None:
    history_before(db_session)
    triaged = record(db_session, commit=99, hour=100, tests={TEST: F, OTHER: F}, branch="pr",
                     ci_run_id="777")  # fmt: skip
    before = run_triage(db_session, TEST, OTHER, ci_run_id="777")
    history_after(db_session)

    after = run_triage(db_session, TEST, OTHER, ci_run_id="777")
    as_of = run_triage(db_session, TEST, OTHER, as_of=at(100))

    assert before.run_id == after.run_id == triaged.id
    assert before.cut_at == after.cut_at == at(100)
    assert summary(after) == summary(before)
    assert [r.verdict for r in after.results] == [Verdict.FLAKY, Verdict.UNKNOWN]
    # as_of alone, without the CI run: the same cut, and the triaged run itself not counted
    assert as_of.run_id is None
    assert [r.verdict for r in as_of.results] == [r.verdict for r in before.results]


def test_the_triaged_main_run_is_not_its_own_streak(db_session: Session) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: P})
    record(db_session, commit=2, hour=2, tests={TEST: F}, ci_run_id="main-2")

    found = result(db_session, commit=2, ci_run_id="main-2", change=Change(("src/cart.py",), True))

    assert found.history.broken_on_main is None
    assert found.verdict is Verdict.NEW_REGRESSION


def test_file_paths_reported_at_or_after_the_cut_off_are_not_used(db_session: Session) -> None:
    record(db_session, commit=1, hour=1, tests={"t::a": P})
    results = [ParsedTestResult("t::a", "t", "a", "pkg/test_a.py", P, 5, 1)]
    meta = RunMetadata(repo=REPO, commit_sha=sha(2), branch="main", is_main=True,
                       started_at=at(50), variant="linux")  # fmt: skip
    create_run(db_session, meta, results, rollup=False)

    early = load_histories(db_session, target(as_of=at(10)), ["t::a"])
    late = load_histories(db_session, target(as_of=at(60)), ["t::a"])

    assert early is not None and late is not None
    assert early.tests["t::a"].file_path is None
    assert late.tests["t::a"].file_path == "pkg/test_a.py"


# --- batching -----------------------------------------------------------------------------


def count_queries(session: Session, test_ids: Sequence[str]) -> int:
    statements: list[str] = []

    def before(*args: Any) -> None:
        if not args[2].startswith(("SAVEPOINT", "RELEASE")):  # the test session's own
            statements.append(args[2])

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", before)
    try:
        run_triage(session, *test_ids)
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return len(statements)


def test_query_count_does_not_grow_with_failures(db_session: Session) -> None:
    many = [f"tests.test_m{i}::test_x" for i in range(60)]
    record(db_session, commit=1, hour=0, tests=dict.fromkeys(many, P))
    for n in range(2, 80):  # enough main runs for every round of the streak walk
        record(db_session, commit=n, hour=n, tests=dict.fromkeys(many, F))
    fail_then_pass(db_session, 200, 200)
    record(db_session, commit=300, hour=300, tests=dict.fromkeys(many, F), branch="b",
           changed=("x.txt",))  # fmt: skip
    record(db_session, commit=301, hour=301, tests=dict.fromkeys(many, P), branch="b",
           changed=("x.txt",))  # fmt: skip

    few, lots = count_queries(db_session, many[:2]), count_queries(db_session, many)

    assert few == lots
    assert lots <= 14


def test_other_legs_of_the_triaged_ci_run_are_left_out(db_session: Session) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: P}, variant="macos")
    record(db_session, commit=1, hour=1, tests={TEST: P})
    # The same main CI run: its macOS leg finished (and was recorded) a bit earlier.
    record(db_session, commit=2, hour=1.9, tests={TEST: F}, variant="macos", ci_run_id="m2")
    record(db_session, commit=2, hour=2, tests={TEST: F}, ci_run_id="m2")

    triaged = result(db_session, commit=2, ci_run_id="m2")
    unidentified = result(db_session, commit=2, as_of=at(2))

    assert triaged.history.other_variants_failing == ()
    # Without the CI run ID, an earlier upload is just history.
    assert [r.variant for r in unidentified.history.other_variants_failing] == ["macos"]


# --- a recorded CI run attempt ------------------------------------------------------------


def ci_triage(session: Session, ci_run_id: str, run_attempt: int = 1) -> list[VariantTriage]:
    variants = triage_ci_run(session, REPO, ci_run_id, run_attempt)
    assert variants is not None
    return variants


def verdicts(variant: VariantTriage) -> dict[str, Verdict]:
    assert variant.report is not None
    return {r.test_id: r.verdict for r in variant.report.results}


def test_ci_run_failures_are_final_attempts_of_each_variant(db_session: Session) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: P, OTHER: P})
    record(db_session, commit=2, hour=5, branch="pr", ci_run_id="7",
           tests={TEST: F, OTHER: [F, P], "t::late": [P, F]})  # fmt: skip
    record(db_session, commit=2, hour=5, branch="pr", ci_run_id="7", variant="macos",
           tests={TEST: P, OTHER: P})  # fmt: skip

    linux, macos = ci_triage(db_session, "7")

    assert (linux.variant, macos.variant) == ("linux", "macos")
    # Failed then passed on a retry: not a failure. Passed then failed: one.
    assert set(verdicts(linux)) == {TEST, "t::late"}
    assert macos.report is None  # nothing failed: nothing to triage
    assert linux.report is not None and linux.report.run_id == linux.run_id


def test_ci_run_uses_the_stored_change_links(db_session: Session) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: P})
    graph = ChangeGraph(affected_files={"tests/test_cart.py": ["src/money.py"]})
    record(db_session, commit=2, hour=5, branch="pr", ci_run_id="8", tests={TEST: F},
           changed=("src/money.py",), graph=graph)  # fmt: skip

    [linux] = ci_triage(db_session, "8")

    assert linux.links_known
    assert linux.report is not None
    [regression] = linux.report.results
    assert regression.verdict == Verdict.NEW_REGRESSION
    assert [link.changed for link in regression.links] == ["src/money.py"]


def test_ci_run_without_stored_links_is_an_unknown_change(db_session: Session) -> None:
    # A graph failed at record time (runs.affected NULL): an import-only link can't be seen,
    # so the change must not look unrelated (that would make a real regression look flaky).
    record(db_session, commit=1, hour=1, tests={TEST: P, "t::broken": P})
    record(db_session, commit=2, hour=2, tests={TEST: P, "t::broken": F})
    fail_then_pass(db_session, 10, 10)  # TEST has flaky evidence
    record(db_session, commit=20, hour=20, branch="pr", ci_run_id="9",
           tests={TEST: F, "t::broken": F}, changed=("src/money.py",), graph=None)  # fmt: skip

    [linux] = ci_triage(db_session, "9")

    assert (linux.changed_files_known, linux.links_known) == (True, False)
    # Rule 3, not rule 7 (flaky); rule 2 still applies: it was failing on main already.
    assert verdicts(linux) == {TEST: Verdict.UNKNOWN, "t::broken": Verdict.PRE_EXISTING}


def test_ci_run_never_uses_its_own_attempt_as_history(db_session: Session) -> None:
    # The macOS leg of the same attempt ran the new test earlier. It isn't history, so the
    # Linux failure has none; a re-run (attempt 2) does see attempt 1's results.
    record(db_session, commit=1, hour=1, tests={TEST: P})
    record(db_session, commit=2, hour=4, branch="pr", ci_run_id="10", variant="macos",
           tests={"t::new": P})  # fmt: skip
    record(db_session, commit=2, hour=5, branch="pr", ci_run_id="10", tests={"t::new": F})
    record(db_session, commit=2, hour=6, branch="pr", ci_run_id="10", run_attempt=2,
           tests={"t::new": F})  # fmt: skip

    first = ci_triage(db_session, "10")[0]
    rerun = ci_triage(db_session, "10", run_attempt=2)[0]

    assert first.report is not None and rerun.report is not None
    assert not first.report.results[0].history.seen
    assert rerun.report.results[0].history.seen


def test_ci_run_file_paths_from_later_runs_are_not_used(db_session: Session) -> None:
    # Rule 4: a new test whose own file changed is a regression. The path must be the run's
    # own (or older): a later run's path would be peeking.
    record(db_session, commit=1, hour=1, tests={TEST: P})
    record(db_session, commit=2, hour=5, branch="pr", ci_run_id="11", tests={"web::adds": F},
           changed=("web/cart.test.js",), file_path="web/cart.test.js")  # fmt: skip

    assert verdicts(ci_triage(db_session, "11")[0]) == {"web::adds": Verdict.NEW_REGRESSION}

    record(db_session, commit=3, hour=9, tests={"web::adds": P}, file_path="web/moved.test.js")

    assert verdicts(ci_triage(db_session, "11")[0]) == {"web::adds": Verdict.UNKNOWN}


def test_ci_run_unknown_repo_or_attempt(db_session: Session) -> None:
    record(db_session, commit=1, hour=1, tests={TEST: P}, ci_run_id="12")

    assert triage_ci_run(db_session, "nobody/nothing", "12") is None
    assert ci_triage(db_session, "12", run_attempt=2) == []
    assert ci_triage(db_session, "nope") == []
