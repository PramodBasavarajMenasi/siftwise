"""Kotlin in Java selection (#102): the parser (cli/kotlinsrc.py), Kotlin files in the Java
graph (both directions in mixed projects), test classes from Kotlin sources, selection, the
fallbacks, untested_changes, re-runs, and the fixture projects for real with
SIFTWISE_REAL_TOOL_TESTS=1 (tests/fixtures/kotlin_gradle, kotlin_maven, kotlin_mixed)."""

import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from siftwise.cli import javatests
from siftwise.cli import main as cli
from siftwise.cli.javagraph import build_java_graph, detect_build, java_change
from siftwise.cli.kotlinsrc import parse_kotlin, tokenize
from siftwise.cli.nearest import nearest_test
from siftwise.cli.reruns import Checkout, Target, classify, invocations
from siftwise.core.risk import Order
from siftwise.core.selector import (
    JavaBuildInfo,
    KnownTest,
    LinkStatus,
    Mode,
    NotChecked,
    RepoHistory,
    file_links,
    select_tests,
)
from tests.jvmfixture import copy_fixture
from tests.test_reruns import real_tools, require, run_for_real

FIXTURES = Path(__file__).parent / "fixtures"
GRADLE = FIXTURES / "kotlin_gradle"
MAVEN = FIXTURES / "kotlin_maven"
MIXED = FIXTURES / "kotlin_mixed"

MONEY = "core/src/main/kotlin/acme/core/Money.kt"
RATES = "core/src/main/kotlin/acme/core/Rates.kt"
STRINGS = "core/src/main/kotlin/acme/core/text/Strings.kt"
CART = "app/src/main/kotlin/acme/app/Cart.kt"
RECEIPT = "app/src/main/kotlin/acme/app/Receipt.kt"
BANNER = "app/src/main/kotlin/acme/app/Banner.kt"
TAX = "lib/src/main/java/acme/lib/Tax.java"
PRICE = "lib/src/main/kotlin/acme/lib/Price.kt"
DISCOUNTS = "lib/src/main/kotlin/acme/lib/Discounts.kt"
LABEL = "lib/src/main/java/acme/lib/Label.java"

# Test IDs as each tool reports them: Gradle adds "()", Surefire doesn't.
GRADLE_TESTS = (
    "acme.core.MoneyTest::adds two amounts()",
    "acme.core.MoneyTest::zero()",
    "acme.core.RatesTest::euro()",
    "acme.app.CartTest::total()",
    "acme.app.CartTest::in euro()",
    "acme.app.ReceiptTest::shouts()",
    "acme.app.BannerTest::text()",
)
MAVEN_TESTS = tuple(t.removesuffix("()") for t in GRADLE_TESTS)
MIXED_TESTS = (
    "acme.lib.TaxTest::tenPercent()",
    "acme.lib.InvoiceTest::total()",
    "acme.lib.LabelTest::brackets()",
    "acme.lib.PriceTest::taxed()",
)


def write(root: Path, files: dict[str, str]) -> Path:
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content, encoding="utf-8")
    return root


def history(*ids: str) -> RepoHistory:
    return RepoHistory(
        tests={i: KnownTest(i, avg_duration_ms=10, runs=20, failures=0) for i in ids}
    )


def selected(fixture: Path, changed: list[str], ids: tuple[str, ...], **kwargs: Any) -> Any:
    change = java_change(fixture, changed)
    assert change is not None
    java = JavaBuildInfo.from_json(change.payload())
    return select_tests(history(*ids), changed, True, affected_files=change.files, java=java,
                        **kwargs)  # fmt: skip


def classes(selection: Any) -> set[str]:
    return {t.test_id.split("::", 1)[0] for t in selection.tests}


# --- the tokenizer and the parser -------------------------------------------------------------


def test_comments_strings_and_templates() -> None:
    tokens, ok = tokenize('/* a /* nested */ Hidden */ val s = "x ${Shown.y} $name" + """raw\n'
                          ' "Quoted" ${ Raw.z }""" // Gone\n')  # fmt: skip
    ids = {t.value for t in tokens if t.kind == "id"}
    assert ok and {"Shown", "y", "name", "Raw", "z"} <= ids
    assert not {"Hidden", "Gone", "Quoted"} & ids  # comments and string text aren't code
    assert [t.kind for t in tokens if t.kind in ("str", "tstr")] == ["tstr", "tstr"]


def test_backtick_names_and_char_literals() -> None:
    tokens, ok = tokenize("fun `adds two`() = '}' + '\\'' + 1..2\n")
    assert ok and [t.value for t in tokens][:2] == ["fun", "adds two"]
    assert "}" not in [t.value for t in tokens]  # the char literal holds no brace


def test_the_parser_reads_imports_declarations_and_members() -> None:
    source = parse_kotlin("""
@file:JvmName("Prices")
package acme.shop

import acme.core.Money
import acme.core.Rates as R
import acme.util.*
import acme.core.format

typealias Cents = Long
enum class Kind { A, B }
data class Item(val name: String, val price: Money)
sealed interface Shape
object Registry { fun get() = 1 }
fun interface Pricer { fun price(): Long }
annotation class Marker
class Cart(private val items: List<Item>) { val inner = 1 }
fun <T> List<T>.second(): T = this[1]
val String.shout: String get() = uppercase()
val lazyThing by lazy { R.load() }
operator fun Money.plus(other: Money): Money = Money(cents + other.cents)
const val MAX = 10
val k = Cart::class
""")
    assert source.ok and source.package == "acme.shop"
    assert [(i.name, i.star, i.alias) for i in source.imports] == [
        ("acme.core.Money", False, None),
        ("acme.core.Rates", False, "R"),
        ("acme.util", True, None),
        ("acme.core.format", False, None),
    ]
    assert source.types == (
        "Cents",
        "Kind",
        "Item",
        "Shape",
        "Registry",
        "Pricer",
        "Marker",
        "Cart",
    )
    assert source.classes == ("Item", "Registry", "Cart")  # what a runner can run
    # Constructor parameters and members of classes aren't top-level.
    assert source.members == ("second", "shout", "lazyThing", "plus", "MAX", "k")
    assert source.operators and source.jvm_name == "Prices"
    assert source.facade("Prices") == "Prices"
    assert parse_kotlin("package a\nfun f() = 1\n").facade("cart") == "CartKt"


@pytest.mark.parametrize(
    "source",
    ['val x = "open\n', "class A {\n", "/* open", "fun f() = g(\n", 'val s = "${open"\n'],
)
def test_what_cant_be_closed_isnt_ok(source: str) -> None:
    assert not parse_kotlin(source).ok


# --- the build model and the graph ------------------------------------------------------------


def test_kotlin_builds_are_analysed() -> None:
    for fixture in (GRADLE, MAVEN, MIXED):
        build = detect_build(fixture)
        assert build is not None and not build.unsupported
        assert all(not m.unanalysed for m in build.modules), build.modules


def test_test_classes_come_from_the_kotlin_sources() -> None:
    change = java_change(GRADLE, [])
    assert change is not None
    # ReceiptTests.kt holds ReceiptTest; MoneyTest.kt holds MoneyTest and RatesTest.
    assert change.test_classes == {
        "app": ["acme.app.BannerTest", "acme.app.CartTest", "acme.app.ReceiptTest"],
        "core": ["acme.core.MoneyTest", "acme.core.RatesTest"],
    }
    payload = change.payload()
    assert payload["kotlin_files"]["app/src/test/kotlin/acme/app/ReceiptTests.kt"] == {
        "package": "acme.app", "classes": ["ReceiptTest"],
    }  # fmt: skip
    assert "unparsed" not in payload


@pytest.mark.parametrize(
    ("changed", "users"),
    [
        # A member import of an extension function: only Receipt uses it.
        (STRINGS, {"app/src/test/kotlin/acme/app/ReceiptTests.kt"}),
        # An alias import (rate as exchangeRate) and the same package without an import.
        (RATES, {"app/src/test/kotlin/acme/app/CartTest.kt",
                 "app/src/test/kotlin/acme/app/ReceiptTests.kt",
                 "core/src/test/kotlin/acme/core/MoneyTest.kt"}),
        # A top-level function, used through the same package (Receipt -> Cart).
        (RECEIPT, {"app/src/test/kotlin/acme/app/ReceiptTests.kt"}),
        (BANNER, {"app/src/test/kotlin/acme/app/BannerTest.kt"}),
    ],
)  # fmt: skip
def test_kotlin_edges(changed: str, users: set[str]) -> None:
    change = java_change(GRADLE, [changed])
    assert change is not None and set(change.files) == users


@pytest.mark.parametrize(
    ("changed", "users"),
    [
        # Kotlin uses Java: Price calls Tax (same package, no import).
        (TAX, {"lib/src/test/java/acme/lib/InvoiceTest.java",
               "lib/src/test/java/acme/lib/TaxTest.java",
               "lib/src/test/kotlin/acme/lib/PriceTest.kt"}),
        # Java uses Kotlin: Invoice calls the @file:JvmName("Discounts") facade.
        (DISCOUNTS, {"lib/src/test/java/acme/lib/InvoiceTest.java"}),
        # ... and the Kotlin class.
        (PRICE, {"lib/src/test/java/acme/lib/InvoiceTest.java",
                 "lib/src/test/kotlin/acme/lib/PriceTest.kt"}),
        (LABEL, {"lib/src/test/java/acme/lib/LabelTest.java"}),
    ],
)  # fmt: skip
def test_mixed_edges_both_ways(changed: str, users: set[str]) -> None:
    change = java_change(MIXED, [changed])
    assert change is not None and set(change.files) == users


def test_an_operator_function_links_its_package_and_importers(tmp_path: Path) -> None:
    root = write(tmp_path, {
        "settings.gradle.kts": "", "build.gradle.kts": 'plugins { kotlin("jvm") }\n',
        "src/main/kotlin/a/Ops.kt": "package a\noperator fun Int.invoke(x: Int) = this + x\n",
        "src/main/kotlin/a/Use.kt": "package a\nfun use() = 1(2)\n",  # names no function
        "src/main/kotlin/b/Far.kt": "package b\nimport a.*\nfun far() = 3(4)\n",
        "src/main/kotlin/c/None.kt": "package c\nfun none() = 5\n",
    })  # fmt: skip
    build = detect_build(root)
    assert build is not None
    edges = build_java_graph(root, build).edges
    assert "src/main/kotlin/a/Ops.kt" in edges["src/main/kotlin/a/Use.kt"]
    assert "src/main/kotlin/a/Ops.kt" in edges["src/main/kotlin/b/Far.kt"]
    assert not edges["src/main/kotlin/c/None.kt"]


def test_an_import_nothing_declares_and_an_unparsable_file_are_dynamic(tmp_path: Path) -> None:
    root = write(tmp_path, {
        "settings.gradle.kts": "", "build.gradle.kts": 'plugins { kotlin("jvm") }\n',
        "src/main/kotlin/a/Real.kt": "package a\nclass Real\n",
        "src/main/kotlin/a/Gen.kt": "package a\nimport a.Generated\nclass Gen\n",
        "src/main/kotlin/a/Broken.kt": 'package a\nclass Broken { val s = "open\n',
        "src/test/kotlin/a/GenTest.kt": "package a\nclass GenTest { val g = Gen() }\n",
        "src/test/kotlin/a/BrokenTest.kt": "package a\nclass BrokenTest { val b = Broken() }\n",
    })  # fmt: skip
    build = detect_build(root)
    assert build is not None
    graph = build_java_graph(root, build)
    assert graph.dynamic_files["src/main/kotlin/a/Gen.kt"] == "a.Generated"
    assert graph.unparsed == {"src/main/kotlin/a/Broken.kt"}
    _, _, dynamic = graph.affected(["src/main/kotlin/a/Real.kt"])
    assert dynamic == {
        "src/test/kotlin/a/BrokenTest.kt": "src/main/kotlin/a/Broken.kt",
        "src/test/kotlin/a/GenTest.kt": "src/main/kotlin/a/Gen.kt",
    }


# --- selection ------------------------------------------------------------------------------


def test_a_kotlin_change_selects_its_kotlin_tests() -> None:
    selection = selected(GRADLE, [STRINGS], GRADLE_TESTS, order=Order.RISK)
    assert selection.mode is Mode.SELECTIVE, selection.reason
    assert classes(selection) == {"acme.app.ReceiptTest"}  # nothing else uses shout()
    assert selection.commands == ("gradle :app:test --tests acme.app.ReceiptTest",)


def test_name_mapping_by_file_and_by_class() -> None:
    # Receipt.kt (only a top-level function) -> ReceiptTest, declared in ReceiptTests.kt.
    selection = selected(GRADLE, [RECEIPT], GRADLE_TESTS)
    assert classes(selection) == {"acme.app.ReceiptTest"}
    assert "acme.app.ReceiptTest tests changed" in selection.tests[0].reasons[0]


def test_the_alias_import_selects_through_the_graph() -> None:
    selection = selected(GRADLE, [RATES], GRADLE_TESTS)
    assert classes(selection) == {"acme.core.RatesTest", "acme.core.MoneyTest",
                                  "acme.app.CartTest", "acme.app.ReceiptTest"}  # fmt: skip
    assert "acme.app.BannerTest" not in classes(selection)


def test_a_changed_kotlin_test_file_runs_every_class_in_it() -> None:
    selection = selected(GRADLE, ["core/src/test/kotlin/acme/core/MoneyTest.kt"], GRADLE_TESTS)
    assert classes(selection) == {"acme.core.MoneyTest", "acme.core.RatesTest"}
    assert selection.commands == (
        "gradle :core:test --tests acme.core.MoneyTest --tests acme.core.RatesTest",
    )


def test_a_new_kotlin_test_class_runs_though_history_doesnt_know_it(tmp_path: Path) -> None:
    root = tmp_path / "shop"
    copy_fixture(GRADLE, root)
    new = "app/src/test/kotlin/acme/app/Extra.kt"
    write(root, {new: "package acme.app\nclass ExtraTest { }\nclass NotATestHelper\n"})
    selection = selected(root, [new], GRADLE_TESTS)
    assert selection.mode is Mode.SELECTIVE
    assert selection.commands == ("gradle :app:test --tests acme.app.ExtraTest",)


def test_maven_kotlin_commands() -> None:
    selection = selected(MAVEN, [MONEY], MAVEN_TESTS, order=Order.RISK)
    assert classes(selection) == {"acme.core.MoneyTest", "acme.core.RatesTest",
                                  "acme.app.CartTest", "acme.app.ReceiptTest"}  # fmt: skip
    [command] = selection.commands
    assert command.startswith("mvn -B -pl ") and " -am test " in command


def test_mixed_selection_both_ways() -> None:
    java_to_kotlin = selected(MIXED, [DISCOUNTS], MIXED_TESTS)
    assert classes(java_to_kotlin) == {"acme.lib.InvoiceTest"}
    kotlin_to_java = selected(MIXED, [TAX], MIXED_TESTS)
    assert classes(kotlin_to_java) == {"acme.lib.TaxTest", "acme.lib.InvoiceTest",
                                       "acme.lib.PriceTest"}  # fmt: skip
    unrelated = selected(MIXED, [LABEL], MIXED_TESTS)
    assert classes(unrelated) == {"acme.lib.LabelTest"}


def test_a_source_no_test_reaches_still_falls_back() -> None:
    root_kt = "app/src/main/kotlin/acme/app/Lonely.kt"
    change = java_change(GRADLE, [root_kt])
    assert change is not None
    java = JavaBuildInfo.from_json(change.payload())
    selection = select_tests(history(*GRADLE_TESTS), [root_kt], True, java=java)
    assert selection.mode is Mode.FULL and "maps to no known tests" in selection.reason


# --- what falls back ----------------------------------------------------------------------------


def fallback(tmp_path: Path, files: dict[str, str], changed: str) -> str:
    root = write(tmp_path, {"settings.gradle.kts": "", **files})
    change = java_change(root, [changed])
    assert change is not None
    java = JavaBuildInfo.from_json(change.payload())
    selection = select_tests(history("a.ATest::t()"), [changed], True, java=java,
                             affected_files=change.files)  # fmt: skip
    assert selection.mode is Mode.FULL
    return selection.reason


KT = "src/main/kotlin/a/A.kt"
A = {KT: "package a\nclass A\n", "src/test/kotlin/a/ATest.kt": "package a\nclass ATest\n"}


@pytest.mark.parametrize(
    ("files", "changed", "why"),
    [
        ({"build.gradle.kts": 'plugins { kotlin("multiplatform") }\n', **A}, KT,
         "isn't analysed (Kotlin Multiplatform (its source sets aren't analysed))"),
        ({"build.gradle.kts": 'plugins { kotlin("jvm") }\n',
          "src/commonMain/kotlin/a/C.kt": "package a\nclass C\n", **A},
         "src/commonMain/kotlin/a/C.kt",
         "isn't analysed (Kotlin Multiplatform source sets (src/commonMain isn't analysed))"),
        ({"build.gradle.kts": 'plugins { kotlin("jvm"); kotlin("kapt") }\n', **A}, KT,
         "annotation processors"),
        ({"build.gradle.kts": 'plugins { id("com.google.devtools.ksp") }\n', **A}, KT,
         "annotation processors"),
        ({"build.gradle.kts": 'sourceSets { main { kotlin.srcDir("gen") } }\n', **A}, KT,
         "custom source sets"),
        ({"build.gradle.kts": "", "src/main/groovy/a/G.groovy": "class G {}\n", **A},
         "src/main/groovy/a/G.groovy", "Groovy or Scala source changed"),
        ({"build.gradle.kts": "", **A}, "tools/release.main.kts",
         "Kotlin script changed (not analysed)"),
        ({"build.gradle.kts": "", **A, KT: "package a\nclass A {\n"}, KT,
         "Kotlin file siftwise couldn't parse"),
        ({"build.gradle.kts": "", **A}, "src/kotlin/a/Odd.kt",
         "Kotlin file outside the build's source roots"),
        ({"build.gradle.kts": "", **A}, "src/main/kotlin/a/Odd.java",
         "Java file outside the build's source roots"),
        ({"build.gradle.kts": 'plugins { id("com.android.library") }\n', **A}, KT,
         "the Java build couldn't be read (an Android build"),
    ],
)  # fmt: skip
def test_what_falls_back(tmp_path: Path, files: dict[str, str], changed: str, why: str) -> None:
    reason = fallback(tmp_path, files, changed)
    assert why in reason, reason


def test_a_deleted_kotlin_source_falls_back_but_a_deleted_test_doesnt() -> None:
    change = java_change(GRADLE, [])
    assert change is not None
    java = JavaBuildInfo.from_json(change.payload())
    gone_source = select_tests(history(*GRADLE_TESTS), [RATES], True, java=java,
                               deleted_files=[RATES])  # fmt: skip
    assert gone_source.mode is Mode.FULL and "Kotlin file deleted" in gone_source.reason
    test = "app/src/test/kotlin/acme/app/BannerTest.kt"
    gone_test = select_tests(history(*GRADLE_TESTS), [test], True, java=java,
                             deleted_files=[test])  # fmt: skip
    assert gone_test.mode is Mode.SELECTIVE and not gone_test.tests


def test_maven_kotlin_source_dirs(tmp_path: Path) -> None:
    pom = ('<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
           "<artifactId>a</artifactId><build><sourceDirectory>src/main/kotlin</sourceDirectory>"
           "<plugins><plugin><artifactId>kotlin-maven-plugin</artifactId><executions><execution>"
           "<configuration><sourceDirs><sourceDir>{}</sourceDir></sourceDirs></configuration>"
           "</execution></executions></plugin></plugins></build></project>")  # fmt: skip
    write(tmp_path / "ok", {"pom.xml": pom.format("${project.basedir}/src/main/java")})
    write(tmp_path / "custom", {"pom.xml": pom.format("src/generated/kotlin")})
    write(tmp_path / "kapt", {"pom.xml": pom.format("src/main/kotlin").replace(
        "<execution>", "<execution><goals><goal>kapt</goal></goals>")})  # fmt: skip
    flags = {}
    for name in ("ok", "custom", "kapt"):
        build = detect_build(tmp_path / name)
        assert build is not None
        flags[name] = build.modules[0].unanalysed
    assert flags["ok"] == ()
    assert flags["custom"] == ("custom Kotlin sourceDirs 'src/generated/kotlin'",)
    assert flags["kapt"] == ("annotation processors (kapt: generated sources aren't analysed)",)


# --- untested_changes ---------------------------------------------------------------------------


def test_kotlin_files_take_the_normal_path_in_untested_changes() -> None:
    change = java_change(GRADLE, [STRINGS, BANNER, "app/src/main/kotlin/acme/app/Lonely.kt"])
    assert change is not None
    java = JavaBuildInfo.from_json(change.payload())
    found = {f.path: f for f in file_links(
        history(*GRADLE_TESTS), [STRINGS, "app/src/main/kotlin/acme/app/Lonely.kt",
                                 "app/src/test/kotlin/acme/app/CartTest.kt"], True,
        affected_files=change.files, java=java,
    )}  # fmt: skip
    assert found[STRINGS].status is LinkStatus.LINKED
    assert found[STRINGS].tests == ("acme.app.ReceiptTest::shouts()",)
    assert found["app/src/main/kotlin/acme/app/Lonely.kt"].status is LinkStatus.NO_LINKED_TEST
    assert found["app/src/test/kotlin/acme/app/CartTest.kt"].status is LinkStatus.TEST_FILE
    groovy = file_links(history(*GRADLE_TESTS), ["src/main/groovy/a/G.groovy"], True, java=java)
    assert groovy[0].reason_code is NotChecked.JAVA_BUILD


def test_nearest_kotlin_test() -> None:
    files = [MONEY, "core/src/test/kotlin/acme/core/MoneyTest.kt", RATES, CART]
    assert nearest_test(MONEY, files) == "core/src/test/kotlin/acme/core/MoneyTest.kt"
    assert nearest_test(RATES, files) == "core/src/test/kotlin/acme/core/MoneyTest.kt"
    assert nearest_test(CART, files) is None


# --- re-runs ------------------------------------------------------------------------------------

KOTLIN_TESTS = """package acme

import org.junit.jupiter.api.*
import org.junit.jupiter.params.ParameterizedTest
import org.junit.jupiter.params.provider.ValueSource

class CartTest {
    @Test
    fun `passes the second time`() {
        val marker = java.io.File("ran-once")
        val first = !marker.exists()
        marker.writeText("")
        Assertions.assertFalse(first)
    }

    @Test
    fun plain() { Assertions.assertTrue(true) }

    @Test
    @DisplayName("shown name")
    fun named() { Assertions.assertTrue(true) }

    @ParameterizedTest(name = "{0} is one")
    @ValueSource(ints = [1, 2])
    fun one(n: Int) { Assertions.assertEquals(1, n) }

    @Nested
    inner class Inside {
        @Test
        fun `also fails`() { Assertions.fail<Unit>("always") }
    }

    @Test
    fun `not re-run`() { Assertions.fail<Unit>("must be filtered out") }
}
"""


def test_kotlin_test_classes_and_methods() -> None:
    found = {c.qualified: c for c in javatests.parse_kotlin_test_classes(KOTLIN_TESTS)}
    assert set(found) == {"acme.CartTest", "acme.CartTest$Inside"}
    methods = {m.name: m for m in found["acme.CartTest"].methods}
    assert set(methods) == {"passes the second time", "plain", "named", "one", "not re-run"}
    assert methods["named"].display == "shown name"
    assert methods["one"].template == "{0} is one"
    cls = list(found.values())
    assert javatests.find_method(cls, "acme.CartTest", "passes the second time()") == (
        "acme.CartTest", "passes the second time",
    )  # Gradle's name  # fmt: skip
    assert javatests.find_method(cls, "acme.CartTest", "passes the second time") == (
        "acme.CartTest", "passes the second time",
    )  # Surefire's  # fmt: skip
    assert javatests.find_method(cls, "acme.CartTest", "shown name") == ("acme.CartTest", "named")
    assert javatests.find_method(cls, "acme.CartTest", "[2] 2 is one") is not None
    assert javatests.find_method(cls, "acme.CartTest$Inside", "also fails()") == (
        "acme.CartTest$Inside", "also fails",
    )  # fmt: skip


def kotlin_checkout(root: Path, source: str = KOTLIN_TESTS, name: str = "Tests.kt") -> Path:
    # The file isn't named after its class: the re-run finds the class in any Kotlin test file.
    return write(root, {"settings.gradle.kts": 'rootProject.name = "cart"\n',
                        f"src/test/kotlin/acme/{name}": source})  # fmt: skip


def test_kotlin_tests_re_run_by_method(tmp_path: Path) -> None:
    checkout = Checkout(kotlin_checkout(tmp_path))
    target = classify("acme.CartTest::passes the second time()", None, checkout)
    assert target == Target("acme.CartTest::passes the second time()", "gradle",
                            "acme.CartTest\0passes the second time")  # fmt: skip
    nested = classify("acme.CartTest$Inside::also fails()", None, checkout)
    assert isinstance(nested, Target) and nested.arg == "acme.CartTest$Inside\0also fails"
    [command] = invocations([target, nested], tmp_path / "out", 1, (), tmp_path)
    assert command.argv[1:] == ["test", "--continue",
                                "--tests", "acme.CartTest.passes the second time",
                                "--tests", "acme.CartTest$Inside.also fails"]  # fmt: skip


def test_a_kotlin_name_siftwise_wont_pass_on_isnt_re_run(tmp_path: Path) -> None:
    source = "package acme\nclass OddTest {\n    @Test fun `a, b`() {}\n}\n"
    checkout = Checkout(kotlin_checkout(tmp_path, source))
    why = classify("acme.OddTest::a, b()", None, checkout)
    assert why == "a test method name siftwise won't pass to gradle: 'a, b'"


# --- the CLI ----------------------------------------------------------------------------------


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_the_cli_builds_the_graph_for_a_kotlin_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    copy_fixture(GRADLE, root)
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    (root / STRINGS).write_text((root / STRINGS).read_text() + "\n// changed\n", encoding="utf-8")
    git(root, "commit", "-am", "change")
    monkeypatch.chdir(root)
    body = cli.selection_request("acme/shop", "HEAD~1", "HEAD")
    assert body["affected_files"] == {"app/src/test/kotlin/acme/app/ReceiptTests.kt": [STRINGS]}
    assert body["java"]["kotlin_files"][STRINGS] == {"package": "acme.core.text", "classes": []}
    assert body["java"]["test_classes"]["core"] == ["acme.core.MoneyTest", "acme.core.RatesTest"]
    assert not cli.local_change("HEAD~1", "HEAD", java_graph=False).graph_complete


# --- for real -----------------------------------------------------------------------------------


def run_selected(fixture: Path, root: Path, changed: str, ids: tuple[str, ...]) -> set[str]:
    """Copy a fixture, select for a change, run the commands, and return the test classes
    that reported."""
    copy_fixture(fixture, root)
    selection = selected(root, [changed], ids, order=Order.RISK)
    assert selection.mode is Mode.SELECTIVE, selection.reason
    for command in selection.commands:
        argv = shlex.split(command)
        argv[0] = shutil.which(argv[0]) or argv[0]
        proc = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=1200)
        assert proc.returncode == 0, (command, proc.stdout[-3000:], proc.stderr[-2000:])
    return {p.name.removeprefix("TEST-").removesuffix(".xml") for p in root.rglob("TEST-*.xml")}


@real_tools
def test_the_kotlin_gradle_commands_for_real(tmp_path: Path) -> None:
    require("gradle")
    ran = run_selected(GRADLE, tmp_path / "shop", STRINGS, GRADLE_TESTS)
    assert ran == {"acme.app.ReceiptTest"}


@real_tools
def test_the_kotlin_maven_commands_for_real(tmp_path: Path) -> None:
    require("mvn")
    ran = run_selected(MAVEN, tmp_path / "shop", RATES, MAVEN_TESTS)
    assert ran == {"acme.core.MoneyTest", "acme.core.RatesTest", "acme.app.CartTest",
                   "acme.app.ReceiptTest"}  # fmt: skip


@real_tools
def test_the_mixed_commands_for_real(tmp_path: Path) -> None:
    require("gradle")
    ran = run_selected(MIXED, tmp_path / "mixed", DISCOUNTS, MIXED_TESTS)
    assert ran == {"acme.lib.InvoiceTest"}  # Java test of a Kotlin change


GRADLE_BUILD = """plugins { kotlin("jvm") version "2.2.20" }
repositories { mavenCentral() }
dependencies {
    testImplementation(platform("org.junit:junit-bom:5.11.3"))
    testImplementation("org.junit.jupiter:junit-jupiter")
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")
}
tasks.test { useJUnitPlatform() }
"""


@real_tools
def test_kotlin_re_runs_with_gradle_for_real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require("gradle")
    root = kotlin_checkout(tmp_path / "cart")
    monkeypatch.chdir(root)  # record re-runs in the job's checkout
    write(root, {"build.gradle.kts": GRADLE_BUILD})
    (root / "ran-once").touch()
    ids = ("acme.CartTest::passes the second time()", "acme.CartTest$Inside::also fails()")
    statuses = run_for_real([classify(t, None, Checkout(root)) for t in ids], root)
    assert statuses[ids[0]].value == "passed"
    assert statuses[ids[1]].value == "failed"
    assert "acme.CartTest::not re-run()" not in statuses


# The Maven fixture's root POM as a single project.
KOTLIN_POM = (
    MAVEN.joinpath("pom.xml").read_text(encoding="utf-8")
    .replace("<packaging>pom</packaging>", "")
    .replace("<module>core</module>", "")
    .replace("<module>app</module>", "")
)  # fmt: skip


@real_tools
def test_kotlin_re_runs_with_maven_for_real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require("mvn")
    root = tmp_path / "cart"
    write(root, {"pom.xml": KOTLIN_POM, "src/test/kotlin/acme/Tests.kt": KOTLIN_TESTS})
    monkeypatch.chdir(root)
    (root / "ran-once").touch()
    ids = ("acme.CartTest::passes the second time", "acme.CartTest$Inside::also fails")
    statuses = run_for_real([classify(t, None, Checkout(root)) for t in ids], root)
    assert statuses[ids[0]].value == "passed"
    assert statuses[ids[1]].value == "failed"
    assert "acme.CartTest::not re-run" not in statuses
