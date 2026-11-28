"""The JVM mutation eval's pure parts (scripts/eval/mutation_jvm.py, mutators.kotlin_mutants):
Kotlin mutants, the selection's classes, the JDK per Gradle version, and the report."""

from pathlib import Path

import pytest

from scripts.eval import mutation_jvm as jvm
from scripts.eval.mutators import (
    FLIP_COMPARISON,
    INT_PLUS_ONE,
    NEGATE_CONDITION,
    REPLACE_RETURN,
    kotlin_mutants,
)

SOURCE = """package a

// if (x < 1) a comment
fun f(x: Int, s: String): Boolean {
    if (x < 10 && s == "a < b") return true
    val t = "${x > 3}"
    while (x >= 0 || check(x)) { }
    for (i in 0..10) {}
    val g: List<Int> = listOf(1)
    val raw = \"\"\"x > 1
      y < 2\"\"\"
    return false
}
"""


def test_kotlin_mutants_keep_the_types() -> None:
    found = [(m.line, m.operator, m.original, m.replacement)
             for m in kotlin_mutants("a.kt", SOURCE, range(1, 20))]  # fmt: skip
    assert found == [
        (5, NEGATE_CONDITION, 'x < 10 && s == "a < b"', '!(x < 10 && s == "a < b")'),
        (5, FLIP_COMPARISON, "<", ">="),
        (5, INT_PLUS_ONE, "10", "11"),
        (5, FLIP_COMPARISON, "==", "!="),
        (5, REPLACE_RETURN, "true", "false"),
        (7, NEGATE_CONDITION, "x >= 0 || check(x)", "!(x >= 0 || check(x))"),
        (7, FLIP_COMPARISON, ">=", "<"),
        (7, INT_PLUS_ONE, "0", "1"),
        (9, INT_PLUS_ONE, "1", "2"),
        (12, REPLACE_RETURN, "false", "true"),
    ]  # comments, strings, templates, a range (0..10), generics and raw strings left alone


def test_kotlin_mutants_only_on_the_changed_lines() -> None:
    assert {m.line for m in kotlin_mutants("a.kt", SOURCE, [7])} == {7}
    for m in kotlin_mutants("a.kt", SOURCE, range(1, 20)):
        assert m.apply(SOURCE) != SOURCE


def test_the_selections_classes() -> None:
    commands = ("./gradlew :a:test --tests x.ATest --tests x.BTest :b:test --tests y.CTest",)
    assert jvm.selected_classes(commands) == {"x.ATest", "x.BTest", "y.CTest"}
    assert jvm.outer("x.ATest$Inner::m()") == "x.ATest"


@pytest.mark.parametrize(
    ("version", "home"),
    [("7.6.1", "/usr/lib/jvm/jdk-17"), ("8.14.3", None), ("9.9.2", "/usr/lib/jvm/jdk-26")],
)
def test_the_jdk_gradle_runs_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str, home: str | None
) -> None:
    props = tmp_path / "gradle" / "wrapper" / "gradle-wrapper.properties"
    props.parent.mkdir(parents=True)
    props.write_text(f"distributionUrl=https\\://x/gradle-{version}-bin.zip\n", encoding="utf-8")
    monkeypatch.setattr("os.path.isdir", lambda p: True)
    monkeypatch.setenv("JAVA_HOME", "/usr/lib/jvm/jdk-21")
    assert jvm.java_home(tmp_path) == (home or "/usr/lib/jvm/jdk-21")


def test_the_report_splits_narrow_selections_and_directions() -> None:
    chosen = [{"id": "m1"}, {"id": "m2"}]
    commits = [
        {"key": "c1", "pr": "#1", "chosen": chosen, "candidates": 5, "mode": "selective",
         "tests_share": 0.1, "selected_classes": 3, "test_classes": 40, "sample": "targeted"},
        {"key": "c2", "pr": "#2", "chosen": chosen[:1], "candidates": 2, "mode": "full",
         "reason": "moshi isn't analysed (custom source sets): moshi/src/main/x.kt"},
        {"key": "c3", "pr": "#3", "chosen": [], "candidates": 0},
    ]  # fmt: skip
    mutant = {"path": "a.kt", "operator": FLIP_COMPARISON, "line": 1, "start": 0, "end": 1,
              "original": "<", "replacement": ">="}  # fmt: skip
    mutants = [
        {"change": "c1", "outcome": "caught", "reached_by": ["java"], "mutant": mutant,
         "killer_languages": ["java"], "seconds": 10, "runs": []},
        {"change": "c1", "outcome": "caught", "reached_by": ["kotlin"], "mutant": mutant,
         "killer_languages": ["kotlin"], "seconds": 10, "runs": []},
        {"change": "c2", "outcome": "not_killed", "reached_by": ["java", "kotlin"],
         "mutant": mutant, "seconds": 10, "runs": []},
    ]  # fmt: skip
    lines = jvm.report(commits, mutants)
    assert "all mutants (killed and selected / killed): 100.0% (2 of 2; 95% CI" in "\n".join(lines)
    assert any(line.startswith("narrow selections (1 changes): 100.0% (2 of 2") for line in lines)
    assert "  share of tests selected: median 10.0% (10.0%)" in lines
    assert "  1 x moshi isn't analysed (custom source sets)" in lines
    assert any("java 1 (1 caught" in line and "kotlin 1 (1 caught" in line for line in lines)
