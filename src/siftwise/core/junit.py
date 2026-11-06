"""JUnit XML parser.

Handles the dialects emitted by pytest, jest-junit, go-junit-report and Maven Surefire /
Gradle. All of them share the ``<testsuites>/<testsuite>/<testcase>`` shape; the differences
handled here are:

* root may be ``<testsuites>`` or a bare ``<testsuite>``, and suites may be nested;
* ``classname`` may be missing: it falls back to the enclosing suite's ``package`` attribute
  (Go reporters that name suites after the top-level test, e.g. ipfs/kubo's CI), then to the
  suite's ``name``;
* ``file`` may live on the testcase, the suite, or nowhere; without one, jest-junit's
  path-named suites (``usePathForSuiteName``, with the default classname = name templates)
  and its "Test suite failed to run" results give the file;
* Surefire reruns are recorded as ``<flakyFailure>``/``<flakyError>`` (failed, then passed)
  and ``<rerunFailure>``/``<rerunError>`` (failed every time) children;
* retry plugins (e.g. Gradle test-retry) repeat the whole ``<testcase>`` element.

Every attempt becomes its own ``ParsedTestResult`` with a 1-based ``attempt`` number, so
flakiness within a single run is preserved rather than collapsed.
"""

from __future__ import annotations

import posixpath
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring


class Status(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ParsedTestResult:
    test_id: str
    classname: str
    name: str
    file_path: str | None
    status: Status
    duration_ms: int | None
    attempt: int
    message: str | None = None


class JUnitParseError(ValueError):
    """Raised when input is not well-formed JUnit XML."""


_FLAKY_TAGS = {"flakyFailure": Status.FAILED, "flakyError": Status.ERROR}
_RERUN_TAGS = {"rerunFailure": Status.FAILED, "rerunError": Status.ERROR}


def parse_junit(data: bytes | str) -> list[ParsedTestResult]:
    """Parse one JUnit XML document into per-attempt test results, in document order."""
    try:
        root = fromstring(data)
    except (ParseError, DefusedXmlException) as exc:
        raise JUnitParseError(f"invalid JUnit XML: {exc}") from exc

    tag = _local(root.tag)
    if tag not in ("testsuites", "testsuite"):
        raise JUnitParseError(f"unexpected root element <{tag}>")

    results: list[ParsedTestResult] = []
    attempts: Counter[str] = Counter()
    # A bare <testsuite> root is itself a suite: its attributes apply to its testcases.
    is_suite = tag == "testsuite"
    _walk(
        root,
        suite=_Suite(
            name=(root.get("name") or "") if is_suite else "",
            package=(root.get("package") or "") if is_suite else "",
            file=root.get("file") if is_suite else None,
        ),
        results=results,
        attempts=attempts,
    )
    return results


def make_test_id(classname: str, name: str) -> str:
    """Canonical test identity: ``classname::name`` with whitespace collapsed."""
    return f"{_normalize(classname)}::{_normalize(name)}"


@dataclass(frozen=True, slots=True)
class _Suite:
    """Attributes inherited from the enclosing (possibly nested) <testsuite> elements."""

    name: str
    package: str
    file: str | None


def _walk(
    elem: Element,
    *,
    suite: _Suite,
    results: list[ParsedTestResult],
    attempts: Counter[str],
) -> None:
    for child in elem:
        tag = _local(child.tag)
        if tag in ("testsuite", "testsuites"):
            nested = _Suite(
                name=child.get("name") or suite.name,
                package=child.get("package") or suite.package,
                file=child.get("file") or suite.file,
            )
            _walk(child, suite=nested, results=results, attempts=attempts)
        elif tag == "testcase":
            _parse_testcase(child, suite, results, attempts)


# The name of a class-level result: a <testcase> naming its class but no test.
CLASS_LEVEL = "(class)"


def _parse_testcase(
    case: Element,
    suite: _Suite,
    results: list[ParsedTestResult],
    attempts: Counter[str],
) -> None:
    name = _normalize(case.get("name") or "")
    if not name and _normalize(case.get("classname") or ""):
        # Surefire writes name="" for a class skipped or failing as a whole (a JUnit 5
        # @Nested class with no Docker, an @BeforeAll that throws): the class itself.
        name = CLASS_LEVEL
    if not name:
        raise JUnitParseError("<testcase> without a name attribute")
    # Without a classname, prefer the suite's package over its name: some Go reporters name
    # each suite after the top-level test, so the name alone collides across packages.
    classname = _normalize(case.get("classname") or suite.package or suite.name)
    test_id = make_test_id(classname, name)
    file_path = normalize_path(case.get("file") or suite.file) or _reported_js_path(
        suite.name, case.get("classname"), case.get("name")
    )
    duration_ms = _parse_duration(case.get("time"))

    def emit(status: Status, message: str | None, duration: int | None) -> None:
        attempts[test_id] += 1
        results.append(
            ParsedTestResult(
                test_id=test_id,
                classname=classname,
                name=name,
                file_path=file_path,
                status=status,
                duration_ms=duration,
                attempt=attempts[test_id],
                message=message,
            )
        )

    outcome: tuple[Status, str | None] = (Status.PASSED, None)
    flaky: list[tuple[Status, str | None]] = []
    reruns: list[tuple[Status, str | None]] = []
    for child in case:
        tag = _local(child.tag)
        if tag == "error":
            outcome = (Status.ERROR, _message(child))
        elif tag == "failure" and outcome[0] is not Status.ERROR:
            outcome = (Status.FAILED, _message(child))
        elif tag == "skipped" and outcome[0] is Status.PASSED:
            outcome = (Status.SKIPPED, _message(child))
        elif tag in _FLAKY_TAGS:
            flaky.append((_FLAKY_TAGS[tag], _message(child)))
        elif tag in _RERUN_TAGS:
            reruns.append((_RERUN_TAGS[tag], _message(child)))

    # Flaky attempts failed before the final (reported) outcome; reruns came after it.
    # Per-attempt timing is not recorded, so the testcase time goes on the reported outcome.
    for status, message in flaky:
        emit(status, message, None)
    emit(outcome[0], outcome[1], duration_ms)
    for status, message in reruns:
        emit(status, message, None)


_JS_EXTENSIONS = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"})
# jest-junit's classname for a test file that failed to run (its name is then the file).
_JEST_SUITE_FAILED = "Test suite failed to run"


def is_js_test_path(text: str | None) -> bool:
    """Whether a report's suite name or classname is a JS/TS test file's path, as jest-junit's
    ``{filepath}`` / ``{filename}`` templates write it (Jest's default testMatch: ``*.test.*``,
    ``*.spec.*``, ``__tests__/``)."""
    if not text or any(c.isspace() for c in text):
        return False
    path = text.replace("\\", "/")
    stem, ext = posixpath.splitext(posixpath.basename(path))
    if ext not in _JS_EXTENSIONS:
        return False
    return stem.endswith((".test", ".spec")) or "__tests__" in path.split("/")[:-1]


def _reported_js_path(suite_name: str, classname: str | None, name: str | None) -> str | None:
    """jest-junit's test file where it names one without ``addFileAttribute``: a suite named
    by its path (``usePathForSuiteName``, ``suiteNameTemplate: "{filepath}"``) with the default
    classname and title templates (classname = name), or a file that failed to run. Nothing
    else: Vitest names suites and classnames by path too, and isn't Jest."""
    classname, name = _normalize(classname or ""), _normalize(name or "")
    if classname == _JEST_SUITE_FAILED and is_js_test_path(name):
        return normalize_path(name)
    if classname and classname == name and is_js_test_path(suite_name):
        return normalize_path(suite_name)
    return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _normalize(value: str) -> str:
    return " ".join(value.split())


def normalize_path(path: str | None) -> str | None:
    if not path:
        return None
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path or None


def _parse_duration(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        # Older Surefire versions emit locale-formatted times such as "1,234.567".
        seconds = float(raw.strip().replace(",", ""))
    except ValueError:
        return None
    if seconds < 0:
        return None
    return round(seconds * 1000)


def _message(elem: Element) -> str | None:
    message = elem.get("message")
    if message and message.strip():
        return message.strip()
    text = "".join(elem.itertext()).strip()
    return text or None
