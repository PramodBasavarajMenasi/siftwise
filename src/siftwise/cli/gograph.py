"""Go import graph from ``go list -deps -test -json ./...``, for selecting dependent tests.

For every package of the checkout's modules, ``GoGraph.deps`` holds the checkout's packages
its test binary is built from: the package itself plus everything its tests (in-package and
``_test`` package) import, transitively. ``go list`` reports ``Deps`` transitively already,
so a package is affected by a change exactly when its deps include a changed package.

A checkout may hold several modules: a ``go.work`` workspace, or ``go.mod`` files below the
root. ``go list ./...`` never crosses into a nested module (not even in a workspace), so it
runs once per module, in the module's directory (``find_go_modules``), and the graphs are
merged (``merge_graphs``). A package of another module of the checkout (a ``replace`` to a
local directory, or a workspace module) counts as the checkout's own: a change there
affects the tests that import it.

``go list`` runs with ``-mod=mod`` turned into ``-mod=readonly`` (``readonly_goflags``), so
selecting tests never edits go.mod or go.sum; ``-mod=vendor`` is kept.
"""

from __future__ import annotations

import json
import os
import posixpath
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from siftwise.cli.repoconfig import read_go_module

Package = dict[str, Any]
# Directories Go itself ignores for ./... (plus node_modules), never a module of the checkout.
_SKIP = frozenset({"vendor", "testdata", "node_modules"})


@dataclass(frozen=True)
class GoGraph:
    module: str | None
    # repo-relative package directory ("" for the module root) -> import path
    dirs: Mapping[str, str]
    # testable import path -> main-module import paths its test build depends on
    deps: Mapping[str, frozenset[str]]

    def changed_packages(self, changed_files: Iterable[str]) -> set[str]:
        """Packages with changed non-test ``.go`` files (test files can't be imported)."""
        return {
            self.dirs[posixpath.dirname(path)]
            for path in changed_files
            if path.endswith(".go")
            and not path.endswith("_test.go")
            and posixpath.dirname(path) in self.dirs
        }

    def affected(self, changed_files: Iterable[str]) -> dict[str, list[str]]:
        """Package -> the changed packages its tests depend on, for every affected package."""
        changed = self.changed_packages(changed_files)
        if not changed:
            return {}
        return {
            package: sorted(deps & changed)
            for package, deps in sorted(self.deps.items())
            if deps & changed
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "dirs": dict(self.dirs),
            "deps": {pkg: sorted(deps) for pkg, deps in self.deps.items()},
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> GoGraph:
        return cls(
            module=data["module"],
            dirs=dict(data["dirs"]),
            deps={pkg: frozenset(deps) for pkg, deps in data["deps"].items()},
        )


def find_go_modules(root: Path) -> dict[str, str]:
    """Repo-relative directory ("" for the root) -> module path, for every Go module in the
    checkout: go.mod files (outside the directories Go ignores: vendor, testdata, and names
    starting with ``.`` or ``_``) and go.work's ``use`` directories inside the checkout."""
    found: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in _SKIP and not d.startswith((".", "_")))  # fmt: skip
        if "go.mod" in filenames:
            module = read_go_module(Path(dirpath))
            rel = _relative_dir(dirpath, root)
            if module and rel is not None:
                found[rel] = module
    for directory in _work_uses(root):
        module = read_go_module(root / directory) if directory is not None else None
        if directory is not None and module:
            found.setdefault(directory, module)
    return dict(sorted(found.items()))


def _work_uses(root: Path) -> list[str | None]:
    """go.work's ``use`` directories, repo-relative (None: outside the checkout)."""
    try:
        text = (root / "go.work").read_text(encoding="utf-8")
    except OSError:
        return []
    uses: list[str | None] = []
    block = False
    for line in text.splitlines():
        words = line.split("//", 1)[0].split()
        if not words:
            continue
        if block:
            if words[0] == ")":
                block = False
                continue
            targets = words[:1]
        elif words[0] == "use":
            if words[1:2] == ["("]:
                block = True
                continue
            targets = words[1:2]
        else:
            continue
        for target in targets:
            uses.append(_relative_dir(str(root / target.strip('"')), root))
    return uses


def readonly_goflags(flags: str) -> str:
    """GOFLAGS with ``-mod=mod`` made ``-mod=readonly`` (``-mod=vendor``/``readonly`` kept):
    listing packages must never write go.mod or go.sum."""
    words = []
    for word in flags.split():
        name, _, value = word.lstrip("-").partition("=")
        if name == "mod" and word.startswith("-") and value not in ("vendor", "readonly"):
            word = "-mod=readonly"
        words.append(word)
    return " ".join(words)


def _goflags(root: Path, go: str) -> str:
    """The GOFLAGS go would use in ``root`` (the environment's, else ``go env -w``'s)."""
    try:
        proc = subprocess.run([go, "env", "GOFLAGS"], cwd=root, capture_output=True,
                              timeout=60, check=False)  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return os.environ.get("GOFLAGS", "")
    if proc.returncode == 0:
        return proc.stdout.decode(errors="replace").strip()
    return os.environ.get("GOFLAGS", "")


def run_go_list(root: Path, go: str = "go", timeout: float = 900) -> list[Package] | None:
    """``go list -deps -test -json ./...`` in ``root`` (a module's directory), read-only, or
    None if it fails for any reason."""
    env = {**os.environ, "GOFLAGS": readonly_goflags(_goflags(root, go))}
    try:
        proc = subprocess.run(
            [go, "list", "-deps", "-test", "-json", "./..."],
            cwd=root,
            env=env,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return parse_go_list(proc.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        return None


def parse_go_list(output: str) -> list[Package]:
    """``go list -json`` prints concatenated JSON objects, not an array."""
    decoder = json.JSONDecoder()
    packages: list[Package] = []
    i, n = 0, len(output)
    while True:
        while i < n and output[i].isspace():
            i += 1
        if i >= n:
            return packages
        obj, i = decoder.raw_decode(output, i)
        if isinstance(obj, dict):
            packages.append(obj)


def build_graph(packages: Iterable[Package], root: Path) -> GoGraph:
    """The graph of one ``go list`` run (in the root or a module's directory): its main
    module's packages are the testable ones; any package of the checkout (a local
    ``replace``, a workspace module) can be a changed one."""
    packages = list(packages)
    main = [p for p in packages if (p.get("Module") or {}).get("Main")]
    module = next(((p.get("Module") or {}).get("Path") for p in main), None)

    dirs: dict[str, str] = {}
    for p in packages:
        path = p.get("ImportPath", "")
        if p.get("ForTest") or " " in path or _is_test_main(p) or not _local(p):
            continue  # test variants share their package's directory
        rel = _relative_dir(p.get("Dir"), root)
        if rel is not None and not _vendored(rel):
            dirs[rel] = path
    in_module = set(dirs.values())

    deps: dict[str, set[str]] = {}
    for p in main:
        if _is_test_main(p):
            continue
        base = p.get("ForTest") or p.get("ImportPath", "").split(" ", 1)[0]
        if base not in in_module:
            continue
        found = {d.split(" ", 1)[0] for d in p.get("Deps") or ()} | {base}
        deps.setdefault(base, set()).update(found & in_module)
    return GoGraph(module, dirs, {pkg: frozenset(d) for pkg, d in deps.items()})


def merge_graphs(graphs: Iterable[GoGraph], module: str | None) -> GoGraph:
    """One graph from per-module ones (``module``: the root's module path, if any)."""
    dirs: dict[str, str] = {}
    deps: dict[str, set[str]] = {}
    for graph in graphs:
        dirs.update(graph.dirs)
        for package, found in graph.deps.items():
            deps.setdefault(package, set()).update(found)
    return GoGraph(module, dirs, {pkg: frozenset(d) for pkg, d in sorted(deps.items())})


def _local(p: Package) -> bool:
    """A package whose source is the checkout's: its main module's, or a module replaced by a
    local directory (a workspace module, ``replace x => ../x``). Not the standard library, a
    downloaded module or a vendored copy."""
    if p.get("Standard"):
        return False
    module = p.get("Module") or {}
    if module.get("Main"):
        return True
    replace = module.get("Replace") or {}
    return bool(replace) and not replace.get("Version")


def _vendored(rel: str) -> bool:
    return any(part == "vendor" or part.startswith(".") for part in rel.split("/"))


def _is_test_main(p: Package) -> bool:
    # The generated "pkg.test" main package that drives a test binary.
    return p.get("Name") == "main" and str(p.get("ImportPath", "")).endswith(".test")


def _relative_dir(directory: str | None, root: Path) -> str | None:
    if not directory:
        return None
    try:
        rel = os.path.relpath(directory, root)
    except ValueError:  # different drive on Windows
        return None
    rel = Path(rel).as_posix()
    if rel.startswith(".."):
        return None
    return "" if rel == "." else rel
