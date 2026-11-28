"""JVM repos (Gradle, Kotlin and Java) for the mutation eval (``scripts/eval/mutation.py``): the
same kill rule, recall and Wilson interval, with a Gradle suite, the Java/Kotlin graph and
Kotlin mutants (#102).

What differs from the pytest repos:

* **History: one run, seeded.** As for mobx (``mutation_jest.py``): the repo records no JUnit
  in CI, so each change's history is ONE full-suite run at the PR's base commit, recorded
  through siftwise's ingest in a database of its own (``<repo>@seed-<base>``) and read back
  with ``load_history``. Selection then rests on the graph and name mapping only: no
  co-change, no flakiness, nothing recently failed.
* **Selection as ``siftwise select`` makes it:** ``javagraph`` at the head (the build model,
  test classes from the sources, the graph for the changed ``.java``/``.kt`` files), the PR's
  diff, ``select_tests`` with ``java``. The selection's own Gradle commands run (with
  ``./gradlew``), so the commands are tested too.
* **Mutants** (``mutators.kotlin_mutants``) on the changed lines of main Kotlin sources, each
  compiled (``:<module>:classes``) before use: one that doesn't compile is dropped and the
  next in ``choose``'s order is tried.
* **Selected = its class runs.** Gradle runs whole test classes (``--tests <class>``): a test
  counts as selected when its outer class is in the selection's commands.
* **Every Gradle test task reruns:** an init script marks them never up to date and never
  cached, so a re-run really runs (a cached failure would confirm nothing).
* **Direction:** each mutant records which test files the graph links to its file: Kotlin
  tests, Java tests, or both (the report counts each).

Runs in ``scripts/eval/mutation-linux.Dockerfile`` (JDKs 17, 21 and 26; ``java_home`` picks
one by the commit's Gradle version): see docs/eval-results.md.

    python -m scripts.eval.mutation_jvm --repo square/moshi --state-dir /state/mutation
    python -m scripts.eval.mutation_jvm --repo square/moshi --state-dir /state/mutation --report
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from sqlalchemy import text

if not __package__:  # run as a file path: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.eval.mutation import (
    CAUGHT,
    FULL,
    MISSED,
    NOT_KILLED,
    OUTCOMES,
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
    recall_of,
    sample_order,
    spent_seconds,
    state_dir_for,
    walk_sample,
)
from scripts.eval.mutators import Mutant, changed_lines, choose, kotlin_mutants
from scripts.eval.run_eval import GitHubCompare, _github_token, merged_pr_targets
from siftwise.cli.javagraph import (
    JavaGraph,
    build_java_graph,
    change_from,
    detect_build,
    kotlin_files,
    test_classes_of,
)
from siftwise.core.junit import JUnitParseError, ParsedTestResult, Status, parse_junit
from siftwise.core.selector import (
    JavaBuildInfo,
    Mode,
    RepoHistory,
    Selection,
    SelectorConfig,
    load_history,
    select_tests,
)
from siftwise.core.shadow import FLAKY_HISTORY, LINKED_NOT_SELECTED, NEW_TEST, NO_LINK
from siftwise.core.shadow import UNKNOWN_CAUSE as UNKNOWN
from siftwise.db import get_sessionmaker

SEED_LABEL = "one-run history (as for a newly installed siftwise): graph and name mapping only"
RANDOM, TARGETED = "random", "targeted"
TARGETED_MAX = 10
# Never up to date, never from the build cache: a re-run runs the tests again.
INIT_SCRIPT = """allprojects {
    tasks.withType(Test).configureEach {
        outputs.upToDateWhen { false }
        outputs.cacheIf { false }
        ignoreFailures = false
        maxHeapSize = "768m"
        maxParallelForks = 1
    }
}
"""
# Bounded memory: one build JVM per run (no daemons left behind), the Kotlin compiler in it,
# two workers. Unbounded, two Gradle daemons, the Kotlin daemon and test JVMs ran Docker
# Desktop's 7.75 GB VM out of memory (OOM kills, the engine answering 500).
GRADLE_FLAGS = (
    "--no-daemon",
    "--max-workers=2",
    "-Dorg.gradle.jvmargs=-Xmx1536m -XX:MaxMetaspaceSize=512m",
    "-Pkotlin.compiler.execution.strategy=in-process",
)
KOTLIN, JAVA = "kotlin", "java"


@dataclass(frozen=True)
class JvmProfile:
    repo: str
    clone_url: str
    branch: str
    window_days: int
    core: str  # the targeted sample's changes have no changed source under this
    gradle_args: tuple[str, ...] = ()
    note: str = ""


JVM_PROFILES = {
    # .github/workflows/build.yml: `./gradlew build check -PkotlinTestMode=REFLECT|KSP` on the
    # JDK in .java-version (26; its toolchain is 21). Here: `./gradlew test`, the default mode.
    # moshi/ (core) adds src/main/java16 through a second Kotlin compilation: siftwise doesn't
    # analyse that module (custom source sets), so a core change runs the full suite.
    "square/moshi": JvmProfile(
        repo="square/moshi",
        clone_url="https://github.com/square/moshi.git",
        branch="master",
        window_days=1080,
        core="moshi/src/",
        note="./gradlew test (CI: build check, two kotlinTestMode legs)",
    ),
    # JVM-only, 21 Gradle projects, convention plugins in a plugin-only included build
    # (build-logic). Every rule of ktlint-ruleset-standard has its own test class.
    "ktlint/ktlint": JvmProfile(
        repo="ktlint/ktlint",  # was pinterest/ktlint (the API answers 301)
        clone_url="https://github.com/ktlint/ktlint.git",
        branch="master",
        window_days=365,
        core="",  # no targeted sample: the random one has narrow selections
        note="./gradlew test",
    ),
}


def seed_repo(repo: str, base: str) -> str:
    return f"{repo}@seed-{base[:12]}"


def is_kotlin_source(path: str) -> bool:
    return path.endswith(".kt") and "/src/main/" in f"/{path}"


def selected_classes(commands: Sequence[str]) -> set[str]:
    """The test classes a selection's Gradle commands run (``--tests <class>``)."""
    found: set[str] = set()
    for command in commands:
        argv = shlex.split(command)
        found.update(argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--tests")
    return found


def outer(test_id: str) -> str:
    return test_id.split("::", 1)[0].split("$", 1)[0]


def gradle_version(src: Path) -> tuple[int, int] | None:
    props = src / "gradle" / "wrapper" / "gradle-wrapper.properties"
    found = re.search(r"gradle-(\d+)\.(\d+)", props.read_text(encoding="utf-8")) if (
        props.is_file()) else None  # fmt: skip
    return (int(found[1]), int(found[2])) if found else None


def java_home(src: Path) -> str:
    """The JDK Gradle runs on, by the commit's Gradle version: 17 before 8.5 (it can't run on
    21), 21 before 9.0, else 26 (ktlint's build logic needs a 26 runtime), when installed."""
    version = gradle_version(src)
    jdk = "/usr/lib/jvm/jdk-{}"
    if version is not None and version < (8, 5) and os.path.isdir(jdk.format(17)):
        return jdk.format(17)
    if (version is None or version >= (9, 0)) and os.path.isdir(jdk.format(26)):
        return jdk.format(26)
    return os.environ.get("JAVA_HOME", jdk.format(21))


# --- running Gradle -------------------------------------------------------------------------


class Gradle:
    """The repo's Gradle test tasks in its checkout, with a mutant applied or not
    (``mutation.Suite``)."""

    def __init__(self, src: Path, work: Path, profile: JvmProfile, run_timeout: float) -> None:
        self.src, self.work, self.profile, self.run_timeout = src, work, profile, run_timeout
        self.commands: tuple[str, ...] = ()  # the selection's
        self.full = False
        self.modules: dict[str, str] = {}  # test class -> Gradle project path (":a")
        self.last_results: list[ParsedTestResult] = []
        self.mutant: Mutant | None = None
        self._original: bytes | None = None
        self._applied = False
        work.mkdir(parents=True, exist_ok=True)
        self.init_script = work / "siftwise-rerun.gradle"
        self.init_script.write_text(INIT_SCRIPT, encoding="utf-8")

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
        self._applied = applied

    def restore(self) -> None:
        self._set(False)
        self.mutant, self._original = None, None

    def compiles(self, mutant: Mutant, project: str) -> bool:
        """Whether the mutant's project compiles with it (``<project>:classes``)."""
        self.use(mutant)
        try:
            self._set(True)
            proc = self._gradle([f"{project}:classes" if project else "classes"],
                                reports=False)  # fmt: skip
            return proc == 0
        finally:
            self.restore()

    # Suite

    def run(self, part: str) -> RunResult:
        self._set(True)
        if part == SELECTED and not self.full:
            statuses: dict[str, Status] = {}
            seconds = 0.0
            for command in self.commands:
                argv = shlex.split(command)[1:]
                result = self._run(argv)
                if result.timed_out:
                    return result
                statuses.update(result.statuses)
                seconds += result.seconds
            return RunResult(statuses, seconds)
        return self._run(["test"])  # FULL, or REST: the whole suite (REST subtracts)

    def tests(self, test_ids: Sequence[str], mutant: bool) -> RunResult:
        """By class and method; JUnit 4 parameterized names (``m[0]``) can't be filtered:
        those run by class (``files``)."""
        self._set(mutant)
        filters: dict[str, list[str]] = {}
        for t in test_ids:
            classname, _, name = t.partition("::")
            method = name.removesuffix("()")
            if not re.fullmatch(r"[\w$ '-]+", method):
                continue
            filters.setdefault(self._project(classname), []).append(f"{classname}.{method}")
        return self._run(self._tasks(filters)) if filters else RunResult({})

    def files(self, test_ids: Sequence[str], mutant: bool) -> RunResult:
        """These tests' classes, whole."""
        self._set(mutant)
        filters: dict[str, list[str]] = {}
        for t in test_ids:
            cls = outer(t)
            filters.setdefault(self._project(cls), [])
            if cls not in filters[self._project(cls)]:
                filters[self._project(cls)].append(cls)
        return self._run(self._tasks(filters)) if filters else RunResult({})

    def _project(self, classname: str) -> str:
        return self.modules.get(classname.split("$", 1)[0], "")

    @staticmethod
    def _tasks(filters: Mapping[str, Sequence[str]]) -> list[str]:
        argv: list[str] = []
        for project, patterns in sorted(filters.items()):
            argv.append(f"{project}:test" if project else ":test")
            argv += [a for p in patterns for a in ("--tests", p)]
        return argv

    def _run(self, args: Sequence[str]) -> RunResult:
        started = time.perf_counter()
        code = self._gradle(args)
        seconds = time.perf_counter() - started
        if code is None:
            return RunResult({}, seconds, timed_out=True)
        results = self._reports()
        if not results and code != 0:
            tail = (self.work / "gradle.log").read_text(encoding="utf-8", errors="replace")
            raise RuntimeError(f"gradle {' '.join(args[:4])} wrote no report (exit {code}): "
                               f"{tail[-1500:]}")  # fmt: skip
        self.last_results = results
        statuses: dict[str, Status] = {}
        for r in results:  # a test ID in two projects: a failure in either counts
            if statuses.get(r.test_id) not in (Status.FAILED, Status.ERROR):
                statuses[r.test_id] = r.status
        return RunResult(statuses, seconds)

    def _gradle(self, args: Sequence[str], reports: bool = True) -> int | None:
        """Run ``./gradlew``; the exit code, None on a timeout. Earlier reports are removed
        first, so only this run's are read."""
        if reports:
            for directory in self.src.glob("**/build/test-results"):
                shutil.rmtree(directory, ignore_errors=True)
        argv = ["./gradlew", "--continue", "-q", "--console=plain", *GRADLE_FLAGS,
                "--init-script", str(self.init_script), *self.profile.gradle_args,
                *args]  # fmt: skip
        env = {**os.environ, "JAVA_HOME": java_home(self.src)}
        with (self.work / "gradle.log").open("wb") as log:
            proc = subprocess.Popen(argv, cwd=self.src, stdout=log, stderr=subprocess.STDOUT,
                                    env=env, start_new_session=True)  # fmt: skip
            try:
                return proc.wait(timeout=self.run_timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                subprocess.run(["./gradlew", "--stop"], cwd=self.src, env=env,
                               capture_output=True, check=False, timeout=120)  # fmt: skip
                return None

    def _reports(self) -> list[ParsedTestResult]:
        results: list[ParsedTestResult] = []
        for report in sorted(self.src.glob("**/build/test-results/**/TEST-*.xml")):
            try:
                results += parse_junit(report.read_bytes())
            except JUnitParseError as exc:
                raise RuntimeError(f"unreadable report {report}: {exc}") from exc
        return results


# --- one change ---------------------------------------------------------------------------


def checkout(src: Path, sha: str) -> None:
    git(src, "checkout", "-q", "-f", "--detach", sha)
    git(src, "clean", "-q", "-f", "-d", "-x")


def mutants_of(src: Path, change: Change) -> tuple[list[Mutant], int]:
    """Every mutant on the change's changed lines of main Kotlin sources, at its head."""
    diff = git(src, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames",
               f"{change.base}...{change.target.commit_sha}", "--", "*.kt").stdout  # fmt: skip
    found: list[Mutant] = []
    files = 0
    for path, lines in sorted(changed_lines(diff).items()):
        if not is_kotlin_source(path) or not (src / path).is_file():
            continue
        try:
            source = (src / path).read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        files += 1
        found += kotlin_mutants(path, source, lines)
    return found, files


def project_of(path: str, info: JavaBuildInfo) -> str:
    module = info.module_of(path)
    return module.name if module is not None else ""


def compiled(suite: Gradle, ordered: Sequence[Mutant], k: int, info: JavaBuildInfo
             ) -> tuple[list[Mutant], int]:  # fmt: skip
    """The first ``k`` of ``ordered`` whose project compiles with them; how many didn't."""
    chosen: list[Mutant] = []
    rejected = 0
    for m in ordered:
        if len(chosen) >= k:
            break
        if suite.compiles(m, project_of(m.path, info)):
            chosen.append(m)
        else:
            rejected += 1
    return chosen, rejected


@dataclass
class JvmPrepared:
    selection: Selection
    history: RepoHistory
    graph: JavaGraph
    info: JavaBuildInfo
    classes: set[str]  # the test classes the selection runs (all of them: full)


def seed(session: Any, profile: JvmProfile, src: Path, suite: Gradle, change: Change
         ) -> dict[str, Any]:  # fmt: skip
    """One full run at the PR's base commit, recorded as the seed repo's only (main) run."""
    from siftwise.core.ingest import create_run
    from siftwise.core.schemas import RunMetadata

    name = seed_repo(profile.repo, change.base)
    runs = session.execute(
        text("SELECT count(*) FROM runs r JOIN repos p ON p.id = r.repo_id WHERE p.name = :n"),
        {"n": name},
    ).scalar_one()
    if runs:
        return {"seed_repo": name, "seeded_before": True}
    began = time.perf_counter()
    checkout(src, change.base)
    result = suite.run(FULL)
    if result.timed_out:
        raise RuntimeError(f"the seed run at {change.base[:9]} timed out")
    when = datetime.fromisoformat(
        git(src, "show", "-s", "--format=%cI", change.base).stdout.strip()
    )
    meta = RunMetadata(repo=name, commit_sha=change.base, branch=profile.branch, is_main=True,
                       ci_run_id="seed", started_at=when, changed_files_known=False)  # fmt: skip
    create_run(session, meta, suite.last_results)
    return {"seed_repo": name, "seed_run_s": round(result.seconds, 1),
            "seed_setup_s": round(time.perf_counter() - began - result.seconds, 1),
            "seed_results": len(result.statuses), "seed_failed": sorted(result.failed)}  # fmt: skip


def prepare(session: Any, profile: JvmProfile, change: Change, src: Path,
            config: SelectorConfig) -> JvmPrepared:  # fmt: skip
    """The selection ``siftwise select`` would make at the head, from the seeded history."""
    history = load_history(session, seed_repo(profile.repo, change.base), config)
    if history is None:
        raise RuntimeError(f"no seeded history for {change.key}")
    changed = list(change.target.changed_files or ())
    deleted = sorted(p for p in changed if not (src / p).exists())
    build = detect_build(src)
    if build is None:
        raise RuntimeError("no Gradle build at the head")
    kotlin = kotlin_files(src, build, changed)
    classes = test_classes_of(src, build, kotlin)
    graph = build_java_graph(src, build, [p for p in deleted if p.endswith((".java", ".kt"))])
    java_change = change_from(build, classes, graph, changed, kotlin)
    info = JavaBuildInfo.from_json(java_change.payload())
    depths = {t: n.depth for t, n in java_change.nearest.items()}
    selection = select_tests(history, changed, True, config, None, java_change.files, deleted,
                             java=info, import_depths=depths or None)  # fmt: skip
    full = selection.mode is Mode.FULL
    run = set(info.test_classes) if full else selected_classes(selection.commands)
    return JvmPrepared(selection, history, graph, info, run)


def reached_by(graph: JavaGraph, path: str) -> list[str]:
    """Which languages' test files the graph links to ``path``: kotlin, java, both or none."""
    files, _, _ = graph.affected([path])
    return sorted({KOTLIN if f.endswith(".kt") else JAVA for f in files})


def jvm_miss_cause(test_id: str, history: RepoHistory, mutated: str, graph: JavaGraph
                   ) -> tuple[str, str]:  # fmt: skip
    known = history.tests.get(test_id)
    if known is None:
        return NEW_TEST, "no result for this test in the seeded history"
    if known.flaky_score:
        return FLAKY_HISTORY, f"flaky before (flaky score {known.flaky_score:.2f})"
    files, _, _ = graph.affected([mutated])
    cls = outer(test_id)
    linked = [f for f in files if f.endswith(tuple(f"/{cls.replace('.', '/')}{e}"
                                                   for e in (".java", ".kt")))]  # fmt: skip
    if linked:
        return UNKNOWN, f"{LINKED_NOT_SELECTED}: {linked[0]}"
    return NO_LINK, f"{cls} doesn't reach {mutated} in the Java/Kotlin graph"


def share(prepared: JvmPrepared) -> tuple[float, float | None]:
    """(share of the seeded run's tests the selection runs, share of their time)."""
    tests = prepared.history.tests.values()
    if prepared.selection.mode is Mode.FULL or not tests:
        return 1.0, 1.0
    run = [t for t in tests if outer(t.test_id) in prepared.classes]
    total_ms = sum(t.avg_duration_ms or 0 for t in tests)
    run_ms = sum(t.avg_duration_ms or 0 for t in run)
    return len(run) / len(tests), (run_ms / total_ms if total_ms else None)


def process_change(
    change: Change,
    record: dict[str, Any] | None,
    profile: JvmProfile,
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
    """Seed, select, mutate and decide one change; 1 if it had mutants, else 0."""
    src, work = state / "src", state / "work"
    commits_path, mutants_path = state / "commits.jsonl", state / "mutants.jsonl"
    sha = change.target.commit_sha
    began = time.perf_counter()
    typer.echo(f"\n## {change.target.branch} {sha[:9]} ({change.target.run_at:%Y-%m-%d})", err=True)
    if not fetch(src, sha, change.target.branch) or not fetch(src, change.base, ""):
        append_jsonl(commits_path, {"key": change.key, "error": "commit unavailable",
                                    "chosen": [], "setup_s": 0})  # fmt: skip
        return 0
    checkout(src, sha)
    candidates, files = mutants_of(src, change)
    if not candidates:
        append_jsonl(commits_path, {"key": change.key, **change_json(change), "files": files,
                                    "candidates": 0, "chosen": [], "sample": sample,
                                    "setup_s": round(time.perf_counter() - began, 1)})  # fmt: skip
        typer.echo(f"  no mutants ({files} Kotlin source file(s) changed)", err=True)
        return 0
    suite = Gradle(src, work, profile, run_timeout)
    try:
        seeded = seed(session, profile, src, suite, change)
        session.commit()
    except RuntimeError as exc:
        append_jsonl(commits_path, {"key": change.key, **change_json(change), "files": files,
                                    "candidates": len(candidates), "chosen": [],
                                    "skipped": f"the base doesn't build or test here: {exc}"[:600],
                                    "sample": sample,
                                    "setup_s": round(time.perf_counter() - began, 1)})  # fmt: skip
        typer.echo(f"  skipped: {str(exc)[:300]}", err=True)
        return 0
    checkout(src, sha)
    prepared = prepare(session, profile, change, src, config)
    selection = prepared.selection
    suite.commands = selection.commands
    suite.full = selection.mode is Mode.FULL
    suite.modules = {
        cls: info.name if (info := prepared.info.module(module)) is not None else ""
        for cls, module in prepared.info.test_classes.items()
    }
    rejected = 0
    if record is None:
        ordered = choose(candidates, len(candidates), f"{seed_value}:{sha}")
        chosen, rejected = compiled(suite, ordered, mutants_per_commit, prepared.info)
    else:
        chosen = [Mutant.from_json(c) for c in record["chosen"]]
    tests_share, time_share = share(prepared)
    setup = time.perf_counter() - began
    if record is None:
        record = {
            "key": change.key, **change_json(change), "files": files,
            "candidates": len(candidates), "compile_rejected": rejected,
            "chosen": [m.to_json() | {"id": m.id, "reached_by": reached_by(prepared.graph, m.path)}
                       for m in chosen],
            "mode": selection.mode.value, "reason": selection.reason,
            "reason_code": selection.reason_code.value if selection.reason_code else None,
            "selected_count": selection.selected_count, "total_known": selection.total_known,
            "selected_classes": len(prepared.classes),
            "test_classes": len(prepared.info.test_classes),
            "tests_share": round(tests_share, 4),
            "time_share": None if time_share is None else round(time_share, 4),
            "time_skipped_pct": None if time_share is None else round(100 * (1 - time_share), 1),
            "commands": list(selection.commands)[:3], "setup_s": round(setup, 1),
            "history": SEED_LABEL, "sample": sample, **seeded,
        }  # fmt: skip
        append_jsonl(commits_path, record)
    if not chosen:
        typer.echo(f"  no mutant compiles ({len(candidates)} candidates)", err=True)
        return 0
    typer.echo(f"  {len(candidates)} mutants on {files} file(s); {len(chosen)} chosen "
               f"({rejected} didn't compile); {selection.mode.value}: {selection.reason[:100]}; "
               f"{len(prepared.classes)} of {len(prepared.info.test_classes)} test classes",
               err=True)  # fmt: skip
    full = selection.mode is Mode.FULL

    def selected(test_id: str) -> bool:
        return full or outer(test_id) in prepared.classes

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
            {t: jvm_miss_cause(t, prepared.history, m.path, prepared.graph)
             for t in decision.killers}
            if decision.outcome == MISSED
            else {}
        )  # fmt: skip
        result = {
            "key": key, "change": change.key, "mutant": m.to_json(), "outcome": decision.outcome,
            "reached_by": reached_by(prepared.graph, m.path),
            "killers": decision.killers[:20], "selected_killers": decision.selected_killers[:20],
            "killer_languages": killer_languages(src, decision.killers),
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


def killer_languages(src: Path, killers: Sequence[str]) -> list[str]:
    """The languages of the killing tests' source files (by class name: ``a/b/C.kt``)."""
    found = set()
    for t in killers:
        rel = outer(t).replace(".", "/")
        if any(src.glob(f"**/src/test/**/{rel}.kt")):
            found.add(KOTLIN)
        elif any(src.glob(f"**/src/test/**/{rel}.java")):
            found.add(JAVA)
    return sorted(found)


# --- the eval -----------------------------------------------------------------------------


def load_changes(state: Path, profile: JvmProfile) -> list[Change]:
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


def outside_core(change: Change, profile: JvmProfile) -> bool:
    """A change whose changed Kotlin main source is all outside the profile's core module."""
    sources = [p for p in change.target.changed_files or () if is_kotlin_source(p)]
    return bool(sources) and not any(p.startswith(profile.core) for p in sources)


def run_eval(profile: JvmProfile, state_root: Path, max_commits: int, mutants_per_commit: int,
             seed_value: int, time_cap_hours: float, run_timeout: float,
             commits: Sequence[str] = ()) -> None:  # fmt: skip
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
    if commits:  # head commits, or PRs ("#2056")
        changes = [c for c in changes if c.target.branch in commits
                   or c.target.commit_sha.startswith(tuple(commits))]  # fmt: skip
    typer.echo(f"{profile.repo}: {len(changes)} merged PRs; {SEED_LABEL}", err=True)
    config = SelectorConfig()
    ordered = sample_order(changes, seed_value)

    with get_sessionmaker()() as session:

        def run_sample(label: str, candidates: Sequence[Change], limit: int) -> bool:
            return walk_sample(
                candidates, read_jsonl(commits_path), done_mutants, limit,
                lambda: spent_seconds(read_jsonl(commits_path), read_jsonl(mutants_path)) >= cap,
                lambda change, record: process_change(
                    change, record, profile, state, session, config, mutants_per_commit,
                    seed_value, run_timeout, cap, done_mutants, label,
                ),
            )  # fmt: skip

        if not run_sample(RANDOM, ordered, max_commits) or commits:
            return
        taken = {c["key"] for c in read_jsonl(commits_path)}
        if not profile.core:
            return
        targeted = [c for c in ordered if c.key not in taken and outside_core(c, profile)]
        typer.echo(f"targeted sample (Kotlin source outside {profile.core}): {len(targeted)} "
                   "candidate change(s)", err=True)  # fmt: skip
        run_sample(TARGETED, targeted, TARGETED_MAX)


# --- the report ---------------------------------------------------------------------------


def fallback_reason(record: Mapping[str, Any]) -> str:
    """A full selection's reason, grouped: build files, an unanalysed module, unmapped..."""
    reason = str(record.get("reason", ""))
    if "isn't analysed" in reason:
        why = re.search(r"isn't analysed \((.*?)\):", reason)
        module = reason.split(" isn't analysed", 1)[0]
        return f"{module} isn't analysed ({why[1] if why else '?'})"
    code = record.get("reason_code") or "other"
    return str(code)


def recall_line(label: str, mutants: Sequence[Mapping[str, Any]]) -> str:
    rec = recall_of(m["outcome"] for m in mutants)
    return f"{label}: {rec}"


def report(commits: Sequence[Mapping[str, Any]], mutants: Sequence[Mapping[str, Any]]
           ) -> list[str]:  # fmt: skip
    """Recall over all mutants and over narrow selections only, the fallbacks by reason, and
    the link directions."""
    by_key = {c["key"]: c for c in commits}
    used = [c for c in commits if c.get("chosen")]
    outcomes = Counter(m["outcome"] for m in mutants)
    lines = [
        f"history: {SEED_LABEL}: one full run at each PR's base commit",
        f"changes: {len(commits)} sampled ({sum(1 for c in commits if c.get('sample') == TARGETED)}"
        f" targeted), {len(used)} with mutants; skipped "
        f"{sum(1 for c in commits if c.get('skipped'))}",
        f"mutants: {len(mutants)} decided of {sum(len(c['chosen']) for c in used)} chosen "
        f"(from {sum(c.get('candidates', 0) for c in used)} candidates; "
        f"{sum(c.get('compile_rejected', 0) for c in used)} didn't compile)",
        "outcomes: " + ", ".join(f"{o} {outcomes[o]}" for o in OUTCOMES),
        recall_line("all mutants (killed and selected / killed)", mutants),
    ]
    narrow = [c for c in used if c.get("mode") == "selective" and c.get("tests_share", 1) < 1]
    narrow_keys = {c["key"] for c in narrow}
    narrow_mutants = [m for m in mutants if m["change"] in narrow_keys]
    lines.append(recall_line(f"narrow selections ({len(narrow)} changes)", narrow_mutants))
    if narrow:
        shares = [100 * c["tests_share"] for c in narrow]
        lines.append(f"  share of tests selected: median {statistics.median(shares):.1f}% "
                     f"({', '.join(f'{s:.1f}%' for s in sorted(shares))})")  # fmt: skip
        for c in narrow:
            lines.append(f"  {c.get('pr', c['key'])}: {c['selected_classes']} of "
                         f"{c['test_classes']} test classes, {100 * c['tests_share']:.1f}% of "
                         f"tests")  # fmt: skip
    full = [c for c in used if c.get("mode") == "full"]
    reasons = Counter(fallback_reason(c) for c in full)
    lines.append(f"full-suite fallbacks: {len(full)} of {len(used)} changes with mutants")
    lines += [f"  {n} x {r}" for r, n in reasons.most_common()]
    wide = [c for c in used if c.get("mode") == "selective" and c.get("tests_share", 1) >= 1]
    if wide:
        lines.append(f"selective but every test: {len(wide)}")
    direction: dict[str, Counter[str]] = {}
    for m in mutants:
        reached = m.get("reached_by") or []
        label = "+".join(reached) if reached else "no test file"
        direction.setdefault(label, Counter())[m["outcome"]] += 1
    lines.append("by the tests the graph links to the mutated file: " + "; ".join(
        f"{label} {sum(c.values())} ({c[CAUGHT]} caught, {c[MISSED]} missed, "
        f"{c[NOT_KILLED]} not killed)" for label, c in sorted(direction.items())))  # fmt: skip
    for scope, part in (("narrow", narrow_mutants),):
        counts: dict[str, Counter[str]] = {}
        for m in part:
            label = "+".join(m.get("reached_by") or []) or "no test file"
            counts.setdefault(label, Counter())[m["outcome"]] += 1
        if counts:
            lines.append(f"  in {scope} selections: " + "; ".join(
                f"{label} {sum(c.values())} ({c[CAUGHT]} caught)"
                for label, c in sorted(counts.items())))  # fmt: skip
    killers = Counter("+".join(m.get("killer_languages") or []) or "none" for m in mutants
                      if m["outcome"] in (CAUGHT, MISSED))  # fmt: skip
    lines.append("killing tests' language: " + ", ".join(f"{k} {n}" for k, n in
                                                         killers.most_common()))  # fmt: skip
    misses = [m for m in mutants if m["outcome"] == MISSED]
    for m in misses:
        lines.append(f"  missed: {Mutant.from_json(m['mutant']).describe()} ({m['cause']}) "
                     f"[{by_key.get(m['change'], {}).get('pr', m['change'])}]")  # fmt: skip
    for m in mutants:
        if m["outcome"] == NOT_KILLED:
            lines.append(f"  not killed: {Mutant.from_json(m['mutant']).describe()}")
    baseline = sorted({t for m in mutants for t in m.get("baseline_failed", ())})
    lines.append(f"baseline failures seen: {len(baseline)} {baseline[:5]}")
    skipped = [c for c in commits if c.get("skipped")]
    lines += [f"  skipped {c.get('pr', c['key'])}: {str(c['skipped'])[:160]}" for c in skipped]
    total = spent_seconds(commits, mutants)
    per_mutant = [m["seconds"] for m in mutants]
    seeds = [c["seed_run_s"] for c in commits if c.get("seed_run_s") is not None]
    if per_mutant and seeds:
        lines.append(f"time: {total / 3600:.2f} h; seed run median {statistics.median(seeds):.0f}s"
                     f"; per mutant median {statistics.median(per_mutant):.0f}s "
                     f"(max {max(per_mutant):.0f}s)")  # fmt: skip
    return lines


app = typer.Typer(add_completion=False)


@app.command()
def main(
    repo: Annotated[str, typer.Option(help=f"One of {', '.join(JVM_PROFILES)}")] = "square/moshi",
    state_dir: Annotated[Path, typer.Option()] = Path(".eval-cache/mutation"),
    max_commits: Annotated[int, typer.Option(help="Random sample: changes with mutants")] = 12,
    mutants_per_commit: Annotated[int, typer.Option()] = 3,
    seed_value: Annotated[int, typer.Option("--seed")] = 102,
    time_cap_hours: Annotated[float, typer.Option()] = 3.0,
    run_timeout: Annotated[float, typer.Option(help="Seconds per Gradle run")] = 1800,
    commit: Annotated[
        list[str] | None, typer.Option(help="Only these changes: head commits or PRs (#2056)")
    ] = None,
    show_report: Annotated[bool, typer.Option("--report")] = False,
) -> None:
    profile = JVM_PROFILES[repo]
    if show_report:
        state = state_dir_for(state_dir, repo)
        for line in report(
            read_jsonl(state / "commits.jsonl"), read_jsonl(state / "mutants.jsonl")
        ):
            typer.echo(line)
        return
    run_eval(profile, state_dir, max_commits, mutants_per_commit, seed_value, time_cap_hours,
             run_timeout, tuple(commit or ()))  # fmt: skip


if __name__ == "__main__":
    app()
