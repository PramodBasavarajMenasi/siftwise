"""Data migrations, run on their own scratch database (the shared test database stays at head)."""

from collections.abc import Iterator
from datetime import date

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.orm import Session

from siftwise.core.history import recompute_repo_stats
from tests.conftest import alembic_config

SCRATCH_DB = "siftwise_migration_test"


@pytest.fixture
def scratch_engine(db_url: str) -> Iterator[tuple[Engine, str]]:
    admin = create_engine(db_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB}"))
        connection.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))
    url = make_url(db_url).set(database=SCRATCH_DB).render_as_string(hide_password=False)
    engine = create_engine(url)
    try:
        yield engine, url
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB}"))
        admin.dispose()


def sha(n: int) -> str:
    return f"{n:040x}"


# Legacy (0005) rows: two repos sharing a test ID, retries, a message, file paths that change
# over time with the newest run ingested first (lowest id), and a test without a path.
LEGACY = f"""
INSERT INTO repos (id, name) VALUES (1, 'acme/shop'), (2, 'acme/other');
INSERT INTO runs (id, repo_id, commit_sha, branch, is_main, ci_run_id, started_at) VALUES
    (10, 1, '{sha(3)}', 'main', true, '3', '2026-01-03'),
    (11, 1, '{sha(1)}', 'main', true, '1', '2026-01-01'),
    (12, 1, '{sha(2)}', 'main', true, '2', '2026-01-02'),
    (13, 2, '{sha(9)}', 'main', true, '9', '2026-01-01');
INSERT INTO test_results (run_id, test_id, file_path, status, duration_ms, attempt, message) VALUES
    (10, 't::a', NULL, 'passed', 5, 1, NULL),
    (11, 't::a', 'tests/old/test_a.py', 'failed', 7, 1, 'boom'),
    (11, 't::a', 'tests/old/test_a.py', 'passed', 6, 2, NULL),
    (12, 't::a', 'tests/test_a.py', 'error', NULL, 1, 'import error'),
    (12, 't::b', NULL, 'skipped', 0, 1, 'skip reason'),
    (13, 't::a', 'other/test_a.py', 'passed', 1, 1, NULL);
"""

LEGACY_ROWS = """
SELECT r.run_id, r.test_id, r.status, r.duration_ms, r.attempt, r.message
FROM test_results r ORDER BY 1, 2, 5
"""


def test_0006_converts_results_in_place_and_back(scratch_engine: tuple[Engine, str]) -> None:
    engine, url = scratch_engine
    config = alembic_config(url)
    command.upgrade(config, "0005")
    with engine.begin() as connection:
        for statement in LEGACY.split(";"):
            if statement.strip():
                connection.execute(text(statement))
        before = connection.execute(text(LEGACY_ROWS)).all()

    command.upgrade(config, "0006")

    with engine.begin() as connection:
        tests = connection.execute(
            text("SELECT repo_id, test_id, file_path, file_path_at::date FROM tests ORDER BY 1, 2")
        ).all()
        rows = connection.execute(
            text(
                "SELECT r.run_id, t.test_id, r.status, r.duration_ms, r.attempt, r.message "
                "FROM test_results r JOIN tests t ON t.id = r.test_key ORDER BY 1, 2, 5"
            )
        ).all()
        primary_key = connection.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'pk_test_results'"
            )
        )
    # Latest path by run time (2026-01-02), though run 10 (newest, no path) was ingested first.
    assert [tuple(t) for t in tests] == [
        (1, "t::a", "tests/test_a.py", date(2026, 1, 2)),
        (1, "t::b", None, None),
        (2, "t::a", "other/test_a.py", date(2026, 1, 1)),
    ]
    codes = {"passed": 0, "failed": 1, "skipped": 2, "error": 3}
    assert [tuple(r) for r in rows] == [
        (run, test, codes[status], duration, attempt, message)
        for run, test, status, duration, attempt, message in before
    ]
    assert primary_key == "PRIMARY KEY (run_id, test_key, attempt)"

    # The converted data rolls up.
    with engine.begin() as connection:
        with Session(bind=connection) as session:
            assert recompute_repo_stats(session, 1, 0) == 2
            session.flush()
        failures = connection.scalar(
            text("SELECT failures FROM test_stats WHERE repo_id = 1 AND test_id = 't::a'")
        )
    assert failures == 1  # run 12 (error); run 11's retry passed

    command.downgrade(config, "0005")

    with engine.begin() as connection:
        after = connection.execute(text(LEGACY_ROWS)).all()
        paths = connection.execute(
            text("SELECT DISTINCT test_id, file_path FROM test_results ORDER BY 1, 2")
        ).all()
        next_id = connection.scalar(
            text(
                "INSERT INTO test_results (run_id, test_id, status) VALUES (10, 't::c', 'passed') "
                "RETURNING id"
            )
        )
    assert after == before
    # Results get their test's latest path: per-result paths were not kept.
    assert [tuple(p) for p in paths] == [
        ("t::a", "other/test_a.py"),
        ("t::a", "tests/test_a.py"),
        ("t::b", None),
    ]
    assert next_id == len(before) + 1  # the id sequence continues after the restored rows
