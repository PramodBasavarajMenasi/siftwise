"""Test fixtures (``fixtures`` in .siftwise.toml): which tests use a changed fixture file.

A fixture is test data: a sample project whose ``pom.xml``, ``build.gradle.kts``,
``tsconfig.json`` or ``go.mod`` isn't the repo's build. A changed file under a ``fixtures``
glob doesn't force the full suite; the server selects the test files listed here for it
(``/select``'s ``fixture_refs``), plus any ``[[depends]]`` rule on it, and runs the full suite
if that finds nothing.

**The fixture unit.** For a changed file, each matching ``fixtures`` glob gives a root: its
leading literal segments (``tests/fixtures`` for ``tests/fixtures/**``; for a glob starting
with ``**``, the path up to its first literal run, e.g. ``.../fixtures`` for
``**/fixtures/**``). The unit is the root's next path component: the fixture directory
(``tests/fixtures/java_gradle``) or, for a file directly in the root, that file
(``tests/fixtures/pytest.xml``). A glob naming the file itself makes the file its own unit.

**The match rule.** A file references a unit when its text contains the unit's name (the
unit's last component, ``java_gradle`` or ``pytest.xml``) as a token: not preceded by a
letter, digit, ``_``, ``.`` or ``-``, and not followed by a letter, digit, ``_`` or ``-``.
So ``FIXTURES / "java_gradle"``, ``"tests/fixtures/java_gradle"``, ``os.path.join(d,
"java_gradle")`` and ``fixture("pytest.xml")`` all match; ``java_gradle2`` doesn't. Read:
every ``.py`` file, Jest test files, Go ``_test.go`` files and Java files under
``src/test/``, outside the fixtures themselves and the directories pytest skips. Then:

- a test file that matches references the fixture;
- a test helper that matches (a ``conftest.py``, or a non-test ``.py`` file in a ``tests``,
  ``test`` or ``testing`` directory: shared paths, another test module's constants) makes
  every Python test file importing it, directly or not, reference it too (the Python import
  graph). The project's own source isn't a helper: it doesn't read test fixtures, and a
  common name there (``gitlab``) would select every test importing it.

It errs towards more tests: a name used anywhere counts, whatever it's joined with. What it
can't see: a name built at run time (``f"java_{kind}"``), a glob over the fixture root, or a
helper in another language. Declare those with ``[[depends]]``.

**Real modules keep today's behaviour.** A file under ``fixtures`` that the project's own
build includes (a module of the root Maven/Gradle build, a ``go.work`` ``use`` directory, a
package of the root JS workspace, a ``.go`` file of a Go module that isn't the fixture's
own) is left out: build files there still force the full suite. An unreadable root Java
build leaves every fixture file out.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from siftwise.cli import gograph, javagraph, jsgraph
from siftwise.cli.pygraph import _skipped_dir, build_py_graph
from siftwise.cli.repoconfig import RepoConfig
from siftwise.core.selector import (
    glob_match,
    is_ignorable,
    is_jest_test_file,
    is_python_test_file,
)

_WILDCARD = re.compile(r"[*?\[]")


def fixture_units(path: str, globs: Sequence[str]) -> list[str]:
    """The fixture units ``path`` belongs to, one per matching ``fixtures`` glob."""
    parts = path.split("/")
    units: set[str] = set()
    for glob in globs:
        if glob.startswith("!") or not glob_match(path, glob):
            continue
        segments = glob.split("/")
        literal = []
        for segment in segments:
            if _WILDCARD.search(segment):
                break
            literal.append(segment)
        if literal:
            root = len(literal)
        else:  # "**/fixtures/**": the path up to the first literal run after the wildcards
            run: list[str] = []
            for segment in segments:
                if _WILDCARD.search(segment):
                    if run:
                        break
                    continue
                run.append(segment)
            found = next((i for i in range(len(parts)) if parts[i : i + len(run)] == run), None)
            root = found + len(run) if run and found is not None else 0
        units.add("/".join(parts[: min(root + 1, len(parts))]))
    return sorted(units)


def _token(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_.\-]){re.escape(name)}(?![A-Za-z0-9_\-])")


def _scanned(path: str) -> bool:
    return (
        (path.endswith(".py") and (is_python_test_file(path) or _is_helper(path)))
        or is_jest_test_file(path)
        or path.endswith("_test.go")
        or (path.endswith(".java") and (path.startswith("src/test/") or "/src/test/" in path))
    )


def _is_test(path: str) -> bool:
    """A scanned file that is a test file."""
    return is_python_test_file(path) if path.endswith(".py") else _scanned(path)


HELPER_DIRS = frozenset({"tests", "test", "testing"})


def _is_helper(path: str) -> bool:
    """A Python test helper: ``conftest.py``, or a non-test module in a test directory."""
    parts = path.split("/")
    return path.endswith(".py") and not is_python_test_file(path) and (
        parts[-1] == "conftest.py" or bool(HELPER_DIRS.intersection(parts[:-1])))  # fmt: skip


def _files(root: Path, config: RepoConfig) -> list[str]:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        directory = Path(dirpath)
        dirnames[:] = [
            d for d in dirnames
            if not _skipped_dir(directory / d) and d not in javagraph.SKIP_DIRS
        ]  # fmt: skip
        rel = directory.relative_to(root).as_posix()
        for name in filenames:
            path = name if rel == "." else f"{rel}/{name}"
            if _scanned(path) and not config.is_fixture(path):
                files.append(path)
    return sorted(files)


class BuildModel:
    """The project's own build, read once: which paths it includes."""

    def __init__(self, root: Path, config: RepoConfig) -> None:
        self.root = root
        self.config = config
        self.java = javagraph.detect_build(root)
        self.go_uses = [u for u in gograph._work_uses(root) if u]
        self.workspace = _workspace_patterns(root)
        self._go_modules: dict[str, str] | None = None

    def member(self, path: str) -> str | None:
        """Why ``path`` belongs to the project's build (then it isn't a fixture), or None."""
        if self.java is not None:
            if self.java.unsupported:
                return f"the root Java build can't be read ({self.java.unsupported})"
            for module in self.java.modules:
                if module.path and _under(path, module.path):
                    return f"module {module.path} of the root {self.java.tool} build"
        for use in self.go_uses:
            if _under(path, use):
                return f"go.work use {use}"
        if path.endswith(".go") and not any(
            part == "testdata" or part.startswith(("_", ".")) for part in path.split("/")[:-1]
        ):
            if self._go_modules is None:
                self._go_modules = gograph.find_go_modules(self.root)
            modules = self._go_modules
            owner = max((d for d in modules if d == "" or _under(path, d)), key=len, default=None)
            if owner is not None and not self.config.is_fixture(posixpath.join(owner, "go.mod")):
                return f"a package of the Go module {modules[owner]}"
        package = self._workspace_package(path)
        if package:
            return f"package {package} of the root JS workspace"
        return None

    def _workspace_package(self, path: str) -> str | None:
        include, exclude = self.workspace
        directory = posixpath.dirname(path)
        while include and directory:
            if (
                (self.root / directory / "package.json").is_file()
                and any(glob_match(directory, p) for p in include)
                and not any(glob_match(directory, p) for p in exclude)
            ):
                return directory
            directory = posixpath.dirname(directory)
        return None


def _under(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory.rstrip("/") + "/")


def _workspace_patterns(root: Path) -> tuple[list[str], list[str]]:
    """The root JS workspace's package globs: (include, exclude)."""
    patterns: list[str] = []
    manifest = _json(root / "package.json")
    if isinstance(manifest, dict):
        workspaces = manifest.get("workspaces")
        if isinstance(workspaces, dict):
            workspaces = workspaces.get("packages")
        if isinstance(workspaces, list):
            patterns += [str(w) for w in workspaces]
    if (root / "pnpm-workspace.yaml").is_file():
        patterns += jsgraph._pnpm_packages(root / "pnpm-workspace.yaml")
    lerna = _json(root / "lerna.json")
    if isinstance(lerna, dict) and isinstance(lerna.get("packages"), list):
        patterns += [str(p) for p in lerna["packages"]]
    include = [p.strip("/").removeprefix("./") for p in patterns if not p.startswith("!")]
    exclude = [p[1:].strip("/").removeprefix("./") for p in patterns if p.startswith("!")]
    return include, exclude


def _json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def fixture_refs(root: Path, changed: Iterable[str], config: RepoConfig) -> dict[str, list[str]]:
    """Changed fixture file -> the test files referencing its fixture (possibly none: the
    server then runs the full suite). Files the project's build includes are left out, as
    are ignorable ones (docs)."""
    candidates = sorted({
        p for p in changed if config.is_fixture(p) and not is_ignorable(p, config.generated)
    })  # fmt: skip
    if not candidates:
        return {}
    build = BuildModel(root, config)
    kept: dict[str, list[str]] = {}
    for path in candidates:
        member = build.member(path)
        if member:
            _err(f"siftwise: fixtures: {path} is in {member}: handled as a build file")
            continue
        kept[path] = fixture_units(path, config.fixtures)
    if not kept:
        return {}
    texts = _read(root, _files(root, config))
    names = {posixpath.basename(u) for units in kept.values() for u in units}
    matching = {name: {f for f, text in texts.items() if _token(name).search(text)}
                for name in names}  # fmt: skip
    refs: dict[str, list[str]] = {}
    graph = None
    for path, units in kept.items():
        found = set().union(*(matching[posixpath.basename(u)] for u in units))
        tests = {f for f in found if _is_test(f)}
        helpers = sorted(f for f in found if _is_helper(f))
        if helpers:
            if graph is None:
                try:
                    graph = build_py_graph(root)
                except (OSError, ValueError, RecursionError) as exc:
                    _err(f"warning: fixtures: no Python import graph ({exc}): {path} is "
                         "handled as a build file")  # fmt: skip
                    continue
            tests |= set(graph.affected(helpers))
        refs[path] = sorted(tests)
    used = sum(1 for files in refs.values() if files)
    _err(f"siftwise: fixtures: {len(refs)} changed fixture file(s), {used} referenced by tests")
    return refs


def _read(root: Path, files: Iterable[str]) -> Mapping[str, str]:
    texts = {}
    for path in files:
        try:
            texts[path] = (root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return texts


def _err(message: str) -> None:
    print(message, file=sys.stderr)
