"""Static Python import graph of a checkout, for selecting tests that import changed code.

``siftwise select`` builds it at the repo root (like ``go list`` for Go) and sends
``PyGraph.affected(changed_files)`` as ``affected_files``: every test file that imports a
changed module, directly or transitively. The eval also uses it to explain misses.

Edges are file -> files it imports (repo-relative paths). ``import a.b.c`` also loads ``a`` and
``a.b``, so those packages' ``__init__.py`` files are edges too. A test file is also treated
as importing every ``conftest.py`` in its directory and above, since pytest loads them.
A file's module names are its path from the repo root and from every package root above it
(a directory with ``pyproject.toml``, ``setup.py`` or ``setup.cfg``, and that root's ``src/``),
so ``import cirq`` in a monorepo reaches ``cirq-core/cirq/__init__.py``. A name more than one
file has (two packages' ``tests/helpers.py``) links to all of them.
Imports are read statically with ``ast``. A string literal that is exactly the dotted name of
a module in the repo (``"pkg.plugins.json"``) also counts as importing it, since plugin
registries and ``importlib.import_module`` load modules by name. Other dynamic imports (names
built at runtime, entry points) are invisible; the selector's other signals and fallbacks
still apply.
"""

from __future__ import annotations

import ast
import os
import posixpath
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from siftwise.core.selector import is_python_test_file

# A directory holding one of these is a package root: its contents import from there.
PACKAGE_MANIFESTS = frozenset({"pyproject.toml", "setup.py", "setup.cfg"})

SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", ".tox", ".nox", "build", "dist", "node_modules", ".eggs",
     "site-packages", "__pycache__", ".mypy_cache", ".pytest_cache"}
)  # fmt: skip


@dataclass(frozen=True)
class PyGraph:
    edges: Mapping[str, frozenset[str]]

    def import_chain(self, test_file: str, targets: set[str]) -> list[str] | None:
        """Shortest import chain from ``test_file`` (plus its conftests) to any target."""
        if test_file not in self.edges:
            return None  # unknown (e.g. deleted) test file: nothing to claim
        starts = [test_file, *conftests_for(test_file, self.edges)]
        parent: dict[str, str | None] = {s: None for s in starts if s in self.edges}
        queue = deque(parent)
        while queue:
            current = queue.popleft()
            if current in targets:
                chain = [current]
                while (previous := parent[chain[-1]]) is not None:
                    chain.append(previous)
                return chain[::-1]
            for nxt in self.edges.get(current, ()):
                if nxt not in parent:
                    parent[nxt] = current
                    queue.append(nxt)
        return None

    def affected(self, changed_files: Iterable[str]) -> dict[str, list[str]]:
        """Test file -> the changed modules it imports (itself excluded), for every test file
        that imports one: directly, transitively, through a parent package's ``__init__.py``
        or through a ``conftest.py`` pytest loads for it."""
        importers: dict[str, set[str]] = defaultdict(set)
        for file, imports in self.edges.items():
            for imported in imports:
                importers[imported].add(file)
        reached_by: dict[str, set[str]] = defaultdict(set)  # file -> changed modules it reaches
        for changed in sorted({p for p in changed_files if p in self.edges}):
            seen = {changed}
            queue = deque(seen)
            while queue:
                for importer in importers.get(queue.popleft(), ()):
                    if importer not in seen:
                        seen.add(importer)
                        queue.append(importer)
            for file in seen:
                reached_by[file].add(changed)
        affected = {}
        for file in sorted(self.edges):
            if not is_python_test_file(file):
                continue
            modules = set(reached_by.get(file, ()))
            for conftest in conftests_for(file, self.edges):
                modules |= reached_by.get(conftest, set())
            modules.discard(file)
            if modules:
                affected[file] = sorted(modules)
        return affected

    def to_json(self) -> dict[str, Any]:
        return {"edges": {f: sorted(e) for f, e in self.edges.items()}}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> PyGraph:
        return cls({f: frozenset(e) for f, e in data["edges"].items()})


def conftests_for(test_file: str, edges: Mapping[str, Any]) -> list[str]:
    directory = posixpath.dirname(test_file)
    found = []
    while True:
        candidate = posixpath.join(directory, "conftest.py") if directory else "conftest.py"
        if candidate in edges and candidate != test_file:
            found.append(candidate)
        if not directory:
            return found
        directory = posixpath.dirname(directory)


def module_of_test(test_id: str) -> str | None:
    """A pytest test ID's module: ``test.test_graph.TestX::t`` -> ``test/test_graph.py``."""
    parts = test_id.partition("::")[0].split(".")
    modules = [i for i, p in enumerate(parts) if p.startswith("test_") or p.endswith("_test")]
    if not modules:
        return None
    return "/".join(parts[: modules[-1] + 1]) + ".py"


def _skipped_dir(path: Path) -> bool:
    """Directories pytest never collects from (its default ``norecursedirs``: dot-dirs,
    ``*.egg``, virtualenvs), plus build and tool output. Pruned while walking: descending into
    a ``.venv`` or a cache first and filtering after took 14 s on one checkout."""
    name = path.name
    return (
        name in SKIP_DIRS
        or name.startswith(".")
        or name.endswith(".egg")
        or (path / "pyvenv.cfg").is_file()
    )


def _python_files(root: Path) -> tuple[list[str], list[str]]:
    """``(.py files, package roots)`` under ``root``, as sorted repo-relative paths."""
    files: list[str] = []
    package_roots: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        directory = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not _skipped_dir(directory / d)]
        rel = directory.relative_to(root)
        files.extend((rel / name).as_posix() for name in filenames if name.endswith(".py"))
        if rel.parts and PACKAGE_MANIFESTS.intersection(filenames):
            package_roots.add(rel.as_posix())
    return sorted(files), sorted(package_roots)


def build_py_graph(root: Path) -> PyGraph:
    files, package_roots = _python_files(root)

    modules: dict[str, set[str]] = defaultdict(set)  # dotted module name -> files
    for file in files:
        for name in _module_names(file, package_roots):
            modules[name].add(file)

    edges: dict[str, frozenset[str]] = {}
    for file in files:
        try:
            tree = ast.parse((root / file).read_bytes(), filename=file)
        except (SyntaxError, ValueError, RecursionError, OSError):
            edges[file] = frozenset()  # unreadable or unparsable: no edges
            continue
        targets: set[str] = set()
        package = _package_of(file)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    targets.update(_resolve(alias.name, modules))
            elif isinstance(node, ast.ImportFrom):
                base = _absolute(node.module, node.level, package)
                if base is None:
                    continue
                targets.update(_resolve(base, modules))
                for alias in node.names:  # `from pkg import submodule`
                    if alias.name != "*":
                        targets.update(_resolve(f"{base}.{alias.name}" if base else alias.name,
                                                modules, ancestors=False))  # fmt: skip
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "." in node.value
                and node.value in modules
            ):
                # A dotted module name in a string: a plugin registry or importlib call
                # loads it at runtime (rdflib registers its parsers/serializers this way).
                targets.update(modules[node.value])
        targets.discard(file)
        edges[file] = frozenset(targets)
    return PyGraph(edges)


def _module_names(file: str, package_roots: Iterable[str] = ()) -> Iterable[str]:
    """The dotted names ``file`` can be imported as: from the repo root (and its ``src/`` or
    ``lib/``), and from each package root above it (and that root's ``src/``)."""
    parts = file[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return []
    names = [".".join(parts)]
    if parts[0] in ("src", "lib") and len(parts) > 1:  # src layout
        names.append(".".join(parts[1:]))
    for package_root in package_roots:
        prefix = package_root.split("/")
        if parts[: len(prefix)] != prefix or len(parts) == len(prefix):
            continue
        inside = parts[len(prefix) :]
        names.append(".".join(inside))
        if inside[0] == "src" and len(inside) > 1:
            names.append(".".join(inside[1:]))
    return names


def _package_of(file: str) -> str:
    parts = file[: -len(".py")].split("/")
    package = parts[:-1]  # a module's package; __init__.py's package is its directory
    if parts[0] in ("src", "lib") and len(package) > 1:
        package = package[1:]
    return ".".join(package)


def _absolute(module: str | None, level: int, package: str) -> str | None:
    if level == 0:
        return module
    pieces = package.split(".") if package else []
    if level - 1 > len(pieces):
        return None
    base = pieces[: len(pieces) - (level - 1)]
    if module:
        base.append(module)
    return ".".join(base)


def _resolve(name: str, modules: Mapping[str, set[str]], ancestors: bool = True) -> set[str]:
    """Files loaded by importing ``name``: the module and (optionally) its parent packages."""
    found: set[str] = set()
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        files = modules.get(".".join(parts[:i]))
        if files:
            found.update(files)
            if not ancestors:
                break
        elif not ancestors and i == len(parts):
            break
    return found
