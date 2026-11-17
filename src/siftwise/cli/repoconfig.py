"""The repo's ``.siftwise.toml``: selection settings the CLI sends with each request.

always_run = ["tests/smoke/**"]
generated = ["build/reports/**"]    # more generated files (siftwise ignores reports by default)
known_flaky = ["tests/test_net.py::*",    # triage: failing + not linked to the change -> flaky
               { test = "tests/test_db.py::test_pool", until = "2026-12-31" }]  # expires
fixtures = ["tests/fixtures/**"]    # test data: its build files select the tests that use it

[[depends]]                         # binary-level suites with no import edge to the code
tests = "test/cli/**"
on = ["**/*.go", "!**/*_test.go"]
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from siftwise.core.selector import DependsRule

FILE_NAME = ".siftwise.toml"


class RepoConfigError(ValueError):
    """The config file is unreadable or invalid."""


@dataclass(frozen=True)
class KnownFlakyEntry:
    """One ``known_flaky`` entry: a glob (test ID or file path), and the last date it applies
    (``until``, inclusive; None = no expiry)."""

    test: str
    until: date | None = None

    def describe(self) -> str:
        return f"{self.test} (until {self.until.isoformat()})" if self.until else self.test

    def to_json(self) -> dict[str, str]:
        return {"test": self.test, "until": self.until.isoformat() if self.until else ""}


@dataclass(frozen=True)
class ActiveKnownFlaky:
    """``known_flaky`` as of a date: the globs that apply, and the entries that expired."""

    globs: tuple[str, ...]
    expired: tuple[KnownFlakyEntry, ...]


def active_known_flaky(entries: tuple[KnownFlakyEntry, ...], on: date) -> ActiveKnownFlaky:
    """THE expiry rule, for every reader (record, triage, explain, MCP, the Action, GitLab):
    an entry applies through its ``until`` date (inclusive) and expires the day after. ``on``
    is the date that decides: for a recorded run, the UTC date the CI run started (see
    ``cli.main.known_flaky_date``); for a local triage or an agent, today in UTC."""
    globs = tuple(dict.fromkeys(e.test for e in entries if e.until is None or on <= e.until))
    expired = tuple(e for e in entries if e.until is not None and on > e.until)
    return ActiveKnownFlaky(globs, expired)


@dataclass(frozen=True)
class RepoConfig:
    depends: tuple[DependsRule, ...] = ()
    always_run: tuple[str, ...] = ()
    # Globs of generated files, on top of selector.GENERATED_GLOBS: never changed source.
    generated: tuple[str, ...] = ()
    # Test ID / file path globs triage calls flaky when they fail unlinked to the change, each
    # optionally with an expiry date (``active_known_flaky`` says which apply when).
    known_flaky: tuple[KnownFlakyEntry, ...] = ()
    # Globs of test data (``!`` excludes, as in [[depends]] ``on``): a change there selects
    # the tests that reference it instead of forcing the full suite (cli/fixtures.py).
    fixtures: tuple[str, ...] = ()

    def is_fixture(self, path: str) -> bool:
        return bool(self.fixtures) and DependsRule("", self.fixtures).matches_file(path)


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
    unknown = set(data) - {"depends", "always_run", "generated", "known_flaky", "fixtures"}
    if unknown:
        raise RepoConfigError(f"{source}: unknown key(s): {', '.join(sorted(unknown))}")

    always_run = data.get("always_run", [])
    if not _is_str_list(always_run):
        raise RepoConfigError(f"{source}: always_run must be a list of strings")
    generated = data.get("generated", [])
    if not _is_str_list(generated):
        raise RepoConfigError(f"{source}: generated must be a list of strings")
    known_flaky = _known_flaky(data.get("known_flaky", []), source)
    fixtures = data.get("fixtures", [])
    if not _is_str_list(fixtures) or (fixtures and all(p.startswith("!") for p in fixtures)):
        raise RepoConfigError(
            f"{source}: fixtures must be a list of globs with at least one non-'!' glob"
        )

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
    return RepoConfig(
        tuple(depends), tuple(always_run), tuple(generated), tuple(known_flaky), tuple(fixtures)
    )


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _known_flaky(value: object, source: str) -> list[KnownFlakyEntry]:
    """``known_flaky``: globs, or tables ``{ test = "<glob>", until = "YYYY-MM-DD" }`` (a TOML
    date works too)."""
    if not isinstance(value, list):
        raise RepoConfigError(f"{source}: known_flaky must be a list of globs or "
                              '{ test = "...", until = "YYYY-MM-DD" } tables')  # fmt: skip
    entries = []
    for i, item in enumerate(value, start=1):
        where = f"{source}: known_flaky #{i}"
        if isinstance(item, str):
            if not item:
                raise RepoConfigError(f"{where}: an empty glob")
            entries.append(KnownFlakyEntry(item))
            continue
        if not isinstance(item, dict):
            raise RepoConfigError(f"{where} must be a glob or a {{ test, until }} table, not "
                                  f"{type(item).__name__}")  # fmt: skip
        unknown = set(item) - {"test", "until"}
        if unknown:
            raise RepoConfigError(f"{where}: unknown field(s) {', '.join(sorted(unknown))} "
                                  "(only 'test' and 'until')")  # fmt: skip
        test, until = item.get("test"), item.get("until")
        if not isinstance(test, str) or not test:
            raise RepoConfigError(f"{where}: 'test' must be a non-empty glob")
        entries.append(KnownFlakyEntry(test, _until(until, where)))
    return entries


def _until(value: object, where: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):  # TOML 2026-12-31T00:00:00: a date is meant
        raise RepoConfigError(f"{where}: 'until' must be a date (YYYY-MM-DD), not a date and time")
    if isinstance(value, date):
        return value
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise RepoConfigError(f"{where}: 'until' must be a date as YYYY-MM-DD, not {value!r}")


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
