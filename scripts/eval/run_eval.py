"""Replay the selector against a repo's real runs: savings, and failures caught or missed.

Two modes:

* default: the most recent PR runs (non-main, ``changed_files_known``), using their recorded
  diffs. ``--failing`` adds every PR run that had failures.
* ``--replay-main``: treat each main run as a PR whose diff is the previous main run's
  commit ... this commit (fetched from the GitHub compare API and cached).
* ``--replay-prs``: every PR merged in the ``--days`` before the end of the repo's recorded
  history (its newest run, never today), from the GitHub API, whether or not
  CI recorded results for it: the diff is the PR's base...head (as ``siftwise select`` sends),
  and history is cut at the PR head commit's time. For repos whose JUnit comes only from main
  (no PR runs), so it measures selection and savings; with no PR results, not recall.

A target is one CI run attempt: its matrix variants are grouped, and a failure in any variant
counts. For each target R:

1. Rebuild the selector's history *as of just before R* (no peeking): only runs strictly
   earlier than R by ``COALESCE(started_at, created_at)``. ``test_stats`` is not used for
   selection; broken-on-main is recomputed per variant from raw results. The repo's raw
   results are read once into memory (``HistoryIndex``), so each target is cheap.
2. With ``--checkout`` (a git clone) or ``--source github`` (each commit's zipball from the
   GitHub API, no git), compute the import graph at R's commit (cached per commit) and pass it
   to the selector, as ``siftwise select`` does: for Go ``go list -deps -test -json ./...``
   (needs ``--go``; listed for ``--goos``, the platform CI tests on), for Python the static
   graph from ``siftwise.cli.pygraph``.
3. Run ``select_tests`` with the repo config from ``--siftwise-toml``.
4. Compare with the tests that actually failed in R. A failure is caught if the selection's
   commands would run it (including new tests in a Python file or Go package run whole).
5. Explain each miss with the signal that would have caught it.
6. Report tests and estimated runtime skipped (each top-level test's average duration from
   test_stats; sizes the savings only, never affects selection).

    uv run python -m scripts.eval.run_eval --repo RDFLib/rdflib --checkout ../rdflib
    uv run python -m scripts.eval.run_eval --repo RDFLib/rdflib --replay-main --checkout ../rdflib
    uv run python -m scripts.eval.run_eval --repo ipfs/kubo --failing \
        --siftwise-toml scripts/eval/kubo.siftwise.toml --checkout ../kubo --go go

Reads SIFTWISE_DATABASE_URL (or .env), and GITHUB_TOKEN for --replay-main. Read-only.
"""

from __future__ import annotations

import bisect
import io
import itertools
import json
import math
import os
import posixpath
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from sqlalchemy import text
from sqlalchemy.orm import Session

if not __package__:  # run as a file path: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.eval import triage_labels as tl
from siftwise.cli.gograph import GoGraph, build_graph, parse_go_list
from siftwise.cli.pygraph import PyGraph, build_py_graph, module_of_test
from siftwise.cli.repoconfig import load_repo_config, read_go_module
from siftwise.config import get_settings
from siftwise.core.junit import Status
from siftwise.core.models import FAILED_CODES_SQL, PASSED_CODE, STATUS_CODES
from siftwise.core.selector import (
    FailedRun,
    KnownTest,
    Mode,
    RepoHistory,
    Selection,
    SelectorConfig,
    WouldRun,
    build_file_scope,
    is_build_file,
    is_ignorable,
    select_tests,
)
from siftwise.core.triage import Change, Failure, Target, recorded_failures, triage_run
from siftwise.db import get_sessionmaker

FAILED_CODES = (STATUS_CODES[Status.FAILED], STATUS_CODES[Status.ERROR])
# Main CI runs read to recompute broken-on-main per target (see broken_on_main_before).
BROKEN_LOOKBACK_CI_RUNS = 20
COMPARE_FILE_CAP = 300


@dataclass(frozen=True)
class TargetRun:
    run_id: int
    ci_run_id: str | None
    commit_sha: str
    branch: str
    run_at: datetime
    # None: unknown diff. For PR mode this comes from the DB; for --replay-main from GitHub.
    changed_files: tuple[str, ...] | None = None
    diff_label: str = ""
    # Every stored run (one per matrix variant) of this CI run attempt; run_id is the first.
    run_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class Missed:
    test_id: str
    known_before: bool
    why: str
    # The signal that would have caught it (or why none would): grouped in the summary.
    signal: str = "none"


@dataclass(frozen=True)
class Counterfactual:
    """The selection if the full-suite fallbacks had not applied."""

    mapped_files: list[str]
    selection: Selection | None  # None: no changed file maps to tests on its own
    caught: list[str]
    missed: list[Missed]


@dataclass(frozen=True)
class Result:
    run: TargetRun
    changed_files: list[str]
    selection: Selection
    failures: list[str]
    caught: list[str]
    missed: list[Missed]
    graph_note: str
    tests_skipped_pct: float
    time_skipped_pct: float
    counterfactual: Counterfactual | None = None
    signal_counts: dict[str, int] = field(default_factory=dict)


# --- history as of a point in time --------------------------------------------------------


@dataclass(frozen=True)
class IndexedRun:
    id: int
    ci_key: str  # COALESCE(ci_run_id, 'run:' || id), as in selector.recent_ci_runs_sql
    run_attempt: int
    run_at: datetime
    is_main: bool
    changed_files_known: bool
    commit_sha: str
    variant: str  # '' = no variant

    @property
    def order(self) -> tuple[datetime, int]:
        return (self.run_at, self.id)


def _bits(mask: int) -> Iterator[int]:
    """Positions of the set bits of ``mask``, lowest first."""
    for i, byte in enumerate(mask.to_bytes((mask.bit_length() + 7) // 8, "little")):
        while byte:
            low = byte & -byte
            yield i * 8 + low.bit_length() - 1
            byte ^= low


def _set_bit(bitmap: bytearray, bit: int) -> None:
    byte = bit >> 3
    if byte >= len(bitmap):
        bitmap.extend(bytes(byte - len(bitmap) + 256))
    bitmap[byte] |= 1 << (bit & 7)


class HistoryIndex:
    """A repo's raw history in memory, to rebuild the selector's history as of any run.

    ``load_history`` reads the *current* history (known tests from test_stats). The eval needs
    it as of each target, with no peeking, which from SQL means re-reading millions of raw
    results per target on a matrix repo. So every result is read once here: per run, a bitmask
    of the tests it reported, the tests that failed, and for main runs each test's final
    outcome. ``history_before`` then applies the selector's definitions in memory.

    File paths are the one exception to "no peeking": results don't keep their own, so every
    target sees each test's latest reported path (``tests.file_path``).
    """

    def __init__(self, session: Session, repo_id: int) -> None:
        params = {"repo_id": repo_id}
        stream = {"yield_per": 100_000}
        self.runs = sorted(
            (
                IndexedRun(*row)
                for row in session.execute(
                    text(
                        """
                        SELECT id, COALESCE(ci_run_id, 'run:' || id), run_attempt,
                               COALESCE(started_at, created_at), is_main, changed_files_known,
                               commit_sha, COALESCE(variant, '')
                        FROM runs WHERE repo_id = :repo_id
                        """
                    ),
                    params,
                )
            ),
            key=lambda r: r.order,
        )
        self._orders = [r.order for r in self.runs]
        # One bit per test of the repo, in test key order.
        self._tests: list[str] = []
        self._paths: list[str | None] = []
        bit_of: dict[int, int] = {}
        for key, test_id, file_path in session.execute(
            text("SELECT id, test_id, file_path FROM tests WHERE repo_id = :repo_id ORDER BY id"),
            params,
        ):
            bit_of[key] = len(self._tests)
            self._tests.append(test_id)
            self._paths.append(file_path)
        seen: dict[int, bytearray] = defaultdict(bytearray)
        for run_id, test_key in session.execute(
            text(
                "SELECT tr.run_id, tr.test_key FROM test_results tr "
                "JOIN runs ru ON ru.id = tr.run_id WHERE ru.repo_id = :repo_id"
            ),
            params,
            execution_options=stream,
        ):
            _set_bit(seen[run_id], bit_of[test_key])
        self._seen = {run_id: int.from_bytes(b, "little") for run_id, b in seen.items()}

        # Any failed/error result (all attempts), as the selector's failure queries read them.
        self._failed: dict[int, set[str]] = defaultdict(set)
        for run_id, test_key in session.execute(
            text(
                "SELECT tr.run_id, tr.test_key FROM test_results tr "
                "JOIN runs ru ON ru.id = tr.run_id "
                f"WHERE ru.repo_id = :repo_id AND tr.status IN {FAILED_CODES_SQL}"
            ),
            params,
        ):
            self._failed[run_id].add(self._tests[bit_of[test_key]])

        # Main runs: each test's final outcome (non-skipped), for broken-on-main.
        main_failed: dict[int, bytearray] = defaultdict(bytearray)
        main_passed: dict[int, bytearray] = defaultdict(bytearray)
        for run_id, test_key, status in session.execute(
            text(
                """
                SELECT DISTINCT ON (tr.run_id, tr.test_key) tr.run_id, tr.test_key, tr.status
                FROM test_results tr JOIN runs ru ON ru.id = tr.run_id
                WHERE ru.repo_id = :repo_id AND ru.is_main
                ORDER BY tr.run_id, tr.test_key, tr.attempt DESC
                """
            ),
            params,
            execution_options=stream,
        ):
            if status in FAILED_CODES:
                _set_bit(main_failed[run_id], bit_of[test_key])
            elif status == PASSED_CODE:
                _set_bit(main_passed[run_id], bit_of[test_key])
        self._main_failed = {r: int.from_bytes(b, "little") for r, b in main_failed.items()}
        self._main_passed = {r: int.from_bytes(b, "little") for r, b in main_passed.items()}

        self._changed = changed_files_of(session, [r.id for r in self.runs])

    def __str__(self) -> str:
        return f"{len(self.runs)} runs, {len(self._tests)} tests"

    def history_before(self, run: TargetRun, config: SelectorConfig) -> RepoHistory:
        """What ``load_history`` would have returned just before ``run`` was ingested.

        Only runs strictly earlier than ``run`` by (run time, id) count. Known tests are those
        in any of the latest ``known_test_runs`` CI runs, which is what test_stats' last-seen
        run encodes for ``load_history``.
        """
        prior = self.runs[: bisect.bisect_left(self._orders, (run.run_at, run.run_id))]

        def recent(n: int, keep: Callable[[IndexedRun], bool]) -> list[IndexedRun]:
            """Runs of the latest ``n`` CI runs among ``prior``, newest first."""
            groups: dict[tuple[str, int], list[IndexedRun]] = defaultdict(list)
            for r in prior:
                if keep(r):
                    groups[(r.ci_key, r.run_attempt)].append(r)
            latest = sorted(
                groups.values(),
                key=lambda g: (max(r.run_at for r in g), max(r.id for r in g)),
                reverse=True,
            )[:n]
            return sorted((r for g in latest for r in g), key=lambda r: r.order, reverse=True)

        mask = 0
        for r in recent(config.known_test_runs, lambda r: True):
            mask |= self._seen.get(r.id, 0)
        tests = {}
        for bit in _bits(mask):
            test_id = self._tests[bit]
            tests[test_id] = KnownTest(test_id, self._paths[bit])

        recent_main_failures: dict[str, tuple[int, str]] = {}
        for r in recent(config.recent_main_runs, lambda r: r.is_main):
            for test_id in self._failed.get(r.id, ()):
                recent_main_failures.setdefault(test_id, (r.id, r.commit_sha))

        failed_runs = tuple(
            FailedRun(r.id, r.commit_sha, frozenset(self._changed.get(r.id, ())),
                      frozenset(self._failed[r.id]))
            for r in recent(config.co_change_runs, lambda r: r.changed_files_known)
            if r.id in self._failed
        )  # fmt: skip

        return RepoHistory(
            tests=tests,
            broken_on_main=self._broken_on_main(
                recent(BROKEN_LOOKBACK_CI_RUNS, lambda r: r.is_main)
            ),
            recent_main_failures=recent_main_failures,
            failed_runs=failed_runs,
        )

    def _broken_on_main(self, main_runs: list[IndexedRun]) -> dict[str, str]:
        """Same definition as core/history.py, from ``main_runs`` (newest first) only.

        Per variant, a test is broken if its latest non-skipped outcome is a failure; its
        streak starts at the oldest failure before a pass. The sha is the earliest start
        across broken variants. Only the last ``BROKEN_LOOKBACK_CI_RUNS`` main CI runs are
        read: the selector only uses *whether* a test is broken, so a shorter lookback
        changes at most the streak sha in a reason, never which tests are selected.
        """
        by_variant: dict[str, list[IndexedRun]] = defaultdict(list)
        for r in main_runs:
            by_variant[r.variant].append(r)
        starts: dict[str, tuple[datetime, int, str]] = {}
        for runs in by_variant.values():
            decided = alive = 0  # tests with a newer outcome / still in a failing streak
            streak_start: dict[int, IndexedRun] = {}
            for r in runs:  # newest first
                failed = self._main_failed.get(r.id, 0)
                passed = self._main_passed.get(r.id, 0)
                alive |= failed & ~decided  # its latest outcome is a failure
                alive &= ~passed  # an older pass ends the streak
                for bit in _bits(failed & alive):
                    streak_start[bit] = r
                decided |= failed | passed
            for bit, r in streak_start.items():
                test_id, start = self._tests[bit], (r.run_at, r.id, r.commit_sha)
                if test_id not in starts or start < starts[test_id]:
                    starts[test_id] = start
        return {test_id: start[2] for test_id, start in starts.items()}


def changed_files_of(session: Session, run_ids: list[int]) -> dict[int, set[str]]:
    changed: dict[int, set[str]] = defaultdict(set)
    if run_ids:
        for run_id, path in session.execute(
            text("SELECT run_id, path FROM changed_files WHERE run_id = ANY(:ids)"),
            {"ids": run_ids},
        ).all():
            changed[run_id].add(path)
    return changed


def actual_failures(session: Session, run_ids: list[int]) -> list[str]:
    """Tests whose final attempt failed in any of these runs (any variant of a CI run)."""
    rows = session.execute(
        text(
            f"""
            SELECT DISTINCT t.test_id FROM (
                SELECT DISTINCT ON (run_id, test_key) test_key, status FROM test_results
                WHERE run_id = ANY(:run_ids) ORDER BY run_id, test_key, attempt DESC
            ) final JOIN tests t ON t.id = final.test_key
            WHERE final.status IN {FAILED_CODES_SQL} ORDER BY t.test_id
            """
        ),
        {"run_ids": run_ids},
    ).all()
    return [row[0] for row in rows]


def top_level_durations(session: Session, repo_id: int, go: bool) -> dict[str, float]:
    """Average duration of each top-level test. Go subtests (``TestX/sub``) are inside their
    parent's time, so they're dropped; for other languages every test is top-level."""
    rows = session.execute(
        text(
            "SELECT test_id, avg_duration_ms FROM test_stats "
            "WHERE repo_id = :repo_id AND avg_duration_ms IS NOT NULL"
        ),
        {"repo_id": repo_id},
    ).all()
    return {t: d for t, d in rows if not go or "/" not in t.partition("::")[2]}


# --- targets ------------------------------------------------------------------------------


# One row per CI run attempt: its variants (matrix legs) are grouped together. Runs without
# a CI id stand alone. run_at/id are the earliest, so history cutoffs exclude every variant.
_CI_RUNS = """
SELECT min(r.id) AS id, r.ci_run_id, r.commit_sha, r.branch,
       min(COALESCE(r.started_at, r.created_at)) AS run_at,
       array_agg(r.id ORDER BY r.id) AS run_ids
FROM runs r
WHERE r.repo_id = :repo_id AND {where}
GROUP BY COALESCE(r.ci_run_id, r.id::text), r.run_attempt, r.ci_run_id, r.commit_sha, r.branch
{having}
"""


def pr_targets(session: Session, repo_id: int, limit: int, failing: bool) -> list[TargetRun]:
    query = _CI_RUNS.format(
        where="NOT r.is_main AND r.changed_files_known",
        having=f"""HAVING NOT :failing OR bool_or(EXISTS (
            SELECT 1 FROM test_results tr
            WHERE tr.run_id = r.id AND tr.status IN {FAILED_CODES_SQL}))""",
    )
    rows = session.execute(
        text(f"{query} ORDER BY run_at DESC, id DESC LIMIT :limit"),
        {"repo_id": repo_id, "limit": limit, "failing": failing},
    ).all()
    changed = changed_files_of(session, [row[0] for row in rows])
    return [
        TargetRun(
            run_id, ci_run_id, sha, branch, run_at,
            changed_files=tuple(sorted(changed.get(run_id, ()))),
            diff_label="PR diff",
            run_ids=tuple(run_ids),
        )
        for run_id, ci_run_id, sha, branch, run_at, run_ids in rows
    ]  # fmt: skip


def main_replay_targets(
    session: Session, repo_id: int, repo: str, compare: GitHubCompare, last: int | None = None
) -> list[TargetRun]:
    """Every main run but the first (or the ``last`` most recent), as a PR from the previous
    main run's commit."""
    query = _CI_RUNS.format(where="r.is_main", having="")
    rows = session.execute(text(f"{query} ORDER BY run_at, id"), {"repo_id": repo_id}).all()
    if last:
        rows = rows[-(last + 1) :]  # each target diffs against the run before it
    targets = []
    for prev, (run_id, ci_run_id, sha, branch, run_at, run_ids) in itertools.pairwise(rows):
        prev_sha = prev[2]
        if prev_sha == sha:
            continue  # a re-run of the same commit: nothing changed
        files = compare.files(repo, prev_sha, sha)
        label = f"{prev_sha[:7]}...{sha[:7]}"
        targets.append(
            TargetRun(
                run_id,
                ci_run_id,
                sha,
                branch,
                run_at,
                changed_files=files,
                diff_label=label,
                run_ids=tuple(run_ids),
            )
        )
    return targets


def history_end(session: Session, repo_id: int) -> datetime | None:
    """The repo's newest recorded run time: where its history ends."""
    end: datetime | None = session.execute(
        text("SELECT max(COALESCE(started_at, created_at)) FROM runs WHERE repo_id = :repo_id"),
        {"repo_id": repo_id},
    ).scalar_one()
    return end


def merged_pr_targets(
    token: str, repo: str, days: int, compare: GitHubCompare, until: datetime
) -> list[TargetRun]:
    """Every PR merged in the ``days`` up to ``until`` (the end of the recorded history),
    oldest first, as a target: the PR's base...head diff, history cut at its head commit's time
    (when CI would have selected for it).

    Counted back from the history's end, never from today: a PR merged after it would be
    selected against history that stops before it, and a re-run months later would replay a
    different set of PRs."""
    gh = httpx.Client(
        base_url="https://api.github.com",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=60,
    )
    cutoff = until - timedelta(days=days)
    prs: list[dict[str, Any]] = []
    for page in range(1, 50):
        batch = gh.get(f"/repos/{repo}/pulls", params={"state": "closed", "sort": "updated",
                       "direction": "desc", "per_page": 100, "page": page}).json()  # fmt: skip
        prs += [p for p in batch
                if p.get("merged_at") and cutoff <= _iso(p["merged_at"]) <= until]  # fmt: skip
        if len(batch) < 100 or _iso(batch[-1]["updated_at"]) < cutoff:
            break
    targets = []
    for pr in sorted(prs, key=lambda p: p["merged_at"]):
        head, base = pr["head"]["sha"], pr["base"]["sha"]
        commit = gh.get(f"/repos/{repo}/commits/{head}")
        # The time CI last ran on the PR; merged_at if the commit can't be read.
        when = (commit.json()["commit"]["committer"]["date"] if commit.is_success
                else pr["merged_at"])  # fmt: skip
        targets.append(
            TargetRun(
                0,  # no stored run: history is everything strictly before `when`
                None,
                head,
                f"#{pr['number']}",
                _iso(when),
                changed_files=compare.files(repo, base, head),
                diff_label=f"PR #{pr['number']} {base[:7]}...{head[:7]}",
            )
        )
    return targets


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# Waits (seconds) between attempts of a GitHub request that failed at the network level.
NETWORK_RETRY_WAITS = (0, 10, 30, 60)


def get_with_retries(http: httpx.Client, url: str) -> httpx.Response:
    """GET, retrying network errors (DNS, connect, timeouts). If the network stays down, the
    last error is raised: the eval stops, and ``--resume`` continues later. Never turn an
    outage into "unavailable": that would be written into the results as a real answer (no
    import graph, unknown fix diff) and skew them. HTTP errors are returned, not retried."""
    error: httpx.TransportError | None = None
    attempts = len(NETWORK_RETRY_WAITS)
    for attempt, wait in enumerate(NETWORK_RETRY_WAITS, start=1):
        time.sleep(wait)
        try:
            return http.get(url)
        except httpx.TransportError as exc:
            error = exc
            # One line per failed attempt, on stderr: the error class and the request path
            # only. The token is in the client's headers and is never logged; neither is the
            # error's message, which could quote a request.
            then = (f"retrying in {NETWORK_RETRY_WAITS[attempt]}s" if attempt < attempts
                    else "giving up")  # fmt: skip
            typer.echo(f"github: attempt {attempt}/{attempts} failed "
                       f"({type(exc).__name__}) on GET {url}; {then}", err=True)  # fmt: skip
    assert error is not None
    raise error


class GitHubCompare:
    """Changed files between two commits, from the GitHub compare API, cached on disk."""

    def __init__(self, token: str, cache_dir: Path) -> None:
        self._cache_dir = cache_dir
        self._http = httpx.Client(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=60,
        )

    def files(self, repo: str, base: str, head: str) -> tuple[str, ...] | None:
        cache = self._cache_dir / f"compare-{base}-{head}.json"
        if cache.exists():
            data = json.loads(cache.read_text())
        else:
            response = get_with_retries(self._http, f"/repos/{repo}/compare/{base}...{head}")
            if response.status_code != httpx.codes.OK:
                return None  # not cached: a later run may succeed
            data = response.json()
            cache.write_text(json.dumps({"files": data.get("files", [])}))
        entries = data.get("files", [])
        if len(entries) >= COMPARE_FILE_CAP:
            return None  # truncated
        paths: list[str] = []
        for entry in entries:
            paths.append(entry["filename"])
            if entry.get("previous_filename"):
                paths.append(entry["previous_filename"])
        return tuple(sorted(set(paths)))


class GitHubSource:
    """A repo's source at any commit, from the GitHub zipball API: no clone, no git."""

    def __init__(self, token: str, repo: str) -> None:
        self.repo = repo
        self._http = httpx.Client(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            # codeload sometimes stalls without closing: fail fast, then retry (extract()).
            timeout=httpx.Timeout(60, connect=10),
            follow_redirects=True,
        )

    def extract(self, sha: str, into: Path) -> list[str] | None:
        """Unpack the tree at ``sha`` into ``into``; its file paths, or None if GitHub says
        it's unavailable. Network errors are retried, then raised (``get_with_retries``)."""
        # codeload times out now and then on big archives: retried like any network error
        response = get_with_retries(self._http, f"/repos/{self.repo}/zipball/{sha}")
        if response.status_code != httpx.codes.OK:
            return None  # this target gets no graph (reported), the rest of the eval goes on
        files = []
        links: dict[str, str] = {}  # link path -> its target, relative to the link's directory
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            for info in zf.infolist():
                # Every entry is under one "<owner>-<repo>-<sha>/" folder.
                name = info.filename.partition("/")[2]
                if not name or info.is_dir():
                    continue
                files.append(name)
                if stat.S_ISLNK(info.external_attr >> 16):
                    links[name] = zf.read(info).decode("utf-8", errors="replace")
                    continue
                target = _long_path(into / name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info))
        _materialize_links(into, links)
        return sorted(files)


def _long_path(path: Path) -> Path:
    """On Windows, an extended-length path, so deep trees aren't cut at 260 characters."""
    if os.name != "nt":
        return path
    absolute = str(path.resolve())
    return Path(absolute if absolute.startswith("\\\\?\\") else "\\\\?\\" + absolute)


def _materialize_links(root: Path, links: dict[str, str]) -> None:
    """Replace zipball symlinks with copies of their targets.

    The zipball stores a symlink as a small file holding its target path. Creating real
    symlinks on Windows needs extra privileges, and ``go list`` must see the directory a link
    points at, so copy the target (file or directory) instead. Links to links resolve over
    several passes; links pointing outside the tree, or nowhere, are left out.
    """
    pending = dict(links)
    while pending:
        progressed = False
        for name, target in list(pending.items()):
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
            if posixpath.isabs(target) or resolved == ".." or resolved.startswith("../"):
                del pending[name]  # outside the tree: nothing to copy
                continue
            if resolved in pending:
                continue  # a link to a link: copy once that one exists
            source, dest = _long_path(root / resolved), _long_path(root / name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, dest, dirs_exist_ok=True)
            elif source.is_file():
                shutil.copyfile(source, dest)
            del pending[name]
            progressed = True
        if not progressed:
            break  # cycles or dangling links


class GraphProvider:
    """Import graphs at a commit, from a checkout or GitHub zipballs, cached per commit.

    Go: ``go list``. Python: ``pygraph``. Both are fed to the selector, as ``siftwise select``
    does; the Python graph also explains misses. The commit's file list gives the deleted files.
    """

    def __init__(
        self,
        checkout: Path | None,
        go: str,
        cache_dir: Path,
        github: GitHubSource | None = None,
        goos: str | None = None,
    ) -> None:
        self.checkout = checkout
        self.go = go
        self.cache_dir = cache_dir
        self.github = github
        self.goos = goos
        # The one commit unpacked from GitHub at a time: (sha, directory). The directory is
        # private to this provider: evals running side by side each wipe their own tree.
        self._unpacked: tuple[str, Path] | None = None
        self._src: Path | None = None

    @property
    def has_source(self) -> bool:
        return self.checkout is not None or self.github is not None

    def _root_at(self, sha: str) -> Path | None:
        """A directory holding the tree at ``sha``: the checkout, or an unpacked zipball."""
        if self.checkout is not None:
            return self.checkout if self._checkout(sha) else None
        if self.github is None:
            return None
        if self._unpacked and self._unpacked[0] == sha:
            return self._unpacked[1]
        if self._src is None:
            self._src = Path(tempfile.mkdtemp(prefix="src-", dir=self.cache_dir))
        self._unpacked = None
        root = self._src / "tree"
        shutil.rmtree(_long_path(root), ignore_errors=True)
        files = self.github.extract(sha, root)
        if files is None:
            return None
        (self.cache_dir / f"files-{sha}.json").write_text(json.dumps(files))
        self._unpacked = (sha, root)
        return root

    def go_graph_at(self, sha: str) -> tuple[GoGraph | None, str]:
        cache = self.cache_dir / f"gograph-{sha}.json"
        if cache.exists():
            data = json.loads(cache.read_text())
            if "error" in data:
                return None, f"go list failed at {sha[:7]}: {data['error']}"
            return GoGraph.from_json(data), f"import graph at {sha[:7]}"
        if not self.has_source:
            return None, "no import graph (no --checkout or --source)"
        root = self._root_at(sha)
        if root is None:
            return None, f"no source for {sha[:7]}"
        # go list evaluates build tags for the host: list for the platform CI tests on.
        env = {**os.environ, **({"GOOS": self.goos} if self.goos else {})}
        proc = subprocess.run(
            [self.go, "list", "-deps", "-test", "-json", "./..."],
            cwd=root,
            capture_output=True,
            timeout=1800,
            check=False,
            env=env,
        )
        if proc.returncode != 0:
            error = proc.stderr.decode(errors="replace").strip().splitlines()[-1:] or ["?"]
            cache.write_text(json.dumps({"error": error[0][:300]}))
            return None, f"go list failed at {sha[:7]}: {error[0][:120]}"
        graph = build_graph(parse_go_list(proc.stdout.decode(errors="replace")), root)
        cache.write_text(json.dumps(graph.to_json()))
        return graph, f"import graph at {sha[:7]}"

    def close(self) -> None:
        """Remove this provider's unpack directory."""
        if self._src is not None:
            shutil.rmtree(_long_path(self._src), ignore_errors=True)
            self._src = None

    def has_py_graph(self, sha: str) -> bool:
        return (self.cache_dir / f"pygraph-{sha}.json").exists()

    def py_graph_at(self, sha: str) -> PyGraph | None:
        cache = self.cache_dir / f"pygraph-{sha}.json"
        if cache.exists():
            return PyGraph.from_json(json.loads(cache.read_text()))
        root = self._root_at(sha)
        if root is None:
            return None
        graph = build_py_graph(root)
        cache.write_text(json.dumps(graph.to_json()))
        return graph

    def deleted_at(self, sha: str, paths: list[str]) -> tuple[str, ...] | None:
        """The ``paths`` that don't exist at ``sha``: what ``siftwise select`` sends as
        ``deleted_files``. None if the commit's file list is unavailable."""
        files = self._files_at(sha)
        if files is None:
            return None
        return tuple(sorted(p for p in paths if p not in files))

    def _files_at(self, sha: str) -> frozenset[str] | None:
        cache = self.cache_dir / f"files-{sha}.json"
        if cache.exists():
            return frozenset(json.loads(cache.read_text()))
        if self.checkout is None:
            # Unpacking the zipball writes the file list (cached above).
            if self._root_at(sha) is None or not cache.exists():
                return None
            return frozenset(json.loads(cache.read_text()))
        if not self._fetch(sha):
            return None
        proc = self._run_git("ls-tree", "-r", "--name-only", "-z", sha)
        if proc.returncode != 0:
            return None
        files = sorted(f for f in proc.stdout.decode("utf-8", errors="replace").split("\0") if f)
        cache.write_text(json.dumps(files))
        return frozenset(files)

    def _fetch(self, sha: str) -> bool:
        """Whether the checkout has commit ``sha``, fetching it if needed."""
        if self._git("cat-file", "-e", f"{sha}^{{commit}}"):
            return True
        return self._git("fetch", "-q", "origin", sha)

    def _checkout(self, sha: str) -> bool:
        self._fetch(sha)
        return self._git("checkout", "-q", "-f", "--detach", sha)

    def _git(self, *args: str) -> bool:
        return self._run_git(*args).returncode == 0

    def _run_git(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        assert self.checkout is not None
        return subprocess.run(
            ["git", "-C", str(self.checkout), *args], capture_output=True, timeout=600, check=False
        )


# --- evaluation ---------------------------------------------------------------------------


def would_run(selection: Selection, test_id: str) -> bool:
    """Whether the selection's commands would execute ``test_id``."""
    return WouldRun(selection)(test_id)


@dataclass(frozen=True)
class Explainer:
    """Explains misses for one target run (``go``: Go repo; else Python/pytest)."""

    history: RepoHistory
    changed_files: list[str]
    go_module: str | None
    go: bool
    go_graph: GoGraph | None
    graphs: GraphProvider
    sha: str

    def __call__(self, test_id: str) -> Missed:
        known_before = test_id in self.history.tests
        if not known_before and not self._top_level_known(test_id):
            return Missed(test_id, False, "new test: not in history, so no signal can name it",
                          "new test")  # fmt: skip
        return self._go(test_id) if self.go else self._python(test_id)

    def _top_level_known(self, test_id: str) -> bool:
        if not self.go:
            return False
        package, _, name = test_id.partition("::")
        top = name.split("/", 1)[0]
        return any(
            t.partition("::")[0] == package and t.partition("::")[2].split("/", 1)[0] == top
            for t in self.history.tests
        )

    def _go(self, test_id: str) -> Missed:
        package = test_id.partition("::")[0]
        rel = (
            package[len(self.go_module) :].lstrip("/")
            if self.go_module and package.startswith(self.go_module)
            else package
        )
        dirs = sorted({posixpath.dirname(p) for p in self.changed_files if p.endswith(".go")})
        changed = ", ".join(f"./{d}" for d in dirs) or "(no Go files changed)"
        if self.go_graph is None:
            return Missed(test_id, True, f"./{rel} unchanged ({changed}); no import graph",
                          "go imports (graph unavailable)")  # fmt: skip
        return self._recurring(test_id) or Missed(
            test_id, True,
            f"./{rel} has no changed files and doesn't import {changed}; needs a declared "
            "dependency (e.g. a suite that runs a built binary)",
            "declared dependency",
        )  # fmt: skip

    def _python(self, test_id: str) -> Missed:
        test_file = module_of_test(test_id)
        changed_py = {p for p in self.changed_files if p.endswith(".py")}
        graph = self.graphs.py_graph_at(self.sha) if test_file and changed_py else None
        if test_file and graph and test_file in graph.edges:
            chain = graph.import_chain(test_file, changed_py)
            if chain:
                hops = " -> ".join(chain[:5]) + (" -> ..." if len(chain) > 5 else "")
                return Missed(test_id, True, f"imports a changed module: {hops}",
                              "python imports")  # fmt: skip
        recurring = self._recurring(test_id)
        if recurring:
            return recurring
        if not changed_py:
            return Missed(test_id, True, "no Python file changed; failure looks unrelated",
                          "none (unrelated)")  # fmt: skip
        if graph is None or not test_file or test_file not in graph.edges:
            return Missed(test_id, True, f"no import graph for {test_file or test_id}",
                          "python imports (graph unavailable)")  # fmt: skip
        return Missed(
            test_id, True,
            f"{test_file} doesn't import any changed module "
            f"({', '.join(sorted(changed_py)[:3])}); failure looks unrelated",
            "none (unrelated)",
        )  # fmt: skip

    def _recurring(self, test_id: str) -> Missed | None:
        """A test that already failed in earlier runs with unrelated changes: likely flaky."""
        earlier = [run for run in self.history.failed_runs if test_id in run.failed_tests]
        if not earlier:
            return None
        return Missed(
            test_id, True,
            f"also failed in {len(earlier)} earlier run(s) (latest run {earlier[0].run_id}); "
            "likely flaky: needs flaky detection / recently-failed beyond main",
            "flaky / recurring",
        )  # fmt: skip


def counterfactual(
    history: RepoHistory,
    changed: list[str],
    failures: list[str],
    config: SelectorConfig,
    affected: dict[str, list[str]],
    affected_files: dict[str, list[str]],
    deleted: tuple[str, ...],
    explain: Explainer,
) -> Counterfactual:
    """Select from only the changed files that are covered by a signal on their own."""

    def select(files: list[str]) -> Selection:
        return select_tests(history, files, True, config, affected, affected_files, deleted)

    mapped = [
        p
        for p in changed
        if (not is_build_file(p) or build_file_scope(p) is not None)
        and not is_ignorable(p, config.generated)
        and select([p]).mode is Mode.SELECTIVE
    ]
    if not mapped:
        return Counterfactual([], None, [], [])
    selection = select(mapped)
    runs = WouldRun(selection)
    caught = [f for f in failures if runs(f)]
    missed = [explain(f) for f in failures if f not in caught]
    return Counterfactual(mapped, selection, caught, missed)


def skipped_pcts(
    selection: Selection, history: RepoHistory, durations: dict[str, float]
) -> tuple[float, float]:
    """(% of known tests skipped, % of estimated top-level runtime skipped)."""
    if selection.mode is Mode.FULL or not history.tests:
        return 0.0, 0.0
    tests_pct = 100 * (1 - selection.selected_count / len(history.tests))
    top_level = [t for t in history.tests if t in durations]
    total = sum(durations[t] for t in top_level)
    runs = WouldRun(selection)
    run = sum(durations[t] for t in top_level if runs(t))
    time_pct = 100 * (1 - run / total) if total else 0.0
    return tests_pct, time_pct


def evaluate(
    session: Session,
    index: HistoryIndex,
    run: TargetRun,
    config: SelectorConfig,
    graphs: GraphProvider,
    durations: dict[str, float],
    go: bool,
) -> Result:
    history = index.history_before(run, config)
    known = run.changed_files is not None
    changed = list(run.changed_files or ())
    graph: GoGraph | None = None
    affected_files: dict[str, list[str]] = {}
    if not known:
        graph_note = "diff unknown"
    elif go:
        graph, graph_note = graphs.go_graph_at(run.commit_sha)
    elif not graphs.has_source and not graphs.has_py_graph(run.commit_sha):
        graph_note = "python: no --checkout, no import graph"
    elif not any(p.endswith(".py") for p in changed):
        graph_note = "python: no Python file changed"
    else:
        # As `siftwise select` does: the static import graph at the target's commit.
        py_graph = graphs.py_graph_at(run.commit_sha)
        if py_graph is None:
            graph_note = f"python: no import graph at {run.commit_sha[:7]}"
        else:
            affected_files = py_graph.affected(changed)
            graph_note = f"python imports at {run.commit_sha[:7]}"
    affected = graph.affected(changed) if graph else {}
    # As `siftwise select` does: changed files that no longer exist are never run.
    deleted = graphs.deleted_at(run.commit_sha, changed) if known else ()
    if deleted is None:
        deleted = ()
        graph_note += "; deleted files unknown (no file list)"
    explain = Explainer(history, changed, config.go_module, go, graph, graphs, run.commit_sha)
    selection = select_tests(history, changed, known, config, affected, affected_files, deleted)
    failures = actual_failures(session, list(run.run_ids or (run.run_id,)))
    runs = WouldRun(selection)
    caught = [f for f in failures if runs(f)]
    missed = [explain(f) for f in failures if f not in caught]
    cf = (
        counterfactual(
            history, changed, failures, config, affected, affected_files, deleted, explain
        )
        if failures and selection.mode is Mode.FULL and known
        else None
    )
    signals: dict[str, int] = defaultdict(int)
    for t in selection.tests:
        for reason in t.reasons:
            signals[_signal(reason)] += 1
    tests_pct, time_pct = skipped_pcts(selection, history, durations)
    return Result(
        run, changed, selection, failures, caught, missed, graph_note,
        tests_pct, time_pct, cf, dict(signals),
    )  # fmt: skip


def target_key(run: TargetRun) -> str:
    """Identifies a target across eval runs (for --resume)."""
    runs = ",".join(map(str, run.run_ids or (run.run_id,)))
    return f"{run.diff_label}|{run.commit_sha}|{runs}"


def resumed_keys(path: Path) -> set[str]:
    """Keys of the targets already in a --resume file. A line torn by a crash is ignored, so
    that target runs again."""
    if not path.exists():
        return set()
    keys = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            keys.add(json.loads(line)["key"])
        except (ValueError, KeyError, TypeError):
            continue
    return keys


def result_json(r: Result) -> dict[str, Any]:
    """One target's result, for --json-out and --resume."""
    cf = r.counterfactual
    return {
        "key": target_key(r.run),
        "target": r.run.diff_label,
        "branch": r.run.branch,
        "commit": r.run.commit_sha,
        "run_ids": list(r.run.run_ids),
        "changed_files": r.changed_files,
        "mode": r.selection.mode.value,
        "reason": r.selection.reason,
        "selected": r.selection.selected_count,
        "total_known": r.selection.total_known,
        "tests_skipped_pct": round(r.tests_skipped_pct, 2),
        "time_skipped_pct": round(r.time_skipped_pct, 2),
        "signals": r.signal_counts,
        "graph": r.graph_note,
        "failures": len(r.failures),
        "caught": len(r.caught),
        "missed": [{"test": m.test_id, "signal": m.signal} for m in r.missed],
        "without_fallbacks": None if cf is None else {
            "mapped": bool(cf.selection), "caught": len(cf.caught),
            "missed": [{"test": m.test_id, "signal": m.signal} for m in cf.missed],
        },
    }  # fmt: skip


def _signal(reason: str) -> str:
    for prefix, name in (
        ("imports changed package", "go imports"),
        ("imports changed module", "python imports"),
        ("declared dependency", "declared"),
        ("co-change", "co-change"),
        ("failed on main", "recently failed"),
        ("broken on main", "recently failed"),
        ("always-run", "always-run"),
    ):
        if reason.startswith(prefix):
            return name
    return "path mapping"


# --- triage eval (--triage) -----------------------------------------------------------------


@dataclass(frozen=True)
class RunMeta:
    run: tl.Run
    ci_run_id: str | None
    run_attempt: int
    changed_files_known: bool


def load_run_meta(session: Session, repo_id: int) -> dict[int, RunMeta]:
    rows = session.execute(
        text(
            "SELECT id, COALESCE(started_at, created_at), commit_sha, branch, is_main,"
            " COALESCE(variant, ''), ci_run_id, run_attempt, changed_files_known"
            " FROM runs WHERE repo_id = :repo_id"
        ),
        {"repo_id": repo_id},
    )
    return {row[0]: RunMeta(tl.Run(*row[:6]), row[6], row[7], row[8]) for row in rows}


class RunChanges:
    """Each run's change as ``siftwise record`` would have stored it, had it recorded live: the
    run's changed files and the import graphs at its commit. ``links`` is None when the diff is
    unknown or a graph that applies can't be built (record then stores ``affected`` NULL, and
    triage treats the change as unknown)."""

    def __init__(
        self,
        graphs: GraphProvider,
        config: SelectorConfig,
        go: bool,
        meta: dict[int, RunMeta],
        changed: dict[int, set[str]],
    ) -> None:
        self.graphs, self.config, self.go = graphs, config, go
        self.meta, self.changed = meta, changed
        self._links: dict[int, dict[str, Any] | None] = {}

    def links(self, run_id: int) -> dict[str, Any] | None:
        """``runs.affected`` for the run (the engine's ``run_links`` callback)."""
        if run_id not in self._links:
            meta = self.meta.get(run_id)
            if meta is None or not meta.changed_files_known:
                self._links[run_id] = None
            else:
                files = sorted(self.changed.get(run_id, ()))
                self._links[run_id] = self.graph_links(meta.run.commit, files)
        return self._links[run_id]

    def change(self, run_id: int) -> Change:
        links = self.links(run_id)
        if links is None:
            return Change((), False)
        return Change.from_graph(sorted(self.changed.get(run_id, ())), links)

    def graph_links(self, commit: str, files: list[str]) -> dict[str, Any] | None:
        """``ChangeGraph`` fields for ``files`` changed at ``commit``, as ``local_change``
        builds them: Go for any change (a go.mod repo), Python when a .py file changed."""
        packages: dict[str, list[str]] = {}
        py_files: dict[str, list[str]] = {}
        if self.go and files:
            graph, _ = self.graphs.go_graph_at(commit)
            if graph is None:
                return None
            packages = graph.affected(files)
        if not self.go and any(f.endswith(".py") for f in files):
            py_graph = self.graphs.py_graph_at(commit)
            if py_graph is None:
                return None
            py_files = py_graph.affected(files)
        return {
            "affected_packages": packages,
            "affected_files": py_files,
            "depends": [{"tests": r.tests, "on": list(r.on)} for r in self.config.depends],
            "go_module": self.config.go_module,
            "generated": list(self.config.generated),
        }


class FixLinks:
    """``triage_labels.LinkedFix``: does the diff from the failing commit to the passing one
    touch a file linked to the test? The selector's change links (as triage's), over a GitHub
    compare, with the import graphs at the passing commit."""

    def __init__(
        self, repo: str, compare: GitHubCompare, changes: RunChanges, paths: dict[str, str | None]
    ) -> None:
        self.repo, self.compare, self.changes, self.paths = repo, compare, changes, paths

    def __call__(self, test: str, failed: tl.Run, passed: tl.Run) -> bool | None:
        files = self.compare.files(self.repo, failed.commit, passed.commit)
        if files is None:
            return None
        links = self.changes.graph_links(passed.commit, list(files))
        if links is None:
            return None
        change = Change.from_graph(files, links)
        return bool(change.links([KnownTest(test, self.paths.get(test))]).get(test))


def label_timelines(
    session: Session, repo_id: int, test_ids: list[str], meta: dict[int, RunMeta]
) -> dict[tuple[str, str], list[tuple[tl.Run, str]]]:
    """Every final outcome of these tests, per (test, variant), oldest first. Labels read the
    future on purpose; verdicts never see this."""
    status = {PASSED_CODE: tl.PASS, **{code: tl.FAIL for code in FAILED_CODES}}
    timelines: dict[tuple[str, str], list[tuple[tl.Run, str]]] = defaultdict(list)
    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (tr.run_id, tr.test_key) tr.run_id, t.test_id, tr.status
            FROM tests t JOIN test_results tr ON tr.test_key = t.id
            WHERE t.repo_id = :repo_id AND t.test_id = ANY(:ids)
            ORDER BY tr.run_id, tr.test_key, tr.attempt DESC
            """
        ),
        {"repo_id": repo_id, "ids": test_ids},
    )
    for run_id, test_id, code in rows:
        run = meta[run_id].run
        timelines[(test_id, run.variant)].append((run, status.get(code, tl.SKIP)))
    for timeline in timelines.values():
        timeline.sort(key=lambda item: item[0].order)
    return timelines


def triage_targets(session: Session, repo_id: int) -> dict[int, list[Failure]]:
    """Every recorded run with failures: its failing tests (final attempt), Go subtests folded
    into a failing parent."""
    run_ids = list(
        session.scalars(
            text(
                f"""
                SELECT DISTINCT tr.run_id FROM test_results tr JOIN runs r ON r.id = tr.run_id
                WHERE r.repo_id = :repo_id AND tr.status IN {FAILED_CODES_SQL}
                """
            ),
            {"repo_id": repo_id},
        )
    )
    targets = {}
    for run_id, failures in recorded_failures(session, run_ids).items():
        kept = set(tl.fold_subtests(f.test_id for f in failures))
        if kept:
            targets[run_id] = [f for f in failures if f.test_id in kept]
    return targets


def triage_eval(
    session: Session,
    repo: str,
    repo_id: int,
    changes: RunChanges,
    fixes: FixLinks,
    window_days: int,
    resume: Path | None,
    wanted: tuple[str, ...],
    verbose: bool,
) -> list[tl.Scored]:
    """Triage every run with failures, oldest first, as of just before it (the production
    engine, cut off at the run: no peeking), and label each failure from what came after."""
    targets = triage_targets(session, repo_id)
    meta = changes.meta
    order = sorted(targets, key=lambda run_id: meta[run_id].run.order)
    test_ids = sorted({f.test_id for failures in targets.values() for f in failures})
    timelines = label_timelines(session, repo_id, test_ids, meta)
    session.rollback()
    typer.echo(f"# triage: {len(order)} runs with failures, {len(test_ids)} failing tests")
    done = resumed_triage(resume) if resume else {}
    scored = [s for records in done.values() for s in records]
    for run_id in order:
        info = meta[run_id]
        run = info.run
        if f"triage|{run_id}" in done or (wanted and not run.commit.startswith(wanted)):
            continue
        change = changes.change(run_id)
        target = Target(repo, run.commit, run.variant or None, info.ci_run_id, info.run_attempt)
        report = triage_run(session, target, targets[run_id], change, window_days, changes.links)
        assert report is not None
        records = [
            tl.Scored(
                r.test_id, run_id, r.verdict.value, r.confidence.value,
                tl.label(r.test_id, run, timelines[(r.test_id, run.variant)], fixes),
            )
            for r in report.results
        ]  # fmt: skip
        session.rollback()  # read-only: don't hold a snapshot across targets
        scored += records
        line = {
            "key": f"triage|{run_id}",
            "run_id": run_id,
            "commit": run.commit,
            "branch": run.branch,
            "variant": run.variant,
            "is_main": run.is_main,
            "change": "known" if change.changed_files_known else "unknown",
            "reasons": {r.test_id: r.reasons[0] if r.reasons else "" for r in report.results},
            "failures": [_scored_json(s) for s in records],
        }
        if resume:
            with resume.open("a", encoding="utf-8") as out:
                out.write(json.dumps(line) + "\n")
        counts = Counter(f"{s.verdict}/{s.label.kind or '-'}" for s in records)
        summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        typer.echo(
            f"run {run_id} {run.commit[:7]} {run.branch[:30]} [{run.variant or '-'}] "
            f"{line['change']}: {summary}"
        )
        if verbose:
            for s in records:
                typer.echo(f"    {s.verdict:<15} {s.confidence:<6} {s.label.kind or '-':<12} "
                           f"{s.test}  ({s.label.reason})")  # fmt: skip
    return scored


def _scored_json(s: tl.Scored) -> dict[str, Any]:
    return {
        "test": s.test,
        "verdict": s.verdict,
        "confidence": s.confidence,
        "label": s.label.kind,
        "label_reason": s.label.reason,
        "when": s.label.when,
    }


def resumed_triage(path: Path) -> dict[str, list[tl.Scored]]:
    """Targets already in a --triage --resume file, by key."""
    done: dict[str, list[tl.Scored]] = {}
    if not path.exists():
        return done
    for text_line in path.read_text(encoding="utf-8").splitlines():
        try:
            line = json.loads(text_line)
            done[line["key"]] = [
                tl.Scored(f["test"], line["run_id"], f["verdict"], f["confidence"],
                          tl.Label(f["label"], f["label_reason"], f.get("when", "")))
                for f in line["failures"]
            ]  # fmt: skip
        except (ValueError, KeyError, TypeError):
            continue  # a line torn by a crash: that target runs again
    return done


def print_triage_summary(title: str, scored: list[tl.Scored]) -> None:
    m = tl.metrics(scored)
    typer.echo(f"\n=== triage: {title} ===")
    typer.echo(f"  failures: {m.failures}; labelled {m.labelled} ({_pct(m.labelled, m.failures)})")
    for reason, n in m.excluded.items():
        typer.echo(f"    excluded, {reason}: {n}")
    typer.echo("  label \\ verdict   " + "".join(f"{v:>16}" for v in tl.VERDICTS))
    for lab in tl.LABELS:
        cells = "".join(f"{m.confusion.get((lab, v), 0):>16}" for v in tl.VERDICTS)
        typer.echo(f"  {lab:<18}{cells}")
    for verdict, lab in (("new_regression", tl.REGRESSION), ("flaky", tl.FLAKY),
                         ("pre_existing", tl.PRE_EXISTING)):  # fmt: skip
        parts = [f"all {_ratio(*m.precision(verdict, lab))}"]
        parts += [f"{c} {_ratio(*m.precision(verdict, lab, c))}" for c in ("high", "medium")]
        typer.echo(f"  precision of {verdict}: " + ", ".join(parts))
    for lab, verdict in ((tl.REGRESSION, "new_regression"), (tl.FLAKY, "flaky"),
                         (tl.PRE_EXISTING, "pre_existing")):  # fmt: skip
        typer.echo(f"  recall of {lab} as {verdict}: {_ratio(*m.recall(lab, verdict))}")
    typer.echo(f"  regressions called flaky: {len(m.regressions_called_flaky)}")
    for s in m.regressions_called_flaky:
        typer.echo(f"    run {s.run_id}: {s.test} ({s.label.reason})")
    typer.echo(f"  unknown: {_ratio(m.unknown_all, m.failures)} of all failures, "
               f"{_ratio(m.unknown_labelled, m.labelled)} of labelled ones")  # fmt: skip
    if m.flaky_when:
        typer.echo("  flaky labels, same-commit pass seen: "
                   + ", ".join(f"{k} {v}" for k, v in sorted(m.flaky_when.items())))  # fmt: skip


def _pct(n: int, total: int) -> str:
    return f"{100 * n / total:.1f}%" if total else "-"


def _ratio(n: int, total: int) -> str:
    return f"{n}/{total} ({_pct(n, total)})" if total else "0/0"


# --- report -------------------------------------------------------------------------------


def print_result(r: Result, verbose: bool) -> None:
    s = r.selection
    head = (
        f"run {r.run.run_id} {r.run.commit_sha[:7]} {r.run.branch[:40]} "
        f"@ {r.run.run_at:%Y-%m-%d} [{r.run.diff_label}]: {s.mode.value}, "
        f"{s.selected_count}/{s.total_known} tests, "
        f"skips {r.tests_skipped_pct:.0f}% tests / {r.time_skipped_pct:.0f}% time"
    )
    interesting = r.failures or verbose
    typer.echo(head + ("" if interesting else f" | {s.reason[:90]}"))
    if not interesting:
        return
    files = ", ".join(r.changed_files[:5]) + (
        f" (+{len(r.changed_files) - 5})" if len(r.changed_files) > 5 else ""
    )
    typer.echo(f"  changed ({len(r.changed_files)}): {files or '(none or unknown)'}")
    typer.echo(f"  {s.reason}")
    typer.echo(f"  {r.graph_note}")
    if r.signal_counts:
        typer.echo(
            "  signals: " + ", ".join(f"{k} {v}" for k, v in sorted(r.signal_counts.items()))
        )
    if s.mode is Mode.SELECTIVE and s.commands:
        typer.echo(f"  $ {s.command[:200]}{'...' if len(s.command) > 200 else ''}")
    if r.failures:
        typer.echo(
            f"  failures: {len(r.failures)} ({len(r.caught)} caught, {len(r.missed)} missed)"
        )
    for test_id in r.caught:
        typer.echo(f"    CAUGHT  {test_id}")
    for m in r.missed:
        typer.echo(f"    MISSED  {m.test_id}  [{m.signal}]")
        typer.echo(f"            {m.why}")
    cf = r.counterfactual
    if cf is not None:
        if cf.selection is None:
            typer.echo("  without fallbacks: nothing covered; only the fallback protects this run")
        else:
            typer.echo(
                f"  without fallbacks: {cf.selection.selected_count} tests, "
                f"would catch {len(cf.caught)}/{len(r.failures)}"
            )
            for m in cf.missed:
                typer.echo(f"    WOULD MISS  {m.test_id}  [{m.signal}]\n                {m.why}")


def print_summary(title: str, results: list[Result]) -> None:
    if not results:
        return
    full = [r for r in results if r.selection.mode is Mode.FULL]
    selective = [r for r in results if r.selection.mode is Mode.SELECTIVE]
    n = len(results)
    typer.echo(f"\n=== {title}: {n} runs ===")
    typer.echo(
        f"  full: {len(full)} ({100 * len(full) / n:.0f}%), "
        f"selective: {len(selective)} ({100 * len(selective) / n:.0f}%)"
    )
    reasons: dict[str, int] = defaultdict(int)
    for r in full:
        reasons[r.selection.reason.split(":")[0]] += 1
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        typer.echo(f"    full because {reason}: {count}")
    if selective:
        avg_tests = sum(r.tests_skipped_pct for r in selective) / len(selective)
        avg_time = sum(r.time_skipped_pct for r in selective) / len(selective)
        typer.echo(
            f"  skipped in selective runs: avg {avg_tests:.1f}% of tests, {avg_time:.1f}% of time"
        )
    overall_time = sum(r.time_skipped_pct for r in results) / n
    typer.echo(f"  est. time skipped over all runs (full = 0%): {overall_time:.1f}%")
    signals: dict[str, int] = defaultdict(int)
    for r in selective:
        for name in r.signal_counts:
            signals[name] += 1
    if signals:
        typer.echo(
            "  selective runs using each signal: "
            + ", ".join(f"{k} {v}" for k, v in sorted(signals.items()))
        )
    failures = sum(len(r.failures) for r in results)
    caught = sum(len(r.caught) for r in results)
    typer.echo(
        f"  failures: {failures} total in {sum(1 for r in results if r.failures)} runs, "
        f"{failures - caught} missed; caught {recall(caught, failures)}"
    )
    sel_failures = sum(len(r.failures) for r in selective)
    sel_caught = sum(len(r.caught) for r in selective)
    if sel_failures:
        typer.echo(f"    in selective runs: caught {recall(sel_caught, sel_failures)}")
    cfs = [r for r in results if r.counterfactual is not None]
    if cfs:
        covered = [r for r in cfs if r.counterfactual and r.counterfactual.selection]
        cf_failures = sum(len(r.failures) for r in covered)
        cf_caught = sum(len(r.counterfactual.caught) for r in covered if r.counterfactual)
        only_fallback = sum(
            len(r.failures) for r in cfs if r.counterfactual and not r.counterfactual.selection
        )
        typer.echo(
            f"  full runs, without fallbacks: caught {recall(cf_caught, cf_failures)}; "
            f"{only_fallback} more failures had nothing but a fallback"
        )
    _print_miss_signals("missed", [m for r in results for m in r.missed])
    _print_miss_signals(
        "without fallbacks, would miss",
        [m for r in results if r.counterfactual for m in r.counterfactual.missed],
    )
    notes: dict[str, int] = defaultdict(int)
    for r in results:
        notes[r.graph_note.split(" at ")[0]] += 1
    typer.echo("  import graph: " + ", ".join(f"{k} ({v})" for k, v in notes.items()))


def recall(caught: int, total: int) -> str:
    """``caught/total (pct)``, the percentage truncated, never rounded up: 2049 of 2050 is
    99.95%, not 100%."""
    if not total:
        return "0/0"
    pct = math.floor(10_000 * caught / total) / 100
    return f"{caught}/{total} ({pct:.2f}%)"


def _print_miss_signals(label: str, missed: list[Missed]) -> None:
    if not missed:
        return
    by_signal: dict[str, int] = defaultdict(int)
    for m in missed:
        by_signal[m.signal] += 1
    typer.echo(
        f"  {label}, by the signal that would have caught it: "
        + ", ".join(f"{k} {v}" for k, v in sorted(by_signal.items(), key=lambda kv: -kv[1]))
    )


def _github_token() -> str | None:
    """GITHUB_TOKEN from the environment, else from .env (the working directory's, then the
    repo root's). The environment always wins. Never printed or logged."""
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    for env in (Path(".env"), Path(__file__).resolve().parents[2] / ".env"):
        value = _dotenv_value(env, "GITHUB_TOKEN")
        if value:
            return value
    return None


def _dotenv_value(path: Path, name: str) -> str | None:
    """``name``'s value in a .env file: ``NAME=value`` lines, with optional ``export``,
    quotes and comments."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.removeprefix("export ").partition("=")
        if sep and key.strip() == name:
            value = value.strip()
            if value[:1] in "\"'" and value[-1:] == value[:1] and len(value) > 1:
                return value[1:-1]
            return value.split(" #", 1)[0].strip()
    return None


def main(
    repo: Annotated[str, typer.Option(help="Repository name in siftwise, e.g. RDFLib/rdflib.")],
    runs: Annotated[int, typer.Option(min=1, help="Most recent PR runs to evaluate.")] = 10,
    failing: Annotated[
        bool, typer.Option("--failing", help="Also evaluate every PR run that had failures.")
    ] = False,
    replay_main: Annotated[
        bool, typer.Option("--replay-main", help="Treat each main run as a PR.")
    ] = False,
    replay_prs: Annotated[
        bool,
        typer.Option("--replay-prs", help="Every PR merged in the --days before the history ends."),
    ] = False,
    days: Annotated[
        int, typer.Option(help="--replay-prs: days back from the newest recorded run.")
    ] = 90,
    source: Annotated[
        str, typer.Option(help="'checkout' (--checkout) or 'github' (zipballs, no git).")
    ] = "checkout",
    goos: Annotated[str | None, typer.Option(help="GOOS for go list (e.g. linux).")] = None,
    json_out: Annotated[Path | None, typer.Option(help="Write per-target results here.")] = None,
    last: Annotated[
        int | None, typer.Option(help="--replay-main: only the most recent N main runs.")
    ] = None,
    commits: Annotated[
        str | None, typer.Option(help="Only targets at these commits (comma-separated prefixes).")
    ] = None,
    resume: Annotated[
        Path | None,
        typer.Option(
            help="JSONL of per-target results: appended as each target finishes, and targets "
            "already in it are skipped, so an interrupted eval continues where it stopped."
        ),
    ] = None,
    language: Annotated[
        str, typer.Option(help="auto, go or python (auto: go if the checkout has go.mod).")
    ] = "auto",
    go_module: Annotated[
        str | None, typer.Option(help="Go module path (default: from the checkout's go.mod).")
    ] = None,
    siftwise_toml: Annotated[
        Path | None, typer.Option(help="Repo config with [[depends]] / always_run.")
    ] = None,
    checkout: Annotated[
        Path | None, typer.Option(help="Git clone of the repo, for import graphs.")
    ] = None,
    go: Annotated[str, typer.Option(help="go binary for `go list`.")] = "go",
    cache_dir: Annotated[Path, typer.Option(help="Cache for compares and graphs.")] = Path(
        ".eval-cache"
    ),
    verbose: Annotated[bool, typer.Option("--verbose", help="Details for every run.")] = False,
    triage: Annotated[
        bool,
        typer.Option(
            "--triage",
            help="Triage every recorded run with failures as of just before it, and score the "
            "verdicts against proxy labels from what happened after (see triage_labels.py).",
        ),
    ] = False,
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    if checkout and not go_module:
        go_module = read_go_module(checkout)
    is_go = language == "go" or (language == "auto" and go_module is not None)
    repo_config = load_repo_config(siftwise_toml) if siftwise_toml else None
    config = SelectorConfig(
        go_module=go_module,
        depends=repo_config.depends if repo_config else (),
        always_run=repo_config.always_run if repo_config else (),
        generated=repo_config.generated if repo_config else (),
    )
    github_source = None
    if source == "github":
        token = _github_token()
        if not token:
            typer.echo("error: --source github needs GITHUB_TOKEN", err=True)
            raise typer.Exit(2)
        github_source = GitHubSource(token, repo)
    graphs = GraphProvider(checkout, go, cache_dir, github_source, goos)
    with get_sessionmaker()() as session:
        repo_id = session.execute(
            text("SELECT id FROM repos WHERE name = :name"), {"name": repo}
        ).scalar_one_or_none()
        if repo_id is None:
            typer.echo(f"error: unknown repo {repo!r}", err=True)
            raise typer.Exit(2)
        wanted = tuple(c.strip() for c in commits.split(",") if c.strip()) if commits else ()
        if triage:
            token = _github_token()
            if not token:
                typer.echo("error: --triage needs GITHUB_TOKEN (fix diffs)", err=True)
                raise typer.Exit(2)
            meta = load_run_meta(session, repo_id)
            changes = RunChanges(graphs, config, is_go, meta,
                                 changed_files_of(session, list(meta)))  # fmt: skip
            paths: dict[str, str | None] = {
                test_id: path
                for test_id, path in session.execute(
                    text("SELECT test_id, file_path FROM tests WHERE repo_id = :repo_id"),
                    {"repo_id": repo_id},
                )
            }
            fixes = FixLinks(repo, GitHubCompare(token, cache_dir), changes, paths)
            window = get_settings().stats_window_days
            scored = triage_eval(
                session, repo, repo_id, changes, fixes, window, resume, wanted, verbose
            )
            print_triage_summary(repo, scored)
            graphs.close()
            return
        durations = top_level_durations(session, repo_id, go=is_go)
        rules = f"{len(config.depends)} declared rule(s)" if config.depends else "no declared rules"
        graph = "go list" if is_go else "python imports"
        if not graphs.has_source:
            graph += " (cached only, no --checkout or --source)"
        typer.echo(f"# {repo} ({'go' if is_go else 'python'}): {rules}; import graph: {graph}")
        started = time.perf_counter()
        index = HistoryIndex(session, repo_id)
        typer.echo(f"history index: {index} in {time.perf_counter() - started:.0f}s", err=True)

        everything: list[Result] = []  # for --json-out

        done = resumed_keys(resume) if resume else set()
        if done:
            typer.echo(f"resuming: {len(done)} targets already in {resume}", err=True)

        def run_all(title: str, targets: list[TargetRun]) -> list[Result]:
            typer.echo(f"\n# {title}")
            results = []
            for target in targets:
                if wanted and not target.commit_sha.startswith(wanted):
                    continue
                if target_key(target) in done:
                    continue
                result = evaluate(session, index, target, config, graphs, durations, is_go)
                print_result(result, verbose)
                results.append(result)
                if resume:  # one line per target as soon as it's done: a crash loses nothing
                    with resume.open("a", encoding="utf-8") as out:
                        out.write(json.dumps(result_json(result)) + "\n")
            everything.extend(results)
            return results

        if replay_prs:
            token = _github_token()
            if not token:
                typer.echo("error: --replay-prs needs GITHUB_TOKEN", err=True)
                raise typer.Exit(2)
            until = history_end(session, repo_id)
            if until is None:
                typer.echo(f"error: no recorded runs for {repo!r}", err=True)
                raise typer.Exit(2)
            pr_window = f"{until - timedelta(days=days):%Y-%m-%d %H:%M} to {until:%Y-%m-%d %H:%M}"
            typer.echo(f"merged PRs window: {pr_window} UTC ({days} days up to the end of the "
                       "recorded history)")  # fmt: skip
            compare = GitHubCompare(token, cache_dir)
            targets = merged_pr_targets(token, repo, days, compare, until)
            print_summary(f"PRs merged {pr_window}", run_all("merged PRs", targets))
        elif replay_main:
            token = _github_token()
            if not token:
                typer.echo("error: --replay-main needs GITHUB_TOKEN", err=True)
                raise typer.Exit(2)
            compare = GitHubCompare(token, cache_dir)
            targets = main_replay_targets(session, repo_id, repo, compare, last)
            print_summary("main runs replayed as PRs", run_all("main replay", targets))
        else:
            recent = run_all("most recent PR runs", pr_targets(session, repo_id, runs, False))
            failing_results: list[Result] = []
            if failing:
                seen = {r.run.run_id for r in recent}
                extra = [
                    t for t in pr_targets(session, repo_id, 10_000, True) if t.run_id not in seen
                ]
                failing_results = [r for r in recent if r.failures] + run_all(
                    "other PR runs with failures", extra
                )
            print_summary("most recent PR runs", recent)
            print_summary("PR runs with failures", failing_results)
        if json_out:
            json_out.write_text(json.dumps([result_json(r) for r in everything], indent=1))
    graphs.close()


if __name__ == "__main__":
    typer.run(main)
