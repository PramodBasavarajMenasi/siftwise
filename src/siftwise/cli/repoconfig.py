"""The repo's ``.siftwise.toml``: selection settings the CLI sends with each request.

always_run = ["tests/smoke/**"]
generated = ["build/reports/**"]    # more generated files (siftwise ignores reports by default)
known_flaky = ["tests/test_net.py::*"]   # triage: failing + not linked to the change -> flaky

[[depends]]                         # binary-level suites with no import edge to the code
tests = "test/cli/**"
on = ["**/*.go", "!**/*_test.go"]
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from siftwise.core.selector import DependsRule

FILE_NAME = ".siftwise.toml"


class RepoConfigError(ValueError):
    """The config file is unreadable or invalid."""


@dataclass(frozen=True)
class RepoConfig:
    depends: tuple[DependsRule, ...] = ()
    always_run: tuple[str, ...] = ()
    # Globs of generated files, on top of selector.GENERATED_GLOBS: never changed source.
    generated: tuple[str, ...] = ()
    # Test ID / file path globs triage calls flaky when they fail unlinked to the change.
    known_flaky: tuple[str, ...] = ()


def load_repo_config(path: Path) -> RepoConfig:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RepoConfigError(f"{path}: {exc}") from exc
    return loads_repo_config(content, str(path))


def loads_repo_config(content: str, source: str = FILE_NAME) -> RepoConfig:
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise RepoConfigError(f"{source}: {exc}") from exc
    return parse_repo_config(data, source)


def parse_repo_config(data: dict[str, Any], source: str = FILE_NAME) -> RepoConfig:
    unknown = set(data) - {"depends", "always_run", "generated", "known_flaky"}
    if unknown:
        raise RepoConfigError(f"{source}: unknown key(s): {', '.join(sorted(unknown))}")

    always_run = data.get("always_run", [])
    if not _is_str_list(always_run):
        raise RepoConfigError(f"{source}: always_run must be a list of strings")
    generated = data.get("generated", [])
    if not _is_str_list(generated):
        raise RepoConfigError(f"{source}: generated must be a list of strings")
    known_flaky = data.get("known_flaky", [])
    if not _is_str_list(known_flaky):
        raise RepoConfigError(f"{source}: known_flaky must be a list of strings")

    rules = data.get("depends", [])
    if not isinstance(rules, list):
        raise RepoConfigError(f"{source}: depends must be an array of tables ([[depends]])")
    depends = []
    for i, rule in enumerate(rules, start=1):
        where = f"{source}: [[depends]] #{i}"
        if not isinstance(rule, dict):
            raise RepoConfigError(f"{where} must be a table")
        if set(rule) - {"tests", "on"}:
            raise RepoConfigError(f"{where}: only 'tests' and 'on' are allowed")
        tests, on = rule.get("tests"), rule.get("on")
        if isinstance(on, str):
            on = [on]
        if not isinstance(tests, str) or not tests:
            raise RepoConfigError(f"{where}: 'tests' must be a non-empty string")
        if not _is_str_list(on) or not on or all(p.startswith("!") for p in on):
            raise RepoConfigError(f"{where}: 'on' needs at least one non-'!' glob")
        depends.append(DependsRule(tests, tuple(on)))
    return RepoConfig(tuple(depends), tuple(always_run), tuple(generated), tuple(known_flaky))


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v for v in value)


def read_go_module(root: Path) -> str | None:
    """The ``module`` path from ``root/go.mod``, if there is one."""
    try:
        lines = (root / "go.mod").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        parts = line.split("//", 1)[0].split()
        if len(parts) == 2 and parts[0] == "module":
            return parts[1].strip('"')
    return None
