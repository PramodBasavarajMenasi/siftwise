"""The example's siftwise history, between jobs that each run their own server.

    python ci/history.py restore   wait for Postgres, migrate, load .siftwise-history/
    python ci/history.py wait      wait for this job's siftwise server
    python ci/history.py dump      save the database to .siftwise-history/ (an artifact)

restore reads .siftwise-history/ from an earlier job of this pipeline (its artifacts), else
from the latest successful pipeline of the default branch: its siftwise-rollup job's artifacts,
downloaded with CI_JOB_TOKEN, which may read its own project's job artifacts on every tier
(https://docs.gitlab.com/api/job_artifacts/#download-job-artifacts-by-reference-name,
https://docs.gitlab.com/ci/jobs/ci_job_token/). Merge request pipelines read main's history
and never write it. The log names the endpoint and, on failure, the HTTP status, the start of
GitLab's answer and what it means; never the token.

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
from collections.abc import Mapping
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


def artifacts_path(project: str, ref: str) -> str:
    """The API path (after CI_API_V4_URL) of the rollup job's artifacts archive on ``ref``:
    https://docs.gitlab.com/api/job_artifacts/#download-job-artifacts-by-reference-name.
    search_recent_successful_pipelines: the newest of the ref's last 100 successful pipelines
    whose job has artifacts, not only the newest successful pipeline."""
    params = {"job": ROLLUP_JOB, "search_recent_successful_pipelines": "true"}
    project, ref = urllib.parse.quote(project, safe=""), urllib.parse.quote(ref, safe="")
    return f"/projects/{project}/jobs/artifacts/{ref}/download?{urllib.parse.urlencode(params)}"


# What each answer means for this request (GitLab's lib/api/helpers.rb and
# lib/api/ci/job_artifacts.rb: a refused job token is 401, an allowlist or policy refusal 403).
_HINTS = {
    401: "the job token was refused (it is valid only while its job runs)",
    403: "the job token may not read this project's artifacts (Settings > CI/CD > Job token "
    "permissions)",
    404: "no successful pipeline on {ref} with a {job} job that kept artifacts, or none had "
    "finished yet: let the default branch's pipeline pass, then run this pipeline again",
}
_PROJECT_NOT_FOUND = "GitLab can't see the project with this job token (CI_PROJECT_ID?)"


def _snippet(body: bytes, token: str) -> str:
    text = " ".join(body.decode("utf-8", errors="replace").split()).replace(token, "[MASKED]")
    return text[:200]


def fetch_from_default_branch(env: Mapping[str, str] = os.environ) -> bool:
    token = env.get("CI_JOB_TOKEN")
    api, project = env.get("CI_API_V4_URL"), env.get("CI_PROJECT_ID")
    if not (token and api and project):
        print("history: no CI_JOB_TOKEN, CI_API_V4_URL or CI_PROJECT_ID: default branch not read")
        return False
    ref = env.get("CI_DEFAULT_BRANCH") or "main"
    path = artifacts_path(project, ref)
    # The token goes in the JOB-TOKEN header, never in the URL, so no log line can hold it.
    request = urllib.request.Request(
        api.rstrip("/") + path,
        headers={"JOB-TOKEN": token, "User-Agent": "siftwise-gitlab-example"},
    )
    try:
        with urllib.request.build_opener(_NoTokenElsewhere).open(request, timeout=120) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        body = _snippet(exc.read(), token)
        hint = _HINTS.get(exc.code, "unexpected answer").format(ref=ref, job=ROLLUP_JOB)
        if exc.code == 404 and "Project Not Found" in body:
            hint = _PROJECT_NOT_FOUND
        print(f"history: GET {path}: HTTP {exc.code} {body!r}: {hint}")
        print("history: nothing from the default branch")
        return False
    except (OSError, ValueError) as exc:  # URLError, timeouts, a bad CI_API_V4_URL
        print(f"history: GET {path}: failed: {_snippet(str(exc).encode(), token)}")
        return False
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        print(f"history: GET {path}: HTTP 200 but not a zip: {_snippet(data[:300], token)!r}")
        return False
    with archive:
        for name in archive.namelist():
            parts = Path(name).parts
            # Only our own files, never outside the directory.
            if len(parts) == 2 and parts[0] == HISTORY.name and ".." not in parts:
                archive.extract(name)
    if not HISTORY.is_dir():
        print(f"history: GET {path}: HTTP 200, but the archive has no {HISTORY}/")
        return False
    print(f"history: GET {path}: HTTP 200, {len(data)} bytes: the default branch's history")
    return True


if __name__ == "__main__":
    commands = {"restore": restore, "dump": dump, "wait": wait_for_server}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(f"usage: python ci/history.py {'|'.join(commands)}")
    commands[sys.argv[1]]()
