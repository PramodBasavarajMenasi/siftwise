"""Data migrations, run on their own scratch database (the shared test database stays at head)."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from siftwise import migrate
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

    # The converted data rolls up (with today's rollup, so at today's schema).
    command.upgrade(config, "head")
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


# --- siftwise server migrate ---------------------------------------------------------------


HEAD = ScriptDirectory.from_config(migrate.alembic_config()).get_current_head()


def revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        if not connection.scalar(text("SELECT to_regclass('alembic_version') IS NOT NULL")):
            return None
        version: str | None = connection.scalar(text("SELECT version_num FROM alembic_version"))
        return version


def test_migrate_upgrades_a_fresh_database_from_the_package(
    scratch_engine: tuple[Engine, str],
) -> None:
    engine, url = scratch_engine

    migrate.upgrade(url)
    migrate.upgrade(url)  # nothing left to do

    assert HEAD is not None and revision(engine) == HEAD


def test_migrate_waits_for_another_replica_holding_the_lock(
    scratch_engine: tuple[Engine, str],
) -> None:
    engine, url = scratch_engine
    lock = dict(zip(("ns", "key"), migrate.MIGRATION_LOCK, strict=True))

    with engine.connect() as other, ThreadPoolExecutor(max_workers=1) as pool:
        other.execute(text("SELECT pg_advisory_lock(:ns, :key)"), lock)
        other.commit()
        replica = pool.submit(migrate.upgrade, url)
        try:
            with pytest.raises(TimeoutError):
                replica.result(timeout=1.5)  # blocked on the lock...
            assert revision(engine) is None  # ...having migrated nothing
        finally:
            other.execute(text("SELECT pg_advisory_unlock(:ns, :key)"), lock)
            other.commit()
        replica.result(timeout=120)  # then runs, and raises if it failed

    assert revision(engine) == HEAD


def test_0013_adds_api_tokens_and_drops_them_again(scratch_engine: tuple[Engine, str]) -> None:
    engine, url = scratch_engine
    config = alembic_config(url)
    command.upgrade(config, "0013")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO api_tokens (token_hash, repo, scope) VALUES (:h, 'acme/shop', 'read')"
            ),
            {"h": "0" * 64},
        )
        row = connection.execute(
            text("SELECT name, created_at, last_used_at FROM api_tokens")
        ).one()
        assert row.name == "" and row.created_at is not None and row.last_used_at is None
        with pytest.raises(Exception, match="ck_api_tokens_scope"), connection.begin_nested():
            connection.execute(
                text("INSERT INTO api_tokens (token_hash, repo, scope) VALUES ('1', 'x', 'admin')")
            )
        with pytest.raises(Exception, match="uq_api_tokens_token_hash"), connection.begin_nested():
            connection.execute(
                text("INSERT INTO api_tokens (token_hash, repo, scope) VALUES (:h, 'x', 'write')"),
                {"h": "0" * 64},
            )

    command.downgrade(config, "0012")

    with engine.connect() as connection:
        assert connection.execute(text("SELECT to_regclass('api_tokens')")).scalar() is None
    command.upgrade(config, "head")


def test_0014_adds_fixture_files_and_the_fixture_cause(scratch_engine: tuple[Engine, str]) -> None:
    engine, url = scratch_engine
    config = alembic_config(url)
    command.upgrade(config, "0014")
    with engine.connect() as connection:
        default = connection.execute(text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'shadow_selections' AND column_name = 'fixture_files'"
        )).scalar()  # fmt: skip
        check = connection.execute(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_shadow_failures_cause'"
        )).scalar()  # fmt: skip
    assert default is not None and "'{}'" in default
    assert check is not None and "'fixture'" in check

    command.downgrade(config, "0013")

    with engine.connect() as connection:
        check = connection.execute(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_shadow_failures_cause'"
        )).scalar()  # fmt: skip
        column = connection.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'shadow_selections' AND column_name = 'fixture_files'"
        )).scalar()  # fmt: skip
    assert check is not None and "'fixture'" not in check and column is None
    command.upgrade(config, "head")


def test_0015_adds_known_flaky_expired_and_drops_it_again(
    scratch_engine: tuple[Engine, str],
) -> None:
    engine, url = scratch_engine
    config = alembic_config(url)
    command.upgrade(config, "0015")
    column = text(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = 'runs' AND column_name = 'known_flaky_expired'"
    )
    with engine.connect() as connection:
        default = connection.execute(column).scalar()
    assert default is not None and "'[]'" in default

    command.downgrade(config, "0014")

    with engine.connect() as connection:
        assert connection.execute(column).first() is None
    command.upgrade(config, "head")


# 0016 shadow data: a selective selection with a budget, one without, their results and
# failures (one caught, one missed with a cause, one flaky).
SHADOW_0016 = f"""
INSERT INTO repos (id, name) VALUES (1, 'acme/shop');
INSERT INTO runs (id, repo_id, commit_sha, branch, is_main, ci_run_id, variant, started_at)
VALUES (10, 1, '{sha(1)}', 'pr', false, '7', 'linux', '2026-11-01'),
       (11, 1, '{sha(1)}', 'pr', false, '7', 'mac', '2026-11-01'),
       (12, 1, '{sha(2)}', 'pr', false, '8', NULL, '2026-11-02');
INSERT INTO tests (id, repo_id, test_id) VALUES (1, 1, 't::a'), (2, 1, 't::b'), (3, 1, 't::c');
INSERT INTO shadow_selections (id, repo_id, ci_run_id, run_attempt, variant, commit_sha,
    created_at, mode, reason, selected_keys, go_selected_keys, go_packages, python_files,
    selected_count, total_known, estimated_ms_total, estimated_ms_would_run, status, budget)
VALUES (1, 1, '7', 1, NULL, '{sha(1)}', '2026-11-01', 'selective', 'why', '{{1,3}}', '{{}}',
        '{{}}', '{{}}', 2, 3, 300, 200, 'compared',
        '{{"seconds": 60, "full_suite": false, "left_out": 1, "left_out_ms": 5,
          "left_out_risk_share": 0.1, "before": {{"full": false, "keys": [1, 2, 3],
          "go_keys": [], "go_packages": [], "python_files": [], "java_classes": []}}}}'),
       (2, 1, '8', 1, NULL, '{sha(2)}', '2026-11-02', 'full', 'build', '{{}}', '{{}}', '{{}}',
        '{{}}', 3, 3, 300, 300, 'compared', NULL);
INSERT INTO shadow_results (run_id, selection_id, failing, caught, missed, flaky, tests_total,
    tests_would_run, ms_total, ms_would_run)
VALUES (10, 1, 3, 1, 1, 1, 3, 2, 300, 200), (11, 1, 0, 0, 0, 0, 3, 2, 310, 210),
       (12, 2, 1, 1, 0, 0, 3, 3, 290, 290);
INSERT INTO shadow_failures (run_id, test_key, would_run, verdict, confidence, cause,
    cause_detail, suggestion)
VALUES (10, 1, true, 'new_regression', 'high', NULL, NULL, NULL),
       (10, 2, false, 'unknown', 'low', 'budget', 'left out', NULL),
       (10, 3, true, 'flaky', 'high', NULL, NULL, NULL),
       (12, 1, true, 'pre_existing', 'medium', NULL, NULL, NULL);
SELECT setval('shadow_selections_id_seq', 2);
"""
SHADOW_ROWS_0016 = {
    "shadow_selections": "SELECT id, ci_run_id, mode, selected_keys, budget, status "
    "FROM shadow_selections ORDER BY id",
    "shadow_results": "SELECT run_id, selection_id, failing, caught, missed, flaky, "
    "tests_total, tests_would_run, ms_total, ms_would_run FROM shadow_results ORDER BY run_id",
    "shadow_failures": "SELECT run_id, test_key, would_run, verdict, confidence, cause "
    "FROM shadow_failures ORDER BY run_id, test_key",
}


def test_0017_generalises_the_shadow_tables_and_back(scratch_engine: tuple[Engine, str]) -> None:
    engine, url = scratch_engine
    config = alembic_config(url)
    command.upgrade(config, "0016")
    with engine.begin() as connection:
        connection.execute(text(SHADOW_0016))
    with engine.connect() as connection:
        before = {t: connection.execute(text(q)).all() for t, q in SHADOW_ROWS_0016.items()}

    command.upgrade(config, "0017")

    with engine.begin() as connection:
        for old in SHADOW_ROWS_0016:
            assert connection.execute(text(f"SELECT to_regclass('{old}')")).scalar() is None
        rows = connection.execute(text(
            "SELECT id, shadow, ranked, budget->>'limited', budget->>'applied', reason_code, "
            "reason_items FROM ci_selections ORDER BY id"
        )).all()  # fmt: skip
        assert [tuple(r) for r in rows] == [
            (1, True, False, "true", "true", None, []),
            (2, True, False, None, None, None, []),
        ]
        # Which client uploaded a run: new, empty for every existing run.
        assert connection.execute(text(
            "SELECT count(*), count(client), count(client_version) FROM runs"
        )).one() == (3, 0, 0)  # fmt: skip
        connection.execute(text("UPDATE runs SET client = 'github', client_version = '0.4.0' "
                                "WHERE id = 10"))  # fmt: skip
        # The measured time moved to runs; the verdict counts were filled from the failures.
        assert connection.execute(text(
            "SELECT id, tests_run, measured_ms FROM runs ORDER BY id"
        )).all() == [(10, 3, 300.0), (11, 3, 310.0), (12, 3, 290.0)]  # fmt: skip
        assert connection.execute(text(
            "SELECT run_id, new_regressions, pre_existing, unknown, flaky, failures_selected, "
            "caught, missed FROM run_outcomes ORDER BY run_id"
        )).all() == [(10, 1, 0, 1, 1, 2, 1, 1), (11, 0, 0, 0, 0, 0, 0, 0),
                     (12, 0, 1, 0, 0, 1, 1, 0)]  # fmt: skip
        # 0017 rows 0016 can't hold: a non-shadow selection with an outcome, an outcome
        # without a selection, an attempt outcome. Downgrade drops them, keeps the rest.
        connection.execute(text(f"""
            INSERT INTO runs (id, repo_id, commit_sha, branch, is_main, ci_run_id, started_at,
                              tests_run, measured_ms)
            VALUES (13, 1, '{sha(3)}', 'pr', false, '9', '2026-11-03', 3, 100),
                   (14, 1, '{sha(4)}', 'pr', false, '10', '2026-11-04', 3, 100);
            INSERT INTO ci_selections (repo_id, ci_run_id, run_attempt, commit_sha, mode,
                reason, selected_keys, go_selected_keys, go_packages, python_files,
                selected_count, total_known, estimated_ms_total, estimated_ms_would_run,
                shadow, ranked, budget)
            VALUES (1, '9', 1, '{sha(3)}', 'selective', 'x', '{{2,1}}', '{{}}', '{{}}', '{{}}',
                    2, 3, 300, 200, false, true, '{{"seconds": 60, "applied": true,
                    "limited": false}}');
            INSERT INTO run_outcomes (run_id, selection_id, failing, flaky)
            VALUES (13, currval('ci_selections_id_seq'), 1, 0), (14, NULL, 1, 0);
            INSERT INTO run_failures (run_id, test_key, would_run, rank, verdict, confidence)
            VALUES (13, 1, true, 2, 'unknown', 'low'), (14, 1, NULL, NULL, 'unknown', 'low');
            INSERT INTO attempt_outcomes (repo_id, ci_run_id, run_attempt, fail_on)
            VALUES (1, '9', 1, '{{new_regression}}');
        """))  # fmt: skip

    command.downgrade(config, "0016")

    with engine.connect() as connection:
        after = {t: connection.execute(text(q)).all() for t, q in SHADOW_ROWS_0016.items()}
        assert after == before
        assert connection.execute(text("SELECT to_regclass('attempt_outcomes')")).scalar() is None
        assert connection.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'runs' "
            "AND column_name IN ('measured_ms', 'tests_run', 'client', 'client_version')"
        )).all() == []  # fmt: skip
    command.upgrade(config, "head")
    with engine.connect() as connection:  # and back up: the columns again, empty (the
        # downgrade kept the runs, only their client columns went)
        assert connection.execute(text(
            "SELECT count(*), count(client), count(client_version) FROM runs"
        )).one() == (5, 0, 0)  # fmt: skip


def test_0018_adds_the_outcome_source_and_drops_it_again(
    scratch_engine: tuple[Engine, str],
) -> None:
    engine, url = scratch_engine
    config = alembic_config(url)
    command.upgrade(config, "0017")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO repos (id, name) VALUES (1, 'acme/shop');"
            "INSERT INTO attempt_outcomes (repo_id, ci_run_id, run_attempt) VALUES (1, '9', 1)"
        ))  # fmt: skip

    command.upgrade(config, "0018")

    source = text("SELECT ci_run_id, source FROM attempt_outcomes ORDER BY 1")
    with engine.begin() as connection:
        assert connection.execute(source).all() == [("9", "ci")]  # existing rows were live
        connection.execute(text(
            "INSERT INTO attempt_outcomes (repo_id, ci_run_id, run_attempt, source) "
            "VALUES (1, '10', 1, 'backfill')"))  # fmt: skip
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO attempt_outcomes (repo_id, ci_run_id, run_attempt, source) "
            "VALUES (1, '11', 1, 'guess')"))  # fmt: skip

    command.downgrade(config, "0017")

    with engine.connect() as connection:
        column = text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'attempt_outcomes' AND column_name = 'source'"
        )
        assert connection.execute(column).all() == []
        kept = text("SELECT ci_run_id FROM attempt_outcomes ORDER BY 1")
        assert connection.execute(kept).scalars().all() == ["10", "9"]
    command.upgrade(config, "head")
    with engine.connect() as connection:  # back up: every row reads as a live one again
        assert connection.execute(source).all() == [("10", "ci"), ("9", "ci")]


def test_0019_adds_test_variant_stats_and_drops_it_again(
    scratch_engine: tuple[Engine, str],
) -> None:
    engine, url = scratch_engine
    config = alembic_config(url)
    command.upgrade(config, "0018")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO repos (id, name) VALUES (1, 'acme/shop');"
            "INSERT INTO tests (id, repo_id, test_id) VALUES (7, 1, 't::a')"
        ))  # fmt: skip

    command.upgrade(config, "0019")

    rows = text("SELECT test_key, variant, runs, avg_duration_ms FROM test_variant_stats")
    with engine.begin() as connection:
        assert connection.execute(rows).all() == []  # empty: the next rollup fills it
        connection.execute(text(
            "INSERT INTO test_variant_stats (repo_id, test_key, variant, runs, avg_duration_ms, "
            "last_seen_at) VALUES (1, 7, 'linux', 3, 12.5, now())"))  # fmt: skip
        assert connection.execute(rows).all() == [(7, "linux", 3, 12.5)]
    with pytest.raises(IntegrityError), engine.begin() as connection:  # one row per variant
        connection.execute(text(
            "INSERT INTO test_variant_stats (repo_id, test_key, variant, runs, last_seen_at) "
            "VALUES (1, 7, 'linux', 1, now())"))  # fmt: skip
    with engine.begin() as connection:  # rows go with their test
        connection.execute(text("DELETE FROM tests WHERE id = 7"))
        assert connection.execute(rows).all() == []

    command.downgrade(config, "0018")

    table = text("SELECT to_regclass('test_variant_stats')")
    with engine.connect() as connection:
        assert connection.scalar(table) is None
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(table) is not None
