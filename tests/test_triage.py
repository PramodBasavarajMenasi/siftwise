"""Triage verdict rules (pure: histories are built by hand) and the selector's change links."""

from datetime import UTC, datetime, timedelta

import pytest

from siftwise.core.selector import (
    LINK_DECLARED,
    LINK_GO_IMPORT,
    LINK_NESTED_BUILD,
    LINK_PATH,
    LINK_PY_IMPORT,
    LINK_ROOT_BUILD,
    LINK_TEST_FILE,
    DependsRule,
    KnownTest,
    RepoHistory,
    SelectorConfig,
    Strength,
    change_links,
    select_tests,
)
from siftwise.core.triage import (
    SAME_COMMIT,
    UNRELATED_FAIL_THEN_PASS,
    Change,
    Failure,
    FlakyEvent,
    RunRef,
    Streak,
    TestHistory,
    TriageResult,
    Verdict,
    messages_match,
    normalize_message,
    triage,
)

T0 = datetime(2026, 10, 1, tzinfo=UTC)
TEST = "tests.test_cart::test_total"  # tests/test_cart.py
SHOP = "github.com/acme/shop"
GO_PKG = f"{SHOP}/cart"
H, M, L = Strength.HIGH, Strength.MEDIUM, Strength.LOW


def sha(n: int) -> str:
    return f"{n:040x}"


def run(n: int, *, branch: str = "main", variant: str | None = "linux") -> RunRef:
    return RunRef(n, sha(n), branch, variant, T0 + timedelta(hours=n))


def change(*files: str, known: bool = True, **kwargs: object) -> Change:
    return Change(tuple(files), known, **kwargs)  # type: ignore[arg-type]


PASSES_ON_MAIN = TestHistory(seen=True, last_main_pass=run(1))
UNRELATED = change("docs/x.txt", "src/other.py")


def one(
    history: TestHistory | None,
    change_: Change,
    message: str | None = None,
    test_id: str = TEST,
    file_path: str | None = None,
) -> TriageResult:
    histories = {} if history is None else {test_id: history}
    [result] = triage([Failure(test_id, message, file_path)], histories, change_, 90, "linux")
    return result


def flaky_event(n: int, kind: str = UNRELATED_FAIL_THEN_PASS, message: str = "") -> FlakyEvent:
    return FlakyEvent(
        kind, sha(n), "linux", (run(n, branch="b"),), (run(n + 1, branch="b"),),
        (message,) if message else (),
    )  # fmt: skip


def streak(failing: int, message: str | None = "AssertionError: assert 2 == 3") -> Streak:
    return Streak("linux", run(10), failing, run(9), message)


# --- 1. same commit passed ----------------------------------------------------------------


def test_a_pass_on_the_same_commit_and_variant_is_flaky_even_if_linked_and_broken() -> None:
    history = TestHistory(seen=True, same_commit_passes=(run(5),), broken_on_main=streak(3))

    result = one(history, change("src/cart.py"))

    assert (result.verdict, result.confidence) == (Verdict.FLAKY, H)
    assert "passed on this commit and variant before (run 5)" in result.reasons[0]


# --- 2. pre-existing ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("failing", "message", "confidence", "note"),
    [
        (2, None, H, None),  # a streak of 2+ main runs
        (1, None, M, None),  # one failing main run
        (1, "AssertionError: assert 5 == 7", H, "same failure message as on main"),
        (1, "KeyError: 'total'", M, "main fails with a different error"),
    ],
)
def test_failing_on_main_is_pre_existing(
    failing: int, message: str | None, confidence: Strength, note: str | None
) -> None:
    history = TestHistory(seen=True, broken_on_main=streak(failing), last_main_pass=run(9))

    result = one(history, UNRELATED, message)

    assert (result.verdict, result.confidence) == (Verdict.PRE_EXISTING, confidence)
    assert result.reasons[0].startswith(f"failing on main on linux since {sha(10)[:7]}")
    assert result.reasons[1] == f"last passed on main at {sha(9)[:7]}"
    assert (note in result.reasons) if note else len(result.reasons) == 2


def test_pre_existing_and_touched_by_this_change_drops_to_medium() -> None:
    history = TestHistory(seen=True, broken_on_main=streak(5))

    result = one(history, change("src/cart.py"))

    assert (result.verdict, result.confidence) == (Verdict.PRE_EXISTING, M)
    assert result.reasons[-1] == (
        "this change also touches it: tests/test_cart.py tests changed src/cart.py"
    )


def test_failing_on_main_on_another_variant_is_only_a_note() -> None:
    history = TestHistory(
        seen=True, last_main_pass=run(1), other_variants_failing=(run(4, variant="macos"),)
    )

    result = one(history, change("src/cart.py"))

    assert result.verdict is Verdict.NEW_REGRESSION
    assert result.reasons[-1] == f"also failing on main on macos (latest main run {sha(4)[:7]})"


# --- 3. unknown diff ----------------------------------------------------------------------


def test_unknown_diff_is_unknown_even_when_flaky() -> None:
    plain = one(PASSES_ON_MAIN, change(known=False))
    flaky = one(TestHistory(seen=True, flaky=(flaky_event(1),)), change(known=False))

    assert (plain.verdict, plain.reasons) == (
        Verdict.UNKNOWN, ("diff unknown: can't tell if this change caused it",))  # fmt: skip
    assert flaky.verdict is Verdict.UNKNOWN
    assert "but the diff is unknown: re-run to tell" in flaky.reasons[0]
    assert plain.links == flaky.links == ()


# --- 4. no history ------------------------------------------------------------------------


def test_a_new_test_whose_file_changed_is_a_new_regression() -> None:
    result = one(None, change("tests/test_cart.py"))

    assert (result.verdict, result.confidence) == (Verdict.NEW_REGRESSION, M)
    assert "new test: no history before this run" in result.reasons[0]


def test_a_new_go_test_in_a_package_whose_test_file_changed_is_a_new_regression() -> None:
    new_go = f"{GO_PKG}::TestNew"
    config = SelectorConfig(go_module="github.com/acme/shop")
    in_test_file = one(None, change("cart/new_test.go", config=config), test_id=new_go)
    in_source = one(None, change("cart/total.go", config=config), test_id=new_go)

    assert (in_test_file.verdict, in_test_file.confidence) == (Verdict.NEW_REGRESSION, M)
    assert in_source.verdict is Verdict.UNKNOWN


def test_a_new_test_linked_only_indirectly_is_unknown() -> None:
    result = one(TestHistory(seen=False), change("src/cart.py"))

    assert result.verdict is Verdict.UNKNOWN
    assert result.reasons == (
        "no history for this test before this run",
        "linked to this change (tests/test_cart.py tests changed src/cart.py), but there's "
        "nothing to compare",
    )


# --- 5-6. linked --------------------------------------------------------------------------


def test_linked_and_known_flaky_is_unknown() -> None:
    history = TestHistory(seen=True, last_main_pass=run(1), flaky=(flaky_event(3),))

    result = one(history, change("src/cart.py"))

    assert (result.verdict, result.confidence) == (Verdict.UNKNOWN, L)
    assert result.reasons[0] == "linked to this change but known flaky: re-run to tell"
    assert "1 unrelated fail-then-pass" in result.reasons[2]


@pytest.mark.parametrize(
    ("change_", "confidence", "signal"),
    [
        (change("tests/test_cart.py"), H, LINK_TEST_FILE),
        (change("src/cart.py"), H, LINK_PATH),
        (change("src/money.py", affected_files={"tests/test_cart.py": ["src/money.py"]}), M,
         LINK_PY_IMPORT),
        (change("pyproject.toml"), L, LINK_ROOT_BUILD),
    ],
)  # fmt: skip
def test_linked_is_a_new_regression_as_strong_as_its_link(
    change_: Change, confidence: Strength, signal: str
) -> None:
    result = one(PASSES_ON_MAIN, change_)

    assert (result.verdict, result.confidence) == (Verdict.NEW_REGRESSION, confidence)
    assert result.links[0].signal == signal
    assert result.reasons[1:] == (
        f"passes on main on linux (last main run {sha(1)[:7]})",
        "no flaky history in 90 days",
    )


def test_strongest_link_decides_and_no_main_pass_lowers_confidence() -> None:
    both = change("pyproject.toml", "src/cart.py")
    linked = one(PASSES_ON_MAIN, both)
    no_main = one(TestHistory(seen=True), both)

    assert (linked.confidence, linked.links[0].signal) == (H, LINK_PATH)
    assert linked.reasons[0].endswith("(+1 more)")
    assert (no_main.verdict, no_main.confidence) == (Verdict.NEW_REGRESSION, M)
    assert "no passing main run on linux to compare with" in no_main.reasons


# --- 7-8. not linked ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("events", "message", "confidence"),
    [
        ((flaky_event(1),), None, M),
        ((flaky_event(1), flaky_event(5, SAME_COMMIT)), None, H),
        ((flaky_event(1, message="timeout after 30.5s"),), "timeout after 12s", H),
        ((flaky_event(1, message="timeout after 30.5s"),), "connection refused", M),
    ],
)
def test_unlinked_with_flaky_evidence_is_flaky(
    events: tuple[FlakyEvent, ...], message: str | None, confidence: Strength
) -> None:
    result = one(TestHistory(seen=True, flaky=events), UNRELATED, message)

    assert (result.verdict, result.confidence) == (Verdict.FLAKY, confidence)
    assert result.reasons[0].startswith(f"flaky {len(events)} time")
    assert result.reasons[1] == "not linked to this change"


def test_flaky_counts_keep_the_two_kinds_apart() -> None:
    events = (flaky_event(1), flaky_event(3), flaky_event(5, SAME_COMMIT))
    result = one(TestHistory(seen=True, flaky=events), UNRELATED)

    assert result.flaky_counts == {SAME_COMMIT: 1, UNRELATED_FAIL_THEN_PASS: 2}
    assert result.reasons[0] == (
        "flaky 3 times in 90 days (1 same-commit, 2 unrelated fail-then-pass)"
    )


def test_unlinked_without_evidence_is_unknown() -> None:
    result = one(PASSES_ON_MAIN, UNRELATED)

    assert (result.verdict, result.confidence) == (Verdict.UNKNOWN, L)
    assert result.reasons[0].startswith("not linked to this change, and nothing shows it's flaky")


def test_an_empty_known_diff_links_nothing() -> None:
    assert one(PASSES_ON_MAIN, change()).verdict is Verdict.UNKNOWN


def test_duplicate_failures_get_one_result_in_first_seen_order() -> None:
    failures = [Failure("b::t"), Failure("a::t", "first"), Failure("b::t"), Failure("a::t")]

    results = triage(failures, {}, UNRELATED)

    assert [r.test_id for r in results] == ["b::t", "a::t"]


# --- messages -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("assert 2 == 3", "assert 5 == 7", True),
        ("timeout at 2026-10-01T10:00:00Z", "timeout at 2026-10-02 11:30:01.5+02:00", True),
        ("<Obj at 0x7f3a2b>", "<Obj at 0xdeadbeef>", True),
        ("missing /tmp/pytest-of-ci/pytest-3/x0/out.txt", "missing /tmp/abc/out.txt", True),
        (r"missing C:\Users\me\AppData\Local\Temp\x\f.txt", r"missing C:\Temp\y\g.txt", True),
        ("1b4e28ba-2fa1-11d2-883f-0016d3cca427", "00000000-0000-0000-0000-000000000000", True),
        ("KeyError: 'a'", "KeyError: 'b'", False),
    ],
)  # fmt: skip
def test_messages_match_ignores_what_changes_between_runs(a: str, b: str, same: bool) -> None:
    assert messages_match(a, b) is same
    assert normalize_message(a) == normalize_message(a)


def test_messages_match_needs_both_messages() -> None:
    assert messages_match(None, "x") is None
    assert messages_match("  ", "x") is None


# --- change links -------------------------------------------------------------------------


def known(*test_ids: str) -> list[KnownTest]:
    return [KnownTest(t) for t in test_ids]


def test_change_links_signals_and_strengths() -> None:
    config = SelectorConfig(
        go_module="github.com/acme/shop",
        depends=(DependsRule("e2e/**", ("cmd/**",)),),
    )
    tests = known(TEST, "tests.test_db::test_q", f"{GO_PKG}::TestAdd",
                  f"{SHOP}/e2e::TestE2E", f"{SHOP}/tools/x::TestX")  # fmt: skip
    links = change_links(
        tests,
        ["src/cart.py", "cmd/main.go", "tools/x/go.mod", "README.md"],
        config,
        affected_packages={GO_PKG: ["github.com/acme/shop/cmd"]},
        affected_files={"tests/test_db.py": ["src/cart.py"]},
    )

    def signals(test_id: str) -> list[tuple[str, Strength]]:
        return [(link.signal, link.strength) for link in links[test_id]]

    assert signals(TEST) == [(LINK_PATH, H)]
    assert signals("tests.test_db::test_q") == [(LINK_PY_IMPORT, M)]
    assert signals(f"{GO_PKG}::TestAdd") == [(LINK_GO_IMPORT, M)]
    assert signals("github.com/acme/shop/e2e::TestE2E") == [(LINK_DECLARED, M)]
    assert signals("github.com/acme/shop/tools/x::TestX") == [(LINK_NESTED_BUILD, M)]


def test_change_links_root_build_file_links_everything_low() -> None:
    links = change_links(known(TEST, "tests.test_db::test_q"), ["go.mod", "docs/a.md"])

    assert {t: [(x.signal, x.strength) for x in v] for t, v in links.items()} == {
        TEST: [(LINK_ROOT_BUILD, L)],
        "tests.test_db::test_q": [(LINK_ROOT_BUILD, L)],
    }


def test_change_links_skip_deleted_test_files_and_ignorable_files() -> None:
    links = change_links(known(TEST), ["tests/test_cart.py", "CHANGELOG.md"],
                         deleted_files=["tests/test_cart.py"])  # fmt: skip
    assert links == {TEST: ()}


@pytest.mark.parametrize(
    "changed",
    [
        ["src/cart.py"],
        ["tests/test_cart.py", "docs/index.md"],
        ["cart/total.go"],
        ["cmd/main.go"],
        ["src/money.py"],
        ["sub/go.mod"],
        ["src/cart.py", "cmd/main.go", "README.md"],
    ],
)
def test_change_links_agree_with_the_selectors_change_signals(changed: list[str]) -> None:
    # With no failure history and no always-run patterns, select_tests selects exactly the
    # tests the change links to (unless it falls back to the full suite).
    tests = known(TEST, "tests.test_db::test_q", f"{GO_PKG}::TestAdd",
                  f"{SHOP}/e2e::TestE2E", f"{SHOP}/sub::TestSub")  # fmt: skip
    config = SelectorConfig(go_module="github.com/acme/shop",
                            depends=(DependsRule("e2e/**", ("cmd/**",)),))  # fmt: skip
    packages = {GO_PKG: [f"{SHOP}/cmd"]}
    files = {"tests/test_db.py": ["src/money.py"]}
    history = RepoHistory(tests={t.test_id: t for t in tests})

    selection = select_tests(history, changed, True, config, packages, files)
    links = change_links(tests, changed, config, packages, files)

    assert selection.mode.value == "selective", selection.reason
    assert {t.test_id for t in selection.tests} == {t for t, v in links.items() if v}
