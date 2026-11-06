"""JUnit 5 display names mapped back to methods (cli/javatests.py), and re-runs by method."""

from __future__ import annotations

from pathlib import Path

import pytest

from siftwise.cli.javatests import UNKNOWN, find_method, parse_test_classes
from siftwise.cli.reruns import NO_RUNNER, Checkout, Target, classify

# The names below are what Gradle 8.10.2 and Surefire 3.5.2 (phrased reporter) wrote for this
# class with JUnit 5.11.3, checked by hand.
CART_TEST = """package acme;

import static org.junit.jupiter.api.Assertions.*;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.*;

@DisplayName("A shopping cart")
class CartTest {
    private static final String NAME = "constant";
    private final int[] numbers = {1, 2};

    @Test @DisplayName("adds up the total") void total() { fail("x"); }
    @Test void plain() { }
    @ParameterizedTest @ValueSource(ints = {1, 2}) void param(int n) { assertEquals(1, n); }
    @ParameterizedTest(name = "{0} plus {1}") @CsvSource({"1,2", "3,4"}) @DisplayName("sums")
    void sums(int a, int b) { assertEquals(3, a + b); }
    @RepeatedTest(3) void again(@SuppressWarnings("x") RepetitionInfo info) { }
    @Test
    @DisplayName("a long " +
                 "name") // joined literals
    void joined() { new Thread() { public void run() { helper("not a test"); } }; }
    void helper(String s) { if (s.isEmpty()) { return; } }

    @Nested @DisplayName("when empty") class Empty {
        @Test @DisplayName("has no items") void none() { fail("y"); }
        @Test @DisplayName("adds up the total") void total() { }
    }
}
"""

# Gradle's names (classname = the class, name = the display name or invocation name), and
# Surefire's phrased ones (classname = the class's display names).
NAMES = [
    ("acme.CartTest", "adds up the total", "total"),
    ("acme.CartTest", "plain()", "plain"),
    ("acme.CartTest", "[1] 1", "param"),
    ("acme.CartTest", "1 plus 2", "sums"),
    ("acme.CartTest", "repetition 2 of 3", "again"),
    ("acme.CartTest", "a long name", "joined"),
    ("acme.CartTest", "total", "total"),  # Surefire's default names: the method
    ("acme.CartTest", "sums(int, int)[2]", "sums"),
    ("acme.CartTest$Empty", "has no items", "none"),
    ("acme.CartTest$Empty", "adds up the total", "total"),
    ("A shopping cart", "adds up the total", "total"),
    ("A shopping cart", "sums 1 plus 2", "sums"),
    ("A shopping cart", "param(int)[1] 1", "param"),
    ("A shopping cart when empty", "has no items", "none"),
]


@pytest.mark.parametrize(("classname", "name", "method"), NAMES)
def test_reported_names_map_to_their_method(classname: str, name: str, method: str) -> None:
    owner = "acme.CartTest$Empty" if "mpty" in classname else "acme.CartTest"

    assert find_method(parse_test_classes(CART_TEST), classname, name) == (owner, method)


def test_the_parser_reads_classes_methods_and_names() -> None:
    cart, empty = parse_test_classes(CART_TEST)

    assert (cart.qualified, cart.phrased) == ("acme.CartTest", "A shopping cart")
    assert (empty.qualified, empty.phrased) == ("acme.CartTest$Empty", "A shopping cart when empty")
    # Only the class's own methods: not the anonymous class's run(), not calls.
    assert [m.name for m in cart.methods] == [
        "total", "plain", "param", "sums", "again", "joined", "helper"]  # fmt: skip
    assert {m.name: m.template for m in cart.methods if m.template} == {
        "param": "[{index}] {arguments}",
        "sums": "{0} plus {1}",
        "again": "repetition {currentRepetition} of {totalRepetitions}",
    }


def test_two_methods_that_may_carry_a_name_are_never_a_guess() -> None:
    source = """package a;
    class T {
        @ParameterizedTest @ValueSource(ints = 1) void one(int n) {}
        @ParameterizedTest @ValueSource(ints = 2) void two(int n) {}
        @Test @DisplayName("same") void a() {}
        @Test @DisplayName("same") void b() {}
    }"""
    classes = parse_test_classes(source)

    assert "one, two" in str(find_method(classes, "a.T", "[1] 1"))
    assert "a, b" in str(find_method(classes, "a.T", "same"))


@pytest.mark.parametrize(
    "annotation",
    [
        "@DisplayName(NAME)",  # a constant: its text is unknown
        "@TestFactory",  # dynamic tests are named at run time
    ],
)
def test_a_name_siftwise_cant_read_makes_any_match_ambiguous(annotation: str) -> None:
    source = f"""package a;
    class T {{
        @Test @DisplayName("works") void a() {{}}
        @Test {annotation} void b() {{}}
    }}"""
    classes = parse_test_classes(source)

    assert isinstance(find_method(classes, "a.T", "works"), str)


def test_a_display_name_generator_makes_undecorated_names_unknown() -> None:
    source = """package a;
    @DisplayNameGeneration(DisplayNameGenerator.ReplaceUnderscores.class)
    class T {
        @Test @DisplayName("works") void a() {}
        @Test void adds_up() {}
    }"""
    [cls] = parse_test_classes(source)

    assert [m.display for m in cls.methods] == ["works", UNKNOWN]
    assert isinstance(find_method([cls], "a.T", "works"), str)
    assert find_method([cls], "a.T", "adds_up()") == ("a.T", "adds_up")  # the method itself


def test_a_class_the_source_doesnt_declare_says_nothing() -> None:
    classes = parse_test_classes(CART_TEST)

    assert find_method(classes, "acme.OtherTest", "adds up the total") is None
    assert find_method(classes, "A trolley", "adds up the total") is None
    # The method may be inherited: the reported name stands.
    assert find_method(classes, "acme.CartTest", "inherited") is None
    assert "no method of acme.CartTest" in str(find_method(classes, "acme.CartTest", "no such"))


# --- re-runs ---------------------------------------------------------------------------------


@pytest.fixture
def checkout(tmp_path: Path) -> Checkout:
    (tmp_path / "build.gradle").write_text("")
    source = tmp_path / "src" / "test" / "java" / "acme" / "CartTest.java"
    source.parent.mkdir(parents=True)
    source.write_text(CART_TEST, encoding="utf-8")
    return Checkout(tmp_path)


@pytest.mark.parametrize(("classname", "name", "method"), NAMES)
def test_display_names_re_run_by_method(
    checkout: Checkout, classname: str, name: str, method: str
) -> None:
    owner = "acme.CartTest$Empty" if "mpty" in classname else "acme.CartTest"

    target = classify(f"{classname}::{name}", None, checkout)

    assert target == Target(f"{classname}::{name}", "gradle", f"{owner}\0{method}")


def test_a_display_name_that_cant_be_mapped_keeps_its_verdict_with_a_reason(
    checkout: Checkout,
) -> None:
    reason = classify("acme.CartTest::no such test", None, checkout)

    assert isinstance(reason, str)
    assert reason.startswith("a JUnit 5 display name siftwise can't map to one method")
    # No source for the class: a display name can't be re-run, a method name still can.
    assert "isn't a method name" in str(classify("acme.Gone::adds up", None, checkout))
    assert isinstance(classify("acme.Gone::addsUp", None, checkout), Target)
    # A phrased classname no test class carries: not Java as far as siftwise can tell.
    assert classify("Cart totals::Cart totals", None, checkout) == NO_RUNNER


def test_two_source_files_that_disagree_are_never_a_guess(checkout: Checkout) -> None:
    other = checkout.root / "web" / "src" / "test" / "java" / "acme" / "CartTest.java"
    other.parent.mkdir(parents=True)
    other.write_text(CART_TEST.replace("void total()", "void sum()"), encoding="utf-8")

    reason = classify("acme.CartTest::adds up the total", None, checkout)

    assert isinstance(reason, str) and "disagree" in reason
    # Where they agree, the method is known.
    assert isinstance(classify("acme.CartTest::1 plus 2", None, checkout), Target)
