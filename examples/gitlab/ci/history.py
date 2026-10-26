"""The example's siftwise history, between jobs that each run their own server.

    python ci/history.py restore   wait for Postgres, migrate, load .siftwise-history/
    python ci/history.py wait      wait for this job's siftwise server
    python ci/history.py dump      save the database to .siftwise-history/ (an artifact)

restore reads .siftwise-history/ from an earlier job of this pipeline (its artifacts), else
from the latest successful pipeline of the default branch: its siftwise-rollup job's artifacts,
downloaded with CI_JOB_TOKEN, which may read job artifacts
(https://docs.gitlab.com/api/job_artifacts/#download-job-artifacts-by-reference-name,
https://docs.gitlab.com/ci/jobs/ci_job_token/). Merge request pipelines read main's history
and never write it.

Not part of siftwise: a real project runs one server, and its jobs need none of this.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.client import HTTPMessage
from pathlib import Path
from typing import IO

import psycopg
from psycopg import sql

HISTORY = Path(".siftwise-history")
SERVER = "http://127.0.0.1:8000"
SERVER_LOG = Path("/tmp/siftwise-server.log")
ROLLUP_JOB = "siftwise-rollup"


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
    HISTORY.mkdir(exist_ok=True)
    with connect() as conn:
        names = tables(conn)
        for table in names:
            query = sql.SQL("COPY {} TO STDOUT (FORMAT binary)").format(sql.Identifier(table))
            with (HISTORY / f"{table}.copy").open("wb") as out, conn.cursor().copy(query) as copy:
                for block in copy:
                    out.write(block)
    print(f"history: saved {len(names)} tables to {HISTORY}/")


def restore() -> None:
    wait_for_postgres()
    subprocess.run(["siftwise", "server", "migrate"], check=True)
    if not HISTORY.is_dir() and not fetch_from_default_branch():
        print("history: none yet, starting empty")
        return
    with connect() as conn:
        names = tables(conn)
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
    print(f"history: restored {HISTORY}/")


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


class _NoTokenElsewhere(urllib.request.HTTPRedirectHandler):
    """Follow the redirect GitLab may answer with (a CDN), without sending the job token to
    another host."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if (
            new is not None
            and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc
        ):
            new.remove_header("Job-token")
        return new


def fetch_from_default_branch() -> bool:
    env = os.environ
    token = env.get("CI_JOB_TOKEN")
    api, project = env.get("CI_API_V4_URL"), env.get("CI_PROJECT_ID")
    if not (token and api and project):
        return False
    ref = urllib.parse.quote(env.get("CI_DEFAULT_BRANCH", "main"), safe="")
    url = f"{api}/projects/{project}/jobs/artifacts/{ref}/download?job={ROLLUP_JOB}"
    request = urllib.request.Request(url, headers={"JOB-TOKEN": token})
    try:
        with urllib.request.build_opener(_NoTokenElsewhere).open(request, timeout=120) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:  # 404: no successful default-branch pipeline yet
        print(f"history: nothing from the default branch (HTTP {exc.code})")
        return False
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            parts = Path(name).parts
            # Only our own files, never outside the directory.
            if len(parts) == 2 and parts[0] == HISTORY.name and ".." not in parts:
                archive.extract(name)
    print("history: from the default branch's latest successful pipeline")
    return HISTORY.is_dir()


if __name__ == "__main__":
    commands = {"restore": restore, "dump": dump, "wait": wait_for_server}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(f"usage: python ci/history.py {'|'.join(commands)}")
    commands[sys.argv[1]]()
