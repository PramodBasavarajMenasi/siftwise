"""Jest test files for jest-junit reports without addFileAttribute (cli/jestfiles.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from siftwise.cli import jestfiles
from siftwise.cli.jestfiles import (
    STOPPED,
    JestFinder,
    finder,
    resolve,
    unresolved_note,
    with_files,
)
from siftwise.core.junit import is_js_test_path, parse_junit

# jest-junit 16.0.0's default output (no addFileAttribute) for the files below: one suite per
# file, named after its first describe ("undefined" when the first test has none), classname
# = name = "<describe titles> <test title>".
REPORT = b"""<?xml version="1.0" encoding="UTF-8"?>
<testsuites name="jest tests" tests="6" failures="1" errors="0" time="1.545">
  <testsuite name="undefined" errors="0" failures="0" skipped="0" tests="2">
    <testcase classname=" top level" name=" top level" time="0.009"></testcase>
    <testcase classname="Util works" name="Util works" time="0.001"></testcase>
  </testsuite>
  <testsuite name="Cart" errors="0" failures="1" skipped="0" tests="4">
    <testcase classname="Cart totals adds up" name="Cart totals adds up" time="0.009"/>
    <testcase classname="Cart empty" name="Cart empty" time="0.003"><failure>no</failure></testcase>
    <testcase classname="Cart sums 1 and 2" name="Cart sums 1 and 2" time="0.003"/>
    <testcase classname="Cart sums 3 and 4" name="Cart sums 3 and 4" time="0.003"/>
  </testsuite>
</testsuites>
"""

CART = """describe('Cart', () => {
  describe("totals", () => { test(`adds up`, () => {}); });
  test('empty', () => { expect(2).toBe(3); });
  test.each([[1, 2], [3, 4]])('sums %i and %i', (a, b) => {});
});
"""
UTIL = """test('top level', () => {});
describe('Util', () => { it('works', () => {}); });
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    for path, text in {
        "package.json": '{"devDependencies": {"jest": "29.7.0", "jest-junit": "16.0.0"}}',
        "src/__tests__/cart.test.js": CART,
        "lib/util.spec.ts": UTIL,
        "lib/util.ts": "export const x = 'top level';",  # not a test file
        "node_modules/x/cart.test.js": CART,  # never read
    }.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text, encoding="utf-8")
    return tmp_path


def test_suites_without_a_file_get_the_one_test_file_that_has_their_names(root: Path) -> None:
    data, found = with_files(REPORT, JestFinder(root))

    assert found.unresolved == {}
    assert {r.test_id: r.file_path for r in parse_junit(data)} == {
        "top level::top level": "lib/util.spec.ts",
        "Util works::Util works": "lib/util.spec.ts",
        "Cart totals adds up::Cart totals adds up": "src/__tests__/cart.test.js",
        "Cart empty::Cart empty": "src/__tests__/cart.test.js",
        "Cart sums 1 and 2::Cart sums 1 and 2": "src/__tests__/cart.test.js",  # test.each
        "Cart sums 3 and 4::Cart sums 3 and 4": "src/__tests__/cart.test.js",
    }
    # Nothing else changed: the same tests, statuses and times.
    before = [(r.test_id, r.status, r.duration_ms) for r in parse_junit(REPORT)]
    assert [(r.test_id, r.status, r.duration_ms) for r in parse_junit(data)] == before


def test_two_files_that_may_be_it_are_never_a_guess(root: Path) -> None:
    copy = root / "packages" / "web" / "cart.spec.js"
    copy.parent.mkdir(parents=True)
    copy.write_text(CART, encoding="utf-8")

    data, found = with_files(REPORT, JestFinder(root))

    assert "more than one test file may be it" in found.unresolved["Cart"]
    assert "packages/web/cart.spec.js" in found.unresolved["Cart"]
    paths = {r.test_id: r.file_path for r in parse_junit(data)}
    assert paths["Cart empty::Cart empty"] is None
    assert paths["Util works::Util works"] == "lib/util.spec.ts"  # the other suite is known
    assert "1 Jest suite(s)" in unresolved_note(found.unresolved)


def test_a_suite_no_file_has_names_for_stays_as_it_was(root: Path) -> None:
    (root / "src" / "__tests__" / "cart.test.js").write_text("test('other', () => {});")

    data, found = with_files(REPORT, JestFinder(root))

    assert found.unresolved["Cart"] == "no Jest test file in the checkout has its test names"
    assert parse_junit(data)[2].file_path is None


def test_reports_with_files_or_other_shapes_are_left_alone(root: Path) -> None:
    finder = JestFinder(root)
    with_file = REPORT.replace(b'name="Cart empty" time', b'name="Cart empty" file="x.js" time')
    pytest_report = (b'<testsuites><testsuite name="pytest"><testcase classname="tests.test_a" '
                     b'name="test_x"/></testsuite></testsuites>')  # fmt: skip

    assert with_files(pytest_report, finder) == (pytest_report, resolve(pytest_report, finder)[0])
    assert resolve(pytest_report, finder)[0].files == {}
    _, found = with_files(with_file, finder)
    assert "Cart empty::Cart empty" not in found.files  # the suite names a file already
    assert with_files(b"not xml <testcase", finder)[0] == b"not xml <testcase"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("src/cart.test.js", True),
        ("src\\__tests__\\cart.ts", True),
        ("cart.spec.tsx", True),
        ("src/cart.js", False),  # not a test file
        ("Cart adds up", False),
        ("tests.test_cart", False),
        ("weird name.test.js", False),
    ],
)
def test_js_test_paths(text: str, expected: bool) -> None:
    assert is_js_test_path(text) is expected


def test_the_parser_takes_the_file_from_jest_junit_path_templates() -> None:
    report = b"""<testsuites>
      <testsuite name="src/a.test.js"><testcase classname="A works" name="A works"/></testsuite>
      <testsuite name="src\\c.test.js">
        <testcase classname="Test suite failed to run" name="src\\c.test.js"><error/></testcase>
      </testsuite>
      <testsuite name="Plain"><testcase classname="Plain works" name="Plain works"/></testsuite>
      <testsuite name="src/v.test.ts">
        <testcase classname="src/v.test.ts" name="Vitest &gt; works"/>
      </testsuite>
    </testsuites>"""

    # Vitest names suites and classnames by path too: it isn't Jest, so no file (no runner:
    # the full suite, as before).
    assert [r.file_path for r in parse_junit(report)] == [
        "src/a.test.js", "src/c.test.js", None, None]  # fmt: skip


def test_another_runners_test_files_never_become_jests(root: Path) -> None:
    """Mocha's or Vitest's *.spec.js has the same names: outside a Jest project, no file."""
    (root / "package.json").write_text('{"devDependencies": {"mocha": "10.0.0"}}')

    _, found = with_files(REPORT, JestFinder(root))

    assert found.files == {}
    assert set(found.unresolved) == {"undefined", "Cart"}


def test_a_title_of_only_placeholders_matches_no_name(root: Path) -> None:
    """test.each(...)('%s') or `$a: $b` would match any name: such a file made every suite of
    jestjs/jest look ambiguous. It can't be found by name; the other files still are."""
    each = root / "src" / "each.test.js"
    each.write_text("test.each([['a']])('%s', () => {});\ntest.each`a`('$a: $b', () => {});")

    data, found = with_files(REPORT, JestFinder(root))

    assert found.unresolved == {}
    assert {r.file_path for r in parse_junit(data)} == {"lib/util.spec.ts",
                                                       "src/__tests__/cart.test.js"}  # fmt: skip


def test_the_search_runs_only_for_suites_without_a_file(root: Path) -> None:
    finder = JestFinder(root)
    with_file = REPORT.replace(b'name="undefined"', b'name="undefined" file="lib/util.spec.ts"')
    with_file = with_file.replace(b'name="Cart" ', b'name="Cart" file="src/c.test.js" ')

    assert with_files(with_file, finder) == (with_file, resolve(with_file, finder)[0])
    assert finder._files is None and finder._literals == {}  # nothing walked or read


def test_files_outside_a_jest_project_are_never_read(root: Path) -> None:
    (root / "tools").mkdir()
    (root / "tools" / "package.json").write_text('{"devDependencies": {"mocha": "10"}}')
    (root / "package.json").write_text('{"workspaces": ["tools", "web"]}')
    (root / "web").mkdir()
    (root / "web" / "package.json").write_text('{"devDependencies": {"jest": "29"}}')
    (root / "web" / "cart.test.js").write_text(CART)
    (root / "tools" / "cart.test.js").write_text(CART)  # Mocha's: same names

    finder = JestFinder(root)
    data, found = with_files(REPORT, finder)

    assert finder.files() == ["web/cart.test.js"]
    assert set(finder._literals) == {"web/cart.test.js"}
    assert parse_junit(data)[2].file_path == "web/cart.test.js"
    assert found.unresolved == {"undefined": "no Jest test file in the checkout has its test names"}


def test_past_the_time_limit_no_suite_gets_a_file_and_the_search_stays_stopped(
    root: Path,
) -> None:
    finder = JestFinder(root, time_limit=0)

    data, found = with_files(REPORT, finder)

    assert data == REPORT  # a partial read could make the wrong file look like the only one
    assert found.unresolved == {"undefined": STOPPED, "Cart": STOPPED}
    assert finder.stopped
    finder.time_limit = 60  # stopped for good: a later report isn't searched either
    assert resolve(REPORT, finder)[0].unresolved == {"undefined": STOPPED, "Cart": STOPPED}
    note = unresolved_note(found.unresolved, 0)
    assert "2 Jest suite(s)" in note
    assert "2 not searched (the search stopped at its 0 s limit)" in note


def test_one_finder_per_checkout_for_the_whole_command(root: Path) -> None:
    assert finder(root) is finder(root / "src" / "..")
    assert finder(root).time_limit == jestfiles.TIME_LIMIT_S
