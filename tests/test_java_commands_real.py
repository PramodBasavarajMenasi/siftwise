"""Real Maven and Gradle runs for #54: one invocation over several modules runs exactly the
classes it names (and why a class name two modules share keeps its module apart), and a
module pom.xml change runs its module's and its dependents' tests.

Run with SIFTWISE_REAL_TOOL_TESTS=1 (mvn and gradle on PATH; they download JUnit once)."""

import shlex
import shutil
import subprocess
from pathlib import Path

from siftwise.cli.javagraph import java_change
from siftwise.core.selector import JavaBuildInfo, KnownTest, RepoHistory, select_tests
from tests.test_reruns import real_tools, require

FIXTURES = Path(__file__).parent / "fixtures"
POM = """<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
<groupId>acme</groupId>{}<version>1</version></project>"""
ROOT_POM = POM.format("""<artifactId>root</artifactId><packaging>pom</packaging>
<modules><module>a</module><module>b</module></modules>
<properties><maven.compiler.release>17</maven.compiler.release>
<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId>
<version>5.11.3</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><groupId>org.apache.maven.plugins</groupId>
<artifactId>maven-surefire-plugin</artifactId><version>3.5.2</version></plugin></plugins></build>""")
CHILD_POM = POM.format(
    "<parent><groupId>acme</groupId><artifactId>root</artifactId><version>1</version></parent>"
    "<artifactId>{}</artifactId>"
)
GRADLE_ROOT = """subprojects {
    apply(plugin = "java")
    repositories { mavenCentral() }
    dependencies {
        "testImplementation"(platform("org.junit:junit-bom:5.11.3"))
        "testImplementation"("org.junit.jupiter:junit-jupiter")
        "testRuntimeOnly"("org.junit.platform:junit-platform-launcher")
    }
    tasks.withType<Test> { useJUnitPlatform() }
}
"""


def source_of(package: str, name: str) -> str:
    return (f"package {package};\n\nimport org.junit.jupiter.api.Test;\n\n"
            f"class {name} {{ @Test void ok() {{}} }}\n")  # fmt: skip


def write(root: Path, files: dict[str, str]) -> Path:
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content, encoding="utf-8")
    return root


# a and b both have acme.shared.SameTest; b also has an ATest in another package.
SOURCES = {
    "a/src/test/java/acme/a/ATest.java": source_of("acme.a", "ATest"),
    "a/src/test/java/acme/shared/SameTest.java": source_of("acme.shared", "SameTest"),
    "b/src/test/java/acme/b/BTest.java": source_of("acme.b", "BTest"),
    "b/src/test/java/acme/b/ATest.java": source_of("acme.b", "ATest"),
    "b/src/test/java/acme/shared/SameTest.java": source_of("acme.shared", "SameTest"),
}


def run(argv: list[str], cwd: Path) -> None:
    argv = [shutil.which(argv[0]) or argv[0], *argv[1:]]
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=900)
    assert proc.returncode == 0, (argv, proc.stdout[-3000:], proc.stderr[-2000:])


def reported(root: Path) -> set[tuple[str, str]]:
    """(module, test class) of every report written."""
    return {
        (p.relative_to(root).parts[0], p.name.removeprefix("TEST-").removesuffix(".xml"))
        for p in root.rglob("TEST-*.xml")
    }


def clean(root: Path) -> None:
    for pattern in ("target", "build"):
        for found in root.rglob(pattern):
            if found.is_dir():
                shutil.rmtree(found)


@real_tools
def test_one_maven_invocation_runs_exactly_the_classes_it_names(tmp_path: Path) -> None:
    require("mvn")
    root = write(tmp_path, {"pom.xml": ROOT_POM, "a/pom.xml": CHILD_POM.format("a"),
                            "b/pom.xml": CHILD_POM.format("b"), **SOURCES})  # fmt: skip
    base = ["mvn", "-B", "-q", "-pl", "a,b", "-am", "test",
            "-Dsurefire.failIfNoSpecifiedTests=false"]  # fmt: skip

    run([*base, "-Dtest=acme.a.ATest,acme.b.BTest"], root)
    # Fully qualified names: acme.b.ATest (same simple name, other package) doesn't run.
    assert reported(root) == {("a", "acme.a.ATest"), ("b", "acme.b.BTest")}

    clean(root)
    run([*base, "-Dtest=acme.shared.SameTest"], root)
    # A name both modules have runs in both: why the selector keeps such a module apart.
    assert reported(root) == {("a", "acme.shared.SameTest"), ("b", "acme.shared.SameTest")}


def test_the_selector_keeps_a_module_with_a_shared_class_name_apart(tmp_path: Path) -> None:
    root = write(tmp_path, {"pom.xml": ROOT_POM, "a/pom.xml": CHILD_POM.format("a"),
                            "b/pom.xml": CHILD_POM.format("b"), **SOURCES})  # fmt: skip
    change = java_change(root, ["a/pom.xml"])
    assert change is not None
    java = JavaBuildInfo.from_json(change.payload())
    assert java.shared_classes == {"acme.shared.SameTest"}
    history = RepoHistory({i: KnownTest(i) for i in (
        "acme.a.ATest::ok()", "acme.shared.SameTest::ok()", "acme.b.BTest::ok()")})  # fmt: skip
    # a's POM: a's classes, SameTest among them (a's own copy: the known test ID is shared,
    # so b's copy runs too). A module with a shared name runs on its own.
    selection = select_tests(history, ["a/pom.xml"], True, java=java)
    assert selection.commands == (
        "mvn -B -pl a -am test -Dtest=acme.a.ATest,acme.shared.SameTest "
        "-Dsurefire.failIfNoSpecifiedTests=false",
        "mvn -B -pl b -am test -Dtest=acme.shared.SameTest -Dsurefire.failIfNoSpecifiedTests=false",
    )
    # Without a shared name, modules share one invocation.
    tests_changed = ["a/src/test/java/acme/a/ATest.java", "b/src/test/java/acme/b/BTest.java"]
    selection = select_tests(history, tests_changed, True, java=java)
    assert selection.commands == (
        "mvn -B -pl a,b -am test -Dtest=acme.a.ATest,acme.b.BTest "
        "-Dsurefire.failIfNoSpecifiedTests=false",
    )


@real_tools
def test_one_gradle_command_filters_each_project_on_its_own(tmp_path: Path) -> None:
    require("gradle")
    root = write(tmp_path, {"settings.gradle.kts": 'include("a", "b")',
                            "build.gradle.kts": GRADLE_ROOT, **SOURCES})  # fmt: skip
    run(["gradle", "-q", ":a:test", "--tests", "acme.shared.SameTest",
         ":b:test", "--tests", "acme.b.BTest"], root)  # fmt: skip
    # Each --tests filters the task before it: b's SameTest doesn't run.
    assert reported(root) == {("a", "acme.shared.SameTest"), ("b", "acme.b.BTest")}


@real_tools
def test_a_module_pom_change_runs_its_modules_and_dependents_tests(tmp_path: Path) -> None:
    require("mvn")
    root = tmp_path / "shop"
    shutil.copytree(FIXTURES / "java_maven", root, ignore=shutil.ignore_patterns("target"))
    change = java_change(root, ["core/pom.xml"])
    assert change is not None
    ids = ("acme.core.MoneyTest::plus", "acme.app.CartTest::total",
           "acme.app.ReceiptTest::renders", "acme.app.CartIT::emptyCart")  # fmt: skip
    selection = select_tests(RepoHistory({i: KnownTest(i) for i in ids}), ["core/pom.xml"],
                             True, java=JavaBuildInfo.from_json(change.payload()))  # fmt: skip
    assert len(selection.commands) == 1  # one Maven invocation for core and app
    run(shlex.split(selection.commands[0]), root)
    expected = {cls.partition("::")[0] for cls in ids}
    assert {cls for _, cls in reported(root)} == expected
