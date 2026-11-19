"""scripts/eval/mutation.py and mutators.py: the mutators, the kill rule, recall and its
interval. Real mutation runs (clones, installs, test suites) are not part of the suite."""

import ast
import os
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.eval import mutation as mu
from scripts.eval import mutation_jest as mj
from scripts.eval import mutators as mt
from scripts.eval.run_eval import TargetRun
from siftwise.cli.jsgraph import JsGraph
from siftwise.cli.pygraph import PyGraph
from siftwise.core.junit import Status
from siftwise.core.selector import KnownTest, RepoHistory
from siftwise.core.shadow import FLAKY_HISTORY, NEW_TEST, NO_LINK, UNKNOWN_CAUSE

# --- changed lines ------------------------------------------------------------------------


def test_changed_lines_reads_new_side_hunks() -> None:
    diff = """diff --git a/pkg/a.py b/pkg/a.py
--- a/pkg/a.py
+++ b/pkg/a.py
@@ -3 +3 @@ def f():
-    return 1
+    return 2
@@ -10,0 +11,3 @@ def g():
+x = 1
+y = 2
+z = 3
@@ -20,2 +23,0 @@
-gone
-gone
diff --git a/old.py b/old.py
--- a/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-a
-b
"""
    assert mt.changed_lines(diff) == {"pkg/a.py": {3, 11, 12, 13}}


# --- Python mutants -----------------------------------------------------------------------

SOURCE = """import logging
from typing import TYPE_CHECKING

log = logging.getLogger(__name__)
if TYPE_CHECKING:
    from x import Y


def f(a: int = 3, b: "list[int]" = ()) -> int:
    if a < 2 and not b:
        log.debug("small")
        save(a)
        return a + 1
    while a == 0:
        a -= 1
    return True if a in b else False


def g() -> None:
    print("hi")
    return None


if __name__ == "__main__":
    f(1)
"""


def _mutants(source: str = SOURCE, lines: set[int] | None = None) -> list[mt.Mutant]:
    every = set(range(1, source.count("\n") + 2))
    return mt.python_mutants("pkg/m.py", source, every if lines is None else lines)


def test_python_mutants_cover_each_operator() -> None:
    found = {(m.line, m.operator, m.original, m.replacement) for m in _mutants()}
    assert (10, mt.FLIP_COMPARISON, "a < 2", "(a >= 2)") in found
    assert (10, mt.NEGATE_CONDITION, "a < 2 and not b", "(not (a < 2 and not b))") in found
    assert (10, mt.INT_PLUS_ONE, "2", "3") in found
    assert (12, mt.REMOVE_CALL, "save(a)", "pass") in found
    assert (13, mt.REPLACE_RETURN, "a + 1", "None") in found
    assert (13, mt.INT_PLUS_ONE, "1", "2") in found
    assert (14, mt.FLIP_COMPARISON, "a == 0", "(a != 0)") in found
    assert (16, mt.FLIP_COMPARISON, "a in b", "(a not in b)") in found
    # A conditional expression's test is negated; a returned conditional is replaced.
    assert (16, mt.NEGATE_CONDITION, "a in b", "(not (a in b))") in found
    assert (16, mt.REPLACE_RETURN, "True if a in b else False", "None") in found


def test_python_mutants_leave_out_what_changes_no_behaviour() -> None:
    found = _mutants()
    originals = {(m.line, m.original) for m in found}
    # Annotations are left alone; the default value is behaviour, so it's a mutant.
    assert [(m.operator, m.original) for m in found if m.line == 9] == [(mt.INT_PLUS_ONE, "3")]
    assert (11, 'log.debug("small")') not in originals  # logging call
    assert (20, 'print("hi")') not in originals  # print
    assert not [m for m in found if m.line == 21]  # return None
    assert not [m for m in found if m.line in (5, 24)]  # TYPE_CHECKING, __main__ guards
    assert all(m.operator != mt.REPLACE_RETURN or m.original != "None" for m in found)


def test_python_mutants_only_on_wanted_lines_and_all_compile() -> None:
    found = _mutants(lines={14, 15})
    assert {m.line for m in found} == {14, 15}
    for m in found:
        mutated = m.apply(SOURCE)
        assert mutated != SOURCE
        ast.parse(mutated)
        # Only the span changes: everything around it is byte for byte the same.
        assert mutated.encode()[: m.start] == SOURCE.encode()[: m.start]


def test_python_mutants_not_condition_is_unwrapped_and_bools_flip() -> None:
    source = "def f(x):\n    if not x:\n        return False\n    return True\n"
    found = {(m.operator, m.original, m.replacement) for m in _mutants(source)}
    assert (mt.NEGATE_CONDITION, "not x", "(x)") in found
    assert (mt.REPLACE_RETURN, "False", "True") in found
    assert (mt.REPLACE_RETURN, "True", "False") in found


def test_python_mutants_handle_non_ascii_and_crlf() -> None:
    source = 'def f(s):\r\n    t = "héllo wörld"; return s == t\r\n'
    found = _mutants(source)
    flip = next(m for m in found if m.operator == mt.FLIP_COMPARISON)
    assert flip.original == "s == t"
    mutated = flip.apply(source)
    assert mutated == 'def f(s):\r\n    t = "héllo wörld"; return (s != t)\r\n'


def test_python_mutants_drop_ones_that_dont_compile() -> None:
    # `return` value replaced in an async generator would be a SyntaxError... any mutant that
    # doesn't compile is dropped. A file that doesn't parse gives none.
    assert mt.python_mutants("x.py", "def f(:\n", {1}) == []
    assert mt.compiles("x = 1\n")
    assert not mt.compiles("x = = 1\n")
    source = "async def f():\n    yield 1\n    return\n"
    assert all(mt.compiles(m.apply(source)) for m in _mutants(source))


def test_mutant_apply_checks_the_original_and_round_trips_json() -> None:
    m = next(m for m in _mutants() if m.operator == mt.REMOVE_CALL)
    assert mt.Mutant.from_json(m.to_json()) == m
    with pytest.raises(ValueError, match="expected"):
        m.apply(SOURCE.replace("save(a)", "load(a)"))
    assert "remove_call: save(a) -> pass" in m.describe()


def test_choose_is_seeded_and_spreads_operators() -> None:
    found = _mutants()
    first = mt.choose(found, 3, "74:abc")
    assert first == mt.choose(list(reversed(found)), 3, "74:abc")  # same seed, same pick
    assert len({m.operator for m in first}) == 3
    assert mt.choose(found, 100, "1") and len(mt.choose(found, 100, "1")) == len(found)
    assert mt.choose([], 3, "x") == []


# --- JS mutants ---------------------------------------------------------------------------


def test_js_mutants_by_token_skip_strings_and_comments() -> None:
    source = (
        "export function f(a: number): boolean {\n"  # 1
        "  if (a >= 10) return true;\n"  # 2
        '  const s = "a < b 5"; // x === 3\n'  # 3
        "  const t = `multi\n"  # 4
        "line ${a < 2}`;\n"  # 5
        "  return a === 2;\n"  # 6
        "}\n"
    )
    found = mt.js_mutants("src/f.ts", source, range(1, 8))
    got = {(m.line, m.operator, m.original, m.replacement) for m in found}
    assert (2, mt.FLIP_COMPARISON, ">=", "<") in got
    assert (2, mt.INT_PLUS_ONE, "10", "11") in got
    assert (2, mt.REPLACE_RETURN, "true", "false") in got
    assert (6, mt.FLIP_COMPARISON, "===", "!==") in got
    assert (6, mt.REPLACE_RETURN, "a === 2", "undefined") in got
    assert not [m for m in found if m.line in (3, 4, 5)]  # in a string, comment or template
    for m in found:
        assert m.apply(source) != source


# --- the kill rule ------------------------------------------------------------------------


class FakeSuite:
    """Results by part and by test: ``mutant`` and ``baseline`` say which tests fail."""

    def __init__(
        self,
        selected_part: Sequence[str],
        rest_part: Sequence[str] = (),
        fails_with_mutant: Sequence[str] = (),
        fails_on_baseline: Sequence[str] = (),
        flaky: Sequence[str] = (),
        timed_out: str | None = None,
        hangs: Sequence[str] = (),
        unnamed: Sequence[str] = (),
        unrunnable: Sequence[str] = (),
    ) -> None:
        self.unnamed = set(unnamed)  # re-running by name runs nothing; by file works
        self.unrunnable = set(unrunnable)  # neither a name nor a file re-run executes these
        self.parts = {mu.SELECTED: list(selected_part), mu.REST: list(rest_part),
                      mu.FULL: [*selected_part, *rest_part]}  # fmt: skip
        self.mutant, self.baseline = set(fails_with_mutant), set(fails_on_baseline)
        self.flaky = set(flaky)  # fail in the part's run only
        self.timed_out = timed_out
        self.hangs = set(hangs)  # re-running these with the mutant hangs
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def run(self, part: str) -> mu.RunResult:
        self.calls.append((part, ()))
        if part == self.timed_out:
            return mu.RunResult({}, 1.0, timed_out=True)
        failing = self.mutant | self.baseline | self.flaky
        return mu.RunResult({t: Status.FAILED if t in failing else Status.PASSED
                             for t in self.parts[part]}, 2.0)  # fmt: skip

    def tests(self, test_ids: Sequence[str], mutant: bool) -> mu.RunResult:
        self.calls.append(("mutant" if mutant else "baseline", tuple(test_ids)))
        if mutant and self.hangs.intersection(test_ids):
            return mu.RunResult({}, 9.0, timed_out=True)
        failing = (self.mutant if mutant else set()) | self.baseline
        return mu.RunResult({t: self._status(t, failing, by_name=True)
                             for t in test_ids}, 0.5)  # fmt: skip

    def files(self, test_ids: Sequence[str], mutant: bool) -> mu.RunResult:
        self.calls.append(("files-mutant" if mutant else "files-baseline", tuple(test_ids)))
        failing = (self.mutant if mutant else set()) | self.baseline
        return mu.RunResult({t: self._status(t, failing, by_name=False)
                             for t in test_ids if t not in self.unrunnable}, 0.5)  # fmt: skip

    def _status(self, test_id: str, failing: set[str], by_name: bool) -> Status:
        if by_name and test_id in self.unnamed | self.unrunnable:
            return Status.SKIPPED  # a name filter that matches nothing: the test didn't run
        return Status.FAILED if test_id in failing else Status.PASSED


def _decide(suite: FakeSuite, selected: set[str], full: bool = False) -> mu.Decision:
    return mu.decide(suite, lambda t: t in selected, full, {})


def test_caught_by_a_selected_test_skips_the_rest_of_the_suite() -> None:
    suite = FakeSuite(["a", "b"], ["c"], fails_with_mutant=["a", "c"])
    d = _decide(suite, {"a", "b"})
    assert d.outcome == mu.CAUGHT and d.selected_killers == ["a"] and d.killers == ["a"]
    assert [kind for kind, _ in suite.calls] == [mu.SELECTED, "mutant", "baseline"]


def test_killed_only_outside_the_selection_is_a_miss() -> None:
    suite = FakeSuite(["a"], ["c", "d"], fails_with_mutant=["d"])
    d = _decide(suite, {"a"})
    assert d.outcome == mu.MISSED and d.killers == ["d"] and d.selected_killers == []
    assert [kind for kind, _ in suite.calls] == [mu.SELECTED, mu.REST, "mutant", "baseline"]


def test_killed_by_an_unselected_test_in_a_selected_file_is_a_miss() -> None:
    # The selected files run whole; a test in them the selection didn't name doesn't count.
    suite = FakeSuite(["a", "a2"], ["c"], fails_with_mutant=["a2"])
    d = _decide(suite, {"a"})
    assert d.outcome == mu.MISSED and d.killers == ["a2"]
    assert mu.REST not in [kind for kind, _ in suite.calls]


def test_no_test_kills_it() -> None:
    d = _decide(FakeSuite(["a"], ["c"]), {"a"})
    assert d.outcome == mu.NOT_KILLED and d.killers == []


def test_baseline_failures_and_flaky_failures_dont_kill() -> None:
    suite = FakeSuite(["a", "b"], ["c"], fails_on_baseline=["a"], flaky=["b", "c"])
    d = _decide(suite, {"a", "b"})
    assert d.outcome == mu.NOT_KILLED
    assert d.baseline_failed == ["a"]  # fails with and without the mutant
    assert set(d.unconfirmed) == {"b", "c"}  # failed once, passed when re-run with it


def test_baseline_results_are_cached_per_commit() -> None:
    suite = FakeSuite(["a"], fails_on_baseline=["a"])
    baseline: dict[str, bool] = {}
    mu.decide(suite, lambda t: True, True, baseline)
    mu.decide(suite, lambda t: True, True, baseline)
    assert baseline == {"a": False}
    assert [kind for kind, _ in suite.calls].count("baseline") == 1


def test_full_selection_runs_once() -> None:
    suite = FakeSuite(["a"], ["c"], fails_with_mutant=["c"])
    d = _decide(suite, {"a", "c"}, full=True)
    assert d.outcome == mu.CAUGHT and suite.calls[0] == (mu.FULL, ())
    d = _decide(FakeSuite(["a"], ["c"]), {"a", "c"}, full=True)
    assert d.outcome == mu.NOT_KILLED


def test_timeouts_are_outside_recall() -> None:
    assert _decide(FakeSuite(["a"], timed_out=mu.SELECTED), {"a"}).outcome == mu.TIMEOUT
    d = _decide(FakeSuite(["a"], ["c"], timed_out=mu.REST), {"a"})
    assert d.outcome == mu.TIMEOUT


def test_a_hanging_rerun_is_a_timeout() -> None:
    suite = FakeSuite(["a", "b"], fails_with_mutant=["a", "b"], hangs=["b"])
    d = _decide(suite, {"a", "b"})
    assert d.outcome == mu.TIMEOUT and d.runs[-1]["timed_out"]


def test_selected_candidates_are_checked_first_and_the_cap_leaves_it_undecided() -> None:
    many = [f"t{i:03}" for i in range(mu.CONFIRM_CAP + 10)]
    # Every one fails once, none again: past the cap, unchecked ones might have killed it.
    suite = FakeSuite(many, flaky=many)
    d = _decide(suite, set(), full=True)
    assert d.outcome == mu.UNDECIDED
    assert len(d.unconfirmed) == mu.CONFIRM_CAP
    # A selected killer listed last by name is still checked in the first batch.
    suite = FakeSuite([*many, "z"], fails_with_mutant=[*many, "z"])
    d = _decide(suite, {"z"}, full=True)
    assert d.outcome == mu.CAUGHT and d.selected_killers == ["z"]
    assert suite.calls[1][0] == "mutant" and suite.calls[1][1][0] == "z"


# --- recall and its interval --------------------------------------------------------------


def test_wilson_interval_known_values() -> None:
    assert mu.wilson(0, 0) is None
    low, high = mu.wilson(10, 10) or (0, 0)
    assert low == pytest.approx(0.7225, abs=1e-4) and high == pytest.approx(1.0)
    low, high = mu.wilson(5, 10) or (0, 0)
    assert low == pytest.approx(0.2366, abs=1e-4) and high == pytest.approx(0.7634, abs=1e-4)
    low, high = mu.wilson(0, 20) or (1, 1)
    assert low == 0.0 and high == pytest.approx(0.1611, abs=1e-4)
    low, high = mu.wilson(57, 60) or (0, 0)
    assert 0.86 < low < 0.87 and 0.98 < high < 0.99


def test_recall_counts_only_mutants_the_full_suite_kills() -> None:
    outcomes = [mu.CAUGHT] * 9 + [mu.MISSED] + [mu.NOT_KILLED, mu.TIMEOUT, mu.UNDECIDED] * 4
    r = mu.recall_of(outcomes)
    assert (r.caught, r.missed, r.total, r.value) == (9, 1, 10, 0.9)
    assert str(r) == "90.0% (9 of 10; 95% CI 59.6-98.2%)"
    assert str(mu.recall_of([mu.NOT_KILLED])) == "n/a (no mutant killed by the full suite)"


# --- miss causes --------------------------------------------------------------------------


def test_miss_causes_shadow_style() -> None:
    history = RepoHistory(
        tests={
            "test.test_a::t": KnownTest("test.test_a::t", "test/test_a.py"),
            "test.test_b::t": KnownTest("test.test_b::t", "test/test_b.py", flaky_score=0.25),
            "test.test_c::t": KnownTest("test.test_c::t", "test/test_c.py"),
        }
    )
    graph = PyGraph(edges={
        "test/test_a.py": frozenset(), "test/test_b.py": frozenset(),
        "test/test_c.py": frozenset({"pkg/m.py"}), "pkg/m.py": frozenset(),
    })  # fmt: skip
    assert mu.miss_cause("test.test_new::t", history, "pkg/m.py", graph)[0] == NEW_TEST
    assert mu.miss_cause("test.test_b::t", history, "pkg/m.py", graph)[0] == FLAKY_HISTORY
    cause, detail = mu.miss_cause("test.test_a::t", history, "pkg/m.py", graph)
    assert cause == NO_LINK and "test/test_a.py doesn't import pkg/m.py" in detail
    cause, detail = mu.miss_cause("test.test_c::t", history, "pkg/m.py", graph)
    assert cause == UNKNOWN_CAUSE and "test/test_c.py -> pkg/m.py" in detail
    causes = {"x": (NO_LINK, ""), "y": (FLAKY_HISTORY, "")}
    assert mu.mutant_cause(causes) == FLAKY_HISTORY
    assert mu.mutant_cause({}) is None


# --- sampling and files -------------------------------------------------------------------


def test_source_files_exclude_tests_and_other_roots() -> None:
    roots = ("rdflib/",)
    assert mu.is_source_file("rdflib/term.py", roots)
    assert mu.is_source_file("rdflib/plugins/sparql/parser.py", roots)
    for path in ("test/test_term.py", "rdflib/test_x.py", "rdflib/x_test.py",
                 "rdflib/conftest.py", "rdflib/testing/helpers.py", "docs/conf.py",
                 "rdflib/data.ttl", "setup.py"):  # fmt: skip
        assert not mu.is_source_file(path, roots), path


def test_cirq_profile_mutates_package_sources_only() -> None:
    roots = mu.PROFILES["quantumlib/Cirq"].source_roots
    assert mu.is_source_file("cirq-core/cirq/ops/raw_types.py", roots)
    assert mu.is_source_file("cirq-google/cirq_google/engine/engine.py", roots)
    for path in ("cirq-core/cirq/ops/raw_types_test.py", "dev_tools/modules.py",
                 "cirq-google/cirq_google/api/v2/run_context_pb2.py",
                 "cirq-core/setup.py"):  # fmt: skip
        assert not mu.is_source_file(path, roots), path


def _change(sha: str, day: int) -> mu.Change:
    when = datetime(2026, 8, day, tzinfo=UTC)
    return mu.Change(TargetRun(0, None, sha, f"#{day}", when, ("rdflib/a.py",), "PR"), "base")


def test_sample_order_is_seeded_and_canonical() -> None:
    changes = [_change(f"{i:040x}", i) for i in range(1, 21)]
    first = mu.sample_order(changes, 74)
    assert first == mu.sample_order(list(reversed(changes)), 74)
    assert first != mu.sample_order(changes, 75)
    assert sorted(c.key for c in first) == sorted(c.key for c in changes)
    back = mu.change_from_json(mu.change_json(changes[0]))
    assert back.key == changes[0].key and back.target.run_at == changes[0].target.run_at


# --- report -------------------------------------------------------------------------------


def test_report_lines_summarize_the_records() -> None:
    m = mt.python_mutants("pkg/m.py", "def f(a):\n    return a < 1\n", {2})[0]
    chosen = [m.to_json() | {"id": m.id}]
    commits = [
        {"key": "#1@a", "chosen": chosen, "candidates": 4, "mode": "selective",
         "time_skipped_pct": 10.0, "setup_s": 30},
        {"key": "#2@b", "chosen": chosen, "candidates": 2, "mode": "full",
         "time_skipped_pct": 0.0, "setup_s": 50},
        {"key": "#3@c", "chosen": [], "candidates": 0, "setup_s": 2},
    ]  # fmt: skip
    run = {"kind": "selected", "seconds": 60}
    mutants = [
        {"key": "1", "change": "#1@a", "mutant": m.to_json(), "outcome": mu.CAUGHT,
         "runs": [run], "seconds": 70, "baseline_failed": ["test.test_x::broken"]},
        {"key": "2", "change": "#2@b", "mutant": m.to_json(), "outcome": mu.MISSED,
         "cause": NO_LINK, "causes": {"test.test_y::t": [NO_LINK, "no import"]},
         "runs": [run, {"kind": "rest", "seconds": 40}], "seconds": 110},
    ]  # fmt: skip
    text = "\n".join(mu.report_lines(commits, mutants))
    assert "changes sampled: 3 (2 with mutants; 1 without a mutable changed line)" in text
    assert "caught 1, missed 1, not_killed 0" in text
    assert "recall: 50.0% (1 of 2; 95% CI 9.5-90.5%)" in text
    assert "1 selective, 1 full; estimated time skipped: median 5.0%" in text
    assert "no_link: pkg/m.py:2 flip_comparison" in text
    assert "test.test_y::t: no_link: no import" in text
    assert "baseline failures seen: 1 (test.test_x::broken)" in text
    assert "selected 2x median 60s" in text
    assert f"{(30 + 50 + 2 + 70 + 110) / 3600:.2f} h in all" in text


def test_rerunning_no_tests_runs_nothing(tmp_path: Path) -> None:
    # An empty argument list would make pytest run the whole suite: never call it with one.
    profile = mu.PROFILES["RDFLib/rdflib"]
    missing = tmp_path / "no-python"
    suite = mu.Pytest(missing, tmp_path, tmp_path / "work", profile, 10, 10, 1)
    assert suite.tests([], mutant=False).statuses == {}
    assert not (tmp_path / "work").exists()


def test_shard_run_reads_hangs_crashes_and_the_summary(tmp_path: Path) -> None:
    junit, log = tmp_path / "junit-0.xml", tmp_path / "pytest-0.log"
    log.write_text("....\n+++++ Timeout +++++\n", encoding="utf-8")
    hung = mu.shard_run(0, 1, junit, log)
    assert (hung.hung, hung.crashed, hung.report) == (True, False, False)
    log.write_text("...Windows fatal exception: stack overflow\n", encoding="utf-8")
    assert mu.shard_run(1, 3, junit, log).crashed
    junit.write_text("<testsuites/>", encoding="utf-8")
    log.write_text(
        "E   Failed: Timeout >120.0s\n== 1 failed, 9 passed, 90 deselected in 5.20s ==\n",
        encoding="utf-8",
    )
    done = mu.shard_run(2, 1, junit, log)
    assert done.hung and done.report and not done.crashed
    assert done.summary == "1 failed, 9 passed, 90 deselected in 5.20s"


def test_measure_lines_list_failures_by_file_and_bad_shards() -> None:
    shards = [
        vars(mu.ShardRun(0, 0, True, False, False, "5 passed in 1.00s")),
        vars(mu.ShardRun(1, None, False, True, False, "")),
    ]
    lines = mu.measure_lines({
        "repo": "quantumlib/Cirq", "commit": "1403830e59df", "platform": "linux",
        "seconds": 400.4, "install_s": 150.0, "results": 6, "statuses": {"passed": 5, "failed": 1},
        "failed": ["a.b_test::test_x"], "failed_by_file": {"a/b_test.py": 1},
        "shards": shards, "timed_out": True, "error": None,
    })  # fmt: skip
    assert lines[0] == (
        "quantumlib/Cirq at 1403830e5 on linux: full suite in 400s wall (2 shards; install 150s)"
    )
    assert "baseline failures: 1 in 1 file(s)" in lines
    assert "     1  a/b_test.py" in lines
    assert "shards hung (pytest-timeout): 1" in lines
    assert "shards no report: 1" in lines


def test_test_runs_see_the_virtualenv_as_activated(tmp_path: Path) -> None:
    python = tmp_path / "venv" / "bin" / "python"
    env = mu.activated(python, {"PATH": "/usr/bin", "HOME": "/root"})
    assert env["PATH"].split(os.pathsep) == [str(python.parent), "/usr/bin"]
    assert env["VIRTUAL_ENV"] == str(tmp_path / "venv")
    assert env["HOME"] == "/root"
    assert mu.activated(python, {})["PATH"] == str(python.parent)


# --- Jest repos (mutation_jest.py) --------------------------------------------------------

JEST_PROFILE = mj.JestProfile(
    repo="o/r",
    clone_url="",
    install=(),
    build=(),
    packages=(
        mj.JsPackage("packages/core/", "packages/core/tsconfig.json"),
        mj.JsPackage("packages/ui/"),
    ),
    jest_args=(),
)


def test_js_sources_are_non_test_files_in_a_packages_src() -> None:
    assert mj.is_js_source("packages/core/src/a.ts", JEST_PROFILE)
    assert mj.is_js_source("packages/ui/src/view.tsx", JEST_PROFILE)
    assert not mj.is_js_source("packages/core/src/a.d.ts", JEST_PROFILE)  # types only
    assert not mj.is_js_source("packages/core/src/a.test.ts", JEST_PROFILE)
    assert not mj.is_js_source("packages/core/src/__tests__/a.ts", JEST_PROFILE)
    assert not mj.is_js_source("packages/core/scripts/build.js", JEST_PROFILE)  # not src/
    assert not mj.is_js_source("packages/other/src/a.ts", JEST_PROFILE)
    assert not mj.is_js_source("packages/core/src/a.json", JEST_PROFILE)


def test_jest_selection_files_and_test_names() -> None:
    commands = ["pytest a.py", "jest packages/core/__tests__/a.ts 'packages/ui/b c.test.tsx'"]
    assert mj.jest_files(commands) == ["packages/core/__tests__/a.ts", "packages/ui/b c.test.tsx"]
    assert mj.test_name("packages/ui/b.test.tsx [ui]::Cart adds an item") == "Cart adds an item"
    assert mj.seed_repo("mobxjs/mobx", "0123456789abcdef" * 2) == "mobxjs/mobx@seed-0123456789ab"


def test_tsc_errors_are_compared_by_line() -> None:
    output = (
        "packages/core/src/a.ts(3,5): error TS2322: Type 'undefined' is not assignable.\n"
        "Found 1 error in packages/core/src/a.ts:3\n"
    )
    assert mj.tsc_errors(output) == {
        "packages/core/src/a.ts(3,5): error TS2322: Type 'undefined' is not assignable."
    }
    assert mj.tsc_errors("") == set()


def test_type_check_keeps_the_first_k_mutants_that_add_no_tsc_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "packages/core/src").mkdir(parents=True)
    (tmp_path / "packages/ui/src").mkdir(parents=True)
    source = "export function f(a: number): number {\n  return a + 1;\n}\n"
    (tmp_path / "packages/core/src/a.ts").write_bytes(source.encode())
    (tmp_path / "packages/ui/src/b.js").write_text("x = 1 < 2;\n", encoding="utf-8")
    mutants = mt.js_mutants("packages/core/src/a.ts", source, {2})
    ret = next(m for m in mutants if m.operator == mt.REPLACE_RETURN)  # undefined: a type error
    plus = next(m for m in mutants if m.operator == mt.INT_PLUS_ONE)
    ui = mt.js_mutants("packages/ui/src/b.js", "x = 1 < 2;\n", {1})[0]  # no tsconfig: kept
    old = "packages/core/src/x.ts(9,1): error TS1: present at the head"

    def fake_tsc(src: Path, profile: mj.JestProfile, tsconfig: str) -> str:
        text = (src / "packages/core/src/a.ts").read_text(encoding="utf-8")
        new = "packages/core/src/a.ts(2,3): error TS2322: x" if "undefined" in text else ""
        return f"{old}\n{new}"

    monkeypatch.setattr(mj, "tsc_output", fake_tsc)
    chosen, rejected = mj.type_checked(tmp_path, JEST_PROFILE, [ret, plus, ui], 2)
    assert chosen == [plus, ui] and rejected == 1
    assert (tmp_path / "packages/core/src/a.ts").read_bytes() == source.encode()
    assert mj.type_checked(tmp_path, JEST_PROFILE, [ret, plus, ui], 1) == ([plus], 1)


def test_a_mutant_in_a_built_package_is_rebuilt_and_its_build_put_back(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "packages/core/src").mkdir(parents=True)
    (src / "packages/core/dist").mkdir()
    (src / "packages/core/src/a.ts").write_text("export const n = 1;\n", encoding="utf-8")
    (src / "packages/core/dist/index.js").write_text("built at head\n", encoding="utf-8")
    build = ("import pathlib; s = pathlib.Path('packages/core/src/a.ts').read_text(); "
             "pathlib.Path('packages/core/dist/index.js').write_text('built: ' + s)")  # fmt: skip
    profile = mj.JestProfile(
        repo="o/r", clone_url="", install=(), build=(), jest_args=(),
        packages=(mj.JsPackage("packages/core/", None, (sys.executable, "-c", build),
                               "packages/core/dist"),),
    )  # fmt: skip
    suite = mj.Jest(src, tmp_path / "work", profile, 60)
    suite.snapshot()
    m = mt.js_mutants("packages/core/src/a.ts", "export const n = 1;\n", {1})[0]
    suite.use(m)
    suite._set(True)
    assert (src / "packages/core/dist/index.js").read_text() == "built: export const n = 2;\n"
    suite._set(False)  # the baseline: source and build as at the head
    assert (src / "packages/core/dist/index.js").read_text() == "built at head\n"
    assert (src / "packages/core/src/a.ts").read_text() == "export const n = 1;\n"
    suite._set(True)
    suite.restore()
    assert (src / "packages/core/dist/index.js").read_text() == "built at head\n"
    assert suite.tests([], mutant=False).statuses == {}  # nothing to run: jest isn't called


def test_js_miss_causes_and_time_skipped_by_file() -> None:
    graph = JsGraph({"t/a.test.ts": frozenset({"src/b.ts"}), "src/b.ts": frozenset({"src/c.ts"}),
                     "t/d.test.ts": frozenset(), "src/c.ts": frozenset()}, frozenset())  # fmt: skip
    history = RepoHistory({
        "t/a.test.ts [p]::a": KnownTest("t/a.test.ts [p]::a", "t/a.test.ts", 300.0),
        "t/d.test.ts [p]::d": KnownTest("t/d.test.ts [p]::d", "t/d.test.ts", 100.0),
        "t/d.test.ts [p]::f": KnownTest("t/d.test.ts [p]::f", "t/d.test.ts", None,
                                        flaky_score=0.5),
    })  # fmt: skip
    assert mj.import_chain(graph, "t/a.test.ts", "src/c.ts") == [
        "t/a.test.ts", "src/b.ts", "src/c.ts"]  # fmt: skip
    cause, detail = mj.js_miss_cause("t/a.test.ts [p]::a", history, "src/c.ts", graph,
                                     "t/a.test.ts")  # fmt: skip
    assert cause == UNKNOWN_CAUSE and "t/a.test.ts -> src/b.ts -> src/c.ts" in detail
    assert mj.js_miss_cause("t/d.test.ts [p]::d", history, "src/c.ts", graph,
                            "t/d.test.ts")[0] == NO_LINK  # fmt: skip
    assert mj.js_miss_cause("t/d.test.ts [p]::f", history, "src/c.ts", graph,
                            "t/d.test.ts")[0] == FLAKY_HISTORY  # fmt: skip
    assert mj.js_miss_cause("t/new.test.ts [p]::n", history, "src/c.ts", graph,
                            "t/new.test.ts")[0] == NEW_TEST  # fmt: skip
    assert mj.time_skipped(history, {"t/a.test.ts"}, full=False) == 25.0
    assert mj.time_skipped(history, set(), full=True) == 0.0
    assert mj.time_skipped(RepoHistory({}), set(), full=False) is None


def test_the_report_names_the_seeded_history_and_the_type_check() -> None:
    m = mt.js_mutants("packages/core/src/a.ts", "const n = 1;\n", {1})[0]
    commits = [
        {"key": "#1@a", "chosen": [m.to_json() | {"id": m.id}], "candidates": 5,
         "type_rejected": 2, "mode": "selective", "time_skipped_pct": 80.0, "setup_s": 200,
         "history": mj.SEED_LABEL, "seed_run_s": 82.0, "seed_results": 1151,
         "seed_failed": []},
    ]  # fmt: skip
    lines = mj.seed_lines(commits)
    assert lines[0].startswith("history: one-run history (as for a newly installed siftwise)")
    assert "seed runs: 1, median 82s and 1151 results; failing in a seed run: 0" in lines[1]
    assert "type check: 2 candidate mutant(s) failed tsc and were skipped" in lines
    text = "\n".join(mu.report_lines(commits, []))
    assert "(from 5 candidates)" in text
    assert mj.seed_lines([{"key": "x", "chosen": []}]) == []


def test_a_base_without_the_lockfile_is_skipped_with_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    present = {"good:package-lock.json", "old:yarn.lock"}

    def fake_git(src: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0 if args[-1] in present else 1, "", "")

    monkeypatch.setattr(mj, "git", fake_git)
    profile = mj.JEST_PROFILES["mobxjs/mobx"]
    assert mj.install_problem(tmp_path, "good", profile) is None
    assert mj.install_problem(tmp_path, "old", profile) == "base uses yarn (no package-lock.json)"
    assert mj.install_problem(tmp_path, "bare", profile) == "base has no package-lock.json"


def _js_change(key: str, files: tuple[str, ...]) -> mu.Change:
    target = TargetRun(0, None, key * 40, f"#{key}", datetime(2026, 9, 1, tzinfo=UTC),
                       changed_files=files)  # fmt: skip
    return mu.Change(target, "b" * 40)


def test_the_targeted_sample_takes_changes_outside_the_core_package() -> None:
    profile = mj.JEST_PROFILES["mobxjs/mobx"]
    assert mj.outside_core(_js_change("1", ("packages/mobx-react/src/observer.ts",)), profile)
    both = ("packages/mobx-react/src/observer.ts", "packages/mobx/src/core/atom.ts")
    assert not mj.outside_core(_js_change("2", both), profile)
    no_source = ("README.md", "packages/mobx-react/__tests__/a.tsx")  # nothing to mutate
    assert not mj.outside_core(_js_change("3", no_source), profile)


def test_the_jest_report_splits_the_samples_and_lists_skipped_changes() -> None:
    m = mt.js_mutants("packages/mobx-react/src/a.ts", "const n = 1;\n", {1})[0]
    chosen = [m.to_json() | {"id": m.id}]
    commits = [
        {"key": "#1@a", "pr": "#1", "chosen": chosen, "candidates": 3, "type_rejected": 0,
         "mode": "selective", "selected_files": 67, "test_files": 69, "time_skipped_pct": 2.0,
         "setup_s": 100},
        {"key": "#2@b", "pr": "#2", "chosen": [], "candidates": 4, "setup_s": 1,
         "skipped": "base uses yarn (no package-lock.json)"},
        {"key": "#3@c", "pr": "#3", "chosen": chosen, "candidates": 2, "type_rejected": 1,
         "mode": "selective", "selected_files": 14, "test_files": 70, "time_skipped_pct": 75.0,
         "setup_s": 100, "sample": mj.TARGETED},
    ]  # fmt: skip
    mutants = [
        {"key": "1", "change": "#1@a", "mutant": m.to_json(), "outcome": mu.CAUGHT,
         "runs": [], "seconds": 40},
        {"key": "3", "change": "#3@c", "mutant": m.to_json(), "outcome": mu.NOT_KILLED,
         "runs": [], "seconds": 40, "sample": mj.TARGETED},
    ]  # fmt: skip
    text = "\n".join(mj.report(commits, mutants))
    random, targeted = text.split(f"== {mj.TARGETED_LABEL} ==")
    assert "changes sampled: 1 (1 with mutants" in random  # the skipped one isn't counted
    assert "caught 1, missed 0, not_killed 0" in random
    assert "test files skipped by the selections: [2.9]; >= 30%: 0" in random
    assert "caught 0, missed 0, not_killed 1" in targeted
    assert ">= 30%: 1" in targeted
    assert "skipped changes (not counted): 1" in targeted
    assert "#2 b: base uses yarn (no package-lock.json)" in targeted
    assert mj.wide_selections(commits) == 1


def test_a_resumed_sample_counts_done_changes_once() -> None:
    # Bug: the walk started from the number of recorded changes with mutants AND counted each
    # again where it met it, so a resume with 12 done stopped after 13 (12 + 12 + 1 = 25).
    m = mt.js_mutants("src/a.ts", "const n = 1;\n", {1})[0]
    ordered = [_js_change(str(k), ("src/a.ts",)) for k in range(10)]
    done: dict[str, dict[str, object]] = {
        c.key: {"key": c.key, "chosen": [m.to_json() | {"id": m.id}]} for c in ordered[:3]
    }
    none: dict[str, object] = {"key": ordered[3].key, "chosen": []}  # no mutants: never counted
    records: list[dict[str, object]] = [*done.values(), none]
    done_mutants = {f"{k}|{m.id}" for k in done}
    processed: list[str] = []

    def process(change: mu.Change, record: dict[str, object] | None) -> int:
        processed.append(change.key)
        return 1

    adopted: list[str] = []
    finished = mu.walk_sample(ordered, records, done_mutants, 5, lambda: False, process,
                              lambda r: adopted.append(str(r["key"])))  # fmt: skip
    assert finished
    assert processed == [ordered[4].key, ordered[5].key]  # 3 done + 2 new = 5
    assert adopted == list(done)
    assert not mu.walk_sample(ordered, records, done_mutants, 5, lambda: True, process)


def test_relabel_moves_a_change_and_its_mutants_to_another_sample(tmp_path: Path) -> None:
    (tmp_path / "commits.jsonl").write_text(
        '{"key": "#1@a", "sample": "targeted"}\n{"key": "#2@b"}\n', encoding="utf-8"
    )
    (tmp_path / "mutants.jsonl").write_text(
        '{"key": "x", "change": "#1@a", "sample": "targeted"}\n', encoding="utf-8"
    )
    mj.relabel(tmp_path, "#1@a", mj.RANDOM)
    commits = mu.read_jsonl(tmp_path / "commits.jsonl")
    assert [mj.sample_of(c) for c in commits] == [mj.RANDOM, mj.RANDOM]
    assert mu.read_jsonl(tmp_path / "mutants.jsonl")[0]["sample"] == mj.RANDOM


def test_a_rerun_that_runs_nothing_never_counts_as_passed() -> None:
    # Bug: jest -t with an ESLint RuleTester name matched nothing; the file's tests came back
    # skipped and the candidate killers counted as "failed once, passed when re-run".
    suite = FakeSuite(["a::rule"], ["b::t"], fails_with_mutant=["a::rule"], unnamed=["a::rule"])
    decision = _decide(suite, {"a::rule"})
    assert decision.outcome == mu.CAUGHT and decision.killers == ["a::rule"]
    assert ("files-mutant", ("a::rule",)) in suite.calls  # re-run by its file
    assert ("files-baseline", ("a::rule",)) in suite.calls
    assert decision.unconfirmed == [] and decision.not_rerun == []
    kinds = [r["kind"] for r in decision.runs]
    assert kinds == ["selected", "confirm", "confirm-files", "baseline", "baseline-files"]


def test_a_candidate_no_rerun_can_execute_leaves_the_mutant_undecided() -> None:
    suite = FakeSuite(["a::rule"], ["b::t"], fails_with_mutant=["a::rule"], unrunnable=["a::rule"])
    decision = _decide(suite, {"a::rule"})
    assert decision.outcome == mu.UNDECIDED  # never "not killed": it may well kill it
    assert decision.not_rerun == ["a::rule"] and decision.unconfirmed == []
    full = _decide(FakeSuite(["a::rule"], fails_with_mutant=["a::rule"],
                             unrunnable=["a::rule"]), set(), full=True)  # fmt: skip
    assert full.outcome == mu.UNDECIDED
    # A killer outside the selection, but a selected candidate couldn't be checked: undecided,
    # not missed (that candidate may catch it).
    mixed = FakeSuite(["a::rule"], ["b::t"], fails_with_mutant=["a::rule", "b::t"],
                      unrunnable=["a::rule"])  # fmt: skip
    assert _decide(mixed, {"a::rule"}).outcome == mu.UNDECIDED
    assert mu.executed(mu.RunResult({"x": Status.SKIPPED}), "x") is False
    assert mu.executed(mu.RunResult({"x": Status.PASSED}), "x") is True


def test_a_package_docstring_doctest_is_rerun_from_its_init() -> None:
    # The 8 rdflib "not killed" mutants rested on rdflib.__init__::rdflib, which no re-run had
    # executed: pytest_file knows no file for a package docstring's doctest.
    assert mu.rerun_file("rdflib.__init__::rdflib") == "rdflib/__init__.py"
    assert mu.rerun_file("a.b.__init__::a.b") == "a/b/__init__.py"
    assert mu.rerun_file("test.test_x::test_y") == "test/test_x.py"
    assert mu.rerun_file("rdflib.__init__::other") is None
