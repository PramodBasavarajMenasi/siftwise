"""Time to first failure (pure): how far into a selection's commands the first failing test
runs, by count of tests and by measured time, for the risk-order eval.

The commands are read as they would run: one after another (``&&``), each runner's arguments
in the order given. The model is the eval's, not a promise of every runner:

* pytest runs its arguments in order (without plugins such as pytest-randomly or xdist), and
  a file's tests in definition order, which JUnit doesn't record: within one argument, tests
  are taken by test ID, the same for both orders, so only the argument order differs.
* Go: one command per package, so packages run one after another; within a package, tests
  are taken by test ID. A subtest runs inside its parent: it counts as its parent.
* Jest: files in the order given (Jest itself may reorder them across workers).

A test the commands don't run (not selected) is left out. The position counts the tests run
up to and including the first failing one; the time sums their measured durations.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from dataclasses import dataclass

from siftwise.core.selector import is_go_subtest, pytest_file, pytest_node

_DOCTEST_SUFFIX = " || test $? -eq 5; }"


@dataclass(frozen=True)
class RanTest:
    """A test of the evaluated run: its measured duration and whether it failed."""

    test_id: str
    duration_ms: float
    failed: bool
    file_path: str | None = None


@dataclass(frozen=True)
class FirstFailure:
    position: int  # tests run up to and including the first failure (1 = the very first)
    total: int  # tests the commands run
    ms: float  # measured time up to the end of the first failing test
    total_ms: float


def units(commands: Sequence[str], go_module: str | None = None) -> list[tuple[str, str]]:
    """The commands' arguments in run order: ("py", node or file), ("go", package import
    path) or ("js", file)."""
    found: list[tuple[str, str]] = []
    for command in commands:
        if command.startswith("{ ") and command.endswith(_DOCTEST_SUFFIX):
            command = command[2 : -len(_DOCTEST_SUFFIX)]
        args = shlex.split(command)
        if args[:1] == ["pytest"]:
            found += [("py", a) for a in args[1:] if not a.startswith("-")]
        elif args[:2] == ["go", "test"] and len(args) > 2:
            found.append(("go", _package(args[2], go_module)))
        elif args[:1] == ["jest"]:
            found += [("js", a) for a in args[1:] if not a.startswith("-")]
    return found


def _package(target: str, go_module: str | None) -> str:
    """``go test`` target -> import path (selector._go_target, reversed)."""
    if go_module and target == ".":
        return go_module
    if go_module and target.startswith("./"):
        return f"{go_module}/{target[2:]}"
    return target


def first_failure(
    commands: Sequence[str], tests: Sequence[RanTest], go_module: str | None = None
) -> FirstFailure | None:
    """None when none of the tests the commands run failed."""
    ran = _ran(commands, tests, go_module)
    elapsed = 0.0
    first: tuple[int, float] | None = None
    for position, (ms, failed) in enumerate(ran, start=1):
        elapsed += ms
        if failed and first is None:
            first = (position, elapsed)
    if first is None:
        return None
    return FirstFailure(first[0], len(ran), first[1], elapsed)


def run_time(
    commands: Sequence[str], tests: Sequence[RanTest], go_module: str | None = None
) -> float:
    """The measured time of the tests the commands run."""
    return sum(ms for ms, _ in _ran(commands, tests, go_module))


def _ran(
    commands: Sequence[str], tests: Sequence[RanTest], go_module: str | None
) -> list[tuple[float, bool]]:
    """The top-level tests the commands run, in run order: (measured ms, failed)."""
    order = {unit: n for n, unit in reversed(list(enumerate(units(commands, go_module))))}
    runs: dict[str, tuple[int, float, bool]] = {}  # top-level test -> (unit, ms, failed)
    for test in tests:
        classname, _, name = test.test_id.partition("::")
        # Only a Go subtest folds into its parent: a pytest ID may hold a "/" (a URL param).
        top = (
            f"{classname}::{name.split('/', 1)[0]}" if is_go_subtest(test.test_id) else test.test_id
        )
        keys = [("go", classname), ("js", test.file_path or ""),
                ("py", pytest_node(test.test_id, test.file_path) or ""),
                ("py", pytest_file(test.test_id, test.file_path) or "")]  # fmt: skip
        unit = min((order[k] for k in keys if k in order), default=None)
        if unit is None:
            continue
        is_sub = top != test.test_id
        _, ms, failed = runs.get(top, (unit, 0.0, False))
        # A subtest's time is inside its parent's; its failure is its parent's.
        runs[top] = (unit, ms if is_sub else test.duration_ms, failed or test.failed)
    ran = sorted(runs.items(), key=lambda item: (item[1][0], item[0]))
    return [(ms, failed) for _, (_, ms, failed) in ran]
