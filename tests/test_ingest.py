"""Ingest's batched writes store exactly what the ORM path stored."""

from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import event, insert, text
from sqlalchemy.orm import Session

from siftwise.core import ingest
from siftwise.core.ingest import create_run
from siftwise.core.junit import ParsedTestResult, Status
from siftwise.core.models import MESSAGE_MAX_BYTES, TRUNCATION_MARKER, TestResult
from siftwise.core.schemas import RunMetadata

REPO = "acme/shop"


def result(
    test_id: str,
    status: Status,
    *,
    attempt: int = 1,
    duration: int | None = 5,
    message: str | None = None,
    file_path: str | None = None,
) -> ParsedTestResult:
    classname, _, name = test_id.partition("::")
    return ParsedTestResult(test_id, classname, name, file_path, status, duration, attempt, message)


RESULTS = [
    result("tests.test_a::test_pass", Status.PASSED, file_path="tests/test_a.py"),
    result("tests.test_a::test_fail", Status.FAILED, message="assert 1 == 2"),
    result("tests.test_a::test_skip", Status.SKIPPED, duration=None, message="no network"),
    result("tests.test_a::test_error", Status.ERROR, duration=0, message="é" * 3000),  # > 4 KB
    result("tests.test_b::test_retried", Status.FAILED, message="flake"),
    result("tests.test_b::test_retried", Status.PASSED, attempt=2, duration=7),
    result("github.com/acme/shop/pay::TestCard/sub", Status.PASSED, duration=None),
]


def ingest_run(session: Session, ci: str, results: Sequence[ParsedTestResult]) -> int:
    meta = RunMetadata(repo=REPO, commit_sha="a" * 40, branch="main", is_main=True,
                       ci_run_id=ci, started_at=None)  # fmt: skip
    run, created = create_run(session, meta, results, rollup=False)
    assert created
    return run.id


def stored(session: Session, run_id: int) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in session.execute(
            text(
                "SELECT t.test_id, tr.attempt, tr.status, tr.duration_ms, tr.message"
                " FROM test_results tr JOIN tests t ON t.id = tr.test_key"
                " WHERE tr.run_id = :run ORDER BY t.test_id, tr.attempt"
            ),
            {"run": run_id},
        )
    ]


def known_tests(session: Session) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in session.execute(text(
        "SELECT id, test_id, file_path FROM tests ORDER BY id"))]  # fmt: skip


def use_the_orm_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The code before the batched writes: an upsert of every test ID, and the ORM's
    executemany insert of the results (its column types convert statuses and truncate)."""
    new_keys = ingest._test_keys

    def upsert_all(
        session: Session, repo_id: int, run_id: int, results: list[ParsedTestResult]
    ) -> dict[str, int]:
        session.execute(
            text(
                "INSERT INTO tests (repo_id, test_id) "
                "SELECT :repo_id, t FROM unnest(CAST(:test_ids AS text[])) t ORDER BY t "
                "ON CONFLICT (repo_id, test_id) DO NOTHING"
            ),
            {"repo_id": repo_id, "test_ids": sorted({r.test_id for r in results})},
        )
        return new_keys(session, repo_id, run_id, results)

    def orm_insert(
        session: Session, run_id: int, keys: dict[str, int], results: list[ParsedTestResult]
    ) -> None:
        rows = [
            {"run_id": run_id, "test_key": keys[r.test_id], "status": r.status,
             "duration_ms": r.duration_ms, "attempt": r.attempt, "message": r.message}
            for r in results
        ]  # fmt: skip
        session.execute(insert(TestResult).execution_options(render_nulls=True), rows)

    monkeypatch.setattr(ingest, "_test_keys", upsert_all)
    monkeypatch.setattr(ingest, "_insert_results", orm_insert)


def test_batched_writes_store_the_same_rows_as_the_orm_path(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Known tests first, so the next uploads mix known and new test IDs.
    ingest_run(db_session, "seed", RESULTS[:3])
    later = [*RESULTS, result("tests.test_c::test_new", Status.PASSED, file_path="tests/c.py")]

    new_run = ingest_run(db_session, "new", later)
    new_rows, new_tests = stored(db_session, new_run), known_tests(db_session)
    with monkeypatch.context() as m:
        use_the_orm_path(m)
        orm_run = ingest_run(db_session, "orm", later)

    assert stored(db_session, orm_run) == new_rows
    assert known_tests(db_session) == new_tests  # no test created twice, same paths
    # The rows themselves: statuses, NULL durations, retries, the truncated message.
    by_id = {(row[0], row[1]): row for row in new_rows}
    assert by_id[("tests.test_b::test_retried", 1)][2:] == (1, 5, "flake")  # status codes
    assert by_id[("tests.test_b::test_retried", 2)][2:] == (0, 7, None)
    message = by_id[("tests.test_a::test_error", 1)][4]
    assert message.endswith(TRUNCATION_MARKER)
    assert len(message.encode("utf-8")) <= MESSAGE_MAX_BYTES
    assert by_id[("tests.test_a::test_skip", 1)][3] is None


def test_only_missing_test_ids_are_inserted(db_session: Session) -> None:
    ingest_run(db_session, "first", RESULTS[:2])
    before = known_tests(db_session)
    statements: list[str] = []

    def record(*args: Any) -> None:
        statements.append(args[2])

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        ingest_run(db_session, "same-tests", RESULTS[:2])  # nothing new
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert known_tests(db_session) == before
    assert not [s for s in statements if s.startswith("INSERT INTO tests")]
