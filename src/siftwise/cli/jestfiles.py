"""Which test file a jest-junit ``<testsuite>`` came from, when the report doesn't say.

jest-junit writes a ``file`` attribute only with ``addFileAttribute: "true"`` (off by
default), and by default names a test ``"<describe titles> <test title>"`` as both classname
and name. It does write one ``<testsuite>`` per test file, so a suite's tests share one file:
the Jest test file in the checkout whose string literals make up every test name in the suite
(titles joined by spaces; ``test.each`` titles' ``%s``/``$name`` placeholders match anything).
Only a single such file counts: two files that could both be it, or none, leave the suite
without a file (never a guess), with the reason.

Used where a test's file matters: ``record`` adds the file to the reports it uploads (so the
server can select Jest tests by file), and ``rerun-unknown`` (Jest re-runs a file).
"""

from __future__ import annotations

import os
import posixpath
import re
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree.ElementTree import Element, ParseError, tostring

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from siftwise.cli.jsgraph import SKIP_DIRS, scan
from siftwise.cli.reruns import Checkout
from siftwise.core.junit import is_js_test_path, make_test_id
from siftwise.core.selector import is_jest_test_file

# jest-junit's stand-in test for a failing hook (afterAll...): in the suite, named by no title.
HOOK_FAILURE = "Test execution failure: could be caused by test hooks"
# printf placeholders of test.each titles, and $name / $name.path of its tagged templates.
_PLACEHOLDER = re.compile(r"%[sdifjoOp#]|\$[A-Za-z_][\w.]*")
_MAX_LITERAL = 500
MAX_NOTES = 5
# The whole search's time (walk + reads), per command. jestjs/jest (1,362 test files): ~3 s
# cold on Linux (2026-11-06), mostly tokenizing; 45 s on a cold Windows checkout. Past it, no
# suite gets a file rather than a slow record step.
TIME_LIMIT_S = 20.0
STOPPED = "the test file search stopped at its time limit"


@dataclass
class Resolution:
    """Test ID -> repo-relative file, and the suites left without one (name -> why)."""

    files: dict[str, str] = field(default_factory=dict)
    unresolved: dict[str, str] = field(default_factory=dict)


class _Literals:
    """A file's string literals, as test-name pieces: plain ones, and patterns for titles
    with placeholders."""

    def __init__(self, source: str, jsx: bool) -> None:
        self.plain: set[str] = set()
        self.patterns: list[re.Pattern[str]] = []
        for token in scan(source, jsx):
            if token.kind != "str" or len(token.value) > _MAX_LITERAL:
                continue
            text = " ".join(token.value.split())
            if not text:
                continue
            if _PLACEHOLDER.search(text):
                parts = _PLACEHOLDER.split(text.replace("%%", "\0"))
                if not any(c.isalnum() for c in "".join(parts)):
                    # "%s", "$a: $b": would match any name, so every suite would look like
                    # it may be this file. Its own tests can't be found by name: no file.
                    continue
                regex = ".+?".join(re.escape(p).replace("\0", "%") for p in parts)
                self.patterns.append(re.compile(regex))
            else:
                self.plain.add(text)

    def make(self, name: str) -> bool:
        """Whether ``name`` is literals joined by single spaces."""
        n = len(name)
        ends = [k for k, c in enumerate(name) if c == " "] + [n]
        reachable = {0}
        for start in sorted({0} | {k + 1 for k in ends if k < n}):
            if start not in reachable:
                continue
            for end in ends:
                if end <= start:
                    continue
                piece = name[start:end]
                if piece in self.plain or any(p.fullmatch(piece) for p in self.patterns):
                    if end == n:
                        return True
                    reachable.add(end + 1)
        return False


class SearchStopped(Exception):
    """The search ran out of time (``JestFinder.time_limit``)."""


class JestFinder:
    """The checkout's Jest test files (Jest's default testMatch: ``**/__tests__/**``,
    ``*.test.*``, ``*.spec.*``, in a Jest project; not under node_modules or build folders),
    read all at once when first needed, within ``time_limit`` seconds: past it the search
    stops for good, and every suite not found by then is left without a file (a partial
    read could make the wrong file look like the only one)."""

    def __init__(self, root: Path, time_limit: float = TIME_LIMIT_S) -> None:
        self.root = root
        self.time_limit = time_limit
        self.stopped = False
        self.stop_warned = False  # one warning per command about the time limit
        self._files: list[str] | None = None
        self._literals: dict[str, _Literals] = {}
        self._checkout: Checkout | None = None
        self._loaded = False
        self._deadline: float | None = None

    def files(self) -> list[str]:
        if self._files is None:
            files = []
            for dirpath, _dirnames, filenames in _walk(self.root):
                self._check_time()
                rel = Path(dirpath).relative_to(self.root).as_posix()
                prefix = "" if rel == "." else rel + "/"
                files += [prefix + name for name in filenames
                          if is_jest_test_file(prefix + name)]  # fmt: skip
            # Only files of a Jest project (a package.json with Jest or a jest.config at or
            # above them): another runner's test file (Mocha, Vitest) never becomes Jest's,
            # and isn't read.
            self._files = sorted(f for f in files if self._jest(f))
        return self._files

    def literals(self, path: str) -> _Literals:
        if path not in self._literals:
            self._literals[path] = _Literals(_read(self.root / path), _jsx(path))
        return self._literals[path]

    def load(self) -> None:
        """Walk the checkout and read every test file, or raise SearchStopped once over the
        time limit (then and on every later call)."""
        if self.stopped:
            raise SearchStopped
        if self._loaded:
            return
        self._deadline = time.monotonic() + self.time_limit
        try:
            for path in self.files():
                self._check_time()
                self.literals(path)
        except SearchStopped:
            self.stopped = True
            raise
        finally:
            self._deadline = None
        self._loaded = True

    def _check_time(self) -> None:
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise SearchStopped

    def file_of(self, names: Sequence[str]) -> str | list[str]:
        """The one test file whose literals make every name, else the candidates ([]: none).
        Raises SearchStopped past the time limit."""
        self.load()
        candidates = self.files()
        for name in names:
            candidates = [f for f in candidates if self.literals(f).make(name)]
            if not candidates:
                break
        return candidates[0] if len(candidates) == 1 else candidates

    def _jest(self, path: str) -> bool:
        if self._checkout is None:
            self._checkout = Checkout(self.root)
        return self._checkout.jest_dir((self._checkout.root / path).resolve()) is not None


_FINDERS: dict[Path, JestFinder] = {}


def finder(root: Path) -> JestFinder:
    """The finder of the checkout at ``root``, shared by everything one command does (the
    upload, each re-run round's upload, the re-runs' files): the files are read once."""
    key = root.resolve()
    if key not in _FINDERS:
        _FINDERS[key] = JestFinder(key, TIME_LIMIT_S)
    return _FINDERS[key]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _jsx(path: str) -> bool:
    return posixpath.splitext(path)[1] in (".js", ".jsx", ".tsx", ".mjs", ".cjs")


def _walk(root: Path) -> Iterator[tuple[str, list[str], list[str]]]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        yield dirpath, dirnames, filenames


def _norm(value: str | None) -> str:
    return " ".join((value or "").split())


def _jest_suites(root: Element) -> Iterator[tuple[Element, list[Element]]]:
    """Suites written by jest-junit's default templates with no file: every testcase's
    classname equals its name, and nothing in the suite names a file."""
    for suite in root.iter():
        if suite.tag.rsplit("}", 1)[-1] != "testsuite":
            continue
        cases = [c for c in suite if c.tag.rsplit("}", 1)[-1] == "testcase"]
        if not cases or suite.get("file") or is_js_test_path(_norm(suite.get("name"))):
            continue
        if all(not c.get("file") and _norm(c.get("name")) and
               _norm(c.get("classname")) == _norm(c.get("name")) for c in cases):  # fmt: skip
            yield suite, cases


def resolve(data: bytes, finder: JestFinder) -> tuple[Resolution, Element | None]:
    """The files of a report's Jest suites that don't name one (and the parsed report, with
    ``file`` set on those testcases; None when nothing was found or it isn't XML)."""
    found = Resolution()
    if b"testcase" not in data:
        return found, None
    try:
        root = fromstring(data)
    except (ParseError, DefusedXmlException):
        return found, None
    changed = False
    for suite, cases in _jest_suites(root):
        names = [_norm(c.get("name")) for c in cases]
        titles = [n for n in names if not n.startswith(HOOK_FAILURE)]
        label = _norm(suite.get("name")) or names[0]
        if not titles:
            found.unresolved[label] = "only a failing hook in it"
            continue
        try:
            match = finder.file_of(titles)
        except SearchStopped:
            found.unresolved[label] = STOPPED
            continue
        if isinstance(match, list):
            found.unresolved[label] = (
                f"more than one test file may be it ({', '.join(match[:3])}"
                f"{', ...' if len(match) > 3 else ''})"
                if match
                else "no Jest test file in the checkout has its test names"
            )
            continue
        for case, name in zip(cases, names, strict=True):
            case.set("file", match)
            found.files[make_test_id(name, name)] = match
        changed = True
    return found, (root if changed else None)


def with_files(data: bytes, finder: JestFinder) -> tuple[bytes, Resolution]:
    """The report with ``file`` on the testcases of Jest suites found in the checkout (the
    same bytes when none was), and what was found."""
    found, root = resolve(data, finder)
    if root is None:
        return data, found
    return tostring(root, encoding="utf-8", xml_declaration=True), found


def unresolved_note(unresolved: dict[str, str], time_limit: float = TIME_LIMIT_S) -> str:
    """A warning line for suites left without a file: the suites stopped by the time limit
    as one count, the others with their reasons."""
    stopped = sum(why == STOPPED for why in unresolved.values())
    others = [(name, why) for name, why in unresolved.items() if why != STOPPED]
    shown = [f"{name!r}: {why}" for name, why in others[:MAX_NOTES]]
    more = len(others) - len(shown)
    parts = ([f"{stopped} not searched (the search stopped at its {time_limit:g} s limit)"]
             if stopped else [])  # fmt: skip
    if shown:
        parts.append("; ".join(shown) + (f"; +{more} more" if more else ""))
    return (f"siftwise: no test file for {len(unresolved)} Jest suite(s) (jest-junit's "
            f"addFileAttribute is off): {'; '.join(parts)}. "
            "Their tests can't be selected or re-run by file: set addFileAttribute: \"true\" "
            "in jest-junit's options")  # fmt: skip
