"""cli/reruns.py: how each runner re-runs failing tests, without the tools (fakes and files);
then the real tools, with SIFTWISE_REAL_TOOL_TESTS=1 (ci.yml's real-tools job)."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from siftwise.cli import gha, reruns
from siftwise.cli.reruns import Checkout, Target, classify, invocations
from siftwise.core.junit import Status, parse_junit

ROOT_GO_MOD = "module github.com/acme/shop\n\ngo 1.22\n"
NO_RUNNER = "no runner found for it (pytest, Go, Jest, Maven or Gradle)"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.chdir(checkout)
    monkeypatch.setattr(reruns, "which", lambda name: None)  # no gotestsum unless a test says
    return checkout.resolve()


def write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# --- choosing the runner ------------------------------------------------------------------


def test_pytest_tests_keep_their_node_ids(root: Path) -> None:
    write(root / "tests" / "test_cart.py")

    assert classify("tests.test_cart::test_total", None, Checkout(root)) == Target(
        "tests.test_cart::test_total", "pytest", "tests/test_cart.py::test_total"
    )
    assert classify("tests.test_gone::test_x", None, Checkout(root)) == (
        "its file isn't in the checkout"
    )


def test_go_tests_need_the_module(root: Path) -> None:
    pkg = "github.com/acme/shop/cart"

    assert classify(f"{pkg}::TestTotal", None, Checkout(root)) == (
        "a Go test, but there's no go.mod at the checkout's root"
    )
    write(root / "go.mod", ROOT_GO_MOD)
    checkout = Checkout(root)
    # Subtests re-run through their top-level test.
    assert classify(f"{pkg}::TestTotal/with_discount", None, checkout) == Target(
        f"{pkg}::TestTotal/with_discount", "go", "TestTotal", (pkg,)
    )
    assert classify("github.com/other/lib::TestX", None, checkout) == (
        "its Go package isn't in this module (github.com/acme/shop)"
    )


def test_jest_tests_need_their_file_and_a_jest_package(root: Path) -> None:
    test_id = "cart adds an item::cart adds an item"
    write(root / "web" / "src" / "cart.test.js")

    assert classify(test_id, None, Checkout(root)) == NO_RUNNER  # no file reported
    assert classify(test_id, "web/src/cart.test.js", Checkout(root)) == (
        "a JS/TS test with no Jest (package.json or jest.config) above its file"
    )
    write(root / "web" / "package.json", json.dumps({"devDependencies": {"jest": "^30"}}))
    assert classify(test_id, "web/src/cart.test.js", Checkout(root)) == Target(
        test_id, "jest", "cart adds an item", (str(root / "web"), "src/cart.test.js")
    )
    assert classify(test_id, "web/src/gone.test.js", Checkout(root)) == (
        "its file isn't in the checkout"
    )


def test_jest_paths_relative_to_the_jest_project_are_found(root: Path) -> None:
    # jest-junit's file attribute is relative to the Jest project (here a monorepo package).
    write(root / "web" / "package.json", json.dumps({"jest": {}}))
    write(root / "web" / "src" / "cart.test.js")
    write(root / "node_modules" / "x" / "src" / "price.test.js")  # never searched
    test_id = "cart adds::cart adds"

    target = classify(test_id, "src/cart.test.js", Checkout(root))

    assert target == Target(test_id, "jest", "cart adds", (str(root / "web"), "src/cart.test.js"))
    write(root / "api" / "src" / "cart.test.js")  # now ambiguous: not guessed
    assert classify(test_id, "src/cart.test.js", Checkout(root)) == "its file isn't in the checkout"
    assert classify(test_id, "src/price.test.js", Checkout(root)) == (
        "its file isn't in the checkout"
    )


def test_a_jest_config_file_counts_and_absolute_paths_work(root: Path) -> None:
    write(root / "jest.config.ts")
    test = write(root / "cart.test.ts")

    target = classify("x::x", str(test), Checkout(root))

    assert isinstance(target, Target) and target.group == (str(root), "cart.test.ts")


@pytest.mark.parametrize(
    ("build_file", "runner"),
    [("pom.xml", "maven"), ("build.gradle", "gradle"), ("settings.gradle.kts", "gradle")],
)
def test_java_tests_use_the_build_tool(root: Path, build_file: str, runner: str) -> None:
    write(root / build_file)

    assert classify("com.acme.CartTest::computesTotal", None, Checkout(root)) == Target(
        "com.acme.CartTest::computesTotal", runner, "com.acme.CartTest\0computesTotal"
    )


@pytest.mark.parametrize(
    "name", ["computesTotal()", "computesTotal(int)[1]", "computesTotal[2]", "computesTotal"]
)
def test_java_method_names_lose_parameters_and_indexes(root: Path, name: str) -> None:
    write(root / "pom.xml")

    target = classify(f"com.acme.CartTest$Inner::{name}", None, Checkout(root))

    assert isinstance(target, Target) and target.arg == "com.acme.CartTest$Inner\0computesTotal"


@pytest.mark.parametrize(
    "test_id",
    [
        "com.acme.CartTest::adds an item to the cart",  # a JUnit 5 @DisplayName
        "com.acme.CartTest::rm -rf /; echo",  # nothing but identifiers reaches Maven
        "com.acme.Cart Test::x",
    ],
)
def test_names_that_arent_java_identifiers_are_not_re_run(root: Path, test_id: str) -> None:
    write(root / "pom.xml")

    assert classify(test_id, None, Checkout(root)) == NO_RUNNER


def test_both_build_tools_need_a_wrapper_to_choose(root: Path) -> None:
    write(root / "pom.xml")
    write(root / "build.gradle")
    test_id = "com.acme.CartTest::computesTotal"

    assert classify(test_id, None, Checkout(root)) == NO_RUNNER
    write(root / ("gradlew.bat" if os.name == "nt" else "gradlew"))
    target = classify(test_id, None, Checkout(root))
    assert isinstance(target, Target) and target.runner == "gradle"


def test_unknown_runners_say_so(root: Path) -> None:
    assert classify("Some suite::does a thing", None, Checkout(root)) == NO_RUNNER


# --- commands -------------------------------------------------------------------------------


def test_the_pytest_command_is_unchanged(root: Path) -> None:
    targets = [Target("a", "pytest", "tests/test_a.py::test_a"),
               Target("b", "pytest", "tests/test_b.py::C::test_b")]  # fmt: skip

    [command] = invocations(targets, root / "out", 2, ("uv", "run"), root)

    assert command.argv == [
        "uv", "run", "pytest", f"--junitxml={root / 'out' / 'rerun-2.xml'}", "-p",
        "no:cacheprovider", "tests/test_a.py::test_a", "tests/test_b.py::C::test_b",
    ]  # fmt: skip
    assert command.options() == {}  # exactly the old call: run_command(argv, timeout)


def test_go_runs_one_command_per_package_with_an_escaped_anchored_pattern(root: Path) -> None:
    targets = [Target("1", "go", "TestA", ("example.com/a",)),
               Target("2", "go", "TestB", ("example.com/b",)),
               Target("3", "go", "TestA", ("example.com/a",)),  # a subtest of TestA: once
               Target("4", "go", "Test_C", ("example.com/a",))]  # fmt: skip

    first, second = invocations(targets, root / "out", 1, ("uv", "run"), root)

    assert first.argv == [
        "go", "test", "-json", "-count=1", "-run", "^(TestA|Test_C)$", "example.com/a",
    ]  # fmt: skip
    assert second.argv[-3:] == ["-run", "^(TestB)$", "example.com/b"]  # no rerun-prefix
    assert first.options() == {"stdout": root / "out" / "rerun-1-go-1.json"}
    assert first.report == root / "out" / "rerun-1-go-1.xml"


def test_go_uses_gotestsum_when_installed(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reruns, "which", lambda name: "/usr/bin/gotestsum")

    [command] = invocations([Target("1", "go", "TestA", ("example.com/a",))], root, 1, (), root)

    assert command.argv == [
        "/usr/bin/gotestsum", "--junitfile", str(root / "rerun-1-go-1.xml"), "--format",
        "standard-verbose", "--", "-count=1", "-run", "^(TestA)$", "example.com/a",
    ]  # fmt: skip
    assert command.options() == {}


def test_regex_metacharacters_are_escaped() -> None:
    assert reruns.go_run_pattern(["Test.A", "Test(B)|C"]) == r"^(Test\.A|Test\(B\)\|C)$"
    assert reruns.jest_name_pattern(["cart (eur) totals $5.00", "a/b [x]?"]) == (
        r"^(cart\s+\(eur\)\s+totals\s+\$5\.00|a\/b\s+\[x\]\?)$"
    )


def test_jest_runs_per_file_from_its_package_with_jest_junit(root: Path) -> None:
    web = str(root / "web")
    targets = [Target("1", "jest", "cart adds", (web, "src/cart.test.js")),
               Target("2", "jest", "price formats", (web, "src/price.test.js")),
               Target("3", "jest", "cart totals", (web, "src/cart.test.js"))]  # fmt: skip

    cart, price = invocations(targets, root / "out", 1, ("npx",), root)

    assert cart.argv == [
        "npx", "jest", "--ci", "--runTestsByPath", "src/cart.test.js", "-t",
        r"^(cart\s+adds|cart\s+totals)$", "--reporters=default", "--reporters=jest-junit",
    ]  # fmt: skip
    env = {"JEST_JUNIT_OUTPUT_DIR": str(root / "out"),
           "JEST_JUNIT_OUTPUT_NAME": "rerun-1-jest-1.xml"}  # fmt: skip
    assert cart.options() == {"cwd": Path(web), "env": env}
    assert price.argv[4] == "src/price.test.js"


def test_jest_without_a_prefix_uses_the_projects_own_jest(root: Path) -> None:
    web = root / "web"
    target = Target("1", "jest", "x", (str(web), "x.test.js"))

    assert invocations([target], root, 1, (), root)[0].argv[0] == "jest"
    write(web / "node_modules" / ".bin" / ("jest.cmd" if os.name == "nt" else "jest"))
    assert invocations([target], root, 1, (), root)[0].argv[0] == str(
        web / "node_modules" / ".bin" / "jest"
    )


def test_maven_groups_methods_per_class_in_one_command(root: Path) -> None:
    targets = [Target("1", "maven", "com.acme.CartTest\0computesTotal"),
               Target("2", "maven", "com.acme.PayTest\0charges"),
               Target("3", "maven", "com.acme.CartTest\0addsItem"),
               Target("4", "maven", "com.acme.CartTest\0computesTotal")]  # fmt: skip

    [command] = invocations(targets, root, 1, ("uv", "run"), root)

    assert command.argv == [
        "mvn", "-B", "-Dtest=com.acme.CartTest#computesTotal+addsItem,com.acme.PayTest#charges",
        "-Dsurefire.failIfNoSpecifiedTests=false", "-DfailIfNoTests=false", "test",
    ]  # fmt: skip
    wrapper = write(root / ("mvnw.cmd" if os.name == "nt" else "mvnw"))
    assert invocations(targets, root, 1, (), root)[0].argv[0] == str(wrapper.resolve())


def test_gradle_filters_each_test(root: Path) -> None:
    targets = [Target("1", "gradle", "com.acme.CartTest\0computesTotal"),
               Target("2", "gradle", "com.acme.PayTest\0charges")]  # fmt: skip

    [command] = invocations(targets, root, 1, (), root)

    assert command.argv == [
        "gradle", "test", "--continue", "--tests", "com.acme.CartTest.computesTotal",
        "--tests", "com.acme.PayTest.charges",
    ]  # fmt: skip


# --- reports ------------------------------------------------------------------------------


def go_event(action: str, test: str | None = None, **fields: object) -> str:
    event: dict[str, object] = {"Action": action, "Package": "example.com/a", **fields}
    if test:
        event["Test"] = test
    return json.dumps(event)


GO_JSON = "\n".join([
    "# example.com/a [example.com/a.test]",  # build output: not JSON
    go_event("run", "TestA"),
    go_event("output", "TestA", Output="=== RUN   TestA\n"),
    go_event("run", "TestA/with_space"),
    go_event("output", "TestA/with_space", Output="a_test.go:9: boom\n"),
    go_event("fail", "TestA/with_space", Elapsed=0.01),
    go_event("fail", "TestA", Elapsed=0.02),
    go_event("run", "TestB"),
    go_event("pass", "TestB", Elapsed=0),
    go_event("run", "TestSkip"),
    go_event("skip", "TestSkip", Elapsed=0),
    go_event("run", "TestPanics"),
    go_event("fail", Elapsed=0.5),  # the package failed: TestPanics never finished
]).encode()  # fmt: skip


def test_go_json_becomes_junit_with_the_recorded_test_ids() -> None:
    results = {r.test_id: r for r in parse_junit(reruns.go_json_to_junit(GO_JSON))}

    assert {k: r.status for k, r in results.items()} == {
        "example.com/a::TestA": Status.FAILED,
        "example.com/a::TestA/with_space": Status.FAILED,
        "example.com/a::TestB": Status.PASSED,
        "example.com/a::TestSkip": Status.SKIPPED,
        "example.com/a::TestPanics": Status.FAILED,  # started, never finished
    }
    # As go-junit-report writes it: message "Failed", the test's output in the element's text.
    assert results["example.com/a::TestA/with_space"].message == "Failed"
    assert "a_test.go:9: boom" in reruns.go_json_to_junit(GO_JSON)
    assert parse_junit(reruns.go_json_to_junit(b"no json at all\n")) == []  # a build failure


def test_a_go_invocation_converts_its_json(root: Path) -> None:
    [command] = invocations([Target("1", "go", "TestA", ("example.com/a",))], root, 1, (), root)
    assert command.stdout is not None
    command.stdout.write_bytes(GO_JSON)

    [report] = command.reports()

    assert report == command.report and "TestB" in report.read_text()


def test_maven_and_gradle_reports_are_the_files_the_re_run_wrote(root: Path) -> None:
    reports = Path("target", "surefire-reports")
    write(root / reports / "TEST-com.acme.PayTest.xml", "old")
    rewritten = write(root / "core" / reports / "TEST-com.acme.Cart.xml", "a")
    write(root / reports / "com.acme.PayTest.txt", "not a report")
    out = root.parent / "out"
    [command] = invocations([Target("1", "maven", "com.acme.Cart\0x")], out, 1, (), root)

    with pytest.raises(OSError, match="maven wrote no new reports"):
        command.reports()
    rewritten.write_text("rewritten, longer")
    write(root / "web" / reports / "TEST-com.acme.Cart.xml", "new")  # same name, other module
    write(root / "node_modules" / "x" / reports / "TEST-skip.xml", "x")

    copies = command.reports()  # copied out of the checkout, in path order
    assert [p.parent for p in copies] == [out / "rerun-1-maven" / "reports"] * 2
    assert [p.read_text() for p in copies] == ["rewritten, longer", "new"]


def test_gradle_reports_come_from_any_test_task(root: Path) -> None:
    out = root.parent / "out"
    [command] = invocations([Target("1", "gradle", "com.acme.Cart\0x")], out, 1, (), root)
    write(root / "build" / "test-results" / "test" / "TEST-com.acme.Cart.xml", "x")

    assert [p.name for p in command.reports()] == ["0-TEST-com.acme.Cart.xml"]


@pytest.mark.parametrize(("tool", "under"), [("maven", "target/surefire-reports"),
                                             ("gradle", "build/test-results/test")])  # fmt: skip
def test_the_ci_runs_reports_are_put_back_after_a_re_run(root: Path, tool: str, under: str
                                                         ) -> None:  # fmt: skip
    rewritten = write(root / under / "TEST-com.acme.Cart.xml", "the CI run's")
    removed = write(root / "app" / under / "TEST-com.acme.Pay.xml", "the CI run's too")
    other = write(root / under / "notes.txt", "not a report")
    stamp = rewritten.stat().st_mtime_ns
    [command] = invocations([Target("1", tool, "com.acme.Cart\0x")], root.parent / "out", 1,
                            (), root)  # fmt: skip
    # What Gradle does: empty the results directory, then write the re-run classes' reports.
    removed.unlink()
    rewritten.write_text("the re-run's report")
    new = write(root / under / "TEST-com.acme.New.xml", "the re-run's too")
    other.write_text("changed, not ours")

    assert [p.read_text() for p in command.reports()] == ["the re-run's report",
                                                         "the re-run's too"]  # fmt: skip
    command.restore()

    assert rewritten.read_text() == "the CI run's" and rewritten.stat().st_mtime_ns == stamp
    assert removed.read_text() == "the CI run's too"
    assert not new.exists()
    assert other.read_text() == "changed, not ours"  # only TEST-*.xml are kept
    command.restore()  # once is enough; a second call changes nothing
    assert rewritten.read_text() == "the CI run's"


def test_a_report_that_cant_be_put_back_is_an_error(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = write(root / "target" / "surefire-reports" / "TEST-com.acme.Cart.xml", "ci")
    [command] = invocations([Target("1", "maven", "com.acme.Cart\0x")], root.parent / "out",
                            1, (), root)  # fmt: skip
    report.write_text("the re-run's report")

    def refuse(*args: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(shutil, "copy2", refuse)
    with pytest.raises(OSError, match="1 report"):
        command.restore()


def test_other_runners_have_nothing_to_put_back(root: Path) -> None:
    write(root / "tests" / "test_cart.py")
    [command] = invocations([Target("t", "pytest", "tests/test_cart.py::test_x")], root, 1, (),
                            root)  # fmt: skip

    command.restore()  # nothing to do, no error


def test_run_command_passes_options(tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    script = "import os; print(os.environ['SIFTWISE_X'], os.getcwd())"

    gha.run_command([sys.executable, "-c", script], 60, cwd=tmp_path,
                    env={"SIFTWISE_X": "set"}, stdout=out)  # fmt: skip

    assert out.read_text().split() == ["set", str(tmp_path)]
    with pytest.raises(subprocess.TimeoutExpired):
        gha.run_command([sys.executable, "-c", "import time; time.sleep(5)"], 0.5)


# --- the real tools (SIFTWISE_REAL_TOOL_TESTS=1: ci.yml's real-tools job) -----------------

REAL_TOOLS = os.environ.get("SIFTWISE_REAL_TOOL_TESTS") == "1"
# Minutes each, with downloads: off by default. When on, a missing tool fails the test, so
# a broken setup can't pass as skipped.
real_tools = pytest.mark.skipif(not REAL_TOOLS, reason="set SIFTWISE_REAL_TOOL_TESTS=1")


def require(*tools: str) -> None:
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        pytest.fail(f"SIFTWISE_REAL_TOOL_TESTS=1, but not on PATH: {', '.join(missing)}")


def run_for_real(
    targets: list[Target | str], root: Path, prefix: tuple[str, ...] = ()
) -> dict[str, Status]:
    """Run the targets' commands like record does, and read back the statuses by test ID."""
    assert all(isinstance(t, Target) for t in targets), targets
    statuses: dict[str, Status] = {}
    out = root.parent / "out"
    out.mkdir(exist_ok=True)
    chosen = [t for t in targets if isinstance(t, Target)]
    for command in invocations(chosen, out, 1, prefix, root):
        try:
            gha.run_command(command.argv, 600, **command.options())
            for report in command.reports():
                statuses.update((r.test_id, r.status) for r in parse_junit(report.read_bytes()))
        finally:
            command.restore()
    return statuses


FLAKY_GO = """package flaky

import (
	"os"
	"testing"
)

func TestOnce(t *testing.T) {
	if _, err := os.Stat("ran-once"); err != nil {
		os.WriteFile("ran-once", nil, 0o644)
		t.Fatal("first run fails")
	}
}

func TestAlways(t *testing.T) {
	t.Run("sub case", func(t *testing.T) { t.Fatal("always fails") })
}

func TestNotReRun(t *testing.T) { t.Fatal("must be filtered out") }
"""


@real_tools
@pytest.mark.parametrize("gotestsum", [False, True], ids=["go-test-json", "gotestsum"])
def test_go_for_real(root: Path, monkeypatch: pytest.MonkeyPatch, gotestsum: bool) -> None:
    require("go", *(["gotestsum"] if gotestsum else []))
    if gotestsum:
        monkeypatch.setattr(reruns, "which", shutil.which)
    write(root / "go.mod", "module example.com/flaky\n\ngo 1.21\n")
    write(root / "flaky_test.go", FLAKY_GO)
    (root / "ran-once").touch()  # the CI run's failure already happened
    checkout = Checkout(root)
    ids = ("example.com/flaky::TestOnce", "example.com/flaky::TestAlways/sub_case")
    targets = [classify(t, None, checkout) for t in ids]
    [command] = invocations([t for t in targets if isinstance(t, Target)], root, 1, (), root)
    assert ("gotestsum" in Path(command.argv[0]).name) is gotestsum

    statuses = run_for_real(targets, root)

    assert statuses[ids[0]] is Status.PASSED  # reported under the recorded ID
    assert statuses[ids[1]] is Status.FAILED
    assert "example.com/flaky::TestNotReRun" not in statuses


JEST_HOME = os.environ.get("SIFTWISE_TEST_JEST_HOME")  # a folder with node_modules/jest(-junit)


@real_tools
def test_jest_for_real(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    require("node")
    if not JEST_HOME:
        pytest.fail("set SIFTWISE_TEST_JEST_HOME to a folder whose node_modules has jest and "
                    "jest-junit")  # fmt: skip
    # Inside JEST_HOME, so jest finds the jest-junit reporter in node_modules above it.
    project = Path(JEST_HOME) / f"project-{tmp_path.name}"
    try:
        write(project / "package.json", json.dumps({"devDependencies": {"jest": "*"}}))
        write(project / "src" / "cart.test.js",
              "const fs = require('fs');\n"
              "describe('cart', () => {\n"
              "  test('totals (eur) $5', () => {\n"
              "    const first = !fs.existsSync('ran-once'); fs.writeFileSync('ran-once', '');\n"
              "    expect(first).toBe(false);\n"
              "  });\n"
              "  test('always fails', () => { expect(1).toBe(2); });\n"
              "  test('not re-run', () => { throw new Error('must be filtered out'); });\n"
              "});\n")  # fmt: skip
        (project / "ran-once").touch()
        monkeypatch.chdir(project)
        checkout = Checkout(project)
        ids = ("cart totals (eur) $5::cart totals (eur) $5", "cart always fails::cart always fails")
        jest = Path(JEST_HOME) / "node_modules" / "jest" / "bin" / "jest.js"

        statuses = run_for_real([classify(t, "src/cart.test.js", checkout) for t in ids],
                                project, ("node", str(jest)))  # fmt: skip

        assert statuses[ids[0]] is Status.PASSED
        assert statuses[ids[1]] is Status.FAILED
        assert statuses.get("cart not re-run::cart not re-run") in (None, Status.SKIPPED)
    finally:
        shutil.rmtree(project, ignore_errors=True)


CART_TEST = """package acme;

import static org.junit.jupiter.api.Assertions.*;
import java.nio.file.*;
import org.junit.jupiter.api.Test;

class CartTest {
    @Test void once() throws Exception {
        boolean first = !Files.exists(Path.of("ran-once"));
        Files.writeString(Path.of("ran-once"), "");
        assertFalse(first);
    }
    @Test void always() { fail("always"); }
    @Test void notReRun() { fail("must be filtered out"); }
}
"""

POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>acme</groupId><artifactId>cart</artifactId><version>1</version>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
  <dependencies>
    <dependency><groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId>
      <version>5.11.3</version><scope>test</scope></dependency>
  </dependencies>
  <build><plugins><plugin><groupId>org.apache.maven.plugins</groupId>
    <artifactId>maven-surefire-plugin</artifactId><version>3.5.2</version></plugin></plugins></build>
</project>
"""

BUILD_GRADLE = """plugins { id 'java' }
repositories { mavenCentral() }
dependencies {
    testImplementation platform('org.junit:junit-bom:5.11.3')
    testImplementation 'org.junit.jupiter:junit-jupiter'
    testRuntimeOnly 'org.junit.platform:junit-platform-launcher'
}
test { useJUnitPlatform() }
"""


def ci_reports(directory: Path) -> dict[str, str]:
    """The CI run's reports, before the re-run: one of a re-run class, one of another."""
    reports = {"TEST-acme.CartTest.xml": "<testsuite/>", "TEST-acme.PayTest.xml": "<testsuite/>"}
    for name, text in reports.items():
        write(directory / name, text)
    return reports


def reports_in(directory: Path) -> dict[str, str]:
    return {p.name: p.read_text() for p in directory.glob("TEST-*.xml")}


@real_tools
def test_maven_for_real(root: Path) -> None:
    require("mvn")
    write(root / "pom.xml", POM)
    write(root / "src" / "test" / "java" / "acme" / "CartTest.java", CART_TEST)
    (root / "ran-once").touch()
    before = ci_reports(root / "target" / "surefire-reports")
    ids = ("acme.CartTest::once", "acme.CartTest::always")

    statuses = run_for_real([classify(t, None, Checkout(root)) for t in ids], root)

    assert statuses[ids[0]] is Status.PASSED
    assert statuses[ids[1]] is Status.FAILED
    assert "acme.CartTest::notReRun" not in statuses
    assert reports_in(root / "target" / "surefire-reports") == before


@real_tools
def test_gradle_for_real(root: Path) -> None:
    require("gradle")
    write(root / "settings.gradle", "rootProject.name = 'cart'\n")
    write(root / "build.gradle", BUILD_GRADLE)
    write(root / "src" / "test" / "java" / "acme" / "CartTest.java", CART_TEST)
    (root / "ran-once").touch()
    before = ci_reports(root / "build" / "test-results" / "test")
    ids = ("acme.CartTest::once()", "acme.CartTest::always()")

    statuses = run_for_real([classify(t, None, Checkout(root)) for t in ids], root)

    assert statuses[ids[0]] is Status.PASSED
    assert statuses[ids[1]] is Status.FAILED
    assert "acme.CartTest::notReRun()" not in statuses
    assert reports_in(root / "build" / "test-results" / "test") == before
