"""Test selection: which tests to run for a change.

``select_tests`` is pure: given a repo's ``RepoHistory`` and a change, it returns a
``Selection``. ``load_history`` builds that history from the database, and ``select_for_repo``
does both.

Missing a real failure is worse than running extra tests, so whenever the selector is unsure
it returns ``Mode.FULL`` with a reason. It falls back to the full suite when:

* the changed files are unknown, or the list is empty;
* a build, dependency, CI or test-config file changed. A project manifest or lockfile in a
  subdirectory (``go.mod``, ``package.json``, ``pyproject.toml``, ...) is the exception: it
  belongs to a nested project, so it selects the tests under its own directory instead;
* the repo has no test history;
* any changed source file maps to no known test (non-code files like docs are ignored);
* a selected test has no known runner, so no command can be built for it.

Otherwise it selects the union of these signals, and every test carries its reasons:

* **Path mapping**, language-aware:
  - Python: ``src/x/foo.py`` selects tests in any ``test_foo.py`` / ``foo_test.py``. Test file
    paths come from ``file_path``, or are derived from the pytest classname
    (``tests.test_foo.TestBar`` -> ``tests/test_foo.py``).
  - Go: a changed ``.go`` file selects every test in the same package directory, and that
    package runs whole (no ``-run``), so tests added in the change run too. Packages are
    matched by import-path suffix (``test/cli`` matches ``github.com/ipfs/kubo/test/cli``), or
    exactly when ``SelectorConfig.go_module`` is set.
  - JS/TS: ``foo.ts`` selects ``foo.test.*``, ``foo.spec.*`` and ``__tests__/foo.*``.
  - A changed test file always selects its own tests.
  - A changed or added Python test file runs whole (``pytest path/test_x.py``), so tests new
    in it run too. A deleted one runs nothing.
  - pytest collection errors (``pytest::a.b.c``: a module that failed to import) map to the
    module's file ``a/b/c.py``, which pytest then runs whole; doctests
    (``a.b::a.b.Thing``) to ``pytest --doctest-modules a/b.py``. If no file can be derived,
    the test is still selected but left out of the command rather than forcing the full suite.
* **Python imports**: test files that import a changed module (``affected_files``, from the
  CLI's static import graph) run whole.
* **Changed modules under --doctest-modules**: when history shows the repo's pytest collects
  source modules (doctests, or collection errors in non-test modules), changed and added
  ``.py`` modules run with ``pytest --doctest-modules``, so an import error in a new module
  or a broken doctest is caught.
* **Co-change**: tests that failed in recent runs whose changed files overlap this change.
* **Recently failed**: tests that failed in the last ``recent_main_runs`` main runs, or are
  currently broken on main.
* **Always-run**: ``always_run`` globs (as in ``[[depends]]``), matched against test IDs and
  file paths.

Every selected test also gets a risk score and rank (``core/risk.py``): with
``order=Order.RISK`` the commands list the riskiest tests, files and packages first. The
score never changes which tests are selected.

A time budget (``budget_ms``) is the one exception, and it says so: the selection is cut to
the highest-risk whole units (pytest arguments, Go packages, Jest files) whose estimated time
fits, the result is marked budget-limited (``Budget``) with what was left out, and a full-suite
fallback packs the full suite's known tests the same way ("budget-limited full suite").

Matching is deliberately loose (by file name or package suffix anywhere in the repo): extra
tests are cheap, missed ones are not. Path mapping only sees direct naming relationships, not
imports, so a change can still break a test in a dependent module or package. Co-change and
recently-failed partly cover that. The kubo real-data check measures what is missed.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import functools
import posixpath
import re
import shlex
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from siftwise.core.codes import FAILED_CODES_SQL
from siftwise.core.junit import normalize_path
from siftwise.core.risk import Link, Order, Ranked, Risk, RiskInputs, UnitKey, rank, unit_key

if TYPE_CHECKING:
    # The light CLI (no server extra) imports this module: SQLAlchemy only where it's used.
    from sqlalchemy.orm import Session


class Mode(StrEnum):
    FULL = "full"
    SELECTIVE = "selective"


class Runner(StrEnum):
    PYTEST = "pytest"
    GO = "go"
    JEST = "jest"
    MAVEN = "maven"  # Java test classes, run per module (JavaBuildInfo)
    GRADLE = "gradle"


JAVA_RUNNERS = frozenset({Runner.MAVEN, Runner.GRADLE})


# Full-suite commands, in the order commands are emitted.
FULL_SUITE_COMMANDS: dict[Runner, str] = {
    Runner.PYTEST: "pytest",
    Runner.GO: "go test ./...",
    Runner.JEST: "jest",
}

# Reason for a selective result with nothing to run (e.g. a docs-only change).
NO_TESTS_AFFECTED = "no tests affected"
# Reason for the full suite when the changed files are known but there are none.
NO_NET_CHANGES = "this PR has no net changes"

JS_EXTENSIONS = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"})

# Changing any of these can affect every test: dependencies, build, CI, test configuration.
_BUILD_BASENAMES = frozenset(
    {
        # Python
        "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "Pipfile.lock", "poetry.lock",
        "uv.lock", "pdm.lock", "tox.ini", "pytest.ini", "conftest.py", "noxfile.py",
        # JS/TS
        "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
        "pnpm-lock.yaml", "pnpm-workspace.yaml", "bun.lockb", "bun.lock", ".babelrc", ".nvmrc",
        ".swcrc", "lerna.json", ".yarnrc.yml",
        # Go
        "go.mod", "go.sum", "go.work", "go.work.sum",
        # Java: Maven and Gradle builds, wrappers, Lombok's generation settings
        "pom.xml", "gradle.properties", "mvnw", "mvnw.cmd", "gradlew", "gradlew.bat",
        "lombok.config",
        # Build / CI
        "Dockerfile", "Makefile", ".gitlab-ci.yml",
    }
)  # fmt: skip
_BUILD_GLOBS = (
    "requirements*.txt", "Dockerfile.*", "*.Dockerfile", "docker-compose*.yml",
    "docker-compose*.yaml", "tsconfig*.json", "jsconfig*.json", "jest.config.*", "babel.config.*",
    ".babelrc.*", "swc.config.*", "vitest.config.*", "build.gradle*", "settings.gradle*",
)  # fmt: skip
# gradle/: the wrapper and version catalogs (libs.versions.toml); .mvn/: Maven's wrapper,
# extensions and maven.config.
_BUILD_DIRS = (".github/workflows/", ".github/actions/", "gradle/", ".mvn/")

# Project manifests and their lockfiles. At the repo root they force the full suite; in a
# subdirectory they belong to a nested project (an example module, a sub-package) and only
# affect tests under that directory.
_SCOPED_BUILD_BASENAMES = frozenset(
    {
        "go.mod", "go.sum", "go.work", "go.work.sum",
        "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
        "pnpm-lock.yaml", "bun.lock", "bun.lockb",
        "pyproject.toml", "setup.py", "setup.cfg", "poetry.lock", "uv.lock", "pdm.lock",
        "Pipfile", "Pipfile.lock",
    }
)  # fmt: skip
_SCOPED_BUILD_GLOBS = ("requirements*.txt",)

# Files that cannot affect test outcomes. They never trigger the full suite on their own.
_IGNORABLE_EXTENSIONS = frozenset(
    {".md", ".markdown", ".rst", ".adoc", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp"}
)
_IGNORABLE_BASENAMES = frozenset(
    {
        "LICENSE", "NOTICE", "AUTHORS", "CODEOWNERS", "CONTRIBUTORS", "CHANGELOG",
        ".gitignore", ".gitattributes", ".editorconfig", ".mailmap", ".git-blame-ignore-revs",
        # Configs of tools that check or reformat code outside the test run: git hooks,
        # formatters, prose and spelling linters.
        ".pre-commit-config.yaml", ".pre-commit-hooks.yaml", ".prettierignore",
        ".markdownlintignore", ".codespellrc", ".gitlint", "_typos.toml", ".typos.toml",
        "typos.toml", ".cspell.json", "cspell.json",
    }
)  # fmt: skip
# Same, by pattern (every file format these tools accept, e.g. .prettierrc.json, .cjs, .yaml).
# Not code linters (ruff, flake8, pylint, mypy, eslint): pytest and jest plugins can run them
# as tests, so their configs stay test inputs.
_IGNORABLE_GLOBS = (
    ".prettierrc*", "prettier.config.*", ".markdownlint*", ".yamllint*", ".commitlintrc*",
    "commitlint.config.*", ".lintstagedrc*", "lint-staged.config.*", "cspell.config.*",
    ".cspell.config.*",
)  # fmt: skip
_IGNORABLE_DIRS = ("docs/",)
# Files that test and coverage runs write: reports, caches, logs. Never a change to the code,
# even when untracked or committed by mistake (an untracked junit.xml once made the MCP server
# ask for the full suite). Ignorable like docs; .siftwise.toml's `generated` adds globs.
GENERATED_GLOBS = (
    "**/junit*.xml", "**/coverage.xml", "**/.coverage", "**/.coverage.*", "**/htmlcov/**",
    "**/*.log", "**/.pytest_cache/**", "**/__pycache__/**",
    # Editor and coding-agent settings (an agent's MCP config, workspace files): never code
    # under test, though agents and editors write them in the middle of a change.
    "**/.vscode/**", "**/.cursor/**", "**/.idea/**", "**/.mcp.json", "**/.codex/**",
    "**/.gemini/**",
)  # fmt: skip

_PY_IDENTIFIER = re.compile(r"[A-Za-z_]\w*\Z")
# A directory in a pytest classname: pytest run from a monorepo's root reports
# cirq-google/cirq_google/ops/x_test.py as "cirq-google.cirq_google.ops.x_test", so directories
# above the Python packages may hold characters no module name can, such as "-".
_PY_DIRECTORY = re.compile(r"[A-Za-z0-9_][\w-]*\Z")
# A dotted name whose every part is _PY_DIRECTORY (``[\w-]`` has no ``.``: same parts as split).
_PY_DIRECTORIES = re.compile(r"[A-Za-z0-9_][\w-]*(?:\.[A-Za-z0-9_][\w-]*)*\Z")
# What a doctest classname can't contain: whitespace (as str.isspace) or "/".
_NOT_A_MODULE = re.compile(r"[\s/]")
_GO_TOP_LEVEL = re.compile(r"(Test|Fuzz|Example|Benchmark)")


@dataclass(frozen=True)
class SelectorConfig:
    always_run: tuple[str, ...] = ()
    # The run windows below count CI runs (repo, ci_run_id, run_attempt), not uploads: a CI
    # run with a 20-leg matrix (20 variant uploads) counts once.
    recent_main_runs: int = 10
    # How many recent CI runs to search for co-change failures.
    co_change_runs: int = 500
    # A test is "known" only if it appeared in one of the repo's latest N CI runs, so deleted
    # or renamed tests are never selected (pytest errors on node IDs that no longer exist).
    known_test_runs: int = 50
    # Go module path (e.g. "github.com/ipfs/kubo"). When set, Go commands use ./relative
    # package dirs and root-package changes can be mapped; otherwise import paths are used.
    go_module: str | None = None
    # Every Go module of the checkout when there are several (go.work, nested go.mod):
    # (repo-relative directory, module path). Go commands then run in the package's module
    # (`go test -C <dir> ./pkg`) and packages map to directories through their module.
    go_modules: tuple[tuple[str, str], ...] = ()
    # Declared dependencies from the repo's .siftwise.toml.
    depends: tuple[DependsRule, ...] = ()
    # Generated-file globs from .siftwise.toml, on top of GENERATED_GLOBS.
    generated: tuple[str, ...] = ()


@dataclass(frozen=True)
class DependsRule:
    """``[[depends]]`` in .siftwise.toml: tests matching ``tests`` depend on files matching ``on``.

    Globs support ``*`` (within a path segment), ``**`` (any number of segments) and ``?``.
    Patterns in ``on`` starting with ``!`` exclude files. ``tests`` is matched against a
    test's file path, or for Go tests ``<package dir>/<TestName>``; it may match a suffix of
    the Go import path, so ``test/cli/**`` matches ``github.com/ipfs/kubo/test/cli`` tests.
    """

    tests: str
    on: tuple[str, ...]

    def matches_file(self, path: str) -> bool:
        include = [p for p in self.on if not p.startswith("!")]
        exclude = [p[1:] for p in self.on if p.startswith("!")]
        return any(glob_match(path, p) for p in include) and not any(
            glob_match(path, p) for p in exclude
        )


@functools.lru_cache(maxsize=1024)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(path: str, pattern: str) -> bool:
    """Path glob: ``*`` and ``?`` stay within one segment, ``**`` spans segments."""
    return _glob_regex(pattern).match(path) is not None


@dataclass(frozen=True)
class KnownTest:
    test_id: str
    file_path: str | None = None
    # Mean duration in history (test_stats.avg_duration_ms): sizes the estimate of test time
    # saved and breaks risk-order ties; never affects which tests are selected.
    avg_duration_ms: float | None = None
    # test_stats over the stats window, for the risk order only (core/risk.py): never affect
    # which tests are selected. None: unknown (no stats).
    runs: int | None = None
    failures: int | None = None
    last_failed_at: datetime | None = None
    flaky_score: float | None = None


@dataclass(frozen=True)
class FailedRun:
    run_id: int
    commit_sha: str
    changed_files: frozenset[str]
    failed_tests: frozenset[str]


@dataclass(frozen=True)
class RepoHistory:
    tests: Mapping[str, KnownTest]
    # test_id -> first sha of the current failing streak on main
    broken_on_main: Mapping[str, str] = field(default_factory=dict)
    # test_id -> (run_id, commit_sha) of its most recent failure among the recent main runs
    recent_main_failures: Mapping[str, tuple[int, str]] = field(default_factory=dict)
    # Recent runs (changed_files_known only) with at least one failure, newest first.
    failed_runs: Sequence[FailedRun] = ()
    # The repo's newest run: the risk score's recency is measured back from it, not from now.
    newest_run_at: datetime | None = None


@dataclass(frozen=True)
class SelectedTest:
    test_id: str
    reasons: tuple[str, ...]
    runner: Runner | None
    file_path: str | None
    # Risk order (core/risk.py): rank 1 runs first. Selection never depends on them.
    score: float | None = None
    rank: int | None = None
    rank_reason: str = ""

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


@dataclass(frozen=True)
class Selection:
    mode: Mode
    # Why the full suite was chosen, or a one-line summary of a selective run.
    reason: str
    tests: tuple[SelectedTest, ...]
    selected_count: int
    total_known: int
    commands: tuple[str, ...]
    # Go packages with changed .go files. Their command has no -run filter, so tests added in
    # the same change (not yet in history) run too.
    go_packages_run_whole: tuple[str, ...] = ()
    # Python files passed to pytest as a whole file: changed/added test files, test files that
    # import a changed module, collection errors and doctest modules. New tests in them run too.
    python_files_run_whole: tuple[str, ...] = ()
    # Java test classes the commands run whole (outer class names): every test in them runs,
    # new ones too (a test class with no history among them).
    java_classes_run: tuple[str, ...] = ()
    # Estimated test time from history: all known tests, and the ones the commands would run
    # (see WouldRun). Go subtests are left out: their time is inside their parent's.
    estimated_ms_total: float = 0.0
    estimated_ms_would_run: float = 0.0
    # How the commands list their tests (Order.RISK: highest risk first).
    order: Order = Order.DEFAULT
    # A time budget's effect; None when no budget was asked for.
    budget: Budget | None = None

    @property
    def command(self) -> str:
        """All commands as one shell line (empty if nothing needs to run)."""
        return " && ".join(self.commands)


@dataclass(frozen=True)
class Budget:
    """What a time budget did to a selection (``select_tests(budget_ms=...)``)."""

    budget_ms: float
    # False: it couldn't be applied (``note`` says why), and the selection runs as it is.
    applied: bool = True
    # Selected tests were left out to fit: the run is NOT a complete check of the change.
    limited: bool = False
    # siftwise would have run the full suite: the run packs the full suite's known tests.
    full_suite: bool = False
    note: str = ""
    estimated_ms: float = 0.0  # what runs: its units' estimates
    left_out: int = 0  # selected tests left out (in a full suite: known tests)
    left_out_ms: float = 0.0  # their units' estimates
    left_out_risk_share: float = 0.0  # their share of the selection's total risk score
    unknown_ms: float | None = None  # what a test with no known duration counted as
    # The selection without the budget, for shadow mode (limited runs only): a miss it would
    # have run was left out by the budget.
    before: SelectionScope | None = None


@dataclass(frozen=True)
class JsImport:
    """A Jest test file's nearest changed file in the CLI's JS/TS import graph."""

    module: str
    via: str | None  # the file the test imports on the way; None: it imports it directly
    depth: int  # 1 = a direct import


@dataclass(frozen=True)
class JsImports:
    """What the CLI's JS/TS import graph adds to ``affected_files`` (``/select``'s
    ``js_graph``): reasons and the files it can't see through."""

    nearest: Mapping[str, JsImport] = field(default_factory=dict)
    # Test file -> a file it reaches whose require/import() of a non-literal may load
    # anything: such tests run for any JS change, but never count as covering it.
    dynamic: Mapping[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()  # what the graph couldn't read (a jest.config.ts)


@dataclass(frozen=True)
class ImportDepths:
    """Import depth per test file/package from the CLI's graphs (1 direct, 2+ transitive)."""

    files: Mapping[str, int] = field(default_factory=dict)
    packages: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class JavaModuleInfo:
    path: str  # repo-relative directory; "" = the root project
    name: str  # Maven: the -pl argument; Gradle: the project path (":a:b"; "" for the root)
    unanalysed: tuple[str, ...] = ()  # why its Java sources aren't analysed: full suite

    def prefix(self, root: str) -> str:
        return f"{self.path}/{root}/" if self.path else f"{root}/"


@dataclass(frozen=True)
class JavaBuildInfo:
    """The checkout's Java build as the CLI read it (``/select``'s ``java``), so Java test
    classes can run per module."""

    tool: str  # "maven" or "gradle"
    wrapper: bool = False
    modules: tuple[JavaModuleInfo, ...] = ()
    test_classes: Mapping[str, str] = field(default_factory=dict)  # test class -> module path
    nearest: Mapping[str, JsImport] = field(default_factory=dict)  # test file -> nearest change
    dynamic: Mapping[str, str] = field(default_factory=dict)  # test file -> file it reaches
    notes: tuple[str, ...] = ()
    unsupported: str = ""  # the build couldn't be read: every Java change runs everything

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> JavaBuildInfo:
        """From ``/select``'s ``java`` (``javagraph.JavaChange.payload``)."""
        return cls(
            tool=data["tool"],
            wrapper=bool(data.get("wrapper")),
            modules=tuple(
                JavaModuleInfo(m["path"], m["name"], tuple(m.get("unanalysed") or ()))
                for m in data.get("modules") or ()
            ),
            test_classes={
                c: module
                for module, classes in (data.get("test_classes") or {}).items()
                for c in classes
            },
            nearest={
                test: JsImport(n["module"], n.get("via"), n["depth"])
                for test, n in (data.get("nearest") or {}).items()
            },
            dynamic=dict(data.get("dynamic") or {}),
            notes=tuple(data.get("notes") or ()),
            unsupported=data.get("unsupported") or "",
        )

    @property
    def runner(self) -> Runner:
        return Runner.MAVEN if self.tool == "maven" else Runner.GRADLE

    def module(self, path: str) -> JavaModuleInfo | None:
        return next((m for m in self.modules if m.path == path), None)

    def module_of(self, file: str) -> JavaModuleInfo | None:
        """The deepest module whose directory holds ``file``."""
        found = [m for m in self.modules if not m.path or file.startswith(m.path + "/")]
        return max(found, key=lambda m: len(m.path), default=None)


# --- selection ----------------------------------------------------------------------------


def select_tests(
    history: RepoHistory | None,
    changed_files: Iterable[str],
    changed_files_known: bool,
    config: SelectorConfig | None = None,
    affected_packages: Mapping[str, Sequence[str]] | None = None,
    affected_files: Mapping[str, Sequence[str]] | None = None,
    deleted_files: Iterable[str] = (),
    junit_dir: str | None = None,
    order: Order = Order.DEFAULT,
    budget_ms: float | None = None,
    js_imports: JsImports | None = None,
    java: JavaBuildInfo | None = None,
    import_depths: ImportDepths | None = None,
) -> Selection:
    """Select tests for a change.

    ``affected_packages`` maps a Go package import path to the changed packages it imports
    (directly or transitively), as computed by the CLI from ``go list -deps -test``.
    ``affected_files`` maps a Python test file to the changed modules it imports (directly,
    transitively, via parent packages or conftest.py), from the CLI's static import graph, and
    a Jest test file (repo-relative) to the changed files it imports, from the JS/TS graph;
    ``js_imports`` adds that graph's details (``JsImports``).
    ``import_depths`` optionally says whether each import link is direct (depth 1) or
    transitive (2+), per test file/package. Without it, imports stay indirect as before.
    ``deleted_files`` are changed files that no longer exist: no command names them.
    ``junit_dir``: each pytest command writes ``--junitxml=<junit_dir>/siftwise-<n>.xml``, so
    a line of several pytest commands doesn't overwrite one report with the next.
    ``order``: ``Order.RISK`` lists the riskiest tests first in the commands (core/risk.py).
    Every selected test gets its score and rank either way, and the same tests are selected.
    ``budget_ms``: run only the highest-risk units that fit (see ``Budget``); None = no budget.
    ``java``: the checkout's Java build; without it Java tests have no runner (full suite).
    """
    # Every known test classified once, for the selection and its time estimate.
    tests = _index(history.tests.values() if history else ())
    if java is not None:
        tests = _with_java(tests, java)
    selection = _select(
        history, changed_files, changed_files_known, config or SelectorConfig(),
        affected_packages, affected_files, deleted_files, junit_dir, tests, order, budget_ms,
        js_imports or JsImports(), java, import_depths or ImportDepths(),
    )  # fmt: skip
    return _with_estimate(selection, history, tests)


def _select(
    history: RepoHistory | None,
    changed_files: Iterable[str],
    changed_files_known: bool,
    config: SelectorConfig,
    affected_packages: Mapping[str, Sequence[str]] | None,
    affected_files: Mapping[str, Sequence[str]] | None,
    deleted_files: Iterable[str],
    junit_dir: str | None,
    tests: Mapping[str, _Test],
    order: Order = Order.DEFAULT,
    budget_ms: float | None = None,
    js_imports: JsImports | None = None,
    java: JavaBuildInfo | None = None,
    import_depths: ImportDepths | None = None,
) -> Selection:
    js_imports = js_imports or JsImports()
    import_depths = import_depths or ImportDepths()
    affected_packages = affected_packages or {}
    affected_files = {
        path: modules
        for file, modules in (affected_files or {}).items()
        if (path := normalize_path(file))
    }
    import_file_depth = {
        path: depth
        for file, depth in import_depths.files.items()
        if depth >= 1 and (path := normalize_path(file))
    }
    import_package_depth = {pkg: depth for pkg, depth in import_depths.packages.items() if depth >= 1}
    deleted = frozenset(p for p in (normalize_path(f) for f in deleted_files) if p)

    def import_link(depth: int | None) -> Link:
        return Link.DIRECT if depth == 1 else Link.INDIRECT

    def full(reason: str) -> Selection:
        runners = {t.runner for t in tests.values() if t.runner is not None}
        selection = Selection(
            mode=Mode.FULL,
            reason=reason,
            tests=(),
            selected_count=len(tests),
            total_known=len(tests),
            commands=tuple(
                _with_junit(cmd, junit_dir, n) if r is Runner.PYTEST else cmd
                for n, (r, cmd) in enumerate(FULL_SUITE_COMMANDS.items(), start=1)
                if r in runners
            )
            + (_java_full_command(java, tests) if java and runners & JAVA_RUNNERS else ()),
            order=order,
        )
        if budget_ms is None:
            return selection
        return _full_within_budget(selection, history, tests, _GoLayout.of(config), junit_dir,
                                   order, budget_ms, java)  # fmt: skip

    if not changed_files_known:
        return full("changed files are unknown for this change")
    paths = sorted({p for p in (normalize_path(f) for f in changed_files) if p})
    if not paths:
        # A known but empty diff: e.g. a PR whose commits cancel out. Still "unsure": nothing
        # to select on, so the full suite.
        return full(NO_NET_CHANGES)
    scoped_build = {p: d for p in paths if (d := build_file_scope(p)) is not None}
    build = [p for p in paths if is_build_file(p) and p not in scoped_build]
    if build:
        return full(f"build/config file changed: {_list(build)}")
    if history is None or not tests:
        return full("no test history for this repo")

    selected: dict[str, list[str]] = {}
    links: dict[str, Link] = {}  # each test's strongest link, for the risk score

    def add(test_id: str, reason: str, link: Link) -> None:
        reasons = selected.setdefault(test_id, [])
        if reason not in reasons:
            reasons.append(reason)
        links[test_id] = max(links.get(test_id, Link.HISTORY), link)

    # Python test files, by path: the files pytest can be pointed at whole. (Doctests and
    # collection errors point at their module, which may be a source file.)
    py_test_files: dict[str, list[_Test]] = {}
    for test in tests.values():
        if (
            test.runner is Runner.PYTEST
            and test.py_path
            and not test.doctest
            and test.classname != PYTEST_COLLECTION_CLASSNAME
        ):
            py_test_files.setdefault(test.py_path, []).append(test)

    # Path mapping. Every changed source file must map to at least one test.
    relevant = [p for p in paths if not is_ignorable(p, config.generated) and p not in scoped_build]
    unmapped = []
    whole_packages: set[str] = set()
    py_whole_files: set[str] = set()
    jest_files = _JestFiles(tests.values())
    js_new_files: dict[str, str] = {}  # a new Jest test file's path as Jest runs it -> repo path
    java_classes: dict[str, list[_Test]] = defaultdict(list)  # test class -> its known tests
    for test in tests.values():
        if test.java_class:
            java_classes[test.java_class].append(test)
    java_new: set[tuple[str, str]] = set()  # (module, class) with no known test: run it
    if java is not None:
        problem = _java_problem(relevant, java)
        if problem:
            return full(problem)
    for path in relevant:
        if java is not None and (module := java.module_of(path)) is not None:
            if path.startswith(module.prefix("src/test/resources")):
                # Test resources: every test class of the module may read them.
                for cls, owner in sorted(java.test_classes.items()):
                    if owner != module.path:
                        continue
                    for test in java_classes.get(cls, ()):
                        add(test.test_id, f"test resources changed in {module.name or '.'}: "
                            f"{path}", Link.INDIRECT)  # fmt: skip
                    if not java_classes.get(cls):
                        java_new.add((owner, cls))
                continue
            located = java_class_of(path)
            if located is not None:
                hits, new = _java_path_hits(path, located, java, java_classes, path in deleted)
                java_new.update(new)
                if not hits and not new and not (located[2] and path in deleted):
                    unmapped.append(path)
                for test_id, reason in hits.items():
                    add(test_id, reason, Link.DIRECT)
                continue
        is_py_test_file = path.endswith(".py") and (
            is_python_test_file(path) or path in py_test_files
        )
        if is_py_test_file and path in deleted:
            continue  # a deleted test file: its tests are gone, there's nothing to run
        hits = _map_path(path, tests, config)
        if not hits and is_jest_test_file(path) and path not in deleted:
            # A changed Jest test file reported from its project's root (a suffix of the repo
            # path), or a new one: Jest runs files whole, so tests new in it run too.
            for test in jest_files.tests_for(path):
                hits[test.test_id] = f"test file changed: {path}"
            runnable = None if hits else jest_files.runnable(path)
            if runnable is not None:
                js_new_files[runnable] = path
                continue
        if is_py_test_file:
            # Changed or added test file: run it whole, so tests new in it run too.
            py_whole_files.add(path)
        elif not hits:
            unmapped.append(path)
        for test_id, reason in hits.items():
            add(test_id, reason, Link.DIRECT)
            if path.endswith(".go") and tests[test_id].runner is Runner.GO:
                whole_packages.add(tests[test_id].classname)

    # A repo whose history has doctests or collection errors in source modules runs pytest with
    # --doctest-modules: pytest imports every module, so a changed or added module can fail at
    # import (a "pytest::a.b" collection error) or in its doctests. Run those modules too.
    py_module_files: set[str] = set()
    if any(t.collects_source_module for t in tests.values()):
        py_module_files = {
            p
            for p in relevant
            if p.endswith(".py") and p not in deleted and p not in py_whole_files
        }

    # Python dependents: test files that import a changed module. They run whole.
    covered_by_imports: set[str] = set()  # changed modules imported by a known test file
    for file, modules in sorted(affected_files.items()):
        known = py_test_files.get(file)
        if not known or file in deleted:
            continue
        first = sorted(modules)[0] if modules else "?"
        more = f" (+{len(modules) - 1} more)" if len(modules) > 1 else ""
        for test in known:
            add(
                test.test_id,
                f"imports changed module {first}{more}",
                import_link(import_file_depth.get(file)),
            )
        py_whole_files.add(file)
        covered_by_imports.update(modules)

    # JS/TS dependents: Jest test files that import a changed file (Jest runs files whole).
    for file, modules in sorted(affected_files.items()):
        if file in deleted or posixpath.splitext(file)[1] not in JS_EXTENSIONS or not modules:
            continue
        known_js = jest_files.tests_for(file)
        if not known_js:
            continue
        reason = js_import_reason(modules, js_imports.nearest.get(file), js_imports.notes)
        for test in known_js:
            add(test.test_id, reason, import_link(import_file_depth.get(file)))
        covered_by_imports.update(modules)
    # Java dependents: test classes that use a changed class (run whole, per module).
    if java is not None:
        for file, modules in sorted(affected_files.items()):
            located = java_class_of(file)
            if file in deleted or located is None or not located[2] or not modules:
                continue
            known_java = java_classes.get(located[1], [])
            reason = java_use_reason(modules, java.nearest.get(file), java.notes)
            for test in known_java:
                add(test.test_id, reason, import_link(import_file_depth.get(file)))
            if not known_java and located[1] in java.test_classes and _is_test_class(located[1]):
                java_new.add((java.test_classes[located[1]], located[1]))
            covered_by_imports.update(modules)
        for file, source in sorted(java.dynamic.items()):
            located = java_class_of(file)
            for test in java_classes.get(located[1], []) if located else []:
                add(test.test_id, f"uses {source}, which imports a class the sources don't "
                    "declare (generated?): it may use changed code", Link.INDIRECT)  # fmt: skip
    # Tests that reach an import the graph can't resolve may load any changed file: they run,
    # but cover nothing (a change no static import reaches still falls back as before).
    for file, source in sorted(js_imports.dynamic.items()):
        for test in jest_files.tests_for(file):
            add(test.test_id, f"reaches {source}, whose dynamic import siftwise can't resolve: "
                "it may load changed code", Link.INDIRECT)  # fmt: skip

    # Nested manifests/lockfiles: every known test under their directory runs (Go packages
    # whole). They never force the full suite, even when no known tests live there.
    for path, directory in sorted(scoped_build.items()):
        for test in tests.values():
            if _test_under_dir(test, directory):
                reason = f"build file {path} changed (tests under {directory}/)"
                add(test.test_id, reason, Link.INDIRECT)
                if test.runner is Runner.GO:
                    whole_packages.add(test.classname)

    # Go dependents: packages whose tests import a changed package. They run whole.
    covered_by_dependents: set[str] = set()  # changed packages imported by a tested package
    for test in tests.values():
        imported = affected_packages.get(test.classname) if test.runner is Runner.GO else None
        if imported:
            first = sorted(imported)[0]
            more = f" (+{len(imported) - 1} more)" if len(imported) > 1 else ""
            add(
                test.test_id,
                f"imports changed package {first}{more}",
                import_link(import_package_depth.get(test.classname)),
            )
            whole_packages.add(test.classname)
            covered_by_dependents.update(imported)

    # Declared dependencies (.siftwise.toml [[depends]]).
    covered_by_rules: set[str] = set()
    for rule in config.depends:
        triggers = [p for p in relevant if rule.matches_file(p)]
        if not triggers:
            continue
        matched = [t for t in tests.values() if _test_matches_glob(t, rule.tests, config)]
        if not matched:
            continue
        covered_by_rules.update(triggers)
        for test in matched:
            reason = f"declared dependency: {rule.tests} on {_list(triggers, 1)}"
            add(test.test_id, reason, Link.INDIRECT)
            if test.runner is Runner.GO:
                whole_packages.add(test.classname)

    # A changed file is covered if its own tests, its package's dependents' tests, test files
    # importing it or a declared rule selected something; anything else forces the full suite.
    unmapped = [
        p
        for p in unmapped
        if p not in covered_by_rules
        and p not in covered_by_imports
        and not (
            p.endswith(".go")
            and any(
                _go_package_matches(pkg, posixpath.dirname(p), config.go_module, config.go_modules)
                for pkg in covered_by_dependents
            )
        )
    ]
    if unmapped:
        return full(f"changed file maps to no known tests: {_list(unmapped)}")

    # Co-change: earlier failures in runs that touched the same files. One reason per test,
    # from the most recent such run.
    relevant_set = frozenset(relevant)
    co_changed: set[str] = set()
    for run in history.failed_runs:
        overlap = run.changed_files & relevant_set
        if not overlap:
            continue
        for test_id in sorted(run.failed_tests - co_changed):
            if test_id in tests:
                co_changed.add(test_id)
                add(
                    test_id,
                    f"co-change: failed in run {run.run_id} ({run.commit_sha[:7]}), "
                    f"which also changed {_list(sorted(overlap))}",
                    Link.HISTORY,
                )

    # Recently failed or broken on main.
    for test_id, sha in sorted(history.broken_on_main.items()):
        if test_id in tests:
            add(test_id, f"broken on main since {sha[:7]}", Link.HISTORY)
    for test_id, (run_id, sha) in sorted(history.recent_main_failures.items()):
        if test_id in tests:
            add(test_id, f"failed on main in run {run_id} ({sha[:7]})", Link.HISTORY)

    # Always-run patterns: the same globs as [[depends]] (`**` spans directories, `*` and `?`
    # stay within one), against the test ID and the test's file path.
    for pattern in config.always_run:
        for test in tests.values():
            if glob_match(test.test_id, pattern) or (
                test.file_path and glob_match(test.file_path, pattern)
            ):
                add(test.test_id, f"always-run pattern {pattern!r}", Link.HISTORY)

    # Tests in a deleted Python or Jest file no longer exist (pytest errors on a missing path,
    # and so does Jest: "No tests found").
    gone = {t.test_id for path in deleted for t in jest_files.tests_for(path)}
    gone |= {
        t.test_id
        for path in deleted
        if (located := java_class_of(path)) is not None and located[2]
        for t in java_classes.get(located[1], ())
    }
    chosen = [
        tests[test_id]
        for test_id in sorted(selected)
        if not (tests[test_id].runner is Runner.PYTEST and tests[test_id].py_path in deleted)
        and test_id not in gone
    ]
    unrunnable = [t.test_id for t in chosen if t.runner is None]
    if unrunnable:
        return full(f"no known test runner for selected test(s): {_list(unrunnable)}")

    files_run_whole = (
        py_whole_files
        | py_module_files
        | {
            node
            for t in chosen
            if t.runner is Runner.PYTEST and (node := t.py_node) and "::" not in node
        }
    )
    if chosen:
        summary = f"{len(chosen)} of {len(tests)} known tests selected"
    elif py_whole_files or js_new_files or java_new:
        new_files = len(py_whole_files) + len(js_new_files) + len(java_new)
        summary = f"{new_files} changed test file(s) run whole, no known tests"
    elif py_module_files:
        summary = f"{len(py_module_files)} changed module(s) run with --doctest-modules"
    else:
        summary = NO_TESTS_AFFECTED
    if js_new_files:
        summary += f"; new test file: {_list(sorted(js_new_files.values()))}"
    if java_new:
        summary += f"; test class with no history: {_list(sorted(c for _, c in java_new))}"
    ranks = rank(
        (_risk_inputs(history.tests[t.test_id], links[t.test_id]) for t in chosen),
        history.newest_run_at,
    )
    ranked = _ranked(chosen, ranks, links, history)
    units = _units(chosen, whole_packages, py_whole_files, py_module_files, js_new_files,
                   java_new)  # fmt: skip
    selection = Selection(
        mode=Mode.SELECTIVE,
        reason=summary,
        tests=tuple(
            SelectedTest(
                t.test_id,
                tuple(selected[t.test_id]),
                t.runner,
                t.file_path,
                score=ranks[t.test_id][1].score,
                rank=ranks[t.test_id][0],
                rank_reason=ranks[t.test_id][1].why,
            )
            for t in chosen
        ),
        selected_count=len(chosen),
        total_known=len(tests),
        commands=_commands(units, _GoLayout.of(config), junit_dir, _key(order, ranked), java),
        go_packages_run_whole=tuple(sorted(whole_packages)),
        python_files_run_whole=tuple(sorted(files_run_whole)),
        java_classes_run=_java_classes_run(units),
        order=order,
    )
    if budget_ms is None:
        return selection
    costs = _Costs(tests, history)
    if costs.unknown_ms is None:
        note = "no test durations in history to estimate with: no budget applied"
        return dataclasses.replace(selection, budget=Budget(budget_ms, applied=False, note=note))
    return _pack(selection, units, ranked, costs, budget_ms, _GoLayout.of(config), junit_dir,
                 java=java)  # fmt: skip


def _ranked(
    chosen: Iterable[_Test],
    ranks: Mapping[str, tuple[int, Risk]],
    links: Mapping[str, Link],
    history: RepoHistory,
) -> dict[str, Ranked]:
    """The selected tests as the command order and the budget see them."""
    return {
        t.test_id: Ranked(
            ranks[t.test_id][0],
            ranks[t.test_id][1].score,
            links[t.test_id],
            0.0 if is_go_subtest(t.test_id) else history.tests[t.test_id].avg_duration_ms,
        )
        for t in chosen
    }


def _java_classes_run(units: Iterable[_Unit]) -> tuple[str, ...]:
    return tuple(sorted({u.java_class for u in units if u.java_class}))


def _key(order: Order, ranked: Mapping[str, Ranked]) -> UnitKey | None:
    return None if order is Order.DEFAULT else unit_key(order, ranked)


# --- time budget ----------------------------------------------------------------------------

# An unknown duration counts as this percentile of the known ones: most tests are faster, so a
# budget errs towards running less than it could, not more than fits.
UNKNOWN_DURATION_PERCENTILE = 0.9


class _Costs:
    """A unit's estimated time: the mean durations (``avg_duration_ms``) of the known tests it
    runs, Go subtests left out (their time is inside their parent's). Whole files and whole
    packages count every known test in them, not only the selected ones.

    A known test that was skipped in every run of the window (``runs == 0``: no duration, by
    definition) counts as 0: it will most likely be skipped again. Any other test with no known
    duration counts as ``unknown_ms``, the ``UNKNOWN_DURATION_PERCENTILE`` of the known
    (positive) durations; a unit with no known test (a new test file) counts as one such test.
    """

    def __init__(self, tests: Mapping[str, _Test], history: RepoHistory) -> None:
        self.ms: dict[str, float | None] = {}
        for test_id in tests:
            stats = history.tests[test_id]
            skipped_only = stats.avg_duration_ms is None and stats.runs == 0
            self.ms[test_id] = 0.0 if skipped_only else stats.avg_duration_ms
        timed = sorted(ms for test_id, ms in self.ms.items() if ms and not is_go_subtest(test_id))
        self.unknown_ms: float | None = (
            timed[min(len(timed) - 1, int(UNKNOWN_DURATION_PERCENTILE * len(timed)))]
            if timed
            else None
        )
        self.files: dict[str, list[str]] = defaultdict(list)
        self.doctest_files: dict[str, list[str]] = defaultdict(list)
        self.packages: dict[str, list[tuple[str, str]]] = defaultdict(list)  # (top-level, ID)
        self.jest_files: dict[str, list[str]] = defaultdict(list)
        self.java_classes: dict[tuple[str, str], list[str]] = defaultdict(list)
        for t in tests.values():
            if is_go_subtest(t.test_id):
                continue
            if t.java_class and t.java_module is not None:
                self.java_classes[(t.java_module, t.java_class)].append(t.test_id)
                continue
            if t.runner is Runner.PYTEST and t.py_path:
                (self.doctest_files if t.doctest else self.files)[t.py_path].append(t.test_id)
            elif t.runner is Runner.GO:
                self.packages[t.classname].append((t.name.split("/", 1)[0], t.test_id))
            elif t.runner is Runner.JEST and t.file_path:
                self.jest_files[t.file_path].append(t.test_id)

    def __call__(self, unit: _Unit) -> float:
        assert self.unknown_ms is not None
        if unit.doctest:
            ids = self.doctest_files.get(unit.arg, [])
        elif unit.runner is Runner.PYTEST:
            ids = self.files.get(unit.arg, []) if "::" not in unit.arg else list(unit.test_ids)
        elif unit.runner is Runner.GO:
            names = unit.go_names
            ids = [i for name, i in self.packages.get(unit.arg, []) if not names or name in names]
        elif unit.runner in JAVA_RUNNERS:
            ids = self.java_classes.get((unit.java_module or "", unit.java_class or ""), [])
        else:
            ids = self.jest_files.get(unit.arg, [])
        if not ids:
            return self.unknown_ms
        unknown = self.unknown_ms
        return sum(
            unknown if (ms := self.ms.get(i)) is None else ms for i in ids if not is_go_subtest(i)
        )


def _pack(
    selection: Selection,
    units: Sequence[_Unit],
    ranked: Mapping[str, Ranked],
    costs: _Costs,
    budget_ms: float,
    go_module: str | _GoLayout | None,
    junit_dir: str | None,
    full_reason: str | None = None,
    java: JavaBuildInfo | None = None,
) -> Selection:
    """``selection`` cut to a time budget: walk its units in risk order and keep each one
    whose estimate still fits in what's left (the first always: a budget never runs nothing
    when tests were selected). A unit that doesn't fit is skipped, not the end of the walk:
    one big unit high in the order would otherwise leave most of the budget unused.
    ``full_reason``: siftwise would have run the full suite for this reason, and
    ``selection`` holds the full suite's known tests."""
    risk = unit_key(Order.RISK, ranked)
    walk = sorted(range(len(units)), key=lambda n: (risk(units[n].test_ids), n))
    kept: set[int] = set()
    spent = 0.0
    for n in walk:
        cost = costs(units[n])
        if kept and spent + cost > budget_ms:
            continue
        kept.add(n)
        spent += cost
    left = [units[n] for n in walk if n not in kept]
    kept_units = [units[n] for n in sorted(kept)]  # by path within each kind, as built
    kept_ids = {i for unit in kept_units for i in unit.test_ids}
    left_ids = {i for unit in left for i in unit.test_ids} - kept_ids
    if full_reason is not None and left:
        # Tests no command can name (no known runner) don't run in a packed full suite either.
        left_ids |= {t.test_id for t in selection.tests} - kept_ids
    budget = Budget(budget_ms, full_suite=full_reason is not None, estimated_ms=spent,
                    unknown_ms=costs.unknown_ms)  # fmt: skip
    if not left_ids and not left:
        return dataclasses.replace(selection, budget=budget)
    total = sum(r.score for r in ranked.values())
    share = (
        sum(ranked[i].score for i in left_ids if i in ranked) / total
        if total
        else len(left_ids) / max(len(ranked), 1)  # every score 0: every test counts the same
    )
    left_ms = sum(costs(unit) for unit in left)
    tests = tuple(t for t in selection.tests if t.test_id not in left_ids)
    kept_args = {unit.arg for unit in kept_units if unit.runner is Runner.PYTEST}
    budget = dataclasses.replace(
        budget,
        limited=True,
        left_out=len(left_ids),
        left_out_ms=left_ms,
        left_out_risk_share=share,
        before=SelectionScope(full=True) if full_reason else SelectionScope.of(selection),
    )
    # The budget is a target: the estimates are mean durations, and a run can take longer.
    target = f"estimated {format_duration(spent)} of a {format_duration(budget_ms)} target"
    fits = (
        f"{len(tests):,} of {len(selection.tests):,} "
        f"{'known' if full_reason else 'selected'} tests run; {len(left_ids):,} left out "
        f"(~{format_duration(left_ms)} estimated, {share:.0%} of the "
        f"{'suite' if full_reason else 'selection'}'s risk)"
    )
    reason = (
        f"budget-limited full suite ({target}; siftwise would run the full suite: "
        f"{full_reason}): {fits}"
        if full_reason
        else f"budget-limited ({target}): {fits}"
    )
    return dataclasses.replace(
        selection,
        mode=Mode.SELECTIVE,
        reason=reason,
        tests=tests,
        selected_count=len(tests),
        commands=_commands(kept_units, go_module, junit_dir, _key(selection.order, ranked), java),
        go_packages_run_whole=tuple(
            u.arg for u in kept_units if u.runner is Runner.GO and u.go_names is None
        ),
        python_files_run_whole=tuple(f for f in selection.python_files_run_whole if f in kept_args),
        java_classes_run=_java_classes_run(kept_units),
        budget=budget,
    )


def _full_within_budget(
    selection: Selection,
    history: RepoHistory | None,
    tests: Mapping[str, _Test],
    go_module: str | _GoLayout | None,
    junit_dir: str | None,
    order: Order,
    budget_ms: float,
    java: JavaBuildInfo | None = None,
) -> Selection:
    """A full-suite fallback under a budget: the full suite's known tests packed by risk (every
    test history-only: the change can't be trusted to link them), flagged as a budget-limited
    full suite. Tests no runner can name are left out of a pack (and counted). When the known
    tests can't be ranked or estimated, the full suite runs as it is, and the budget says it
    wasn't applied."""

    def not_applied(note: str) -> Selection:
        budget = Budget(budget_ms, applied=False, full_suite=True, note=note)
        return dataclasses.replace(selection, budget=budget)

    if history is None or not tests:
        return not_applied("no test history to rank the full suite by: running all of it")
    costs = _Costs(tests, history)
    if costs.unknown_ms is None:
        return not_applied("no test durations in history to estimate with: running the full "
                           "suite")  # fmt: skip
    chosen = [tests[test_id] for test_id in sorted(tests)]
    links = dict.fromkeys(tests, Link.HISTORY)
    ranks = rank(
        (_risk_inputs(history.tests[t.test_id], Link.HISTORY) for t in chosen),
        history.newest_run_at,
    )
    whole_packages = {t.classname for t in chosen if t.runner is Runner.GO}
    whole_files = {
        t.py_path for t in chosen if t.runner is Runner.PYTEST and t.py_path and not t.doctest
    }
    units = _units(chosen, whole_packages, whole_files)
    reason = f"full suite: {selection.reason}"
    candidate = dataclasses.replace(
        selection,
        tests=tuple(
            SelectedTest(
                t.test_id,
                (reason,),
                t.runner,
                t.file_path,
                score=ranks[t.test_id][1].score,
                rank=ranks[t.test_id][0],
                rank_reason=ranks[t.test_id][1].why,
            )
            for t in chosen
        ),
        python_files_run_whole=tuple(sorted(whole_files)),
        order=order,
    )
    packed = _pack(candidate, units, _ranked(chosen, ranks, links, history), costs, budget_ms,
                   go_module, junit_dir, full_reason=selection.reason, java=java)  # fmt: skip
    assert packed.budget is not None
    if not packed.budget.limited:
        # Everything fits: the full suite as it is (its commands also run tests not known yet).
        return dataclasses.replace(selection, budget=packed.budget)
    return packed


def format_duration(ms: float) -> str:
    """``4m 50s``, ``38s``, ``1h 5m``: for budgets and estimates."""
    seconds = round(ms / 1000)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def _risk_inputs(test: KnownTest, link: Link) -> RiskInputs:
    return RiskInputs(test.test_id, link, test.runs, test.failures, test.last_failed_at,
                      test.flaky_score, test.avg_duration_ms)  # fmt: skip


@dataclass(frozen=True)
class SelectionScope:
    """Everything ``WouldRun`` reads from a selection: what shadow mode stores, so a stored
    selection answers "would this test have run?" with the same matching."""

    full: bool
    test_ids: frozenset[str] = frozenset()
    # The selected tests with the Go runner: their command is `go test -run '^(TestX)$'`.
    go_test_ids: frozenset[str] = frozenset()
    go_packages: frozenset[str] = frozenset()
    python_files: frozenset[str] = frozenset()
    java_classes: frozenset[str] = frozenset()

    @classmethod
    def of(cls, selection: Selection) -> SelectionScope:
        return cls(
            full=selection.mode is Mode.FULL,
            test_ids=frozenset(t.test_id for t in selection.tests),
            go_test_ids=frozenset(t.test_id for t in selection.tests if t.runner is Runner.GO),
            go_packages=frozenset(selection.go_packages_run_whole),
            python_files=frozenset(selection.python_files_run_whole),
            java_classes=frozenset(selection.java_classes_run),
        )


class WouldRun:
    """Whether a selection's commands would execute a test, including tests that history
    doesn't name: everything in a Python file, Go package or Java test class run whole, and
    the subtests of a Go test selected with ``-run``. Precomputed, for many lookups."""

    def __init__(
        self,
        selection: Selection | SelectionScope,
        py_file: Callable[[str], str | None] | None = None,
    ) -> None:
        """``py_file``: ``pytest_file`` (a test ID's file, no reported path), or a faster
        lookup giving the same answers."""
        scope = selection if isinstance(selection, SelectionScope) else SelectionScope.of(selection)
        self.py_file = py_file or pytest_file
        self.full = scope.full
        self.selected = scope.test_ids
        self.go_packages = scope.go_packages
        self.py_files = scope.python_files
        self.java_classes = scope.java_classes
        # go test -run '^(TestX)$' runs TestX with all of its subtests.
        self.go_top_level = {
            (package, name.split("/", 1)[0])
            for package, _, name in (test_id.partition("::") for test_id in scope.go_test_ids)
        }

    def __call__(self, test_id: str) -> bool:
        if self.full or test_id in self.selected:
            return True
        package, _, name = test_id.partition("::")
        if package in self.go_packages:
            return True  # `go test ./pkg` with no -run: every test in the package, new ones too
        if (package, name.split("/", 1)[0]) in self.go_top_level:
            return True
        if self.java_classes and package.split("$", 1)[0] in self.java_classes:
            return True  # -Dtest=a.FooTest / --tests a.FooTest: the whole class, nested too
        # `pytest path/test_x.py`: every test in the file, new ones too.
        return bool(self.py_files) and self.py_file(test_id) in self.py_files


def _indexed_py_file(index: Mapping[str, _Test]) -> Callable[[str], str | None]:
    """``pytest_file(test_id)`` from the index when the test was classified the same way (no
    file path); a test with a reported path is classified again without it."""

    def py_file(test_id: str) -> str | None:
        test = index.get(test_id)
        if test is None or test.file_path is not None:
            return pytest_file(test_id)
        return test.py_path if test.runner is Runner.PYTEST else None

    return py_file


def _with_estimate(
    selection: Selection, history: RepoHistory | None, index: Mapping[str, _Test]
) -> Selection:
    if history is None:
        return selection
    runs = WouldRun(selection, _indexed_py_file(index))
    total = would_run = 0.0
    for test in history.tests.values():
        if not test.avg_duration_ms or is_go_subtest(test.test_id):
            continue
        total += test.avg_duration_ms
        if runs(test.test_id):
            would_run += test.avg_duration_ms
    return dataclasses.replace(
        selection, estimated_ms_total=total, estimated_ms_would_run=would_run
    )


# --- change links (triage) ----------------------------------------------------------------


class Strength(StrEnum):
    """How directly a change reaches a test."""

    HIGH = "high"  # the test's own file, or path mapping (its module, its Go package)
    MEDIUM = "medium"  # through an import, a declared rule or a nested build file
    LOW = "low"  # a root build/config file changed: every test depends on it

    @property
    def rank(self) -> int:
        return {Strength.LOW: 0, Strength.MEDIUM: 1, Strength.HIGH: 2}[self]


# ChangeLink.signal values.
LINK_TEST_FILE = "test file changed"
LINK_PATH = "path mapping"
LINK_PY_IMPORT = "imports changed module"
LINK_GO_IMPORT = "imports changed package"
LINK_JS_IMPORT = "imports changed module"
LINK_JAVA_USE = "uses changed class"
LINK_DECLARED = "declared dependency"
LINK_NESTED_BUILD = "nested build file"
LINK_ROOT_BUILD = "root build/config file"


@dataclass(frozen=True)
class ChangeLink:
    signal: str
    changed: str  # the changed file, module or package it goes through
    reason: str  # worded as in the selector's reasons
    strength: Strength


def change_links(
    tests: Iterable[KnownTest],
    changed_files: Iterable[str],
    config: SelectorConfig | None = None,
    affected_packages: Mapping[str, Sequence[str]] | None = None,
    affected_files: Mapping[str, Sequence[str]] | None = None,
    deleted_files: Iterable[str] = (),
) -> dict[str, tuple[ChangeLink, ...]]:
    """How a (known) change reaches each test: test_id -> links, empty if it doesn't.

    The selector's change-based signals only, matched exactly as ``select_tests`` matches them:
    the test's file, path mapping, Python and Go imports, ``[[depends]]`` rules and nested
    build files. History signals (co-change, recently failed, always-run) say nothing about
    *this* change, so they never link. A root build or CI file, for which the selector runs
    the full suite, links every test with ``Strength.LOW``. Ignorable files link nothing.
    """
    config = config or SelectorConfig()
    tests_by_id = _index(tests)
    found: dict[str, list[ChangeLink]] = {test_id: [] for test_id in tests_by_id}
    deleted = frozenset(p for p in (normalize_path(f) for f in deleted_files) if p)
    by_file = {
        path: modules
        for file, modules in (affected_files or {}).items()
        if (path := normalize_path(file))
    }
    packages = affected_packages or {}
    js_by_test: dict[str, set[str]] = defaultdict(set)  # test_id -> changed files (JS imports)
    jest_files = _JestFiles(tests_by_id.values())
    for file, modules in by_file.items():
        if file not in deleted and posixpath.splitext(file)[1] in JS_EXTENSIONS:
            for test in jest_files.tests_for(file):
                js_by_test[test.test_id].update(modules)
    # Java, by class name (triage has no build model: recorded runs keep only the edges).
    java_tests: dict[str, list[str]] = defaultdict(list)  # outer class -> test IDs
    for test in tests_by_id.values():
        if test.runner is None and "." in test.classname:
            java_tests[test.classname.split("$", 1)[0]].append(test.test_id)
    java_by_test: dict[str, set[str]] = defaultdict(set)  # test_id -> changed files it uses
    for file, modules in by_file.items():
        located = java_class_of(file)
        if file not in deleted and located is not None and located[2]:
            for test_id in java_tests.get(located[1], ()):
                java_by_test[test_id].update(modules)
    paths = sorted({p for p in (normalize_path(f) for f in changed_files) if p})
    scoped_build = {p: d for p in paths if (d := build_file_scope(p)) is not None}
    build = [p for p in paths if is_build_file(p) and p not in scoped_build]
    relevant = [p for p in paths if not is_ignorable(p, config.generated) and p not in scoped_build]
    for path in relevant:
        located = java_class_of(path)
        if located is None or (located[2] and path in deleted):
            continue
        _, cls, is_test = located
        if is_test:
            reason = f"test file changed: {path}"
            for test_id in java_tests.get(cls, ()):
                found[test_id].append(ChangeLink(LINK_TEST_FILE, path, reason, Strength.HIGH))
            continue
        package, _, simple = cls.rpartition(".")
        for name in JAVA_TEST_NAMES:
            candidate = f"{package}.{name.format(simple)}" if package else name.format(simple)
            for test_id in java_tests.get(candidate, ()):
                found[test_id].append(ChangeLink(LINK_PATH, path,
                                                 f"{candidate} tests changed {path}",
                                                 Strength.HIGH))  # fmt: skip
    py_test_files = {
        t.py_path
        for t in tests_by_id.values()
        if t.runner is Runner.PYTEST
        and not t.doctest
        and t.classname != PYTEST_COLLECTION_CLASSNAME
    }

    for path in relevant:
        if (
            path in deleted
            and path.endswith(".py")
            and (is_python_test_file(path) or path in py_test_files)
        ):
            continue  # a deleted test file: its tests are gone (as in select_tests)
        hits = _map_path(path, tests_by_id, config)
        if not hits and is_jest_test_file(path) and path not in deleted:
            # A changed Jest test file reported from its project's root (as select_tests).
            for test in jest_files.tests_for(path):
                found[test.test_id].append(ChangeLink(LINK_TEST_FILE, path,
                                                      f"test file changed: {path}",
                                                      Strength.HIGH))  # fmt: skip
        for test_id, reason in hits.items():
            test = tests_by_id[test_id]
            signal = LINK_TEST_FILE if path in (test.file_path, test.py_path) else LINK_PATH
            found[test_id].append(ChangeLink(signal, path, reason, Strength.HIGH))

    for test in tests_by_id.values():
        links = found[test.test_id]
        imported: Sequence[str] | None = None
        signal = ""
        if (
            test.runner is Runner.PYTEST
            and test.py_path
            and not test.doctest
            and test.classname != PYTEST_COLLECTION_CLASSNAME
            and test.py_path not in deleted
        ):
            imported, signal = by_file.get(test.py_path), LINK_PY_IMPORT
        elif test.runner is Runner.GO:
            imported, signal = packages.get(test.classname), LINK_GO_IMPORT
        elif test.runner is Runner.JEST and test.test_id in js_by_test:
            imported, signal = sorted(js_by_test[test.test_id]), LINK_JS_IMPORT
        elif test.test_id in java_by_test:
            imported, signal = sorted(java_by_test[test.test_id]), LINK_JAVA_USE
        if imported:
            first = sorted(imported)[0]
            more = f" (+{len(imported) - 1} more)" if len(imported) > 1 else ""
            links.append(ChangeLink(signal, first, f"{signal} {first}{more}", Strength.MEDIUM))
        for path, directory in sorted(scoped_build.items()):
            if _test_under_dir(test, directory):
                reason = f"build file {path} changed (tests under {directory}/)"
                links.append(ChangeLink(LINK_NESTED_BUILD, path, reason, Strength.MEDIUM))
        for rule in config.depends:
            triggers = [p for p in relevant if rule.matches_file(p)]
            if triggers and _test_matches_glob(test, rule.tests, config):
                reason = f"declared dependency: {rule.tests} on {_list(triggers, 1)}"
                links.append(ChangeLink(LINK_DECLARED, triggers[0], reason, Strength.MEDIUM))
        if build:
            reason = f"build/config file changed: {_list(build)}; every test depends on it"
            links.append(ChangeLink(LINK_ROOT_BUILD, build[0], reason, Strength.LOW))
    return {test_id: tuple(links) for test_id, links in found.items()}


def is_jest_test_file(path: str) -> bool:
    """Jest's default ``testMatch``: ``**/__tests__/**`` or ``*.test.*`` / ``*.spec.*``."""
    stem, ext = posixpath.splitext(posixpath.basename(path))
    if ext not in JS_EXTENSIONS:
        return False
    return stem.endswith((".test", ".spec")) or "__tests__" in path.split("/")[:-1]


def js_import_reason(
    modules: Sequence[str], nearest: JsImport | None = None, notes: Sequence[str] = ()
) -> str:
    """``imports changed module src/price.ts (via src/cart.ts) (+1 more)``, plus what the
    graph couldn't read."""
    first = nearest.module if nearest and nearest.module in modules else sorted(modules)[0]
    via = f" (via {nearest.via})" if nearest and nearest.via and nearest.module == first else ""
    more = f" (+{len(modules) - 1} more)" if len(modules) > 1 else ""
    return f"{LINK_JS_IMPORT} {first}{via}{more}" + "".join(f"; {note}" for note in notes)


class _JestFiles:
    """Known Jest tests by file. The CLI's graph names files from the repo root, but
    jest-junit reports them from the Jest project's root (``src/a.test.ts`` for
    ``packages/web/src/a.test.ts``): a known path that the repo path ends with matches too."""

    def __init__(self, tests: Iterable[_Test]) -> None:
        self.by_path: dict[str, list[_Test]] = defaultdict(list)
        for test in tests:
            if test.runner is Runner.JEST and test.file_path:
                self.by_path[test.file_path].append(test)
        self.by_name: dict[str, list[str]] = defaultdict(list)
        for path in self.by_path:
            self.by_name[posixpath.basename(path)].append(path)

    def runnable(self, repo_path: str) -> str | None:
        """A new Jest test file's path as the known tests name theirs (repo-relative, or from
        the Jest project's root), or None if that can't be told: no known Jest test's
        directory ends its directory (the deepest such match gives the project's root) or is
        an ancestor of it (paths are then repo-relative). Then the full suite runs, as before."""
        directory = posixpath.dirname(repo_path)
        prefixes: dict[int, str] = {}  # matched directory depth -> the project root before it
        ancestor = False
        for known in self.by_path:
            known_dir = posixpath.dirname(known)
            if not known_dir:
                continue
            if directory == known_dir:
                prefixes[known_dir.count("/") + 1] = ""
            elif directory.endswith("/" + known_dir):
                prefixes[known_dir.count("/") + 1] = directory[: -len(known_dir) - 1]
            elif directory.startswith(known_dir + "/"):
                ancestor = True
        if prefixes:
            prefix = prefixes[max(prefixes)]  # one string per depth: the suffix decides it
            return repo_path[len(prefix) + 1 :] if prefix else repo_path
        return repo_path if ancestor else None

    def tests_for(self, repo_path: str) -> list[_Test]:
        exact = self.by_path.get(repo_path)
        if exact:
            return exact
        return [
            test
            for known in self.by_name.get(posixpath.basename(repo_path), ())
            if repo_path.endswith("/" + known)
            for test in self.by_path[known]
        ]


def is_go_subtest(test_id: str) -> bool:
    classname, _, name = test_id.partition("::")
    return "/" in name and _looks_like_go(classname, name)


def is_build_file(path: str) -> bool:
    base = posixpath.basename(path)
    return (
        base in _BUILD_BASENAMES
        or any(fnmatch.fnmatchcase(base, glob) for glob in _BUILD_GLOBS)
        or path.startswith(_BUILD_DIRS)
    )


def build_file_scope(path: str) -> str | None:
    """The directory a nested project manifest or lockfile is scoped to.

    None for everything else, including root manifests: those affect the whole repo.
    """
    directory, base = posixpath.split(path)
    if directory and (
        base in _SCOPED_BUILD_BASENAMES
        or any(fnmatch.fnmatchcase(base, glob) for glob in _SCOPED_BUILD_GLOBS)
    ):
        return directory
    return None


def is_ignorable(path: str, generated: Sequence[str] = ()) -> bool:
    """Docs, lint-only configs and generated files: never a reason to run a test.
    ``generated`` adds globs to GENERATED_GLOBS."""
    base = posixpath.basename(path)
    stem, ext = posixpath.splitext(base)
    return (
        ext.lower() in _IGNORABLE_EXTENSIONS
        or base in _IGNORABLE_BASENAMES
        or stem in _IGNORABLE_BASENAMES  # LICENSE.txt, CHANGELOG.txt
        or any(fnmatch.fnmatchcase(base, glob) for glob in _IGNORABLE_GLOBS)
        or path.startswith(_IGNORABLE_DIRS)
        or is_generated(path, generated)
    )


def is_generated(path: str, extra: Sequence[str] = ()) -> bool:
    """A report, cache or log a test run writes (GENERATED_GLOBS, plus ``extra``)."""
    return any(glob_match(path, glob) for glob in (*GENERATED_GLOBS, *extra))


def _list(items: Sequence[str], limit: int = 3) -> str:
    shown = ", ".join(items[:limit])
    return shown if len(items) <= limit else f"{shown} (+{len(items) - limit} more)"


# --- test classification ------------------------------------------------------------------


@dataclass(frozen=True)
class _Test:
    test_id: str
    classname: str
    name: str
    file_path: str | None
    runner: Runner | None
    # Python only: path of the test module, and classes between module and function.
    py_path: str | None = None
    py_classes: tuple[str, ...] = ()
    # Python only: what to pass to pytest (``path::Class::test`` or a whole file). None for a
    # collection error or doctest whose file can't be derived: selected, but left out of
    # commands.
    py_node: str | None = None
    # A doctest (run with ``pytest --doctest-modules <file>``).
    doctest: bool = False
    # Java: the test class (outer, without $Nested) and its module, from JavaBuildInfo.
    java_class: str | None = None
    java_module: str | None = None

    @property
    def collects_source_module(self) -> bool:
        """A doctest, or a collection error in a non-test module: evidence that the repo's
        pytest imports source modules (``--doctest-modules``)."""
        if self.runner is not Runner.PYTEST or not self.py_path:
            return False
        return self.doctest or (
            self.classname == PYTEST_COLLECTION_CLASSNAME and not is_python_test_file(self.py_path)
        )


def _index(known: Iterable[KnownTest]) -> dict[str, _Test]:
    return {k.test_id: _classify(k) for k in known}


# pytest reports a module that fails to import or collect as a testcase with no classname in
# a suite named "pytest", and the module's dotted name as the test name, so its test ID is
# e.g. "pytest::rdflib.plugins.serializers.n3" (or "pytest::test.test_foo").
PYTEST_COLLECTION_CLASSNAME = "pytest"


def _classify(test: KnownTest) -> _Test:
    classname, _, name = test.test_id.partition("::")
    file_path = normalize_path(test.file_path)
    ext = posixpath.splitext(file_path or "")[1]

    if classname == PYTEST_COLLECTION_CLASSNAME:
        module = _collection_error_path(name, file_path)
        return _Test(test.test_id, classname, name, file_path, Runner.PYTEST, module, (), module)
    if _is_doctest(classname, name, ext):
        module = _collection_error_path(classname, file_path)
        return _Test(
            test.test_id, classname, name, file_path, Runner.PYTEST, module, (), module, True
        )

    python = _python_location(classname, file_path)
    if ext == ".go" or (not ext and _looks_like_go(classname, name)):
        runner: Runner | None = Runner.GO
    elif ext in JS_EXTENSIONS:
        runner = Runner.JEST
    elif python is not None:
        runner = Runner.PYTEST
    else:
        runner = None

    if runner is Runner.PYTEST and python is not None:
        py_path, py_classes = python
        node = "::".join((py_path, *py_classes, name))
        return _Test(test.test_id, classname, name, file_path, runner, py_path, py_classes, node)
    return _Test(test.test_id, classname, name, file_path, runner)


def _is_doctest(classname: str, name: str, ext: str) -> bool:
    """pytest ``--doctest-modules`` items: the classname is the module (``rdflib.container``)
    and the name is the documented object's dotted path inside it
    (``rdflib.container.Container``), or the module itself for its module docstring."""
    if not classname or ext not in ("", ".py") or _NOT_A_MODULE.search(classname):
        return False
    return name.startswith(classname + ".") or (name == classname and "." in classname)


def is_python_test_file(path: str) -> bool:
    """pytest's default ``python_files``: ``test_*.py`` or ``*_test.py``."""
    base = posixpath.basename(path)
    return base.endswith(".py") and (base.startswith("test_") or base.endswith("_test.py"))


def pytest_file(test_id: str, file_path: str | None = None) -> str | None:
    """The file pytest runs ``test_id`` from (test module, doctest module or the module of a
    collection error), or None if it isn't a pytest test or no file can be derived."""
    test = _classify(KnownTest(test_id, file_path))
    return test.py_path if test.runner is Runner.PYTEST else None


def pytest_node(test_id: str, file_path: str | None = None) -> str | None:
    """The pytest node ID that runs exactly ``test_id`` (``path::Class::test[param]``), or
    None if it isn't a single pytest item by ID (doctests, collection errors, other runners)."""
    test = _classify(KnownTest(test_id, file_path))
    node = test.py_node
    if test.runner is not Runner.PYTEST or test.doctest or not node or "::" not in node:
        return None
    return node


def locate_test(
    test_id: str, file_path: str | None = None, go_module: str | None = None
) -> tuple[str, str] | None:
    """Where a test lives, as a ``[[depends]]`` rule names it: (its directory, the rule's
    ``tests`` glob). pytest and Jest: the test's file; Go: its package directory, as
    ``<dir>/*`` (rules match Go tests as ``<package dir>/<TestName>``). None if it can't be
    derived (no file, or a Go package outside ``go_module``)."""
    test = _classify(KnownTest(test_id, file_path))
    if test.runner is Runner.GO:
        if go_module and test.classname == go_module:
            return "", "*"
        if not go_module or not test.classname.startswith(go_module + "/"):
            return None
        directory = test.classname[len(go_module) + 1 :]
        return directory, f"{directory}/*"
    path = test.py_path if test.runner is Runner.PYTEST else test.file_path
    if not path:
        return None
    return posixpath.dirname(path), path


def _collection_error_path(module: str, file_path: str | None) -> str | None:
    """The file of a dotted module name: ``a.b.c`` -> ``a/b/c.py`` (or its file_path).

    None if the name isn't a dotted module name, so no file can be derived.
    """
    if file_path and file_path.endswith(".py"):
        return file_path
    parts = module.split(".")
    if not module or not _PY_IDENTIFIER.match(parts[-1]):
        return None
    if not all(_PY_DIRECTORY.match(p) for p in parts[:-1]):
        return None
    return "/".join(parts) + ".py"


def _looks_like_go(classname: str, name: str) -> bool:
    # go-junit-report / gotestsum: classname is the package import path, name is TestX[/sub].
    return (
        "/" in classname
        and not any(c.isspace() for c in classname)
        and _GO_TOP_LEVEL.match(name) is not None
    )


def _is_python_test_module(module: str) -> bool:
    return module.startswith("test_") or module.endswith("_test")


def _python_location(classname: str, file_path: str | None) -> tuple[str, tuple[str, ...]] | None:
    """``(test module path, classes)`` for a pytest classname, or None if it isn't one.

    ``tests.unit.test_foo.TestBar`` -> ``("tests/unit/test_foo.py", ("TestBar",))``.
    """
    if not _PY_DIRECTORIES.match(classname):
        return None
    parts = classname.split(".")
    py_file = file_path if file_path and file_path.endswith(".py") else None
    module_end = max(
        (i for i, p in enumerate(parts) if _is_python_test_module(p) and _PY_IDENTIFIER.match(p)),
        default=None,
    )
    # The test module and the classes in it are Python names; only directories above may not be.
    if module_end is not None and not all(_PY_IDENTIFIER.match(p) for p in parts[module_end:]):
        return None
    if module_end is None and not all(_PY_IDENTIFIER.match(p) for p in parts):
        return None
    if module_end is None:
        if py_file is None:
            return None
        # Module name isn't recognisable; classes are the trailing CamelCase components.
        classes: list[str] = []
        for part in reversed(parts):
            if not part[:1].isupper():
                break
            classes.insert(0, part)
        return py_file, tuple(classes)
    path = py_file or "/".join(parts[: module_end + 1]) + ".py"
    return path, tuple(parts[module_end + 1 :])


# --- path mapping -------------------------------------------------------------------------


def _map_path(path: str, tests: Mapping[str, _Test], config: SelectorConfig) -> dict[str, str]:
    """test_id -> reason, for every known test this changed file maps to."""
    hits: dict[str, str] = {}
    base = posixpath.basename(path)
    stem, ext = posixpath.splitext(base)

    for t in tests.values():
        if path in (t.file_path, t.py_path):
            hits[t.test_id] = f"test file changed: {path}"

    if ext == ".py" and not _is_python_test_module(stem):
        targets = {f"test_{stem}.py", f"{stem}_test.py"}
        for t in tests.values():
            if t.py_path and posixpath.basename(t.py_path) in targets:
                hits.setdefault(t.test_id, f"{t.py_path} tests changed {path}")
    elif ext == ".go":
        directory = posixpath.dirname(path)
        for t in tests.values():
            if t.runner is Runner.GO and _go_package_matches(
                t.classname, directory, config.go_module, config.go_modules
            ):
                hits.setdefault(t.test_id, f"in Go package {t.classname}, which has changed {path}")
    elif ext in JS_EXTENSIONS and not _is_js_test_file(path):
        for t in tests.values():
            if t.file_path and _js_test_matches(t.file_path, stem):
                hits.setdefault(t.test_id, f"{t.file_path} tests changed {path}")
    return hits


def _test_under_dir(test: _Test, directory: str) -> bool:
    prefix = directory + "/"
    if any(path and path.startswith(prefix) for path in (test.file_path, test.py_path)):
        return True
    # Go: the package's import path contains the directory, e.g. a nested module
    # github.com/ipfs/kubo/docs/examples/kubo-as-a-library for docs/examples/kubo-as-a-library.
    return test.runner is Runner.GO and f"/{directory}/" in f"/{test.classname}/"


def _test_matches_glob(test: _Test, pattern: str, config: SelectorConfig) -> bool:
    """Whether a declared-dependency ``tests`` glob covers ``test``."""
    if any(path and glob_match(path, pattern) for path in (test.file_path, test.py_path)):
        return True
    if test.runner is Runner.GO:
        # "<import path>/<TestName>", matched at any path-segment suffix so that
        # "test/cli/**" works with or without go_module: .../kubo/test/cli/TestAdd.
        parts = f"{test.classname}/{test.name.split('/', 1)[0]}".split("/")
        return any(glob_match("/".join(parts[i:]), pattern) for i in range(len(parts)))
    return False


def _go_package_matches(
    package: str, directory: str, module: str | None, modules: Sequence[tuple[str, str]] = ()
) -> bool:
    located = go_package_dir(package, modules)
    if located is not None:
        return located == directory
    if module and (package == module or package.startswith(module + "/")):
        return package[len(module) :].lstrip("/") == directory
    # No module path: match the directory as an import-path suffix. May over-select a
    # same-named directory elsewhere, which is safe. Root files need go_module to map.
    return bool(directory) and (package == directory or package.endswith("/" + directory))


def _is_js_test_file(path: str) -> bool:
    name = posixpath.splitext(posixpath.basename(path))[0]
    return name.endswith((".test", ".spec")) or "__tests__" in path.split("/")


def _js_test_matches(test_path: str, stem: str) -> bool:
    name, ext = posixpath.splitext(posixpath.basename(test_path))
    if ext not in JS_EXTENSIONS:
        return False
    if name in (f"{stem}.test", f"{stem}.spec"):
        return True
    return "__tests__" in test_path.split("/") and name == stem


# --- commands -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Unit:
    """One runnable piece of a selection's commands: a pytest argument (a node or a whole file),
    a module under ``--doctest-modules``, a Go package or a Jest file. A time budget keeps or
    leaves out whole units."""

    runner: Runner
    arg: str  # the pytest node or file, the module, the Go package's import path, the Jest file
    test_ids: tuple[str, ...]  # the selected tests it runs (none: a changed file run whole)
    doctest: bool = False
    # Go: the top-level tests for -run; None = the whole package.
    go_names: tuple[str, ...] | None = None
    # Java: the module and test class it runs.
    java_module: str | None = None
    java_class: str | None = None


def _units(
    tests: Sequence[_Test],
    go_whole_packages: Collection[str] = (),
    py_whole_files: Collection[str] = (),
    py_module_files: Collection[str] = (),
    js_new_files: Collection[str] = (),
    java_new: Collection[tuple[str, str]] = (),
) -> list[_Unit]:
    """The units that run ``tests``: pytest arguments, doctest modules, Go packages, Jest files,
    each kind by path (the default order).

    Go packages in ``go_whole_packages`` run without -run, so tests that history doesn't
    know yet (added in this change) run too. Other Go packages only had tests pulled in by
    co-change, recently-failed or always-run, so they keep -run for just those tests.
    Python files in ``py_whole_files`` are likewise passed to pytest whole.
    """
    units: list[_Unit] = []
    nodes = {t.py_node for t in tests if t.runner is Runner.PYTEST and t.py_node and not t.doctest}
    nodes.update(py_whole_files)
    # A whole file (changed, imports a changed module, or a collection error) already runs
    # every test in it.
    whole_files = {node for node in nodes if "::" not in node}
    by_node: dict[str, list[str]] = {}
    for t in tests:
        if t.runner is Runner.PYTEST and not t.doctest and t.py_node:
            in_whole = t.py_path if t.py_path in whole_files else None
            by_node.setdefault(in_whole or t.py_node, []).append(t.test_id)
    for node in sorted(
        node for node in nodes if "::" not in node or node.partition("::")[0] not in whole_files
    ):
        units.append(_Unit(Runner.PYTEST, node, tuple(by_node.get(node, ()))))
    doctests = [t for t in tests if t.doctest and t.py_node]
    for file in sorted({t.py_node for t in doctests if t.py_node} | set(py_module_files)):
        ids = tuple(t.test_id for t in doctests if t.py_node == file)
        units.append(_Unit(Runner.PYTEST, file, ids, doctest=True))

    # Go: -run matches top-level tests; subtests run as part of their parent.
    go_tests: dict[str, set[str]] = {}
    go_ids: dict[str, list[str]] = {}
    for t in tests:
        if t.runner is Runner.GO:
            go_tests.setdefault(t.classname, set()).add(t.name.split("/", 1)[0])
            go_ids.setdefault(t.classname, []).append(t.test_id)
    for package in sorted(go_tests):
        names = None if package in go_whole_packages else tuple(sorted(go_tests[package]))
        units.append(_Unit(Runner.GO, package, tuple(go_ids[package]), go_names=names))

    jest_ids: dict[str, list[str]] = {file: [] for file in js_new_files}  # new: no known test
    for t in tests:
        if t.runner is Runner.JEST and t.file_path:
            jest_ids.setdefault(t.file_path, []).append(t.test_id)
    units.extend(_Unit(Runner.JEST, file, tuple(jest_ids[file])) for file in sorted(jest_ids))

    # Java: one unit per test class and module; a class with no known test (new) too.
    java_ids: dict[tuple[str, str], list[str]] = {key: [] for key in java_new}
    java_runner: dict[tuple[str, str], Runner] = {}
    for t in tests:
        if t.runner in JAVA_RUNNERS and t.java_class and t.java_module is not None:
            key = (t.java_module, t.java_class)
            java_ids.setdefault(key, []).append(t.test_id)
            java_runner[key] = t.runner
    for (module, cls), class_ids in sorted(java_ids.items()):
        # A new class has no known test to take the runner from; commands come from the
        # build's tool either way.
        runner = java_runner.get((module, cls), Runner.MAVEN)
        units.append(_Unit(runner, f"{module}::{cls}", tuple(class_ids), java_module=module,
                           java_class=cls))  # fmt: skip
    return units


def _build_commands(
    tests: Sequence[_Test],
    go_module: str | None = None,
    go_whole_packages: Collection[str] = (),
    py_whole_files: Collection[str] = (),
    py_module_files: Collection[str] = (),
    junit_dir: str | None = None,
    key: UnitKey | None = None,
) -> tuple[str, ...]:
    """One command per runner (per package for Go), for ``tests`` (see ``_units``)."""
    units = _units(tests, go_whole_packages, py_whole_files, py_module_files)
    return _commands(units, go_module, junit_dir, key)


def _commands(
    units: Sequence[_Unit],
    go_module: str | _GoLayout | None = None,
    junit_dir: str | None = None,
    key: UnitKey | None = None,
    java: JavaBuildInfo | None = None,
) -> tuple[str, ...]:
    """The commands that run ``units``: one per runner (per package for Go, per module for
    Java).

    Every argument is shell-quoted (``shlex.join``): test IDs come from uploaded JUnit, and the
    command is meant for ``eval``.

    Order: without ``key``, by path, package and test ID (the order before risk ordering).
    With ``key`` (``risk.unit_key``: from the selected tests a unit runs), every pytest
    argument, Go package and Jest file comes in key order, and so do the commands.
    """

    def best(group: Iterable[_Unit]) -> tuple[float, ...]:
        return key([i for unit in group for i in unit.test_ids]) if key else ()

    def ordered(group: Iterable[_Unit]) -> list[_Unit]:
        if key is None:
            return list(group)  # units come by path
        return sorted(group, key=lambda unit: (key(unit.test_ids), unit.arg))

    commands: list[tuple[tuple[float, ...], str]] = []  # (key, command)
    pytest_units = [u for u in units if u.runner is Runner.PYTEST and not u.doctest]
    pytest_runs = 0
    if pytest_units:
        pytest_runs += 1
        args = [u.arg for u in ordered(pytest_units)]
        command = _with_junit(shlex.join(["pytest", *args]), junit_dir, pytest_runs)
        commands.append((best(pytest_units), command))
    doctest_units = [u for u in units if u.doctest]
    if doctest_units:
        pytest_runs += 1
        files = [u.arg for u in ordered(doctest_units)]
        command = _with_junit(
            shlex.join(["pytest", "--doctest-modules", *files]), junit_dir, pytest_runs
        )
        if any(not u.test_ids for u in doctest_units):
            # A module may have no doctests: pytest then exits 5 ("no tests collected"),
            # which isn't a failure. Import errors (exit 2) and doctest failures (1) still are.
            command = f"{{ {command} || test $? -eq 5; }}"
        commands.append((best(doctest_units), command))

    for unit in ordered(u for u in units if u.runner is Runner.GO):
        target = _go_target(unit.arg, go_module)
        if unit.go_names is None:
            command = shlex.join(["go", "test", *target])
        else:
            pattern = "^(" + "|".join(unit.go_names) + ")$"
            command = shlex.join(["go", "test", *target, "-run", pattern])
        commands.append((best([unit]), command))

    jest_units = [u for u in units if u.runner is Runner.JEST]
    if jest_units:
        files = [u.arg for u in ordered(jest_units)]
        commands.append((best(jest_units), shlex.join(["jest", *files])))

    by_module: dict[str, list[_Unit]] = defaultdict(list)
    for unit in units:
        if unit.runner in JAVA_RUNNERS and unit.java_module is not None:
            by_module[unit.java_module].append(unit)
    for module, group in sorted(by_module.items()):
        if java is None:
            continue  # no build to run them with (a selection never gets here: no runner)
        classes = [u.java_class for u in ordered(group) if u.java_class]
        commands.append((best(group), java_command(java, module, classes)))
    if key is not None:
        commands.sort(key=lambda pair: pair[0])  # stable: same key keeps pytest, Go, Jest
    return tuple(command for _, command in commands)


# Surefire runs no unit test when -Dtest names no class: for a run of integration tests only.
NO_UNIT_TESTS = "siftwise.NoUnitTests"


def is_integration_test(cls: str) -> bool:
    """Failsafe's default includes: ``IT*``, ``*IT``, ``*ITCase``."""
    simple = cls.rsplit(".", 1)[-1]
    return simple.endswith(("IT", "ITCase")) or (simple.startswith("IT") and simple[2:3].isupper())


def _is_test_class(cls: str) -> bool:
    """Surefire's and Failsafe's default includes: ``Test*``, ``*Test``, ``*Tests``,
    ``*TestCase``, and the integration tests."""
    simple = cls.rsplit(".", 1)[-1]
    return (
        simple.startswith("Test")
        or simple.endswith(("Test", "Tests", "TestCase"))
        or is_integration_test(cls)
    )


def java_command(java: JavaBuildInfo, module: str, classes: Sequence[str]) -> str:
    """One module's test classes: ``./mvnw -B -pl <module> -am test -Dtest=A,B ...`` (with
    Failsafe's ``verify -Dit.test=`` for integration tests), ``./gradlew :<module>:test
    --tests A --tests B``. Maven's ``-am`` builds the modules it depends on (their tests
    filtered out by the same ``-Dtest``), so it works without an installed snapshot."""
    info = java.module(module) or JavaModuleInfo(module, module)
    if java.tool == "maven":
        argv = ["./mvnw" if java.wrapper else "mvn", "-B"]
        if module:
            argv += ["-pl", info.name, "-am"]
        units = [c for c in classes if not is_integration_test(c)]
        its = [c for c in classes if is_integration_test(c)]
        argv += ["verify" if its else "test", f"-Dtest={','.join(units) or NO_UNIT_TESTS}",
                 "-Dsurefire.failIfNoSpecifiedTests=false"]  # fmt: skip
        if its:
            argv += [f"-Dit.test={','.join(its)}", "-Dit.failIfNoSpecifiedTests=false"]
        return shlex.join(argv)
    task = f"{info.name}:test" if info.name else ":test"
    tests = [a for c in classes for a in ("--tests", c)]
    return shlex.join(["./gradlew" if java.wrapper else "gradle", task, *tests])


def _java_full_command(java: JavaBuildInfo, tests: Mapping[str, _Test]) -> tuple[str, ...]:
    """The whole Java suite: ``mvn -B test`` (``verify`` if history has integration tests,
    so Failsafe runs), ``gradle test``."""
    if java.tool == "maven":
        its = any(t.java_class and is_integration_test(t.java_class) for t in tests.values())
        return (
            shlex.join(["./mvnw" if java.wrapper else "mvn", "-B", "verify" if its else "test"]),
        )
    return (shlex.join(["./gradlew" if java.wrapper else "gradle", "test"]),)


def java_class_of(path: str) -> tuple[str, str, bool] | None:
    """``(module directory, class, is a test)`` of a Java file under a conventional source
    root: ``core/src/test/java/a/b/FooTest.java`` -> ``("core", "a.b.FooTest", True)``."""
    if not path.endswith(".java"):
        return None
    for root, test in (("src/test/java", True), ("src/main/java", False)):
        if path.startswith(root + "/"):
            module, rest = "", path[len(root) + 1 :]
        elif f"/{root}/" in path:
            module, _, rest = path.partition(f"/{root}/")
        else:
            continue
        return module, rest[: -len(".java")].replace("/", "."), test
    return None


_OTHER_JVM_SOURCES = (".kt", ".groovy", ".scala")
# Names a main class's tests go by, after Surefire's and Failsafe's default includes.
JAVA_TEST_NAMES = ("{}Test", "{}Tests", "Test{}", "{}TestCase", "{}IT", "{}ITCase")


def _java_problem(paths: Iterable[str], java: JavaBuildInfo) -> str | None:
    """Why a change needs the full suite as far as Java goes, or None."""
    for path in paths:
        ext = posixpath.splitext(path)[1]
        if ext in _OTHER_JVM_SOURCES:
            return f"Kotlin, Groovy or Scala source changed (not analysed): {path}"
        module = java.module_of(path)
        if module is not None and path.startswith(module.prefix("src/main/resources")):
            return (f"main resources changed: {path} (code anywhere may load them at runtime, "
                    "which no graph sees)")  # fmt: skip
        if ext != ".java":
            continue
        if java.unsupported:
            return f"the Java build couldn't be read ({java.unsupported}): {path}"
        located = java_class_of(path)
        if module is None or located is None or located[0] != module.path:
            return (
                f"Java file outside the build's source roots (src/main/java, src/test/java): {path}"
            )
        if module.unanalysed:
            where = module.name if module.path else "the root project"
            return f"{where} isn't analysed ({module.unanalysed[0]}): {path}"
    return None


def _java_path_hits(
    path: str,
    located: tuple[str, str, bool],
    java: JavaBuildInfo,
    java_classes: Mapping[str, Sequence[_Test]],
    deleted: bool,
) -> tuple[dict[str, str], set[tuple[str, str]]]:
    """A changed Java file's tests by name: (test_id -> reason, new classes to run). A test
    class: its own tests (or, new, itself). A main class ``a.b.Foo``: ``a.b.FooTest``,
    ``FooTests``, ``TestFoo``, ``FooIT``... in its module's tests, else in any module's."""
    module, cls, is_test = located
    hits: dict[str, str] = {}
    new: set[tuple[str, str]] = set()
    if is_test:
        if deleted:
            return hits, new
        for test in java_classes.get(cls, ()):
            hits[test.test_id] = f"test file changed: {path}"
        if not hits and _is_test_class(cls) and java.test_classes.get(cls) == module:
            new.add((module, cls))
        return hits, new
    package, _, simple = cls.rpartition(".")
    names = [f"{package}.{n.format(simple)}" if package else n.format(simple)
             for n in JAVA_TEST_NAMES]  # fmt: skip
    candidates = [c for c in names if java.test_classes.get(c) == module] or [
        c for c in names if c in java.test_classes
    ]
    for candidate in candidates:
        known = java_classes.get(candidate, ())
        for test in known:
            hits[test.test_id] = f"{candidate} tests changed {path}"
        if not known:
            new.add((java.test_classes[candidate], candidate))
    return hits, new


def java_use_reason(
    modules: Sequence[str], nearest: JsImport | None = None, notes: Sequence[str] = ()
) -> str:
    """``uses changed class a.b.Price (via a.b.Cart) (+1 more)``, plus what the graph
    couldn't read."""

    def name(file: str) -> str:
        located = java_class_of(file)
        return located[1] if located else file

    first = nearest.module if nearest and nearest.module in modules else sorted(modules)[0]
    via = (
        f" (via {name(nearest.via)})" if nearest and nearest.via and nearest.module == first else ""
    )
    more = f" (+{len(modules) - 1} more)" if len(modules) > 1 else ""
    return f"{LINK_JAVA_USE} {name(first)}{via}{more}" + "".join(f"; {n}" for n in notes)


def _with_java(tests: Mapping[str, _Test], java: JavaBuildInfo) -> dict[str, _Test]:
    """Java tests get the build's runner and their module: a known test whose class (the
    outer one, for a ``$Nested`` class) is one of the checkout's test classes."""
    found = {}
    for test_id, test in tests.items():
        if test.runner is None:
            cls = test.classname.split("$", 1)[0]
            module = java.test_classes.get(cls)
            if module is not None:
                test = dataclasses.replace(
                    test, runner=java.runner, java_class=cls, java_module=module
                )
        found[test_id] = test
    return found


def _with_junit(command: str, junit_dir: str | None, n: int) -> str:
    """``pytest ...`` -> ``pytest --junitxml=<junit_dir>/siftwise-<n>.xml ...``."""
    if not junit_dir:
        return command
    report = posixpath.join(junit_dir.rstrip("/"), f"siftwise-{n}.xml")
    return command.replace("pytest", shlex.join(["pytest", f"--junitxml={report}"]), 1)


@dataclass(frozen=True)
class _GoLayout:
    """The root's module path and every module of the checkout (directory, module path)."""

    module: str | None
    modules: tuple[tuple[str, str], ...] = ()

    @classmethod
    def of(cls, config: SelectorConfig) -> _GoLayout:
        return cls(config.go_module, config.go_modules)


def go_module_of(package: str, modules: Sequence[tuple[str, str]]) -> tuple[str, str] | None:
    """(directory, module path) of the module a Go package belongs to: the longest module path
    it is, or starts with plus "/". None if no module of the checkout has it."""
    found = [(d, m) for d, m in modules if package == m or package.startswith(m + "/")]
    return max(found, key=lambda dm: len(dm[1])) if found else None


def go_package_dir(package: str, modules: Sequence[tuple[str, str]]) -> str | None:
    """A Go package's repo-relative directory, through its module; None if unknown."""
    found = go_module_of(package, modules)
    if found is None:
        return None
    directory, module = found
    return posixpath.join(directory, package[len(module) :].lstrip("/")).strip("/")


def _go_target(package: str, layout: str | _GoLayout | None) -> list[str]:
    """``go test``'s arguments for a package: ``./dir`` from the root's module, ``-C <module
    dir> ./dir`` for another module of the checkout, else the import path."""
    module = layout.module if isinstance(layout, _GoLayout) else layout
    modules = layout.modules if isinstance(layout, _GoLayout) else ()
    found = go_module_of(package, modules)
    if found is not None and found[0]:
        directory, path = found
        rest = package[len(path) :].lstrip("/")
        return ["-C", directory, f"./{rest}" if rest else "."]
    if found is not None:
        module = found[1]
    if module and package == module:
        return ["."]
    if module and package.startswith(module + "/"):
        return ["./" + package[len(module) + 1 :]]
    return [package]  # import paths work with `go test` from inside the module


# --- history from the database ------------------------------------------------------------


# The repo's runs, with the columns recent_ci_runs_sql() expects.
_REPO_RUNS = """
repo_runs AS (
    SELECT id, ci_run_id, run_attempt, commit_sha, is_main, changed_files_known,
           COALESCE(started_at, created_at) AS run_at
    FROM runs WHERE repo_id = :repo_id
)
"""


def recent_ci_runs_sql(source: str, condition: str = "true") -> str:
    """CTEs ``recent_ci`` and ``recent``: every run of the latest ``:n`` *CI runs* in ``source``.

    A CI run is ``(ci_run_id, run_attempt)``: all its matrix variants (one upload each) count
    once, so "the last 50 runs" means 50 CI runs whether the matrix has 1 leg or 20. Runs
    without a ``ci_run_id`` count on their own. ``source`` must have ``id, ci_run_id,
    run_attempt, commit_sha, run_at``; ``condition`` filters it (e.g. ``is_main``).
    ``recent`` has ``id, commit_sha, run_at``. ``condition`` may only use ``source`` columns
    that ``recent_ci`` doesn't have (e.g. ``is_main``), so they need no qualifier.
    """
    return f"""
recent_ci AS (
    SELECT COALESCE(ci_run_id, 'run:' || id) AS ci_key, run_attempt,
           max(run_at) AS ci_at, max(id) AS ci_id
    FROM {source} WHERE {condition}
    GROUP BY 1, 2
    ORDER BY ci_at DESC, ci_id DESC
    LIMIT :n
),
recent AS (
    SELECT s.id, s.commit_sha, s.run_at
    FROM {source} s
    JOIN recent_ci c
      ON COALESCE(s.ci_run_id, 'run:' || s.id) = c.ci_key AND s.run_attempt = c.run_attempt
    WHERE {condition}
)
"""


def load_history(
    session: Session, repo: str, config: SelectorConfig | None = None
) -> RepoHistory | None:
    """The selector's view of ``repo``'s history, or None if the repo is unknown.

    Never reads passing raw results, so its cost doesn't grow with the size of the test suite
    or the matrix: known tests come from ``test_stats`` (its last-seen run, kept by the
    rollup), and the failure queries go through the partial index on failed results. Known
    tests are therefore as fresh as the rollup (stale between a deferred batch ingest and
    ``POST /repos/{repo}/rollup``).
    """
    from sqlalchemy import text

    config = config or SelectorConfig()
    repo_id = session.execute(
        text("SELECT id FROM repos WHERE name = :name"), {"name": repo}
    ).scalar_one_or_none()
    if repo_id is None:
        return None
    params = {"repo_id": repo_id}

    # Tests that appeared in one of the latest N CI runs: their last-seen run is in one of them
    # (core/history.py orders "last seen" by the same CI-run order as recent_ci_runs_sql).
    known = session.execute(
        text(
            f"""
            WITH {_REPO_RUNS}, {recent_ci_runs_sql("repo_runs")}
            SELECT ts.test_id, ts.file_path, ts.avg_duration_ms, ts.runs, ts.failures,
                   ts.last_failed_at, ts.flaky_score
            FROM test_stats ts JOIN recent ON recent.id = ts.last_seen_run_id
            WHERE ts.repo_id = :repo_id
            """
        ),
        {**params, "n": config.known_test_runs},
    ).all()

    broken = session.execute(
        text(
            "SELECT test_id, broken_on_main_since_sha FROM test_stats "
            "WHERE repo_id = :repo_id AND broken_on_main_since_sha IS NOT NULL"
        ),
        params,
    ).all()

    recent_failures = session.execute(
        text(
            f"""
            WITH {_REPO_RUNS}, {recent_ci_runs_sql("repo_runs", "is_main")}
            SELECT DISTINCT ON (t.test_id) t.test_id, recent.id, recent.commit_sha
            FROM recent JOIN test_results tr ON tr.run_id = recent.id
            JOIN tests t ON t.id = tr.test_key
            WHERE tr.status IN {FAILED_CODES_SQL}
            ORDER BY t.test_id, recent.run_at DESC, recent.id DESC
            """
        ),
        {**params, "n": config.recent_main_runs},
    ).all()

    failed = session.execute(
        text(
            f"""
            WITH {_REPO_RUNS}, {recent_ci_runs_sql("repo_runs", "changed_files_known")}
            SELECT recent.id, recent.commit_sha, array_agg(DISTINCT t.test_id)
            FROM recent JOIN test_results tr ON tr.run_id = recent.id
            JOIN tests t ON t.id = tr.test_key
            WHERE tr.status IN {FAILED_CODES_SQL}
            GROUP BY recent.id, recent.commit_sha, recent.run_at
            ORDER BY recent.run_at DESC, recent.id DESC
            """
        ),
        {**params, "n": config.co_change_runs},
    ).all()
    changed: dict[int, set[str]] = {}
    if failed:
        for run_id, path in session.execute(
            text("SELECT run_id, path FROM changed_files WHERE run_id = ANY(:ids)"),
            {"ids": [row[0] for row in failed]},
        ).all():
            changed.setdefault(run_id, set()).add(path)

    newest_run_at = session.execute(
        text("SELECT max(COALESCE(started_at, created_at)) FROM runs WHERE repo_id = :repo_id"),
        params,
    ).scalar_one_or_none()

    return RepoHistory(
        tests={
            row.test_id: KnownTest(
                row.test_id,
                row.file_path,
                row.avg_duration_ms,
                row.runs,
                row.failures,
                row.last_failed_at,
                row.flaky_score,
            )
            for row in known
        },
        broken_on_main=dict(broken),
        recent_main_failures={test_id: (run_id, sha) for test_id, run_id, sha in recent_failures},
        failed_runs=tuple(
            FailedRun(run_id, sha, frozenset(changed.get(run_id, ())), frozenset(test_ids))
            for run_id, sha, test_ids in failed
        ),
        newest_run_at=newest_run_at,
    )


def select_for_repo(
    session: Session,
    repo: str,
    changed_files: Iterable[str],
    changed_files_known: bool,
    config: SelectorConfig | None = None,
) -> Selection:
    return select_tests(
        load_history(session, repo, config), changed_files, changed_files_known, config
    )
