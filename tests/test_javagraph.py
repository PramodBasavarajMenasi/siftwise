"""Java selection (cli/javagraph.py and the selector's Java part): the build model, the
tokenizer and parser, resolution, name mapping, commands, safety fallbacks, triage links, and
the fixture projects, for real with SIFTWISE_REAL_TOOL_TESTS=1."""

import json
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from siftwise.cli import javagraph
from siftwise.cli import main as cli
from siftwise.cli.javagraph import (
    JavaBuild,
    build_java_graph,
    detect_build,
    java_change,
    parse_java,
    tokenize,
)
from siftwise.core.risk import Order
from siftwise.core.selector import (
    LINK_JAVA_USE,
    LINK_PATH,
    LINK_TEST_FILE,
    NO_UNIT_TESTS,
    JavaBuildInfo,
    JavaModuleInfo,
    KnownTest,
    Mode,
    RepoHistory,
    Runner,
    Strength,
    WouldRun,
    change_links,
    is_build_file,
    java_command,
    select_tests,
)
from tests.test_reruns import real_tools, require

FIXTURES = Path(__file__).parent / "fixtures"
MAVEN_FIXTURE = FIXTURES / "java_maven"
GRADLE_FIXTURE = FIXTURES / "java_gradle"
MONEY = "core/src/main/java/acme/core/Money.java"


def write(root: Path, files: dict[str, str]) -> Path:
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content, encoding="utf-8")
    return root


POM = '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>{}</project>'


# --- the build model ------------------------------------------------------------------------


def test_the_maven_fixture_build() -> None:
    build = detect_build(MAVEN_FIXTURE)
    assert build is not None and (build.tool, build.wrapper, build.unsupported) == (
        "maven",
        False,
        "",
    )
    by_path = {m.path: m for m in build.modules}
    assert set(by_path) == {"", "core", "app", "extras"}  # a profile's module counts too
    assert (by_path["core"].name, by_path["core"].unanalysed) == ("core", ())
    assert by_path["extras"].unanalysed == (
        "annotation processors (mapstruct-processor: generated sources aren't analysed)",
    )


def test_the_gradle_fixture_build() -> None:
    build = detect_build(GRADLE_FIXTURE)
    assert build is not None and (build.tool, build.unsupported) == ("gradle", "")
    names = {m.path: m.name for m in build.modules}
    # The commented-out include doesn't count; "app:web" is in app/web.
    assert names == {"": "", "core": ":core", "app/web": ":app:web", "kt": ":kt"}
    kt = next(m for m in build.modules if m.path == "kt")
    assert kt.unanalysed == ("Kotlin sources (src/main/kotlin isn't analysed)",)


def test_maven_modules_nest_and_inherit_their_parents_processors(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "pom.xml": POM.format("<modules><module>services</module></modules>"),
            "services/pom.xml": POM.format(
                "<modules><module>api/pom.xml</module><module>plain</module></modules>"
                "<build><plugins><plugin>"
                "<artifactId>maven-compiler-plugin</artifactId><configuration>"
                "<annotationProcessorPaths><path><artifactId>dagger-compiler</artifactId></path>"
                "</annotationProcessorPaths></configuration></plugin></plugins></build>"
            ),
            # A <parent> is inherited from (relativePath defaults to ../pom.xml); the
            # directory around a module isn't.
            "services/api/pom.xml": POM.format("<parent><artifactId>s</artifactId></parent>"),
            "services/plain/pom.xml": POM.format(""),
        },
    )
    build = detect_build(tmp_path)
    assert build is not None
    flags = {m.path: m.unanalysed for m in build.modules}
    assert flags["services/api"] == flags["services"] != ()
    assert flags["services/plain"] == flags[""] == ()


def test_lombok_alone_is_not_a_generating_processor(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "pom.xml": POM.format(
                "<build><plugins><plugin><configuration><annotationProcessorPaths><path>"
                "<artifactId>lombok</artifactId></path></annotationProcessorPaths></configuration>"
                "</plugin></plugins></build>"
            )
        },
    )
    build = detect_build(tmp_path)
    assert build is not None and build.modules[0].unanalysed == ()


def test_maven_custom_source_directories_are_not_analysed(tmp_path: Path) -> None:
    write(
        tmp_path,
        {"pom.xml": POM.format("<build><sourceDirectory>src/java</sourceDirectory></build>")},
    )
    build = detect_build(tmp_path)
    assert build is not None
    assert build.modules[0].unanalysed == ("custom sourceDirectory 'src/java'",)


@pytest.mark.parametrize(
    ("files", "why"),
    [
        ({"pom.xml": "<project><modules>"}, "pom.xml can't be read"),
        (
            {"pom.xml": POM.format("<modules><module>gone</module></modules>")},
            "module 'gone' of ./pom.xml isn't in the checkout",
        ),
        ({"settings.gradle": "include(projects)"}, "includes projects by something other than"),
        ({"settings.gradle": "includeBuild('logic')"}, "a composite build"),
        (
            {"settings.gradle": "include 'a'\nproject(':a').projectDir = file('x')"},
            "custom directories",
        ),
        ({"build.gradle": "plugins { id 'com.android.application' }"}, "an Android build"),
        ({"pom.xml": POM.format(""), "build.gradle": ""}, "both a Maven and a Gradle build"),
    ],
)
def test_a_build_siftwise_cant_read_is_unsupported(
    tmp_path: Path, files: dict[str, str], why: str
) -> None:
    build = detect_build(write(tmp_path, files))
    assert build is not None and why in build.unsupported


def test_gradle_settings_in_groovy_and_kotlin(tmp_path: Path) -> None:
    write(tmp_path, {"settings.gradle": "include 'a', ':b:c'\ninclude('d')\n/* include 'no' */"})
    build = detect_build(tmp_path)
    assert build is not None
    assert [m.name for m in build.modules] == ["", ":a", ":b:c", ":d"]
    assert [m.path for m in build.modules] == ["", "a", "b/c", "d"]


def test_gradle_custom_source_sets_and_processors(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "settings.gradle.kts": 'include("a", "b", "c")',
            "a/build.gradle.kts": 'sourceSets { main { java.srcDirs("gen") } }',
            "b/build.gradle.kts": 'dependencies { annotationProcessor("g:dagger") }',
            "c/build.gradle.kts": 'dependencies { annotationProcessor("g:lombok") }',
        },
    )
    build = detect_build(tmp_path)
    assert build is not None
    flags = {m.path: m.unanalysed for m in build.modules}
    assert flags["a"] == ("custom source sets",)
    assert flags["b"] == ("annotation processors (generated sources aren't analysed)",)
    assert flags["c"] == ()  # Lombok only


def test_a_gradle_wrapper_decides_between_two_builds(tmp_path: Path) -> None:
    build = detect_build(
        write(tmp_path, {"pom.xml": POM.format(""), "build.gradle": "", "gradlew": ""})
    )
    assert build is not None and (build.tool, build.wrapper, build.unsupported) == (
        "gradle",
        True,
        "",
    )


def test_no_java_build(tmp_path: Path) -> None:
    assert detect_build(tmp_path) is None


# --- the tokenizer and parser ---------------------------------------------------------------


def test_comments_strings_text_blocks_and_chars_hold_no_code() -> None:
    source = '''
        // import a.Line;
        /* class Block {} */
        String s = "class Str { import x.Y; }";
        String t = """
            text block: class Text, "quotes", \\""" still in it
            """;
        char c = '\\''; char d = '"'; char e = '{';
        Object after = new After();
    '''
    values = {t.value for t in tokenize(source) if t.kind == "id"}
    assert {"Line", "Block", "Str", "Y", "Text"}.isdisjoint(values)
    assert {"String", "char", "Object", "After"} <= values
    strings = [t.value for t in tokenize(source) if t.kind == "str"]
    assert strings[0] == "class Str { import x.Y; }"


def test_parse_reads_the_package_imports_and_top_level_types() -> None:
    source = """
        package a.b;
        import static x.Rates.EUR;
        import static x.Util.*;
        import c.d.*;
        import e.F;
        @Deprecated public sealed interface Shape permits Circle, Square {}
        record Circle(double r) implements Shape {}
        non-sealed class Square<T extends Comparable<T>> implements Shape {
            class Nested {}
            void m() { Map<String, List<Foo>> m = null; a.b.c.Fq.go(); Class.forName("q.Named"); }
        }
        @interface Marker {}
        enum Color { RED }
    """
    parsed = parse_java(source)
    assert parsed.package == "a.b"
    assert parsed.imports == (
        ("x.Rates.EUR", True, False),
        ("x.Util", True, True),
        ("c.d", False, True),
        ("e.F", False, False),
    )
    assert parsed.types == ("Shape", "Circle", "Square", "Marker", "Color")  # not Nested
    assert {"Map", "List", "Foo", "Comparable", "Deprecated"} <= parsed.identifiers
    assert ("a", "b", "c", "Fq", "go") in parsed.dotted
    assert parsed.strings == ("q.Named",)


# --- resolution -----------------------------------------------------------------------------


def graph_of(tmp_path: Path, files: dict[str, str], deleted: tuple[str, ...] = ()) -> Any:
    write(tmp_path, {"pom.xml": POM.format(""), **files})
    build = detect_build(tmp_path)
    assert build is not None
    return build_java_graph(tmp_path, build, deleted)


def java(path: str, body: str) -> dict[str, str]:
    return {f"src/main/java/{path}.java": body}


def test_every_kind_of_reference_resolves(tmp_path: Path) -> None:
    graph = graph_of(
        tmp_path,
        {
            **java(
                "a/Imported", "package a; public class Imported { public static class Inner {} }"
            ),
            **java("a/Statics", "package a; public class Statics { public static int N; }"),
            **java("b/Wild", "package b; public class Wild {}"),
            **java("b/Unused", "package b; public class Unused {}"),
            **java("c/Same", "package c; class Same {}"),
            **java("d/Fq", "package d; public class Fq { public static void go() {} }"),
            **java("e/Named", "package e; public class Named {}"),
            **java(
                "c/User",
                """package c;
            import a.Imported.Inner;
            import static a.Statics.N;
            import b.*;
            class User { Inner i; Wild w; Same s; int n = N;
                void m() throws Exception { d.Fq.go(); Class.forName("e.Named"); } }""",
            ),
        },
    )
    assert graph.edges["src/main/java/c/User.java"] == {
        "src/main/java/a/Imported.java",  # the nested class's file
        "src/main/java/a/Statics.java",  # import static
        "src/main/java/b/Wild.java",  # on demand: only the names used (not Unused)
        "src/main/java/c/Same.java",  # same package, no import
        "src/main/java/d/Fq.java",  # fully qualified in code
        "src/main/java/e/Named.java",  # a string naming a class (Class.forName)
    }
    assert not graph.dynamic_files


def test_an_import_of_a_class_no_source_declares_is_dynamic(tmp_path: Path) -> None:
    graph = graph_of(
        tmp_path,
        {
            **java("a/Entity", "package a; public class Entity {}"),
            **java("b/Repo", "package b; import a.QEntity; import java.util.List; class Repo {}"),
        },
    )
    # a.QEntity: generated (QueryDSL) in a package the sources have; java.util isn't ours.
    assert graph.dynamic_files == {"src/main/java/b/Repo.java": "a.QEntity"}


def test_a_deleted_class_is_still_used(tmp_path: Path) -> None:
    graph = graph_of(
        tmp_path,
        {
            "src/test/java/a/GoneTest.java": "package a; class GoneTest { Gone g; }",
        },
        deleted=("src/main/java/a/Gone.java",),
    )
    files, _, _ = graph.affected(["src/main/java/a/Gone.java"])
    assert files == {"src/test/java/a/GoneTest.java": ["src/main/java/a/Gone.java"]}


def test_the_fixture_graph_crosses_modules_with_the_way_in() -> None:
    change = java_change(MAVEN_FIXTURE, [MONEY])
    assert change is not None
    assert change.test_classes == {
        "app": ["acme.app.CartIT", "acme.app.CartTest", "acme.app.ReceiptTest"],
        "core": ["acme.core.MoneyTest"],
    }
    assert set(change.files) == {
        "app/src/test/java/acme/app/CartIT.java",
        "app/src/test/java/acme/app/CartTest.java",
        "app/src/test/java/acme/app/ReceiptTest.java",
        "core/src/test/java/acme/core/MoneyTest.java",
    }
    it = change.nearest["app/src/test/java/acme/app/CartIT.java"]
    assert (it.via, it.depth) == ("app/src/main/java/acme/app/Cart.java", 2)
    receipt = build_java_graph(MAVEN_FIXTURE, change.build).edges[
        "app/src/main/java/acme/app/Receipt.java"
    ]
    assert receipt == {
        "app/src/main/java/acme/app/Cart.java",  # same package
        "core/src/main/java/acme/core/Rates.java",  # import static
        "core/src/main/java/acme/core/text/Strings.java",
    }  # fully qualified
    payload = change.payload()
    assert payload["tool"] == "maven" and payload["test_classes"]["core"] == ["acme.core.MoneyTest"]


def test_no_java_change_no_graph_but_the_test_classes() -> None:
    change = java_change(MAVEN_FIXTURE, ["README.md"])
    assert change is not None and not change.files and change.test_classes["core"]


# --- selection ------------------------------------------------------------------------------


def info(
    fixture: Path = MAVEN_FIXTURE, changed: tuple[str, ...] = (MONEY,)
) -> tuple[JavaBuildInfo, dict[str, list[str]]]:
    change = java_change(fixture, list(changed))
    assert change is not None
    return JavaBuildInfo.from_json(change.payload()), change.files


def history(*ids: str) -> RepoHistory:
    return RepoHistory(
        tests={i: KnownTest(i, avg_duration_ms=10, runs=20, failures=0) for i in ids}
    )


MAVEN_TESTS = (
    "acme.core.MoneyTest::plus",
    "acme.app.CartTest::total",
    "acme.app.ReceiptTest::renders",
    "acme.app.CartIT::emptyCart",
)


def select(changed: list[str], java_info: JavaBuildInfo | None, **kwargs: Any) -> Any:
    return select_tests(history(*MAVEN_TESTS), changed, True, java=java_info, **kwargs)


def test_without_a_java_build_java_tests_have_no_runner() -> None:
    selection = select([MONEY], None, affected_files=info()[1])
    assert selection.mode is Mode.FULL


def test_a_change_runs_the_classes_that_use_it_per_module_in_risk_order() -> None:
    java_info, files = info()
    selection = select([MONEY], java_info, affected_files=files, order=Order.RISK)
    assert selection.mode is Mode.SELECTIVE
    reasons = {t.test_id: t.reasons for t in selection.tests}
    assert reasons["acme.core.MoneyTest::plus"][0] == (f"acme.core.MoneyTest tests changed {MONEY}")
    assert reasons["acme.app.CartIT::emptyCart"] == (
        "uses changed class acme.core.Money (via acme.app.Cart)",
    )
    assert {t.runner for t in selection.tests} == {Runner.MAVEN}
    # The directly mapped test's module first (risk order).
    assert selection.commands == (
        "mvn -B -pl core -am test -Dtest=acme.core.MoneyTest "
        "-Dsurefire.failIfNoSpecifiedTests=false",
        "mvn -B -pl app -am verify -Dtest=acme.app.CartTest,acme.app.ReceiptTest "
        "-Dsurefire.failIfNoSpecifiedTests=false -Dit.test=acme.app.CartIT "
        "-Dit.failIfNoSpecifiedTests=false",
    )


def test_the_gradle_fixture_selects_per_project() -> None:
    java_info, files = info(GRADLE_FIXTURE)
    ids = ("acme.core.MoneyTest::plus()", "acme.web.CartTest::total()")
    selection = select_tests(
        history(*ids), [MONEY], True, affected_files=files, java=java_info, order=Order.RISK
    )
    assert selection.commands == (
        "gradle :core:test --tests acme.core.MoneyTest",
        # ReceiptTest uses Money too but has no history: a new class, run first in its module.
        "gradle :app:web:test --tests acme.web.ReceiptTest --tests acme.web.CartTest",
    )


@pytest.mark.parametrize(
    ("name", "found"),
    [
        ("FooTest", True),
        ("FooTests", True),
        ("TestFoo", True),
        ("FooIT", True),
        ("FooTestCase", True),
        ("FooHelper", False),
    ],
)
def test_a_main_class_maps_to_its_tests_by_name(name: str, found: bool) -> None:
    java_info = JavaBuildInfo(
        "maven", modules=(JavaModuleInfo("m", "m"),), test_classes={f"a.{name}": "m"}
    )
    selection = select_tests(
        history(f"a.{name}::t"), ["m/src/main/java/a/Foo.java"], True, java=java_info
    )
    assert (selection.mode is Mode.SELECTIVE) is found


def test_name_mapping_prefers_the_same_module() -> None:
    java_info = JavaBuildInfo(
        "maven",
        modules=(JavaModuleInfo("x", "x"), JavaModuleInfo("y", "y")),
        test_classes={"a.FooTest": "y", "a.FooTests": "x"},
    )
    selection = select_tests(
        history("a.FooTest::t", "a.FooTests::t"),
        ["x/src/main/java/a/Foo.java"],
        True,
        java=java_info,
    )
    assert [t.test_id for t in selection.tests] == ["a.FooTests::t"]
    # No match in its own module: other modules' tests count.
    elsewhere = select_tests(
        history("a.FooTest::t"),
        ["z/src/main/java/a/Foo.java"],
        True,
        java=JavaBuildInfo(
            "maven",
            modules=(JavaModuleInfo("z", "z"), JavaModuleInfo("y", "y")),
            test_classes={"a.FooTest": "y"},
        ),
    )
    assert [t.test_id for t in elsewhere.tests] == ["a.FooTest::t"]


def test_a_new_or_changed_test_class_runs() -> None:
    java_info = JavaBuildInfo(
        "gradle",
        wrapper=True,
        modules=(JavaModuleInfo("m", ":m"),),
        test_classes={"a.NewTest": "m", "a.OldTest": "m"},
    )
    new = select_tests(
        history("a.OldTest::t"), ["m/src/test/java/a/NewTest.java"], True, java=java_info
    )
    assert new.mode is Mode.SELECTIVE and new.tests == ()
    assert new.commands == ("./gradlew :m:test --tests a.NewTest",)
    assert new.reason.endswith("; test class with no history: a.NewTest")
    changed = select_tests(
        history("a.OldTest::t"), ["m/src/test/java/a/OldTest.java"], True, java=java_info
    )
    assert [t.reasons for t in changed.tests] == [
        ("test file changed: m/src/test/java/a/OldTest.java",)
    ]


def test_test_resources_run_their_modules_tests() -> None:
    java_info, _ = info(changed=())
    selection = select(["app/src/test/resources/data.json"], java_info)
    assert {t.test_id for t in selection.tests} == {
        "acme.app.CartTest::total",
        "acme.app.ReceiptTest::renders",
        "acme.app.CartIT::emptyCart",
    }
    assert selection.tests[0].reasons == (
        "test resources changed in app: app/src/test/resources/data.json",
    )


@pytest.mark.parametrize(
    ("path", "why"),
    [
        ("core/src/main/resources/app.properties", "main resources changed"),
        ("core/src/main/kotlin/acme/X.kt", "Kotlin, Groovy or Scala source changed"),
        ("extras/src/main/java/acme/extras/Mapper.java", "extras isn't analysed (annotation"),
        ("core/src/java/acme/Odd.java", "Java file outside the build's source roots"),
        ("core/src/main/java/acme/core/Unused.java", "changed file maps to no known tests"),
    ],
)
def test_what_java_falls_back_on(path: str, why: str) -> None:
    java_info, _ = info(changed=())
    selection = select([path], java_info)
    assert selection.mode is Mode.FULL and why in selection.reason, selection.reason


def test_an_unreadable_build_falls_back_for_java_changes() -> None:
    java_info = JavaBuildInfo("gradle", unsupported="settings.gradle includes projects by ...")
    selection = select_tests(
        history("a.FooTest::t"), ["src/main/java/a/Foo.java"], True, java=java_info
    )
    assert selection.mode is Mode.FULL
    assert selection.reason.startswith("the Java build couldn't be read (settings.gradle")


@pytest.mark.parametrize(
    "path",
    [
        "pom.xml",
        "core/pom.xml",
        "build.gradle",
        "app/build.gradle.kts",
        "settings.gradle.kts",
        "gradle.properties",
        "gradle/libs.versions.toml",
        "gradle/wrapper/gradle-wrapper.properties",
        ".mvn/maven.config",
        "mvnw",
        "gradlew.bat",
        "lombok.config",
    ],
)
def test_java_build_files_force_the_full_suite(path: str) -> None:
    assert is_build_file(path)


def test_tests_that_use_generated_code_run_but_never_cover() -> None:
    java_info, files = info()
    dynamic = JavaBuildInfo.from_json(
        {
            **_payload(),
            "dynamic": {
                "app/src/test/java/acme/app/ReceiptTest.java": "app/src/main/java/acme/app/Gen.java"
            },
        }
    )
    covered = select(
        [MONEY], dynamic, affected_files={"core/src/test/java/acme/core/MoneyTest.java": [MONEY]}
    )
    assert {t.test_id for t in covered.tests} == {
        "acme.core.MoneyTest::plus",
        "acme.app.ReceiptTest::renders",
    }
    # A change only generated code might reach: the fallback stays.
    assert select(["core/src/main/java/acme/core/Rates.java"], dynamic).mode is Mode.FULL
    assert java_info.tool == "maven" and files


def _payload() -> dict[str, Any]:
    change = java_change(MAVEN_FIXTURE, [])
    assert change is not None
    return change.payload()


def test_a_deleted_test_class_isnt_run() -> None:
    java_info, _ = info(changed=())
    selection = select(
        [MONEY, "core/src/test/java/acme/core/MoneyTest.java"],
        java_info,
        deleted_files=["core/src/test/java/acme/core/MoneyTest.java"],
    )
    assert "acme.core.MoneyTest::plus" not in {t.test_id for t in selection.tests}


def test_the_full_suite_has_a_java_command() -> None:
    java_info, _ = info(changed=())
    selection = select(["Makefile"], java_info)
    assert selection.mode is Mode.FULL and selection.commands == (
        "mvn -B verify",
    )  # an IT is known


def test_a_budget_packs_java_classes() -> None:
    java_info, files = info()
    selection = select([MONEY], java_info, affected_files=files, budget_ms=15, order=Order.RISK)
    assert selection.budget is not None and selection.budget.limited
    assert selection.commands[0].startswith("mvn -B -pl core -am test -Dtest=acme.core.MoneyTest")


# --- commands -------------------------------------------------------------------------------


MAVEN = JavaBuildInfo(
    "maven", modules=(JavaModuleInfo("", "."), JavaModuleInfo("svc/api", "svc/api"))
)
GRADLE = JavaBuildInfo(
    "gradle", wrapper=True, modules=(JavaModuleInfo("", ""), JavaModuleInfo("svc/api", ":svc:api"))
)


@pytest.mark.parametrize(
    ("build", "module", "classes", "command"),
    [
        (
            MAVEN,
            "",
            ["a.FooTest"],
            "mvn -B test -Dtest=a.FooTest -Dsurefire.failIfNoSpecifiedTests=false",
        ),
        (
            MAVEN,
            "svc/api",
            ["a.FooIT"],
            f"mvn -B -pl svc/api -am verify -Dtest={NO_UNIT_TESTS} "
            "-Dsurefire.failIfNoSpecifiedTests=false -Dit.test=a.FooIT "
            "-Dit.failIfNoSpecifiedTests=false",
        ),
        (
            GRADLE,
            "",
            ["a.FooTest", "a.BarTest"],
            "./gradlew :test --tests a.FooTest --tests a.BarTest",
        ),
        (GRADLE, "svc/api", ["a.FooTest"], "./gradlew :svc:api:test --tests a.FooTest"),
    ],
)
def test_commands_per_tool_and_module(
    build: JavaBuildInfo, module: str, classes: list[str], command: str
) -> None:
    assert java_command(build, module, classes) == command


# --- triage ---------------------------------------------------------------------------------


def test_triage_links_java_tests_without_the_build_model() -> None:
    tests = [KnownTest(i) for i in MAVEN_TESTS]
    files = {"app/src/test/java/acme/app/CartTest.java": [MONEY]}
    links = change_links(
        tests, [MONEY, "app/src/test/java/acme/app/ReceiptTest.java"], affected_files=files
    )
    assert [(link.signal, link.strength) for link in links["acme.core.MoneyTest::plus"]] == [
        (LINK_PATH, Strength.HIGH)
    ]
    assert [(link.signal, link.strength) for link in links["acme.app.CartTest::total"]] == [
        (LINK_JAVA_USE, Strength.MEDIUM)
    ]
    assert [link.signal for link in links["acme.app.ReceiptTest::renders"]] == [LINK_TEST_FILE]
    assert links["acme.app.CartIT::emptyCart"] == ()


# --- the CLI --------------------------------------------------------------------------------


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def maven_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(MAVEN_FIXTURE, root)
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    (root / MONEY).write_text((root / MONEY).read_text() + "\n// changed\n", encoding="utf-8")
    git(root, "commit", "-am", "change money")
    monkeypatch.chdir(root)
    return root


@pytest.mark.usefixtures("maven_repo")
def test_the_cli_sends_the_build_and_the_edges() -> None:
    body = cli.selection_request("acme/shop", "HEAD~1", "HEAD")
    assert body["java"]["tool"] == "maven"
    assert body["java"]["test_classes"]["core"] == ["acme.core.MoneyTest"]
    assert body["affected_files"]["app/src/test/java/acme/app/CartTest.java"] == [MONEY]
    change = cli.local_change("HEAD~1", "HEAD")
    assert change.graph_complete and "java" not in change.fields  # /select only
    assert not cli.local_change("HEAD~1", "HEAD", java_graph=False).graph_complete


SERVER = "http://siftwise.test"


def test_an_older_server_gets_the_request_again_without_java() -> None:
    rejected = {"detail": [{"type": "extra_forbidden", "loc": ["body", "java"], "input": {}}]}
    body = {"repo": "acme/shop", "affected_files": {}, "java": {"tool": "maven"}}
    with respx.mock(assert_all_called=True) as api:
        route = api.post(f"{SERVER}/select")
        route.side_effect = [
            httpx.Response(422, json=rejected),
            httpx.Response(200, json={"mode": "full"}),
        ]
        assert cli.post_select(SERVER, "t", body) == {"mode": "full"}
        assert "java" not in json.loads(route.calls.last.request.content)


# --- for real -------------------------------------------------------------------------------


def run_selected(fixture: Path, root: Path, ids: tuple[str, ...]) -> set[str]:
    """Copy a fixture, select for a Money.java change, run the commands, and return the test
    classes that reported."""
    shutil.copytree(fixture, root)
    java_info, files = info(fixture)
    selection = select_tests(
        history(*ids), [MONEY], True, affected_files=files, java=java_info, order=Order.RISK
    )
    for command in selection.commands:
        argv = shlex.split(command)
        argv[0] = shutil.which(argv[0]) or argv[0]
        proc = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=900)
        assert proc.returncode == 0, (command, proc.stdout[-3000:], proc.stderr[-2000:])
    return {p.name.removeprefix("TEST-").removesuffix(".xml") for p in root.rglob("TEST-*.xml")}


@real_tools
def test_the_maven_commands_for_real(tmp_path: Path) -> None:
    require("mvn")
    ran = run_selected(MAVEN_FIXTURE, tmp_path / "shop", MAVEN_TESTS)
    assert ran == {
        "acme.core.MoneyTest",
        "acme.app.CartTest",
        "acme.app.ReceiptTest",
        "acme.app.CartIT",
    }


@real_tools
def test_the_gradle_commands_for_real(tmp_path: Path) -> None:
    require("gradle")
    ids = ("acme.core.MoneyTest::plus()", "acme.web.CartTest::total()")
    ran = run_selected(GRADLE_FIXTURE, tmp_path / "shop", ids)
    # ReceiptTest also uses Money but has no history here: it runs as a new test class.
    assert ran == {"acme.core.MoneyTest", "acme.web.CartTest", "acme.web.ReceiptTest"}


def test_javagraph_class_of_is_the_selectors() -> None:
    assert javagraph.class_of("core/src/test/java/a/b/FooTest.java") == (
        "core",
        "a.b.FooTest",
        True,
    )
    assert javagraph.class_of("src/main/java/Foo.java") == ("", "Foo", False)
    assert javagraph.class_of("core/src/java/Foo.java") is None
    assert isinstance(detect_build(MAVEN_FIXTURE), JavaBuild)


def test_checkers_that_generate_nothing_keep_a_module_analysed(tmp_path: Path) -> None:
    """ErrorProne, NullAway and the Checker Framework only check code (dropwizard's setup)."""
    write(tmp_path, {"pom.xml": POM.format(
        "<build><plugins><plugin><configuration><annotationProcessorPaths>"
        "<path><artifactId>error_prone_core</artifactId></path>"
        "<path><artifactId>nullaway</artifactId></path>"
        "<path><artifactId>errorprone-slf4j</artifactId></path>"
        "<path><artifactId>checker</artifactId></path>"
        "</annotationProcessorPaths></configuration></plugin></plugins></build>")})  # fmt: skip
    build = detect_build(tmp_path)
    assert build is not None and build.modules[0].unanalysed == ()


def test_a_class_run_whole_credits_its_new_tests() -> None:
    """WouldRun (shadow mode, the eval) credits every test of a class the commands run."""
    java_info = JavaBuildInfo("gradle", modules=(JavaModuleInfo("m", ":m"),),
                              test_classes={"a.NewTest": "m"})  # fmt: skip
    selection = select_tests(history("a.OldTest::t"), ["m/src/test/java/a/NewTest.java"], True,
                             java=java_info)  # fmt: skip
    assert selection.java_classes_run == ("a.NewTest",)
    runs = WouldRun(selection)
    assert runs("a.NewTest::brandNew()") and runs("a.NewTest$Nested::deep()")
    assert not runs("a.OldTest::t")
