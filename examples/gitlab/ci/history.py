"""The example's siftwise history, between the jobs of one pipeline, each running its own server.

    python ci/history.py restore            wait for Postgres, migrate, load .siftwise-history/
    python ci/history.py wait               wait for this job's siftwise server
    python ci/history.py dump               save the database to .siftwise-history/ (an artifact)
    python ci/history.py record-base JUNIT  record JUNIT as a main run of the merge request's
                                            base commit (CI_MERGE_REQUEST_DIFF_BASE_SHA)

restore reads .siftwise-history/ from an earlier job of THIS pipeline (its artifacts), else
starts empty. Nothing moves between pipelines: a default-branch pipeline records its own
history, and a merge request pipeline's first job (siftwise-base) runs the full suite at the
MR's base commit and records it with record-base, so the later jobs have main's results to
compare with. Not part of siftwise: a real project runs one server that keeps its history, and
its jobs need none of this.

The dump records its migration revision (alembic_version), and restore loads it into that
schema before migrating on, so a dump from an older siftwise still loads; one from a newer
siftwise (a revision this one doesn't have) is skipped. siftwise's own dogfood workflow
(.github/workflows/dogfood.yml) uses restore and dump too, with SIFTWISE_HISTORY_DIR set:
keep them free of GitLab specifics.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

import psycopg
from psycopg import sql

HISTORY = Path(os.environ.get("SIFTWISE_HISTORY_DIR") or ".siftwise-history")
REVISION = "alembic_version"  # the dump's migration revision, a file in HISTORY
SERVER = "http://127.0.0.1:8000"
SERVER_LOG = Path("/tmp/siftwise-server.log")


def connect() -> psycopg.Connection[tuple[object, ...]]:
    url = os.environ["SIFTWISE_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    return psycopg.connect(url, autocommit=True)


def tables(conn: psycopg.Connection[tuple[object, ...]]) -> list[str]:
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
        "AND tablename <> 'alembic_version' ORDER BY tablename"
    ).fetchall()
    return [str(row[0]) for row in rows]


def dump() -> None:
    HISTORY.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert version is not None
        (HISTORY / REVISION).write_text(str(version[0]), encoding="utf-8")
        names = tables(conn)
        for table in names:
            query = sql.SQL("COPY {} TO STDOUT (FORMAT binary)").format(sql.Identifier(table))
            with (HISTORY / f"{table}.copy").open("wb") as out, conn.cursor().copy(query) as copy:
                for block in copy:
                    out.write(block)
        print(f"history: saved {len(names)} tables to {HISTORY}/ ({counts(conn)})")


def counts(conn: psycopg.Connection[tuple[object, ...]]) -> str:
    runs = conn.execute("SELECT count(*), count(*) FILTER (WHERE is_main) FROM runs").fetchone()
    tests = conn.execute("SELECT count(*) FROM tests").fetchone()
    assert runs is not None and tests is not None
    return f"{runs[0]} runs, {runs[1]} on main, {tests[0]} tests"


def known_revision(revision: str) -> bool:
    from alembic.script import ScriptDirectory
    from alembic.script.revision import ResolutionError
    from alembic.util import CommandError

    from siftwise.migrate import alembic_config

    try:
        return ScriptDirectory.from_config(alembic_config()).get_revision(revision) is not None
    except (ResolutionError, CommandError):
        return False


def restore() -> None:
    from siftwise.migrate import upgrade

    wait_for_postgres()
    url = os.environ["SIFTWISE_DATABASE_URL"]
    saved = HISTORY / REVISION
    revision = saved.read_text(encoding="utf-8").strip() if saved.is_file() else None
    if not HISTORY.is_dir():
        print(f"history: no {HISTORY}/ to load: starting empty")
        upgrade(url)
        return
    if revision is not None and not known_revision(revision):
        print(f"history: {HISTORY}/ is at migration {revision}, which this siftwise doesn't "
              "have (a newer one dumped it): starting empty")  # fmt: skip
        upgrade(url)
        return
    # The dump's own schema first (an older dump has older tables), then this siftwise's.
    upgrade(url, revision or "head")
    with connect() as conn:
        names = tables(conn)
        missing = [name for name in names if not (HISTORY / f"{name}.copy").is_file()]
        # Load in any order: no foreign key triggers while loading (needs a superuser, which the
        # postgres image's POSTGRES_USER is).
        conn.execute("SET session_replication_role = replica")
        conn.execute(sql.SQL("TRUNCATE {} CASCADE").format(sql.SQL(", ").join(
            sql.Identifier(name) for name in names)))  # fmt: skip
        for table in names:
            path = HISTORY / f"{table}.copy"
            if path.is_file():
                query = sql.SQL("COPY {} FROM STDIN (FORMAT binary)").format(sql.Identifier(table))
                with conn.cursor().copy(query) as copy:
                    copy.write(path.read_bytes())
        conn.execute("SET session_replication_role = DEFAULT")
        # New rows must not reuse the restored IDs.
        serials = conn.execute(
            "SELECT c.relname, a.attname,"
            " pg_get_serial_sequence(format('%I', c.relname), a.attname)"
            " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " JOIN pg_attribute a ON a.attrelid = c.oid"
            " WHERE n.nspname = 'public' AND c.relkind = 'r' AND a.attnum > 0"
            " AND NOT a.attisdropped"
        ).fetchall()
        setval = sql.SQL("SELECT setval(%s, COALESCE((SELECT max({}) FROM {}), 0) + 1, false)")
        for owner, column, sequence in serials:
            if sequence:
                ids = setval.format(sql.Identifier(str(column)), sql.Identifier(str(owner)))
                conn.execute(ids, [sequence])
        if missing:
            print(f"history: not in {HISTORY}/, left empty: {', '.join(missing)}")
    if revision is not None:
        upgrade(url)
    with connect() as conn:
        at = f"migration {revision}" if revision else "an unrecorded migration"
        print(
            f"history: restored {len(names) - len(missing)} tables from {HISTORY}/ ({at}): "
            f"{counts(conn)}"
        )


def wait_for_postgres() -> None:
    for _ in range(60):
        try:
            with connect():
                return
        except psycopg.OperationalError:
            time.sleep(1)
    sys.exit("history: Postgres didn't start")


def wait_for_server() -> None:
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"{SERVER}/healthz", timeout=2):
                return
        except OSError:
            time.sleep(1)
    if SERVER_LOG.is_file():
        print(SERVER_LOG.read_text(encoding="utf-8", errors="replace")[-4000:])
    sys.exit("history: the siftwise server didn't start")


def base_started_at(committed_at: str, pipeline_created_at: str | None) -> str:
    """When the base run counts as started: its commit time, but always before this pipeline,
    so triage of this pipeline's run sees it (no peeking) even if a clock was off."""
    started = datetime.fromisoformat(committed_at)
    if pipeline_created_at:
        before_pipeline = datetime.fromisoformat(pipeline_created_at) - timedelta(seconds=1)
        started = min(started, before_pipeline)
    return started.isoformat()


def base_record_args(env: Mapping[str, str], junit: str, committed_at: str) -> list[str]:
    """`siftwise record` arguments for the merge request's base commit, as a default-branch run.

    Plain `siftwise record`, not `siftwise gitlab record`: that one records this pipeline's own
    run (CI_PIPELINE_ID), which the test job uploads later. Same repo name as the gitlab
    commands (the merge request's target project)."""
    repo = env.get("CI_MERGE_REQUEST_PROJECT_PATH") or env["CI_PROJECT_PATH"]
    started_at = base_started_at(committed_at, env.get("CI_PIPELINE_CREATED_AT"))
    return [
        "record", junit,
        "--repo", repo,
        "--branch", env.get("CI_DEFAULT_BRANCH") or "main",
        "--commit", env["CI_MERGE_REQUEST_DIFF_BASE_SHA"],
        "--main",
        "--ci-run-id", f"base-of-{env['CI_PIPELINE_ID']}",
        "--started-at", started_at,
    ]  # fmt: skip


def record_base(junit: str) -> None:
    env = os.environ
    base = env.get("CI_MERGE_REQUEST_DIFF_BASE_SHA")
    if not base:
        print("history: no CI_MERGE_REQUEST_DIFF_BASE_SHA (not a merge request pipeline, or a "
              "merge train): nothing to record")  # fmt: skip
        return
    if not Path(junit).is_file():
        print(f"history: the base commit's tests wrote no {junit}: nothing to record")
        return
    show = ["git", "show", "-s", "--format=%cI", base]
    committed_at = subprocess.run(show, check=True, capture_output=True, text=True).stdout.strip()
    args = base_record_args(env, junit, committed_at)
    print(f"history: recording {base[:12]} ({args[args.index('--branch') + 1]}) as a main run")
    subprocess.run(["siftwise", *args], check=True)


if __name__ == "__main__":
    commands = {"restore": restore, "dump": dump, "wait": wait_for_server}
    if len(sys.argv) == 3 and sys.argv[1] == "record-base":
        record_base(sys.argv[2])
    elif len(sys.argv) == 2 and sys.argv[1] in commands:
        commands[sys.argv[1]]()
    else:
        sys.exit(f"usage: python ci/history.py {'|'.join(commands)} | record-base JUNIT")
