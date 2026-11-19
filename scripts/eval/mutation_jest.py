"""Jest repos for the mutation eval (``scripts/eval/mutation.py``): the same kill rule, recall
and miss causes, with a Jest suite, the JS/TS import graph and TypeScript mutants.

What differs from the pytest repos:

* **History: one run, seeded.** These repos record no JUnit in CI, so each change's history is
  ONE full-suite run at the PR's base commit, recorded through siftwise's own ingest
  (``core.ingest.create_run``) and read back with ``load_history``: one-run history, as for a
  newly installed siftwise. Each change has its own seed repo (``<repo>@seed-<base>``), so
  histories never mix; point ``SIFTWISE_DATABASE_URL`` at a database of their own.
* **Build step.** Tests in one package may import another package's BUILT output (mobx-react
  imports ``mobx`` from ``packages/mobx/dist``), as in CI (``npm run build:test``, then
  ``jest``). A mutant in such a package is followed by its rebuild; the unmutated ``dist`` is
  put back from a copy taken at the head.
* **Mutants** (``mutators.js_mutants``) must type-check: a candidate whose package gets a
  ``tsc --noEmit`` error the head doesn't have is skipped (ts-jest would fail every test file
  importing it before a test ran: a compile error, not a caught bug), and the next candidate
  in ``choose``'s order is tried.
* **Selected = its file runs.** ``jest <files>`` runs every test in each file, tests new in
  the PR too, so a test counts as selected when its file is among the selection's (the
  selector's ``WouldRun`` credits named tests only). Test IDs are ``<file> [<project>]::<full
  name>`` (``JEST_JUNIT_CLASSNAME``): with jest-junit's default templates, same-named tests
  in different files share an ID, and a file run by two Jest projects repeats its IDs.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import statistics
import subprocess
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from sqlalchemy import text

from scripts.eval.mutation import (
    FULL,
    MISSED,
    REST,
    SELECTED,
    Change,
    RunResult,
    _kill_tree,
    append_jsonl,
    change_from_json,
    change_json,
    decide,
    fetch,
    git,
    mutant_cause,
    read_jsonl,
    sample_order,
    spent_seconds,
    state_dir_for,
    walk_sample,
)
from scripts.eval.mutators import Mutant, changed_lines, choose, js_mutants
from scripts.eval.run_eval import GitHubCompare, _github_token, merged_pr_targets
from siftwise.cli.jsgraph import JsGraph, build_js_graph
from siftwise.core.junit import JUnitParseError, ParsedTestResult, Status, parse_junit
from siftwise.core.selector import (
    JS_EXTENSIONS,
    JsImport,
    JsImports,
    Mode,
    RepoHistory,
    Selection,
    SelectorConfig,
    is_ignorable,
    is_jest_test_file,
    load_history,
    select_tests,
)
from siftwise.core.shadow import FLAKY_HISTORY, LINKED_NOT_SELECTED, NEW_TEST, NO_LINK
from siftwise.core.shadow import UNKNOWN_CAUSE as UNKNOWN
from siftwise.db import get_sessionmaker

SEED_LABEL = "one-run history (as for a newly installed siftwise)"
RANDOM, TARGETED = "random", "targeted"
TARGETED_MAX = 10  # changes with mutants in the targeted sample
WIDE_SKIP = 0.30  # a selection "skips a real share" when it leaves out this share of test files
WIDE_MIN = 5  # fewer such selections in the random sample: the targeted sample runs
TARGETED_LABEL = "targeted: changed source outside packages/mobx/src"
JEST_JUNIT = "jest-junit@16.0.0"
# Unique per test: the file and Jest project, then jest-junit's default title (the full name).
JUNIT_CLASSNAME = "{filepath} [{displayName}]"


@dataclass(frozen=True)
class JsPackage:
    """A workspace package: where its source is, how to type-check it, and (when other
    packages' tests import its build output) how to rebuild it and where that output is."""

    root: str  # "packages/mobx/"
    tsconfig: str | None = None
    rebuild: tuple[str, ...] = ()
    dist: str | None = None

    @property
    def source(self) -> str:
        return f"{self.root}src/"


@dataclass(frozen=True)
class JestProfile:
    repo: str
    clone_url: str
    install: tuple[str, ...]  # run in the checkout (network allowed: setup)
    build: tuple[str, ...]  # CI's build step before the tests
    packages: tuple[JsPackage, ...]
    jest_args: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    window_days: int = 365  # merged PRs this far back from the first run
    lockfile: str = "package-lock.json"  # `install` needs it at a commit (npm ci)
    core: str = ""  # the targeted sample's changes have no changed source under this
    branch: str = "main"
    note: str = ""


JEST_PROFILES = {
    # .github/workflows/build_and_test.yml: Node 24, `npm ci --ignore-scripts`,
    # `npm run build:test`, `npm test -- -i`. In band, one process runs out of V8 heap in the
    # eval's 7.57 GiB VM (after 38 files); 3 workers recycled at 1 GB run it in 100 s.
    "mobxjs/mobx": JestProfile(
        repo="mobxjs/mobx",
        clone_url="https://github.com/mobxjs/mobx.git",
        install=("npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"),
        build=("npm", "run", "build:test"),
        packages=(
            JsPackage(
                "packages/mobx/",
                "packages/mobx/tsconfig.json",
                ("npm", "-w", "mobx", "run", "build:test"),
                "packages/mobx/dist",
            ),
            JsPackage("packages/mobx-react-lite/", "packages/mobx-react-lite/tsconfig.json"),
            JsPackage("packages/mobx-react/", "packages/mobx-react/tsconfig.json"),
            JsPackage("packages/mobx-undecorate/", "packages/mobx-undecorate/tsconfig.json"),
            JsPackage("packages/mobx-react-plugin/", "packages/mobx-react-plugin/tsconfig.json"),
            JsPackage("packages/eslint-plugin-mobx/", "packages/eslint-plugin-mobx/tsconfig.json"),
        ),
        jest_args=("-w", "3", "--workerIdleMemoryLimit=1GB"),
        env={"NODE_OPTIONS": "--max-old-space-size=3072"},
        core="packages/mobx/src/",
        note="jest -w 3 --workerIdleMemoryLimit=1GB (CI: -i), after npm run build:test",
    ),
}


def package_of(path: str, profile: JestProfile) -> JsPackage | None:
    return next((p for p in profile.packages if path.startswith(p.root)), None)


def is_js_source(path: str, profile: JestProfile) -> bool:
    """A non-test JS/TS source file in one of the profile's packages' ``src/``."""
    package = package_of(path, profile)
    if package is None or not path.startswith(package.source):
        return False
    if os.path.splitext(path)[1] not in JS_EXTENSIONS or path.endswith(".d.ts"):
        return False
    return not is_jest_test_file(path)


def seed_repo(repo: str, base: str) -> str:
    return f"{repo}@seed-{base[:12]}"


def jest_files(commands: Sequence[str]) -> list[str]:
    """The test files of a selection's ``jest <files>`` commands."""
    files: list[str] = []
    for command in commands:
        argv = shlex.split(command)
        if argv and argv[0] == "jest":
            files += [a for a in argv[1:] if not a.startswith("-")]
    return files


def test_name(test_id: str) -> str:
    """The full Jest name (``describe ... title``): the ID after its ``<file> [project]``."""
    return test_id.split("::", 1)[1] if "::" in test_id else test_id


def tsc_errors(output: str) -> set[str]:
    """tsc's error lines (``file(line,col): error TSnnnn: ...``)."""
    return {line.strip() for line in output.splitlines() if ": error TS" in line}


def import_chain(graph: JsGraph, start: str, target: str) -> list[str] | None:
    """Shortest import chain from ``start`` to ``target`` in the JS/TS graph."""
    parent: dict[str, str | None] = {start: None}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        if current == target:
            chain = [current]
            while (previous := parent[chain[-1]]) is not None:
                chain.append(previous)
            return chain[::-1]
        for nxt in sorted(graph.edges.get(current, ())):
            if nxt not in parent:
                parent[nxt] = current
                queue.append(nxt)
    return None


def js_miss_cause(
    test_id: str, history: RepoHistory, mutated: str, graph: JsGraph, file: str | None
) -> tuple[str, str]:
    """``mutation.miss_cause`` for a Jest test: new, flaky before, linked by the JS/TS graph
    but not selected (unknown), or no link from its file to the mutated file."""
    known = history.tests.get(test_id)
    if known is None:
        return NEW_TEST, "no result for this test in the seeded history"
    if known.flaky_score:
        return FLAKY_HISTORY, f"flaky before (flaky score {known.flaky_score:.2f})"
    if file:
        chain = import_chain(graph, file, mutated)
        if chain:
            return UNKNOWN, f"{LINKED_NOT_SELECTED}: {' -> '.join(chain[:5])}"
    return NO_LINK, f"{file or test_id} doesn't import {mutated} in the JS/TS import graph"


def time_skipped(history: RepoHistory, files: set[str], full: bool) -> float | None:
    """Share of the seeded run's test time in files the selection doesn't run."""
    if full:
        return 0.0
    total = would = 0.0
    for known in history.tests.values():
        ms = known.avg_duration_ms or 0.0
        total += ms
        if known.file_path in files:
            would += ms
    return 100 * (1 - would / total) if total else None


# --- running Jest -------------------------------------------------------------------------


class Jest:
    """The repo's Jest suite in its checkout, with a mutant applied or not (``mutation.Suite``).

    A mutant in a package whose build output other tests import is followed by its rebuild;
    putting the mutant back restores that output from the copy ``snapshot`` took."""

    def __init__(self, src: Path, work: Path, profile: JestProfile, run_timeout: float) -> None:
        self.src, self.work, self.profile, self.run_timeout = src, work, profile, run_timeout
        self.paths: dict[str, str | None] = {}  # test ID -> its file (jest-junit's file attr)
        self.selected_files: list[str] = []
        self.all_files: list[str] = []
        self.last_results: list[ParsedTestResult] = []
        self.mutant: Mutant | None = None
        self._original: bytes | None = None
        self._applied = False
        self._rebuilt = False

    # build output

    def _pristine(self, package: JsPackage) -> Path:
        return self.work / "pristine" / package.root

    def snapshot(self) -> None:
        """Copy each rebuilt package's build output, as built at this commit."""
        for package in self.profile.packages:
            if package.dist:
                target = self._pristine(package)
                shutil.rmtree(target, ignore_errors=True)
                shutil.copytree(self.src / package.dist, target, symlinks=True)

    def _put_back(self, package: JsPackage) -> None:
        assert package.dist
        shutil.rmtree(self.src / package.dist, ignore_errors=True)
        shutil.copytree(self._pristine(package), self.src / package.dist, symlinks=True)

    # mutant state

    def use(self, mutant: Mutant) -> None:
        self.restore()
        self.mutant = mutant
        self._original = (self.src / mutant.path).read_bytes()

    def _set(self, applied: bool) -> None:
        if self.mutant is None or self._original is None or applied == self._applied:
            return
        path = self.src / self.mutant.path
        package = package_of(self.mutant.path, self.profile)
        if applied:
            path.write_bytes(self.mutant.apply(self._original.decode("utf-8")).encode("utf-8"))
            if package and package.rebuild:
                self._rebuilt = True
                proc = run(package.rebuild, self.src, self.profile.env, timeout=600)
                if proc.returncode != 0:
                    raise RuntimeError(f"rebuild failed with {self.mutant.id}: "
                                       f"{proc.stdout[-800:]}")  # fmt: skip
        else:
            path.write_bytes(self._original)
            if package and self._rebuilt:
                self._put_back(package)
                self._rebuilt = False
        self._applied = applied

    def restore(self) -> None:
        self._set(False)
        self.mutant, self._original = None, None

    # Suite

    def list_tests(self) -> list[str]:
        proc = run(self._argv("--listTests"), self.src, self.profile.env, timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(f"jest --listTests failed: {proc.stdout[-800:]}")
        found = []
        for line in proc.stdout.splitlines():
            p = Path(line.strip())
            if p.is_absolute() and p.is_file():
                found.append(p.resolve().relative_to(self.src.resolve()).as_posix())
        self.all_files = sorted(set(found))
        return self.all_files

    def run(self, part: str) -> RunResult:
        self._set(True)
        if part == SELECTED:
            return self._jest(self.selected_files) if self.selected_files else RunResult({})
        if part == REST:
            chosen = set(self.selected_files)
            rest = [f for f in self.all_files if f not in chosen]
            return self._jest(rest) if rest else RunResult({})
        return self._jest(None)

    def tests(self, test_ids: Sequence[str], mutant: bool) -> RunResult:
        """By file and full name (``-t``); a test with no known file can't be run alone."""
        self._set(mutant)
        files = sorted({f for t in test_ids if (f := self.paths.get(t))})
        if not files:
            return RunResult({})
        names = sorted({test_name(t) for t in test_ids})
        return self._jest(files, names)

    def files(self, test_ids: Sequence[str], mutant: bool) -> RunResult:
        """These tests' files, whole: no ``-t`` (a name it can't match runs nothing)."""
        self._set(mutant)
        files = sorted({f for t in test_ids if (f := self.paths.get(t))})
        return self._jest(files) if files else RunResult({})

    def _argv(self, *extra: str) -> list[str]:
        jest = str(self.src / "node_modules" / ".bin" / "jest")
        return [jest, "--ci", *self.profile.jest_args,
                f"--cacheDirectory={self.work / 'jest-cache'}", *extra]  # fmt: skip

    def _jest(self, files: Sequence[str] | None, names: Sequence[str] = ()) -> RunResult:
        """``files`` None: the whole suite."""
        from siftwise.cli.reruns import jest_name_pattern

        self.work.mkdir(parents=True, exist_ok=True)
        report, log_path = self.work / "junit.xml", self.work / "jest.log"
        report.unlink(missing_ok=True)
        argv = self._argv("--passWithNoTests", "--reporters=default", "--reporters=jest-junit")
        if names:
            argv += ["-t", jest_name_pattern(names)]
        if files is not None:
            argv += ["--runTestsByPath", *files]
        env = {**os.environ, **self.profile.env,
               "JEST_JUNIT_OUTPUT_DIR": str(self.work), "JEST_JUNIT_OUTPUT_NAME": report.name,
               "JEST_JUNIT_ADD_FILE_ATTRIBUTE": "true",
               "JEST_JUNIT_CLASSNAME": JUNIT_CLASSNAME}  # fmt: skip
        started = time.perf_counter()
        with log_path.open("wb") as log:
            proc = subprocess.Popen(argv, cwd=self.src, stdout=log, stderr=subprocess.STDOUT,
                                    env=env, start_new_session=os.name != "nt")  # fmt: skip
        try:
            proc.wait(timeout=self.run_timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            return RunResult({}, time.perf_counter() - started, timed_out=True)
        seconds = time.perf_counter() - started
        if not report.exists():
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-800:]
            raise RuntimeError(f"jest wrote no report (exit {proc.returncode}): {tail}")
        try:
            results = parse_junit(report.read_bytes())
        except JUnitParseError as exc:
            raise RuntimeError(f"unreadable jest report: {exc}") from exc
        self.last_results = results
        statuses: dict[str, Status] = {}
        for r in results:  # the last attempt wins
            statuses[r.test_id] = r.status
            self.paths.setdefault(r.test_id, r.file_path)
        return RunResult(statuses, seconds)


def run(argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float
        ) -> subprocess.CompletedProcess[str]:  # fmt: skip
    return subprocess.run(list(argv), cwd=cwd, env={**os.environ, **env}, capture_output=False,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          check=False)  # fmt: skip


# --- one change ---------------------------------------------------------------------------


def checkout(src: Path, sha: str, profile: JestProfile) -> float:
    """Check out ``sha`` clean, install and build it as CI does; seconds taken."""
    began = time.perf_counter()
    git(src, "checkout", "-q", "-f", "--detach", sha)
    git(src, "clean", "-q", "-f", "-d", "-x")
    for argv in (profile.install,
                 ("npm", "i", "--no-save", "--no-audit", "--no-fund", JEST_JUNIT),
                 profile.build):  # fmt: skip
        proc = run(argv, src, profile.env, timeout=1800)
        if proc.returncode != 0:
            raise RuntimeError(f"{' '.join(argv)} failed at {sha[:9]}: {proc.stdout[-800:]}")
    return time.perf_counter() - began


def mutants_of(src: Path, change: Change, profile: JestProfile) -> tuple[list[Mutant], int]:
    """Every mutant on the change's changed lines of non-test JS/TS source, at its head."""
    globs = [f"*{ext}" for ext in sorted(JS_EXTENSIONS)]
    diff = git(src, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames",
               f"{change.base}...{change.target.commit_sha}", "--", *globs).stdout  # fmt: skip
    found: list[Mutant] = []
    files = 0
    for path, lines in sorted(changed_lines(diff).items()):
        if not is_js_source(path, profile) or not (src / path).is_file():
            continue
        try:
            source = (src / path).read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        files += 1
        found += js_mutants(path, source, lines)
    return found, files


def type_checked(
    src: Path, profile: JestProfile, ordered: Sequence[Mutant], k: int
) -> tuple[list[Mutant], int]:
    """The first ``k`` of ``ordered`` whose package type-checks with them as at the head (no
    tsc error the head doesn't have), and how many were rejected on the way."""
    baseline: dict[str, set[str]] = {}
    chosen: list[Mutant] = []
    rejected = 0
    for m in ordered:
        if len(chosen) >= k:
            break
        package = package_of(m.path, profile)
        if package is None or package.tsconfig is None:
            chosen.append(m)
            continue
        if package.tsconfig not in baseline:
            baseline[package.tsconfig] = tsc_errors(tsc_output(src, profile, package.tsconfig))
        path = src / m.path
        original = path.read_bytes()
        try:
            path.write_bytes(m.apply(original.decode("utf-8")).encode("utf-8"))
            errors = tsc_errors(tsc_output(src, profile, package.tsconfig))
        finally:
            path.write_bytes(original)
        if errors - baseline[package.tsconfig]:
            rejected += 1
        else:
            chosen.append(m)
    return chosen, rejected


def tsc_output(src: Path, profile: JestProfile, tsconfig: str) -> str:
    """``tsc --noEmit -p <tsconfig>``'s output, with the checkout's own TypeScript."""
    tsc = [str(src / "node_modules" / ".bin" / "tsc"), "--noEmit", "-p", tsconfig]
    return run(tsc, src, profile.env, 600).stdout


@dataclass
class JsPrepared:
    selection: Selection
    history: RepoHistory
    graph: JsGraph
    selected_files: list[str]
    time_skipped_pct: float | None


def seed(session: Any, profile: JestProfile, src: Path, suite: Jest, change: Change
         ) -> dict[str, Any]:  # fmt: skip
    """One full run at the PR's base commit, recorded as the seed repo's only (main) run.
    Already seeded (a resume): nothing runs."""
    from siftwise.core.ingest import create_run
    from siftwise.core.schemas import RunMetadata

    name = seed_repo(profile.repo, change.base)
    runs = session.execute(
        text("SELECT count(*) FROM runs r JOIN repos p ON p.id = r.repo_id WHERE p.name = :n"),
        {"n": name},
    ).scalar_one()
    if runs:
        return {"seed_repo": name, "seeded_before": True}
    setup_s = checkout(src, change.base, profile)
    result = suite.run(FULL)
    if result.timed_out:
        raise RuntimeError(f"the seed run at {change.base[:9]} timed out")
    when = datetime.fromisoformat(
        git(src, "show", "-s", "--format=%cI", change.base).stdout.strip()
    )
    meta = RunMetadata(repo=name, commit_sha=change.base, branch=profile.branch, is_main=True,
                       ci_run_id="seed", started_at=when, changed_files_known=False)  # fmt: skip
    create_run(session, meta, suite.last_results)
    return {"seed_repo": name, "seed_setup_s": round(setup_s, 1),
            "seed_run_s": round(result.seconds, 1), "seed_results": len(result.statuses),
            "seed_failed": sorted(result.failed)}  # fmt: skip


def prepare(session: Any, profile: JestProfile, change: Change, src: Path,
            config: SelectorConfig) -> JsPrepared:  # fmt: skip
    """The selection ``siftwise select`` would make at the head, from the seeded history: the
    JS/TS graph's ``affected_files``, ``js_graph`` and import depths, as ``local_change``
    sends them."""
    history = load_history(session, seed_repo(profile.repo, change.base), config)
    if history is None:
        raise RuntimeError(f"no seeded history for {change.key}")
    changed = list(change.target.changed_files or ())
    deleted = tuple(sorted(p for p in changed if not (src / p).exists()))
    graph = build_js_graph(src, deleted)
    affected = graph.affected(changed)
    loadable = any(not p.endswith((".py", ".go")) and not is_ignorable(p, config.generated)
                   for p in changed)  # fmt: skip
    js_imports = JsImports(
        nearest={t: JsImport(n.module, n.via, n.depth) for t, n in affected.nearest.items()},
        dynamic=dict(affected.dynamic) if loadable else {},
        notes=tuple(affected.notes),
    )
    depths = {t: n.depth for t, n in affected.nearest.items()}
    selection = select_tests(history, changed, True, config, None, affected.files, deleted,
                             js_imports=js_imports, import_depths=depths or None)  # fmt: skip
    files = sorted(f for f in set(jest_files(selection.commands)) if (src / f).is_file())
    full = selection.mode is Mode.FULL
    return JsPrepared(selection, history, graph, files, time_skipped(history, set(files), full))


def process_change(
    change: Change,
    record: dict[str, Any] | None,
    profile: JestProfile,
    state: Path,
    session: Any,
    config: SelectorConfig,
    mutants_per_commit: int,
    seed_value: int,
    run_timeout: float,
    cap: float,
    done_mutants: set[str],
    sample: str = RANDOM,
) -> int:
    """Seed, select, mutate and decide one change; 1 if it had mutants, else 0 (none, or the
    change was skipped: then it isn't counted)."""
    src, work = state / "src", state / "work"
    commits_path, mutants_path = state / "commits.jsonl", state / "mutants.jsonl"
    sha = change.target.commit_sha
    began = time.perf_counter()
    typer.echo(f"\n## {change.target.branch} {sha[:9]} ({change.target.run_at:%Y-%m-%d})", err=True)
    if not fetch(src, sha, change.target.branch) or not fetch(src, change.base, ""):
        append_jsonl(commits_path, {"key": change.key, "error": "commit unavailable",
                                    "chosen": [], "setup_s": 0})  # fmt: skip
        return 0
    git(src, "checkout", "-q", "-f", "--detach", sha)
    candidates, files = mutants_of(src, change, profile)
    if not candidates:
        append_jsonl(commits_path, {"key": change.key, **change_json(change), "files": files,
                                    "candidates": 0, "chosen": [],
                                    "setup_s": round(time.perf_counter() - began, 1)})  # fmt: skip
        typer.echo(f"  no mutants ({files} source file(s) changed)", err=True)
        return 0
    reason = install_problem(src, change.base, profile)
    if reason:
        append_jsonl(commits_path, {"key": change.key, **change_json(change), "files": files,
                                    "candidates": len(candidates), "chosen": [],
                                    "skipped": reason, "sample": sample,
                                    "setup_s": round(time.perf_counter() - began, 1)})  # fmt: skip
        typer.echo(f"  skipped: {reason}", err=True)
        return 0
    suite = Jest(src, work, profile, run_timeout)
    seeded = seed(session, profile, src, suite, change)
    head_setup = checkout(src, sha, profile)
    suite.snapshot()
    rejected = 0
    if record is None:
        ordered = choose(candidates, len(candidates), f"{seed_value}:{sha}")
        chosen, rejected = type_checked(src, profile, ordered, mutants_per_commit)
    else:
        chosen = [Mutant.from_json(c) for c in record["chosen"]]
    prepared = prepare(session, profile, change, src, config)
    selection = prepared.selection
    suite.list_tests()
    suite.selected_files = prepared.selected_files
    setup = time.perf_counter() - began
    if record is None:
        record = {
            "key": change.key, **change_json(change), "files": files,
            "candidates": len(candidates), "type_rejected": rejected,
            "chosen": [m.to_json() | {"id": m.id} for m in chosen],
            "mode": selection.mode.value, "reason": selection.reason,
            "selected_count": selection.selected_count, "total_known": selection.total_known,
            "selected_files": len(prepared.selected_files), "test_files": len(suite.all_files),
            "time_skipped_pct": prepared.time_skipped_pct, "setup_s": round(setup, 1),
            "head_setup_s": round(head_setup, 1), "history": SEED_LABEL, "sample": sample,
            **seeded,
        }  # fmt: skip
        append_jsonl(commits_path, record)
    if not chosen:
        typer.echo(f"  no mutant type-checks ({len(candidates)} candidates)", err=True)
        return 0
    typer.echo(f"  {len(candidates)} mutants on {files} file(s); {len(chosen)} chosen "
               f"({rejected} rejected by tsc); {selection.mode.value}: "
               f"{selection.reason[:100]}; {len(prepared.selected_files)} of "
               f"{len(suite.all_files)} test files", err=True)  # fmt: skip
    full = selection.mode is Mode.FULL
    chosen_files = set(prepared.selected_files)

    def selected(test_id: str) -> bool:
        return full or suite.paths.get(test_id) in chosen_files

    baseline: dict[str, bool] = {}
    for m in chosen:
        key = f"{change.key}|{m.id}"
        if key in done_mutants:
            continue
        if spent_seconds(read_jsonl(commits_path), read_jsonl(mutants_path)) >= cap:
            break
        t0 = time.perf_counter()
        suite.use(m)
        try:
            decision = decide(suite, selected, full, baseline)
        finally:
            suite.restore()
        causes = (
            {t: js_miss_cause(t, prepared.history, m.path, prepared.graph, suite.paths.get(t))
             for t in decision.killers}
            if decision.outcome == MISSED
            else {}
        )  # fmt: skip
        result = {
            "key": key, "change": change.key, "mutant": m.to_json(), "outcome": decision.outcome,
            "killers": decision.killers[:20], "selected_killers": decision.selected_killers[:20],
            "cause": mutant_cause(causes), "causes": {t: list(c) for t, c in causes.items()},
            "unconfirmed": decision.unconfirmed[:20], "not_rerun": decision.not_rerun[:20],
            "baseline_failed": decision.baseline_failed, "runs": decision.runs,
            "seconds": round(time.perf_counter() - t0, 1), "sample": sample,
        }  # fmt: skip
        append_jsonl(mutants_path, result)
        done_mutants.add(key)
        typer.echo(f"  {decision.outcome:>10}  {m.describe()}  ({result['seconds']:.0f}s)",
                   err=True)  # fmt: skip
    return 1


# --- the eval -----------------------------------------------------------------------------


def load_changes(state: Path, profile: JestProfile) -> list[Change]:
    """Merged PRs in the ``window_days`` before the first run, cached in the state dir so
    every resume samples from the same list (and the same end date)."""
    cache = state / "changes.json"
    if cache.exists():
        return [change_from_json(c) for c in json.loads(cache.read_text(encoding="utf-8"))]
    token = _github_token()
    if not token:
        raise typer.BadParameter("listing merged PRs needs GITHUB_TOKEN")
    (state / "compare").mkdir(parents=True, exist_ok=True)
    until = datetime.now(UTC)
    targets = merged_pr_targets(token, profile.repo, profile.window_days,
                                GitHubCompare(token, state / "compare"), until)  # fmt: skip
    changes = [Change(t, t.base_sha) for t in targets
               if t.changed_files is not None and t.base_sha]  # fmt: skip
    cache.write_text(json.dumps([change_json(c) for c in changes], indent=1), encoding="utf-8")
    return changes


def run_eval(
    profile: JestProfile,
    state_root: Path,
    max_commits: int,
    mutants_per_commit: int,
    seed_value: int,
    time_cap_hours: float,
    run_timeout: float,
) -> None:
    state = state_dir_for(state_root, profile.repo)
    state.mkdir(parents=True, exist_ok=True)
    src = state / "src"
    if not (src / ".git").exists():
        subprocess.run(["git", "-C", str(state), "clone", "-q", profile.clone_url, src.name],
                       check=True, timeout=3600)  # fmt: skip
    commits_path, mutants_path = state / "commits.jsonl", state / "mutants.jsonl"
    done_mutants = {m["key"] for m in read_jsonl(mutants_path)}
    cap = time_cap_hours * 3600
    changes = load_changes(state, profile)
    typer.echo(f"{profile.repo}: {len(changes)} merged PRs in the window; {SEED_LABEL}", err=True)
    config = SelectorConfig()
    ordered = sample_order(changes, seed_value)

    def run_sample(label: str, candidates: Sequence[Change], limit: int) -> bool:
        """Up to ``limit`` changes with mutants from ``candidates``; False if the cap stopped it.
        A change the random walk reaches that an earlier run recorded as targeted belongs to
        the random sample (the seeded order puts it there): it is relabelled."""

        def adopt(record: Mapping[str, Any]) -> None:
            if sample_of(record) != label:
                relabel(state, str(record["key"]), label)

        return walk_sample(
            candidates, read_jsonl(commits_path), done_mutants, limit,
            lambda: spent_seconds(read_jsonl(commits_path), read_jsonl(mutants_path)) >= cap,
            lambda change, record: process_change(
                change, record, profile, state, session, config, mutants_per_commit,
                seed_value, run_timeout, cap, done_mutants, label,
            ),
            adopt,
        )  # fmt: skip

    with get_sessionmaker()() as session:
        if not run_sample(RANDOM, ordered, max_commits):
            return
        records = read_jsonl(commits_path)
        random_records = [c for c in records if sample_of(c) == RANDOM]
        wide = wide_selections(random_records)
        typer.echo(f"\nrandom sample done: {wide} selection(s) skip >= {WIDE_SKIP:.0%} of test "
                   "files", err=True)  # fmt: skip
        if wide >= WIDE_MIN or not profile.core:
            return
        taken = {c["key"] for c in random_records}
        targeted = [c for c in ordered if c.key not in taken and outside_core(c, profile)]
        typer.echo(f"targeted sample ({TARGETED_LABEL}): {len(targeted)} candidate change(s)",
                   err=True)  # fmt: skip
        run_sample(TARGETED, targeted, TARGETED_MAX)


def relabel(state: Path, key: str, sample: str) -> None:
    """Move a recorded change (and its mutants) to ``sample``, rewriting both JSONL files."""
    for name, field_ in (("commits.jsonl", "key"), ("mutants.jsonl", "change")):
        path = state / name
        records = read_jsonl(path)
        for r in records:
            if r.get(field_) == key:
                r["sample"] = sample
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        os.replace(tmp, path)


def sample_of(record: Mapping[str, Any]) -> str:
    return str(record.get("sample", RANDOM))


def install_problem(src: Path, base: str, profile: JestProfile) -> str | None:
    """Why the base commit can't be installed as CI installs today, or None."""
    if git(src, "cat-file", "-e", f"{base}:{profile.lockfile}", check=False).returncode == 0:
        return None
    if git(src, "cat-file", "-e", f"{base}:yarn.lock", check=False).returncode == 0:
        return f"base uses yarn (no {profile.lockfile})"
    return f"base has no {profile.lockfile}"


def outside_core(change: Change, profile: JestProfile) -> bool:
    """A change whose changed JS/TS source is all outside the profile's core package."""
    sources = [p for p in change.target.changed_files or () if is_js_source(p, profile)]
    return bool(sources) and not any(p.startswith(profile.core) for p in sources)


def file_share_skipped(record: Mapping[str, Any]) -> float:
    """Share of the suite's test files a selection leaves out (0 for a full suite)."""
    total = record.get("test_files") or 0
    if record.get("mode") != "selective" or not total:
        return 0.0
    return 1 - int(record.get("selected_files", 0)) / int(total)


def wide_selections(records: Sequence[Mapping[str, Any]]) -> int:
    return sum(1 for c in records if c.get("chosen") and file_share_skipped(c) >= WIDE_SKIP)


def report(commits: Sequence[Mapping[str, Any]], mutants: Sequence[Mapping[str, Any]]
           ) -> list[str]:  # fmt: skip
    """The Jest report: the random and the targeted sample apart, then the skipped changes."""
    from scripts.eval.mutation import report_lines

    sample = {c["key"]: sample_of(c) for c in commits}
    lines: list[str] = []
    for label, title in ((RANDOM, "random sample"), (TARGETED, TARGETED_LABEL)):
        part = [c for c in commits if sample_of(c) == label]
        if not part:
            continue
        done = [m for m in mutants if sample.get(m["change"], RANDOM) == label]
        lines += [f"== {title} ==", *seed_lines(part), *report_lines(part, done)]
        files = sorted(round(100 * file_share_skipped(c), 1) for c in part if c.get("chosen"))
        lines.append(
            f"test files skipped by the selections: {files}; >= {WIDE_SKIP:.0%}: "
            f"{wide_selections(part)}"
        )
    skipped = [c for c in commits if c.get("skipped")]
    lines.append(f"skipped changes (not counted): {len(skipped)}")
    lines += [f"  {c.get('pr', c['key'])} {c['key'].split('@')[-1][:9]}: {c['skipped']}"
              for c in skipped]  # fmt: skip
    return lines


def seed_lines(commits: Sequence[Mapping[str, Any]]) -> list[str]:
    """The report's lines on the seeded history and the type check."""
    seeded = [c for c in commits if c.get("seed_run_s") is not None]
    if not any(c.get("history") for c in commits):
        return []
    lines = [f"history: {SEED_LABEL}: one full run at each PR's base commit"]
    if seeded:
        failed = sorted({t for c in seeded for t in c.get("seed_failed", ())})
        seconds = statistics.median(c["seed_run_s"] for c in seeded)
        results = statistics.median(c["seed_results"] for c in seeded)
        shown = f" ({', '.join(failed[:5])})" if failed else ""
        lines.append(
            f"seed runs: {len(seeded)}, median {seconds:.0f}s and {results:.0f} "
            f"results; failing in a seed run: {len(failed)}{shown}"
        )
    rejected = sum(int(c.get("type_rejected", 0)) for c in commits)
    lines.append(f"type check: {rejected} candidate mutant(s) failed tsc and were skipped")
    return lines
