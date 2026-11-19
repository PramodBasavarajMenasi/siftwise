"""Mutation-based recall: how often a selection runs a test that catches a planted bug.

Replays measure recall only on failures CI happened to record. Here the failures are made:
small mutants (``mutators.py``) on the lines a real merged change touched, and the question
is whether siftwise's selection for that change, from history as of before it, runs a test
that catches each one.

For each sampled change (a merged PR: its base...head diff, its head commit's tree):

1. **Selection, no peeking:** history as of just before the PR head commit's time
   (``run_eval.HistoryIndex``), the PR's diff, and the static import graph at its head, as
   ``siftwise select`` sends them. Made once per change, before any test runs.
2. **Mutants:** up to ``--mutants-per-commit`` on the changed lines of non-test source files,
   spread over operators, with a fixed seed. Mutants that don't compile are dropped.
3. **Kill rule:** a test kills a mutant if it fails with the mutant, fails again when re-run
   with it (flaky failures drop out), and passes on the unmutated commit. Cheapest order
   first (decided before building, #74): the selected tests' files with the mutant; if a
   selected test kills it, it's **caught**. Otherwise the rest of the suite; a kill there is a
   **miss**. No kill anywhere: **not killed** (reported apart, never in recall). Candidate
   killers are re-run with the mutant and then on the baseline, so no full baseline run is
   needed; the tests that fail on the baseline too are reported as baseline failures.
4. **Recall** = caught / (caught + missed), with a Wilson 95% interval. Each miss gets a
   shadow-style cause (``core/shadow.py``): ``new_test``, ``flaky_history``, ``no_link``
   (the import graph doesn't connect the killing test to the mutated file) or ``unknown``.

Runs happen in a private clone and virtualenv under ``--state-dir``. Installs may use the
network; test runs don't: ``pytest-socket`` blocks every host but localhost, and the
profile deselects the tests that need the internet. Each run has a timeout (and each test,
through ``pytest-timeout``), the whole eval a time cap, and everything is appended to JSONL
as it finishes, so a re-run continues where it stopped.

    uv run python -m scripts.eval.mutation --repo RDFLib/rdflib --max-commits 5
    uv run python -m scripts.eval.mutation --repo RDFLib/rdflib --report

Reads SIFTWISE_DATABASE_URL (or .env) for history, and GITHUB_TOKEN to list merged PRs.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import random
import signal
import statistics
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Protocol

import typer
from sqlalchemy import text

if not __package__:  # run as a file path: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.eval.mutators import Mutant, changed_lines, choose, python_mutants
from scripts.eval.run_eval import (
    GitHubCompare,
    HistoryIndex,
    TargetRun,
    _github_token,
    history_end,
    merged_pr_targets,
    with_durations,
)
from siftwise.cli.pygraph import PyGraph, build_py_graph
from siftwise.core.junit import JUnitParseError, Status, parse_junit
from siftwise.core.selector import (
    Mode,
    RepoHistory,
    Selection,
    SelectorConfig,
    WouldRun,
    pytest_file,
    pytest_node,
    select_tests,
)
from siftwise.core.shadow import (
    FLAKY_HISTORY,
    LINKED_NOT_SELECTED,
    NEW_TEST,
    NO_LINK,
    UNKNOWN_CAUSE,
)
from siftwise.db import get_sessionmaker

CAUGHT = "caught"  # a selected test kills it
MISSED = "missed"  # only tests the selection leaves out kill it
NOT_KILLED = "not_killed"  # no test kills it: outside recall
TIMEOUT = "timeout"  # a mutant run hit the run timeout: outside recall
UNDECIDED = "undecided"  # too many candidate killers to confirm: outside recall
OUTCOMES = (CAUGHT, MISSED, NOT_KILLED, TIMEOUT, UNDECIDED)
MISS_CAUSES = (NEW_TEST, FLAKY_HISTORY, NO_LINK, UNKNOWN_CAUSE)  # shadow's order

CONFIRM_CHUNK = 25  # candidate killers re-run per batch
CONFIRM_CAP = 100  # candidates checked per phase before giving up (UNDECIDED)


# --- the kill rule ------------------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    """One pytest run: each test's final status (by siftwise test ID)."""

    statuses: Mapping[str, Status]
    seconds: float = 0.0
    timed_out: bool = False

    @property
    def failed(self) -> set[str]:
        return {t for t, s in self.statuses.items() if s in (Status.FAILED, Status.ERROR)}

    @property
    def passed(self) -> set[str]:
        return {t for t, s in self.statuses.items() if s is Status.PASSED}


SELECTED = "selected"  # the selected tests' files
FULL = "full"  # the whole suite
REST = "rest"  # the whole suite but the selected tests' files


class Suite(Protocol):
    """Runs tests with the mutant applied or on the baseline (the unmutated commit)."""

    def run(self, part: str) -> RunResult:
        """``SELECTED``, ``FULL`` or ``REST`` of the suite, with the mutant."""
        ...

    def tests(self, test_ids: Sequence[str], mutant: bool) -> RunResult:
        """Just these tests, with the mutant or on the baseline."""
        ...

    def files(self, test_ids: Sequence[str], mutant: bool) -> RunResult:
        """These tests' files, whole (no name filter): for a test ``tests`` didn't run."""
        ...


def executed(run: RunResult, test_id: str) -> bool:
    """Whether a run ran the test: a result that isn't "skipped". A name filter that matches
    nothing (jest -t on an ESLint RuleTester name) reports the file's tests skipped or not at
    all: that is no evidence the test passes."""
    status = run.statuses.get(test_id)
    return status is not None and status is not Status.SKIPPED


@dataclass
class Decision:
    outcome: str
    killers: list[str] = field(default_factory=list)
    selected_killers: list[str] = field(default_factory=list)
    unconfirmed: list[str] = field(default_factory=list)  # failed once, passed when re-run
    baseline_failed: list[str] = field(default_factory=list)  # fail without the mutant too
    # Candidate killers a re-run couldn't execute, by name or by file: their outcome is unknown.
    not_rerun: list[str] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)


def decide(
    suite: Suite,
    selected: Callable[[str], bool],
    full_selection: bool,
    baseline: dict[str, bool],
) -> Decision:
    """Apply the kill rule in the cheap order: the selection first, the rest of the suite only
    when no test there kills the mutant. ``baseline``: test ID -> passed on the baseline, a
    per-commit cache that this fills in."""
    decision = Decision(NOT_KILLED)

    def log(kind: str, run: RunResult, size: int | None = None) -> RunResult:
        decision.runs.append({"kind": kind, "seconds": round(run.seconds, 1),
                              "tests": size, "results": len(run.statuses),
                              "failed": len(run.failed), "timed_out": run.timed_out})  # fmt: skip
        return run

    def rerun(kind: str, tests: list[str], mutant: bool) -> RunResult:
        """These tests by name; those that didn't execute (no result, or skipped) again by
        their files. A test neither run executed stays without a result here."""
        run = log(kind, suite.tests(tests, mutant=mutant), len(tests))
        if run.timed_out:  # a hang loses the results of every test in the batch
            raise _TimedOut
        missing = [t for t in tests if not executed(run, t)]
        if not missing:
            return run
        again = log(f"{kind}-files", suite.files(missing, mutant=mutant), len(missing))
        if again.timed_out:
            raise _TimedOut
        statuses = {**run.statuses}
        statuses.update((t, again.statuses[t]) for t in missing if executed(again, t))
        return RunResult(statuses, run.seconds + again.seconds)

    def confirm(candidates: Iterable[str]) -> bool:
        """Check candidate killers, selected ones first, until the outcome is known. False if
        the cap stopped it before every candidate was checked."""
        ordered = sorted(set(candidates), key=lambda t: (not selected(t), t))
        for start in range(0, min(len(ordered), CONFIRM_CAP), CONFIRM_CHUNK):
            chunk = ordered[start : start + CONFIRM_CHUNK]
            confirmed = rerun("confirm", chunk, mutant=True)
            again = confirmed.failed
            decision.not_rerun += [t for t in chunk if not executed(confirmed, t)]
            decision.unconfirmed += [t for t in chunk if executed(confirmed, t)
                                     and t not in again]  # fmt: skip
            unknown = [t for t in chunk if t in again and t not in baseline]
            if unknown:
                base = rerun("baseline", unknown, mutant=False)
                decision.not_rerun += [t for t in unknown if not executed(base, t)]
                baseline.update((t, t in base.passed) for t in unknown if executed(base, t))
            decision.baseline_failed += [t for t in unknown if t in baseline and not baseline[t]]
            found = [t for t in chunk if t in again and baseline.get(t)]
            decision.killers += found
            decision.selected_killers += [t for t in found if selected(t)]
            rest = ordered[start + CONFIRM_CHUNK :]
            # Settled: a selected killer, or a killer once no selected candidate is left.
            if decision.selected_killers or (found and not any(selected(t) for t in rest)):
                return True
        return len(ordered) <= CONFIRM_CAP

    try:
        _decide(decision, suite, log, confirm, full_selection, selected)
    except _TimedOut:
        decision.outcome = TIMEOUT
    return decision


class _TimedOut(Exception):
    """A run hung (the run timeout, or pytest-timeout ending a process): outcome TIMEOUT."""


def _decide(
    decision: Decision,
    suite: Suite,
    log: Callable[..., RunResult],
    confirm: Callable[[Iterable[str]], bool],
    full_selection: bool,
    selected: Callable[[str], bool],
) -> None:
    first = log(FULL if full_selection else SELECTED, suite.run(FULL if full_selection
                                                                else SELECTED))  # fmt: skip
    if first.timed_out:
        decision.outcome = TIMEOUT
        return
    complete = confirm(first.failed)
    if decision.selected_killers:
        decision.outcome = CAUGHT
        return
    if decision.killers:
        # A test in a selected file, but not itself selected; unless a selected candidate
        # couldn't be re-run (it may be a killer: then the mutant is caught).
        unsure = any(selected(t) for t in decision.not_rerun)
        decision.outcome = UNDECIDED if unsure else MISSED
        return
    if decision.not_rerun:
        decision.outcome = UNDECIDED  # a candidate that may kill it couldn't be checked
        return
    if full_selection:
        decision.outcome = NOT_KILLED if complete else UNDECIDED
        return
    rest = log(REST, suite.run(REST))
    if rest.timed_out:
        decision.outcome = TIMEOUT
        return
    complete = confirm(rest.failed - first.failed) and complete
    if decision.killers:
        decision.outcome = MISSED
    elif decision.not_rerun or not complete:
        decision.outcome = UNDECIDED
    else:
        decision.outcome = NOT_KILLED


# --- recall and its interval --------------------------------------------------------------


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float] | None:
    """The Wilson score interval for ``k`` successes in ``n`` trials (95% by default); None
    when ``n`` is 0. Unlike the normal approximation it stays inside [0, 1] and is not zero
    width at k = n, which is where a selector's recall usually is."""
    if n <= 0:
        return None
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True)
class Recall:
    caught: int
    missed: int

    @property
    def total(self) -> int:
        return self.caught + self.missed

    @property
    def value(self) -> float | None:
        return self.caught / self.total if self.total else None

    @property
    def interval(self) -> tuple[float, float] | None:
        return wilson(self.caught, self.total)

    def __str__(self) -> str:
        if not self.total or self.value is None or self.interval is None:
            return "n/a (no mutant killed by the full suite)"
        low, high = self.interval
        return (f"{100 * self.value:.1f}% ({self.caught} of {self.total}; 95% CI "
                f"{100 * low:.1f}-{100 * high:.1f}%)")  # fmt: skip


def recall_of(outcomes: Iterable[str]) -> Recall:
    """Recall over mutants the full suite kills: caught / (caught + missed)."""
    counts = Counter(outcomes)
    return Recall(counts[CAUGHT], counts[MISSED])


# --- why a mutant was missed --------------------------------------------------------------


def miss_cause(
    test_id: str, history: RepoHistory, mutated: str, graph: PyGraph | None
) -> tuple[str, str]:
    """A killing test the selection left out: why, shadow-style, from what the selector had.
    New (no history), flaky before (its flaky score in history), linked by the import graph but
    not selected (unknown), or no link from the test's file to the mutated file."""
    known = history.tests.get(test_id)
    if known is None:
        return NEW_TEST, "no result for this test before the change"
    if known.flaky_score:
        return FLAKY_HISTORY, f"flaky before (flaky score {known.flaky_score:.2f})"
    test_file = pytest_file(test_id, known.file_path)
    if graph is not None and test_file and test_file in graph.edges:
        chain = graph.import_chain(test_file, {mutated})
        if chain:
            return UNKNOWN_CAUSE, f"{LINKED_NOT_SELECTED}: {' -> '.join(chain[:5])}"
    where = test_file or test_id
    return NO_LINK, f"{where} doesn't import {mutated} in the static import graph"


def mutant_cause(causes: Mapping[str, tuple[str, str]]) -> str | None:
    """A missed mutant's cause: the first of its killers' causes in ``MISS_CAUSES`` order."""
    found = {cause for cause, _ in causes.values()}
    return next((c for c in MISS_CAUSES if c in found), None)


# --- repos --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    """How to install and test one repo. ``install``: ``uv pip install`` arguments, with
    ``{src}`` for the checkout. ``source_roots``: where non-test source lives."""

    repo: str
    clone_url: str
    python: str
    install: tuple[str, ...]
    pytest_args: tuple[str, ...]
    source_roots: tuple[str, ...]
    note: str = ""


# pytest >= 8.2 reads arguments from @files (selections of hundreds of files exceed Windows'
# command line); < 9 for the poetry-era commits' own pins.
PYTEST_TOOLS = ("pytest>=8.2,<9", "pytest-xdist", "pytest-timeout", "pytest-socket", "pytest-cov")

CIRQ_PACKAGES = tuple(f"cirq-{p}" for p in ("aqt", "core", "google", "ionq", "pasqal", "web"))

PROFILES = {
    "RDFLib/rdflib": Profile(
        repo="RDFLib/rdflib",
        clone_url="https://github.com/RDFLib/rdflib.git",
        python="3.12",
        install=(
            "-e",
            "{src}[networkx,html,lxml,orjson]",
            "httpx",
            "coverage[toml]",
            "setuptools<72",
            "wheel",
            "testcontainers>=4.13.2,<5",
        ),
        # webtest: needs the internet; testcontainer: needs Docker images pulled from the
        # internet (CI's docs leg deselects both the same way). The GraphDB/RDF4J end-to-end
        # tests start Docker containers too, unmarked in some commits: ignored by path.
        pytest_args=("-m", "not (webtest or testcontainer)", "--ignore-glob=*test_e2e*"),
        source_roots=("rdflib/",),
        note='-m "not (webtest or testcontainer)" --ignore-glob=*test_e2e*',
    ),
    # As ci-monitoring.yml (the history's source): dev.env.txt on Python 3.12, then
    # `check/pytest -n logical`; slow and weekly tests skip by default (root conftest.py).
    # The nine packages are listed one by one: cirq-all-no-contrib.txt's editable paths are
    # relative to pip's directory, not the file's.
    "quantumlib/Cirq": Profile(
        repo="quantumlib/Cirq",
        clone_url="https://github.com/quantumlib/Cirq.git",
        python="3.12",
        install=(
            *(a for pkg in CIRQ_PACKAGES for a in ("-e", f"{{src}}/{pkg}")),
            *("-r", "{src}/cirq-core/cirq/contrib/requirements.txt"),
            *("-r", "{src}/dev_tools/requirements/deps/dev-tools.txt"),
        ),
        # pytest-randomly (in Cirq's dev tools) reorders tests and reseeds random each run:
        # a re-run must repeat the first, so it's off.
        pytest_args=("-p", "no:randomly"),
        source_roots=tuple(f"{pkg}/" for pkg in CIRQ_PACKAGES),
        note="-p no:randomly (slow and weekly tests skipped, as in CI)",
    ),
}

NON_TEST_DIRS = frozenset({"test", "tests", "testing", "docs", "doc", "examples", "scripts"})


def is_source_file(path: str, roots: Sequence[str]) -> bool:
    """A non-test Python source file under one of ``roots``."""
    if not path.endswith(".py") or not any(path.startswith(r) for r in roots):
        return False
    *dirs, name = path.split("/")
    if NON_TEST_DIRS.intersection(dirs) or name == "conftest.py":
        return False
    if name.endswith(("_pb2.py", "_pb2_grpc.py")):  # generated (protobuf)
        return False
    if name == "setup.py":  # packaging, not library code (Cirq has one per package)
        return False
    return not (name.startswith("test_") or name.endswith("_test.py"))


# --- running pytest -----------------------------------------------------------------------


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """End pytest and every process it started (multiprocessing pools, subprocess tests)."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                       check=False)  # fmt: skip
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)  # its own group: start_new_session
    proc.wait()


@dataclass(frozen=True)
class ShardRun:
    """How one pytest process of a run ended, for reports (``Pytest.last_shards``)."""

    shard: int
    exit_code: int | None
    report: bool  # wrote its JUnit file
    hung: bool  # pytest-timeout ended a test (Linux: that test fails; Windows: the process)
    crashed: bool  # a fatal error in the interpreter (faulthandler's "Fatal Python error")
    summary: str  # pytest's last line, e.g. "1459 passed, 21908 deselected in 518.98s"


TIMEOUT_BANNER = "+ Timeout +"  # pytest-timeout's stack dump title
TIMEOUT_FAILURE = "Failed: Timeout >"  # its signal method's failure (Linux, macOS)
CRASH_MARKERS = ("Fatal Python error", "Windows fatal exception")  # faulthandler


def shard_run(k: int, exit_code: int | None, junit: Path, log_path: Path) -> ShardRun:
    text_ = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    lines = [ln.strip(" =") for ln in text_.splitlines() if ln.strip()]
    summary = next((ln for ln in reversed(lines) if " in " in ln and ln[:1].isdigit()), "")
    return ShardRun(
        shard=k,
        exit_code=exit_code,
        report=junit.exists(),
        hung=TIMEOUT_BANNER in text_ or TIMEOUT_FAILURE in text_,
        crashed=any(m in text_ for m in CRASH_MARKERS),
        summary=summary,
    )


def rerun_file(test_id: str, file_path: str | None = None) -> str | None:
    """The file to re-run ``test_id`` from: ``pytest_file``, plus a package docstring's doctest
    (``rdflib.__init__::rdflib`` -> ``rdflib/__init__.py``), which the selector's doctest
    classifier doesn't recognise (its known gap: such doctests have no runner). The repo's
    pytest config must collect doctests (rdflib's addopts has ``--doctest-modules``)."""
    found = pytest_file(test_id, file_path)
    if found:
        return found
    classname, _, name = test_id.partition("::")
    package = classname.removesuffix(".__init__")
    if package != classname and name == package and package.replace(".", "").isidentifier():
        return package.replace(".", "/") + "/__init__.py"
    return None


def activated(python: Path, environ: Mapping[str, str]) -> dict[str, str]:
    """``environ`` as an activated virtualenv sees it (``python``'s): CI activates its venv,
    and tests that run its tools by name (Cirq's ``virtualenv``, ``coverage``) need them on
    PATH."""
    bin_dir = python.parent
    path = environ.get("PATH", "")
    return {**environ, "VIRTUAL_ENV": str(bin_dir.parent),
            "PATH": f"{bin_dir}{os.pathsep}{path}" if path else str(bin_dir)}  # fmt: skip


class Pytest:
    """Runs a repo's pytest suite in its virtualenv, with a mutant applied or not.

    The mutated file is written in place and its bytecode cache removed (a same-size edit
    within the same second could otherwise run stale bytecode); ``restore`` puts the original
    bytes back. Suite parts run in ``workers`` shards; short re-runs in one process.
    """

    def __init__(
        self,
        python: Path,
        src: Path,
        work: Path,
        profile: Profile,
        run_timeout: float,
        test_timeout: int,
        workers: int,
    ) -> None:
        self.python, self.src, self.work, self.profile = python, src, work, profile
        self.run_timeout, self.test_timeout, self.workers = run_timeout, test_timeout, workers
        self.paths: dict[str, str | None] = {}  # test ID -> reported file, for re-runs
        self.selected_files: list[str] = []
        self.last_shards: list[ShardRun] = []  # how the last run's processes ended
        self.mutant: Mutant | None = None
        self._original: bytes | None = None
        self._applied = False

    # mutant state

    def use(self, mutant: Mutant) -> None:
        self.restore()
        self.mutant = mutant
        self._original = (self.src / mutant.path).read_bytes()

    def _set(self, applied: bool) -> None:
        if self.mutant is None or self._original is None or applied == self._applied:
            return
        path = self.src / self.mutant.path
        if applied:
            path.write_bytes(self.mutant.apply(self._original.decode("utf-8")).encode("utf-8"))
        else:
            path.write_bytes(self._original)
        cache = path.parent / "__pycache__"
        for pyc in cache.glob(f"{path.stem}.*.pyc") if cache.is_dir() else ():
            pyc.unlink(missing_ok=True)
        self._applied = applied

    def restore(self) -> None:
        self._set(False)
        self.mutant, self._original = None, None

    # Suite

    def run(self, part: str) -> RunResult:
        self._set(True)
        if part == SELECTED:
            if not self.selected_files:
                return RunResult({})
            return self._pytest(self.selected_files, parallel=True)
        ignores = [f"--ignore={f}" for f in self.selected_files] if part == REST else []
        return self._pytest(ignores, parallel=True)

    def tests(self, test_ids: Sequence[str], mutant: bool) -> RunResult:
        """By node ID; a test that produced no result that way (pytest stops at a node ID it
        can't find, e.g. an odd parametrize ID) is re-run by its file."""
        self._set(mutant)
        if not test_ids:
            return RunResult({})
        nodes = sorted({n for t in test_ids if (n := pytest_node(t, self.paths.get(t)))})
        run = self._pytest(nodes, parallel=False) if nodes else RunResult({})
        missing = [t for t in test_ids if t not in run.statuses]
        files = sorted({f for t in missing if (f := pytest_file(t, self.paths.get(t)))})
        if not files:
            return run
        again = self._pytest(files, parallel=False)
        statuses = {**again.statuses, **run.statuses}
        return RunResult(statuses, run.seconds + again.seconds, run.timed_out or again.timed_out)

    def files(self, test_ids: Sequence[str], mutant: bool) -> RunResult:
        """These tests' files, whole."""
        self._set(mutant)
        files = sorted({f for t in test_ids if (f := rerun_file(t, self.paths.get(t)))})
        return self._pytest(files, parallel=False) if files else RunResult({})

    def _pytest(self, args: Sequence[str], parallel: bool) -> RunResult:
        """``args`` are paths, node IDs or ``--ignore``s; nothing at all means the whole suite,
        so callers with nothing to run don't call this. ``parallel``: ``self.workers``
        independent pytest processes, each running its share of the tests (``SHARD_PLUGIN``),
        not pytest-xdist: xdist refuses to run when its workers collect different test IDs,
        and some suites (rdflib) build parameter IDs at random."""
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "mutation_shard.py").write_text(SHARD_PLUGIN, encoding="utf-8")
        argfile = self.work / "args.txt"
        argfile.write_text("\n".join(args) + "\n", encoding="utf-8")
        shards = self.workers if parallel else 1
        env = {**activated(self.python, os.environ), "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONHASHSEED": "0", "NO_PROXY": "*", "PYTHONIOENCODING": "utf-8",
               "PYTHONPATH": str(self.work)}  # fmt: skip
        # A group of its own per pytest process, so a timeout can end what it started:
        # Windows for taskkill /T, elsewhere a session for killpg.
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        new_session = os.name != "nt"
        started = time.perf_counter()
        procs: list[tuple[subprocess.Popen[bytes], Path, Path]] = []
        for k in range(shards):
            junit, log_path = self.work / f"junit-{k}.xml", self.work / f"pytest-{k}.log"
            junit.unlink(missing_ok=True)
            command = [
                str(self.python), "-m", "pytest", *self.profile.pytest_args, "-q",
                "-p", "no:cacheprovider", "-p", "mutation_shard", "--no-cov", "-n", "0",
                f"--junitxml={junit}",
                # A mutant that breaks one module's import must not stop every other test.
                "--continue-on-collection-errors",
                f"--timeout={self.test_timeout}",
                "--disable-socket", "--allow-hosts=127.0.0.1,localhost,::1",
                f"@{argfile}",
            ]  # fmt: skip
            with log_path.open("wb") as log:
                proc = subprocess.Popen(command, cwd=self.src, stdout=log,
                                        stderr=subprocess.STDOUT, creationflags=flags,
                                        start_new_session=new_session,
                                        env={**env, "MUTATION_SHARD": f"{k}/{shards}"})  # fmt: skip
            procs.append((proc, junit, log_path))
        deadline = started + self.run_timeout
        timed_out = False
        for proc, _, _ in procs:
            try:
                proc.wait(timeout=max(0.0, deadline - time.perf_counter()))
            except subprocess.TimeoutExpired:
                timed_out = True
                break
        if timed_out:
            for proc, _, _ in procs:
                if proc.poll() is None:
                    _kill_tree(proc)
        seconds = time.perf_counter() - started
        self.last_shards = [shard_run(k, proc.returncode, junit, log_path)
                            for k, (proc, junit, log_path) in enumerate(procs)]  # fmt: skip
        if timed_out:
            return RunResult({}, seconds, timed_out)
        statuses: dict[str, Status] = {}
        hung = False
        for proc, junit, log_path in procs:
            if not junit.exists():
                log_text = log_path.read_text(encoding="utf-8", errors="replace")
                if TIMEOUT_BANNER in log_text:
                    # pytest-timeout ended a hanging test by ending the process (the only
                    # method on Windows): that process's results are lost, so the run counts
                    # as timed out rather than as "nothing failed".
                    hung = True
                    continue
                if proc.returncode == 4 and not parallel:
                    continue  # a node ID pytest can't find: the caller retries by file
                # Anything else that writes no report is broken (bad arguments, a crash): stop
                # rather than record "no test failed".
                raise RuntimeError(f"pytest wrote no report (exit {proc.returncode}): "
                                   f"{log_text[-800:]}")  # fmt: skip
            try:
                results = parse_junit(junit.read_bytes())
            except JUnitParseError as exc:
                raise RuntimeError(f"unreadable pytest report {junit}: {exc}") from exc
            for r in results:  # the last attempt wins
                statuses[r.test_id] = r.status
                self.paths.setdefault(r.test_id, r.file_path)
        return RunResult(statuses, seconds, timed_out=hung)


# Loaded into each shard's pytest with -p: keeps the shard's share of the collected tests. By
# test function (the node ID without its parameters), so a parametrized test whose IDs differ
# from one collection to the next (random values) still lands in exactly one shard.
SHARD_PLUGIN = """import os
import zlib


def pytest_collection_modifyitems(config, items):
    spec = os.environ.get("MUTATION_SHARD")
    if not spec:
        return
    k, n = (int(x) for x in spec.split("/"))
    keep, drop = [], []
    for item in items:
        key = item.nodeid.split("[", 1)[0].encode("utf-8")
        (keep if zlib.crc32(key) % n == k else drop).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
"""


# --- one change ---------------------------------------------------------------------------


def git(src: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """git in the eval's own clone (never the siftwise checkout)."""
    proc = subprocess.run(["git", "-C", str(src), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900, check=False)  # fmt: skip
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()[-300:]}")
    return proc


def has_commit(src: Path, sha: str) -> bool:
    return git(src, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def fetch(src: Path, sha: str, pr: str) -> bool:
    """Whether the clone has ``sha``, fetching it (or the PR's head ref) if needed."""
    if has_commit(src, sha):
        return True
    git(src, "fetch", "-q", "origin", sha, check=False)
    if not has_commit(src, sha) and pr.startswith("#"):
        git(src, "fetch", "-q", "origin", f"pull/{pr[1:]}/head", check=False)
    return has_commit(src, sha)


@dataclass(frozen=True)
class Change:
    """A sampled merged PR."""

    target: TargetRun
    base: str

    @property
    def key(self) -> str:
        return f"{self.target.branch}@{self.target.commit_sha}"


def change_json(change: Change) -> dict[str, Any]:
    t = change.target
    return {"sha": t.commit_sha, "base": change.base, "pr": t.branch,
            "run_at": t.run_at.isoformat(), "changed_files": list(t.changed_files or ()),
            "label": t.diff_label}  # fmt: skip


def change_from_json(data: Mapping[str, Any]) -> Change:
    target = TargetRun(
        0,
        None,
        data["sha"],
        data["pr"],
        datetime.fromisoformat(data["run_at"]),
        changed_files=tuple(data["changed_files"]),
        diff_label=data["label"],
    )
    return Change(target, data["base"])


def sample_order(changes: Sequence[Change], seed: int) -> list[Change]:
    """The changes in a seeded random order (from a canonical order, so the same list and
    seed always give the same order)."""
    ordered = sorted(changes, key=lambda c: (c.target.run_at, c.target.commit_sha))
    random.Random(seed).shuffle(ordered)
    return ordered


def mutants_of(src: Path, change: Change, roots: Sequence[str]) -> tuple[list[Mutant], int]:
    """Every compiling mutant on the change's changed lines of non-test source (at its head),
    and how many files were candidates."""
    diff = git(src, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames",
               f"{change.base}...{change.target.commit_sha}", "--", "*.py").stdout  # fmt: skip
    found: list[Mutant] = []
    files = 0
    for path, lines in sorted(changed_lines(diff).items()):
        if not is_source_file(path, roots) or not (src / path).is_file():
            continue
        try:
            source = (src / path).read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        files += 1
        found += python_mutants(path, source, lines)
    return found, files


@dataclass
class Prepared:
    selection: Selection
    history: RepoHistory
    graph: PyGraph
    selected_files: list[str]
    time_skipped_pct: float | None  # estimated from history durations, as replays do
    deleted: tuple[str, ...]


def prepare(
    session: Any, index: HistoryIndex, change: Change, src: Path, config: SelectorConfig
) -> Prepared:
    """The selection ``siftwise select`` would have made for the change, from history before
    its head commit: the same inputs as ``run_eval.evaluate``'s Python branch."""
    target = change.target
    history = index.history_before(target, config)
    changed = list(target.changed_files or ())
    graph = build_py_graph(src)
    affected_files = graph.affected(changed)
    depths = graph.depths(changed)
    deleted = tuple(sorted(p for p in changed if not (src / p).exists()))

    def select(h: RepoHistory) -> Selection:
        return select_tests(h, changed, True, config, {}, affected_files, deleted,
                            import_depths=depths)  # fmt: skip

    selection = select(history)
    timed = select(with_durations(session, index, target, history))
    skipped = None
    if timed.mode is Mode.FULL:
        skipped = 0.0
    elif timed.estimated_ms_total:
        skipped = 100 * (1 - timed.estimated_ms_would_run / timed.estimated_ms_total)
    files = set(selection.python_files_run_whole)
    for t in selection.tests:
        known = history.tests.get(t.test_id)
        path = pytest_file(t.test_id, t.file_path or (known.file_path if known else None))
        if path:
            files.add(path)
    existing = sorted(f for f in files if (src / f).is_file())
    return Prepared(selection, history, graph, existing, skipped, deleted)


# --- state --------------------------------------------------------------------------------


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """The records of a JSONL file; a line torn by a crash is skipped (and redone)."""
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(record) + "\n")


def spent_seconds(
    commits: Sequence[Mapping[str, Any]], mutants: Sequence[Mapping[str, Any]]
) -> float:
    """Work time recorded so far (setup and mutants), for the time cap across resumes."""
    setup = sum(float(c.get("setup_s", 0)) for c in commits)
    return setup + sum(float(m.get("seconds", 0)) for m in mutants)


def state_dir_for(root: Path, repo: str) -> Path:
    return root / repo.replace("/", "__")


def ensure_clone(profile: Profile, src: Path) -> None:
    if not (src / ".git").exists():
        src.parent.mkdir(parents=True, exist_ok=True)
        # -C the state dir: no git command runs with the siftwise checkout as its directory.
        subprocess.run(["git", "-C", str(src.parent), "clone", "-q", profile.clone_url,
                        src.name], check=True, timeout=3600)  # fmt: skip


def ensure_venv(profile: Profile, venv: Path) -> Path:
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        subprocess.run(["uv", "venv", "-q", "--python", profile.python, str(venv)], check=True)
    return python


def install(profile: Profile, python: Path, src: Path) -> None:
    """The change's own package and test tools, into the eval's virtualenv (network allowed:
    this is setup, not a test run)."""
    args = [a.replace("{src}", str(src)) for a in profile.install]
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    proc = subprocess.run(["uv", "pip", "install", "-q", "--python", str(python), *args,
                           *PYTEST_TOOLS], capture_output=True, text=True, env=env,
                          timeout=1800, check=False)  # fmt: skip
    if proc.returncode != 0:
        raise RuntimeError(f"install failed: {proc.stderr.strip()[-500:]}")


# --- the eval -----------------------------------------------------------------------------


def load_changes(
    state: Path, repo: str, session: Any, repo_id: int, min_history_days: int
) -> list[Change]:
    """Merged PRs from the history's start (+ ``min_history_days``) to its end, cached in the
    state dir so every resume samples from the same list."""
    cache = state / "changes.json"
    if cache.exists():
        return [change_from_json(c) for c in json.loads(cache.read_text(encoding="utf-8"))]
    token = _github_token()
    if not token:
        raise typer.BadParameter("listing merged PRs needs GITHUB_TOKEN")
    until = history_end(session, repo_id)
    start = session.execute(
        text("SELECT min(COALESCE(started_at, created_at)) FROM runs WHERE repo_id = :r"),
        {"r": repo_id},
    ).scalar_one()
    if until is None or start is None:
        raise typer.BadParameter(f"no recorded runs for {repo}")
    days = (until - start - timedelta(days=min_history_days)).days
    (state / "compare").mkdir(parents=True, exist_ok=True)
    compare = GitHubCompare(token, state / "compare")
    targets = merged_pr_targets(token, repo, days, compare, until)
    changes = [Change(t, t.base_sha) for t in targets
               if t.changed_files is not None and t.base_sha]  # fmt: skip
    cache.write_text(json.dumps([change_json(c) for c in changes], indent=1), encoding="utf-8")
    return changes


def run_eval(
    repo: str,
    state_root: Path,
    max_commits: int,
    mutants_per_commit: int,
    seed: int,
    time_cap_hours: float,
    run_timeout: float,
    test_timeout: int,
    workers: int,
    min_history_days: int,
) -> None:
    profile = PROFILES.get(repo)
    if profile is None:
        raise typer.BadParameter(f"no profile for {repo}; known: {', '.join(PROFILES)}")
    state = state_dir_for(state_root, repo)
    state.mkdir(parents=True, exist_ok=True)
    src, work = state / "src", state / "work"
    commits_path, mutants_path = state / "commits.jsonl", state / "mutants.jsonl"
    ensure_clone(profile, src)
    python = ensure_venv(profile, state / "venv")
    commits, mutant_records = read_jsonl(commits_path), read_jsonl(mutants_path)
    done_mutants = {m["key"] for m in mutant_records}
    cap = time_cap_hours * 3600
    with get_sessionmaker()() as session:
        repo_id = session.execute(
            text("SELECT id FROM repos WHERE name = :name"), {"name": repo}
        ).scalar_one_or_none()
        if repo_id is None:
            raise typer.BadParameter(f"unknown repo {repo!r}")
        changes = load_changes(state, repo, session, repo_id, min_history_days)
        typer.echo(f"{repo}: {len(changes)} merged PRs in the window", err=True)
        started = time.perf_counter()
        index = HistoryIndex(session, repo_id)
        typer.echo(f"history index: {index} in {time.perf_counter() - started:.0f}s", err=True)
        config = SelectorConfig()
        walk_sample(
            sample_order(changes, seed), commits, done_mutants, max_commits,
            lambda: spent_seconds(read_jsonl(commits_path), read_jsonl(mutants_path)) >= cap,
            lambda change, record: process_change(
                change, record, profile, src, work, python, session, index, config,
                mutants_per_commit, seed, run_timeout, test_timeout, workers, cap,
                commits_path, mutants_path, done_mutants,
            ),
        )  # fmt: skip


def walk_sample(
    ordered: Sequence[Change],
    records: Sequence[Mapping[str, Any]],
    done_mutants: set[str],
    limit: int,
    over_cap: Callable[[], bool],
    process: Callable[[Change, dict[str, Any] | None], int],
    adopt: Callable[[Mapping[str, Any]], None] | None = None,
) -> bool:
    """Walk ``ordered`` until ``limit`` changes with mutants, counting from zero along the
    order: a change done before (every mutant decided) counts where the walk meets it, so a
    resume continues the same sample. ``process`` decides a change (1 if it had mutants);
    ``adopt`` sees each recorded change with mutants that the walk counts. False if the time
    cap stopped it."""
    by_key = {str(c["key"]): c for c in records}
    count = 0
    for change in ordered:
        if count >= limit:
            return True
        record = by_key.get(change.key)
        if record is not None and not record.get("chosen"):
            continue  # sampled before: no mutants (or skipped)
        if over_cap():
            typer.echo("time cap reached", err=True)
            return False
        if record is not None and adopt is not None:
            adopt(record)
        if record is not None and all(f"{change.key}|{m['id']}" in done_mutants
                                      for m in record["chosen"]):  # fmt: skip
            count += 1
            continue
        count += process(change, dict(record) if record is not None else None)
    return True


def process_change(
    change: Change,
    record: dict[str, Any] | None,
    profile: Profile,
    src: Path,
    work: Path,
    python: Path,
    session: Any,
    index: HistoryIndex,
    config: SelectorConfig,
    mutants_per_commit: int,
    seed: int,
    run_timeout: float,
    test_timeout: int,
    workers: int,
    cap: float,
    commits_path: Path,
    mutants_path: Path,
    done_mutants: set[str],
) -> int:
    """Mutate one change and decide each mutant; 1 if it had mutants, else 0."""
    sha = change.target.commit_sha
    began = time.perf_counter()
    typer.echo(f"\n## {change.target.branch} {sha[:9]} ({change.target.run_at:%Y-%m-%d})", err=True)
    if not fetch(src, sha, change.target.branch) or not fetch(src, change.base, ""):
        append_jsonl(commits_path, {"key": change.key, "error": "commit unavailable",
                                    "chosen": [], "setup_s": 0})  # fmt: skip
        return 0
    git(src, "checkout", "-q", "-f", "--detach", sha)
    git(src, "clean", "-q", "-f", "-d", "-x")
    candidates, files = mutants_of(src, change, profile.source_roots)
    chosen = choose(candidates, mutants_per_commit, f"{seed}:{sha}")
    if not chosen:
        append_jsonl(commits_path, {"key": change.key, **change_json(change), "files": files,
                                    "candidates": 0, "chosen": [],
                                    "setup_s": round(time.perf_counter() - began, 1)})  # fmt: skip
        typer.echo(f"  no mutants ({files} source file(s) changed)", err=True)
        return 0
    install(profile, python, src)
    prepared = prepare(session, index, change, src, config)
    selection = prepared.selection
    setup = time.perf_counter() - began
    if record is None:
        record = {
            "key": change.key, **change_json(change), "files": files,
            "candidates": len(candidates), "chosen": [m.to_json() | {"id": m.id} for m in chosen],
            "mode": selection.mode.value, "reason": selection.reason,
            "selected_count": selection.selected_count, "total_known": selection.total_known,
            "selected_files": len(prepared.selected_files),
            "time_skipped_pct": prepared.time_skipped_pct, "setup_s": round(setup, 1),
        }  # fmt: skip
        append_jsonl(commits_path, record)
    typer.echo(f"  {len(candidates)} mutants on {files} file(s); {len(chosen)} chosen; "
               f"{selection.mode.value}: {selection.reason[:100]}", err=True)  # fmt: skip
    suite = Pytest(python, src, work, profile, run_timeout, test_timeout, workers)
    suite.selected_files = prepared.selected_files
    would_run = WouldRun(selection)
    full = selection.mode is Mode.FULL
    baseline: dict[str, bool] = {}
    for m in (Mutant.from_json(c) for c in record["chosen"]):
        key = f"{change.key}|{m.id}"
        if key in done_mutants:
            continue
        spent = spent_seconds(read_jsonl(commits_path), read_jsonl(mutants_path))
        if spent >= cap:
            break
        t0 = time.perf_counter()
        suite.use(m)
        try:
            decision = decide(suite, would_run, full, baseline)
        finally:
            suite.restore()
        causes = (
            {t: miss_cause(t, prepared.history, m.path, prepared.graph) for t in decision.killers}
            if decision.outcome == MISSED
            else {}
        )
        result = {
            "key": key, "change": change.key, "mutant": m.to_json(), "outcome": decision.outcome,
            "killers": decision.killers[:20], "selected_killers": decision.selected_killers[:20],
            "cause": mutant_cause(causes), "causes": {t: list(c) for t, c in causes.items()},
            "unconfirmed": decision.unconfirmed[:20], "not_rerun": decision.not_rerun[:20],
            "baseline_failed": decision.baseline_failed, "runs": decision.runs,
            "seconds": round(time.perf_counter() - t0, 1),
        }  # fmt: skip
        append_jsonl(mutants_path, result)
        done_mutants.add(key)
        typer.echo(f"  {decision.outcome:>10}  {m.describe()}  ({result['seconds']:.0f}s)",
                   err=True)  # fmt: skip
    return 1


# --- report -------------------------------------------------------------------------------


def report_lines(
    commits: Sequence[Mapping[str, Any]], mutants: Sequence[Mapping[str, Any]]
) -> list[str]:
    """The eval's summary, from its JSONL records."""
    sampled = [c for c in commits if "error" not in c and "skipped" not in c]
    used = [c for c in sampled if c.get("chosen")]
    outcomes = Counter(m["outcome"] for m in mutants)
    rec = recall_of(m["outcome"] for m in mutants)
    lines = [
        f"changes sampled: {len(sampled)} ({len(used)} with mutants; "
        f"{len(sampled) - len(used)} without a mutable changed line)",
        f"mutants: {len(mutants)} decided of {sum(len(c['chosen']) for c in used)} chosen "
        f"(from {sum(c.get('candidates', 0) for c in used)} "
        + ("candidates)" if any("type_rejected" in c for c in used) else "compiling candidates)"),
        "outcomes: " + ", ".join(f"{o} {outcomes[o]}" for o in OUTCOMES),
        f"recall: {rec}",
    ]
    modes = Counter(c.get("mode") for c in used)
    skipped = [c["time_skipped_pct"] for c in used if c.get("time_skipped_pct") is not None]
    if skipped:
        lines.append(
            f"selections: {modes.get('selective', 0)} selective, {modes.get('full', 0)} full; "
            f"estimated time skipped: median {statistics.median(skipped):.1f}%, "
            f"mean {statistics.fmean(skipped):.1f}%"
        )
    misses = [m for m in mutants if m["outcome"] == MISSED]
    if misses:
        lines.append("misses (cause: mutant -> killing tests):")
        for m in misses:
            mutant = Mutant.from_json(m["mutant"])
            lines.append(f"  {m['cause']}: {mutant.describe()} [{m['change']}]")
            for test, (cause, detail) in list(m["causes"].items())[:3]:
                lines.append(f"      {test}: {cause}: {detail}")
    by_op: dict[str, Counter[str]] = {}
    for m in mutants:
        by_op.setdefault(m["mutant"]["operator"], Counter())[m["outcome"]] += 1
    lines.append("by operator: " + "; ".join(
        f"{op} {sum(c.values())} ({c[CAUGHT]} caught, {c[MISSED]} missed, "
        f"{c[NOT_KILLED]} not killed)" for op, c in sorted(by_op.items())))  # fmt: skip
    baseline = sorted({t for m in mutants for t in m.get("baseline_failed", ())})
    shown = ", ".join(baseline[:5]) + (", ..." if len(baseline) > 5 else "")
    lines.append(f"baseline failures seen: {len(baseline)}" + (f" ({shown})" if baseline else ""))
    setups = [c["setup_s"] for c in used if "setup_s" in c]
    per_mutant = [m["seconds"] for m in mutants]
    runs: dict[str, list[float]] = {}
    for m in mutants:
        for r in m["runs"]:
            runs.setdefault(r["kind"], []).append(r["seconds"])
    total = spent_seconds(commits, mutants)
    if per_mutant:
        lines.append(
            f"time: {total / 3600:.2f} h in all; setup per change median "
            f"{statistics.median(setups):.0f}s; per mutant median "
            f"{statistics.median(per_mutant):.0f}s (max {max(per_mutant):.0f}s)"
        )
        lines.append("runs: " + "; ".join(
            f"{kind} {len(s)}x median {statistics.median(s):.0f}s"
            for kind, s in sorted(runs.items())))  # fmt: skip
    return lines


# --- sizing: one full-suite run -----------------------------------------------------------


def measure(
    repo: str,
    state_root: Path,
    commit: str | None,
    run_timeout: float,
    test_timeout: int,
    workers: int,
) -> dict[str, Any]:
    """One full-suite run with no mutant, as the eval runs a full part; saved to
    ``measure.jsonl`` in the repo's state dir and returned."""
    profile = PROFILES.get(repo)
    if profile is None:
        raise typer.BadParameter(f"no profile for {repo}; known: {', '.join(PROFILES)}")
    state = state_dir_for(state_root, repo)
    state.mkdir(parents=True, exist_ok=True)
    src = state / "src"
    ensure_clone(profile, src)
    python = ensure_venv(profile, state / "venv")
    if commit is None:
        with get_sessionmaker()() as session:
            repo_id = session.execute(
                text("SELECT id FROM repos WHERE name = :name"), {"name": repo}
            ).scalar_one()
            end = history_end(session, repo_id)
        if end is None:
            raise typer.BadParameter(f"no history for {repo}")
        git(src, "fetch", "-q", "origin")
        commit = git(src, "rev-list", "-1", f"--before={end.isoformat()}",
                     "origin/HEAD").stdout.strip()  # fmt: skip
    git(src, "checkout", "-q", "-f", "--detach", commit)
    git(src, "clean", "-q", "-f", "-d", "-x")
    began = time.perf_counter()
    install(profile, python, src)
    install_s = time.perf_counter() - began
    suite = Pytest(python, src, state / "work", profile, run_timeout, test_timeout, workers)
    error = None
    try:
        run = suite.run(FULL)
    except RuntimeError as exc:  # a shard with no report: reported, not raised
        run, error = RunResult({}), str(exc)[:2000]
    statuses = Counter(s.value for s in run.statuses.values())
    failed_files = Counter(
        pytest_file(t, suite.paths.get(t)) or t.split("::", 1)[0] for t in run.failed
    )
    result: dict[str, Any] = {
        "repo": repo, "commit": git(src, "rev-parse", "HEAD").stdout.strip(),
        "platform": sys.platform, "workers": workers, "test_timeout": test_timeout,
        "install_s": round(install_s, 1), "seconds": round(run.seconds, 1),
        "timed_out": run.timed_out, "error": error, "results": len(run.statuses),
        "statuses": dict(statuses), "failed": sorted(run.failed),
        "failed_by_file": dict(failed_files.most_common()),
        "shards": [vars(s) for s in suite.last_shards],
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }  # fmt: skip
    append_jsonl(state / "measure.jsonl", result)
    return result


def measure_lines(m: Mapping[str, Any]) -> list[str]:
    shards = [ShardRun(**s) for s in m["shards"]]
    lines = [
        f"{m['repo']} at {m['commit'][:9]} on {m['platform']}: full suite in {m['seconds']:.0f}s "
        f"wall ({len(shards)} shards; install {m['install_s']:.0f}s)",
        f"results: {m['results']} "
        + ", ".join(f"{k} {v}" for k, v in sorted(m["statuses"].items())),
        f"baseline failures: {len(m['failed'])} in {len(m['failed_by_file'])} file(s)",
    ]  # fmt: skip
    lines += [f"  {n:>4}  {f}" for f, n in m["failed_by_file"].items()]
    for label, bad in (("hung (pytest-timeout)", [s for s in shards if s.hung]),
                       ("crashed", [s for s in shards if s.crashed]),
                       ("no report", [s for s in shards if not s.report])):  # fmt: skip
        lines.append(f"shards {label}: " + (", ".join(str(s.shard) for s in bad) or "none"))
    lines += [f"  shard {s.shard:>2}: exit {s.exit_code}, {s.summary or '(no summary)'}"
              for s in shards]  # fmt: skip
    if m["timed_out"]:
        lines.append("the run timed out (run timeout, or a shard ended by pytest-timeout)")
    if m["error"]:
        lines.append(f"error: {m['error'][:400]}")
    return lines


app = typer.Typer(add_completion=False)


@app.command()
def main(
    repo: Annotated[str, typer.Option(help="Repository, e.g. RDFLib/rdflib (has a profile).")],
    max_commits: Annotated[int, typer.Option(min=1, help="Changes with mutants to run.")] = 30,
    mutants_per_commit: Annotated[int, typer.Option(min=1)] = 3,
    seed: Annotated[int, typer.Option(help="Seeds the change sample and mutant choice.")] = 74,
    time_cap_hours: Annotated[float, typer.Option(help="Stop starting work after this.")] = 8.0,
    run_timeout: Annotated[float, typer.Option(help="Seconds per pytest run.")] = 1800,
    test_timeout: Annotated[int, typer.Option(help="Seconds per test (pytest-timeout).")] = 120,
    workers: Annotated[
        int, typer.Option(min=1, help="pytest processes per suite run (shards).")
    ] = max(1, os.cpu_count() or 1),
    min_history_days: Annotated[
        int, typer.Option(help="Sample only changes this long after the history starts.")
    ] = 14,
    state_dir: Annotated[Path, typer.Option(help="Clone, venv and results per repo.")] = Path(
        ".eval-cache/mutation"
    ),
    report: Annotated[bool, typer.Option("--report", help="Summarize the results so far.")] = False,
    measure_full: Annotated[
        bool,
        typer.Option(
            "--measure-full",
            help="Time one full-suite run, no mutant, at the last default-branch commit "
            "of the history (or --commit), and stop. Sizes the eval.",
        ),
    ] = False,
    commit: Annotated[str | None, typer.Option(help="--measure-full: this commit.")] = None,
) -> None:
    state_dir = state_dir.resolve()  # pytest runs in the clone: every path must be absolute
    state = state_dir_for(state_dir, repo)
    from scripts.eval import mutation_jest  # imports this module: loaded here, not at the top

    jest_profile = mutation_jest.JEST_PROFILES.get(repo)
    if jest_profile is not None:
        if measure_full:
            raise typer.BadParameter("--measure-full is for pytest repos")
        if not report:
            mutation_jest.run_eval(jest_profile, state_dir, max_commits, mutants_per_commit,
                                   seed, time_cap_hours, run_timeout)  # fmt: skip
        commits = read_jsonl(state / "commits.jsonl")
        for line in mutation_jest.report(commits, read_jsonl(state / "mutants.jsonl")):
            typer.echo(line)
        return
    if measure_full:
        for line in measure_lines(measure(repo, state_dir, commit, run_timeout, test_timeout,
                                          workers)):  # fmt: skip
            typer.echo(line)
        return
    if not report:
        run_eval(repo, state_dir, max_commits, mutants_per_commit, seed, time_cap_hours,
                 run_timeout, test_timeout, workers, min_history_days)  # fmt: skip
    for line in report_lines(read_jsonl(state / "commits.jsonl"),
                             read_jsonl(state / "mutants.jsonl")):  # fmt: skip
        typer.echo(line)


if __name__ == "__main__":
    app()
