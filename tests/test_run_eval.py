"""Core logic of scripts/eval/run_eval.py: what would run, why misses happen, no peeking."""

import io
import json
import subprocess
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
import typer
from sqlalchemy import text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from scripts.eval import run_eval as ev
from scripts.eval import triage_labels as tl
from siftwise.cli.pygraph import PyGraph, build_py_graph
from siftwise.core.ingest import create_run
from siftwise.core.junit import ParsedTestResult, Status
from siftwise.core.schemas import RunMetadata
from siftwise.core.selector import (
    FailedRun,
    KnownTest,
    Mode,
    RepoHistory,
    Runner,
    SelectedTest,
    Selection,
    SelectorConfig,
    load_history,
)

PKG = "github.com/acme/shop/cart"


def selection(
    *tests: tuple[str, Runner], whole: tuple[str, ...] = (), full: bool = False
) -> Selection:
    return Selection(
        mode=Mode.FULL if full else Mode.SELECTIVE,
        reason="",
        tests=tuple(SelectedTest(t, ("r",), runner, None) for t, runner in tests),
        selected_count=len(tests),
        total_known=100,
        commands=(),
        go_packages_run_whole=whole,
    )


# --- would_run ----------------------------------------------------------------------------


def test_full_mode_runs_everything() -> None:
    assert ev.would_run(selection(full=True), "anything::at_all")


def test_exact_test_runs() -> None:
    sel = selection(("tests.test_a::test_x", Runner.PYTEST))
    assert ev.would_run(sel, "tests.test_a::test_x")
    assert not ev.would_run(sel, "tests.test_a::test_y")


def test_whole_go_package_runs_every_test_in_it_including_new_ones() -> None:
    sel = selection((f"{PKG}::TestOld", Runner.GO), whole=(PKG,))
    assert ev.would_run(sel, f"{PKG}::TestBrandNew/sub")
    assert not ev.would_run(sel, "github.com/acme/shop/pay::TestOld")


def test_whole_python_file_runs_every_test_in_it_including_new_ones() -> None:
    sel = Selection(
        mode=Mode.SELECTIVE, reason="", tests=(), selected_count=0, total_known=10, commands=(),
        python_files_run_whole=("tests/test_cart.py", "rdflib/container.py"),
    )  # fmt: skip
    assert ev.would_run(sel, "tests.test_cart.TestNew::test_brand_new")
    assert ev.would_run(sel, "rdflib.container::rdflib.container.Seq")  # doctest module
    assert not ev.would_run(sel, "tests.test_other::test_x")


def test_go_run_filter_covers_subtests_of_selected_top_level_tests() -> None:
    sel = selection((f"{PKG}::TestTotal/discount", Runner.GO))
    assert ev.would_run(sel, f"{PKG}::TestTotal/other_subtest")  # -run '^(TestTotal)$'
    assert not ev.would_run(sel, f"{PKG}::TestOther")


# --- explaining misses --------------------------------------------------------------------

SHA = "a" * 40


def history(*tests: str, failed_runs: tuple[FailedRun, ...] = ()) -> RepoHistory:
    return RepoHistory(tests={t: KnownTest(t) for t in tests}, failed_runs=failed_runs)


@pytest.fixture
def graphs(tmp_path: Path) -> ev.GraphProvider:
    """A provider whose cache holds a Python graph at SHA (no checkout needed)."""
    graph = PyGraph(
        {
            "tests/test_cart.py": frozenset({"shop/cart.py"}),
            "shop/cart.py": frozenset({"shop/money.py"}),
            "shop/money.py": frozenset(),
            "tests/test_other.py": frozenset(),
        }
    )
    (tmp_path / f"pygraph-{SHA}.json").write_text(json.dumps(graph.to_json()))
    return ev.GraphProvider(None, "go", tmp_path)


def explainer(
    hist: RepoHistory, changed: list[str], graphs: ev.GraphProvider, go: bool = False
) -> ev.Explainer:
    return ev.Explainer(hist, changed, None, go, None, graphs, SHA)


def test_new_test_cannot_be_named_by_any_signal(graphs: ev.GraphProvider) -> None:
    missed = explainer(history(), ["shop/money.py"], graphs)("tests.test_cart::test_new")
    assert (missed.known_before, missed.signal) == (False, "new test")


def test_python_import_chain_explains_a_miss(graphs: ev.GraphProvider) -> None:
    missed = explainer(history("tests.test_cart::test_total"), ["shop/money.py"], graphs)(
        "tests.test_cart::test_total"
    )
    assert missed.signal == "python imports"
    assert missed.why == (
        "imports a changed module: tests/test_cart.py -> shop/cart.py -> shop/money.py"
    )


def test_recurring_failure_is_called_flaky(graphs: ev.GraphProvider) -> None:
    earlier = (FailedRun(7, "b" * 40, frozenset({"x.py"}), frozenset({"tests.test_other::t"})),)
    missed = explainer(history("tests.test_other::t", failed_runs=earlier), ["shop/money.py"],
                       graphs)("tests.test_other::t")  # fmt: skip
    assert missed.signal == "flaky / recurring"
    assert "1 earlier run(s) (latest run 7)" in missed.why


def test_unrelated_failure(graphs: ev.GraphProvider) -> None:
    hist = history("tests.test_other::t")
    no_python = explainer(hist, ["docs/index.md"], graphs)("tests.test_other::t")
    not_imported = explainer(hist, ["shop/money.py"], graphs)("tests.test_other::t")
    assert no_python.signal == not_imported.signal == "none (unrelated)"
    assert "doesn't import any changed module" in not_imported.why


def test_missing_python_graph(tmp_path: Path) -> None:
    graphs = ev.GraphProvider(None, "go", tmp_path)  # empty cache, no checkout
    missed = explainer(history("tests.test_cart::t"), ["shop/money.py"], graphs)(
        "tests.test_cart::t"
    )
    assert missed.signal == "python imports (graph unavailable)"


def test_go_miss_outside_the_import_graph_needs_a_declared_dependency(
    graphs: ev.GraphProvider,
) -> None:
    from siftwise.cli.gograph import GoGraph

    go_graph = GoGraph("github.com/acme/shop", {}, {})
    test_id = "github.com/acme/shop/e2e::TestCheckout"
    missed = ev.Explainer(history(test_id), ["cart/cart.go"], "github.com/acme/shop", True,
                          go_graph, graphs, SHA)(test_id)  # fmt: skip
    assert missed.signal == "declared dependency"
    assert "./e2e has no changed files" in missed.why


# --- savings ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("caught", "total", "shown"),
    [
        (2049, 2050, "2049/2050 (99.95%)"),
        (99_999, 100_000, "99999/100000 (99.99%)"),  # never rounded up to 100%
        (140, 140, "140/140 (100.00%)"),
        (1, 3, "1/3 (33.33%)"),
        (0, 0, "0/0"),
    ],
)
def test_recall_shows_exact_counts_and_never_rounds_up(caught: int, total: int, shown: str) -> None:
    assert ev.recall(caught, total) == shown


def test_skipped_percentages() -> None:
    hist = history("t::a", "t::b", "t::c", "t::d")
    durations = {"t::a": 100.0, "t::b": 100.0, "t::c": 600.0, "t::d": 200.0}
    sel = selection(("t::a", Runner.PYTEST))

    tests_pct, time_pct = ev.skipped_pcts(sel, hist, durations)

    assert tests_pct == pytest.approx(75.0)  # 3 of 4 tests skipped
    assert time_pct == pytest.approx(90.0)  # 900 of 1000 ms skipped
    assert ev.skipped_pcts(selection(full=True), hist, durations) == (0.0, 0.0)


@pytest.mark.parametrize(
    ("reason", "signal"),
    [
        ("imports changed package x", "go imports"),
        ("declared dependency: test/cli/** on a.go", "declared"),
        ("co-change: failed in run 3", "co-change"),
        ("failed on main in run 4 (abc)", "recently failed"),
        ("broken on main since abc", "recently failed"),
        ("always-run pattern 'x'", "always-run"),
        ("tests/test_a.py tests changed a.py", "path mapping"),
    ],
)
def test_signal_labels(reason: str, signal: str) -> None:
    assert ev._signal(reason) == signal


# --- targets and history from the database (no peeking) -----------------------------------

REPO = "acme/shop"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def ingest(session: Session, ci: int, hour: int, tests: dict[str, Status], *, variant: str | None,
           is_main: bool = False, run_attempt: int = 1) -> int:  # fmt: skip
    results = [
        ParsedTestResult(t, t.split("::")[0], t.split("::")[1], None, s, 1, 1)
        for t, s in tests.items()
    ]
    meta = RunMetadata(
        repo=REPO, commit_sha=f"{ci:040x}", branch="main" if is_main else "feature",
        is_main=is_main, ci_run_id=str(ci), run_attempt=run_attempt, variant=variant,
        started_at=T0 + timedelta(hours=hour), changed_files=["src/a.py"],
    )  # fmt: skip
    run, _ = create_run(session, meta, results)
    return run.id


P, F = Status.PASSED, Status.FAILED


def test_targets_group_variants_and_union_their_failures(db_session: Session) -> None:
    linux = ingest(db_session, 7, 1, {"t::a": P, "t::b": F}, variant="linux")
    macos = ingest(db_session, 7, 1, {"t::a": F, "t::b": P}, variant="macos")
    repo_id = db_session.execute(
        text("SELECT id FROM repos WHERE name = :n"), {"n": REPO}
    ).scalar_one()

    [target] = ev.pr_targets(db_session, repo_id, 10, failing=False)

    assert target.run_ids == (linux, macos)
    assert target.run_id == linux
    assert target.changed_files == ("src/a.py",)
    assert ev.actual_failures(db_session, list(target.run_ids)) == ["t::a", "t::b"]


def test_history_before_excludes_later_runs_and_sibling_variants(db_session: Session) -> None:
    ingest(db_session, 1, 1, {"t::early": P}, variant="linux", is_main=True)
    ingest(db_session, 2, 2, {"t::target_linux": F}, variant="linux")
    ingest(db_session, 2, 2, {"t::target_macos": F}, variant="macos")
    ingest(db_session, 3, 3, {"t::later": P}, variant="linux", is_main=True)
    repo_id = db_session.execute(
        text("SELECT id FROM repos WHERE name = :n"), {"n": REPO}
    ).scalar_one()
    target = next(t for t in ev.pr_targets(db_session, repo_id, 10, False) if t.ci_run_id == "2")

    hist = ev.HistoryIndex(db_session, repo_id).history_before(target, SelectorConfig())

    # Only CI run 1 happened before CI run 2: neither its own variants nor run 3 leak in.
    assert set(hist.tests) == {"t::early"}
    assert hist.failed_runs == ()


S = Status.SKIPPED


def test_history_index_matches_load_history_after_the_last_run(db_session: Session) -> None:
    # Matrix CI runs on main and branches; a test broken on main on one variant only, a test
    # that recovers, one only skipped lately, and one that stopped appearing.
    ingest(db_session, 1, 1, {"t::a": P, "t::b": F, "t::gone": P}, variant="linux", is_main=True)
    ingest(db_session, 1, 1, {"t::a": P, "t::b": P, "t::gone": P}, variant="macos", is_main=True)
    ingest(db_session, 2, 2, {"t::a": F, "t::b": F, "t::c": P}, variant="linux")
    ingest(db_session, 3, 3, {"t::a": P, "t::b": F, "t::c": S}, variant="linux", is_main=True)
    ingest(db_session, 3, 3, {"t::a": F, "t::b": P, "t::c": S}, variant="macos", is_main=True)
    ingest(db_session, 4, 4, {"t::a": P, "t::b": F, "t::c": P}, variant="linux", is_main=True)
    repo_id = db_session.execute(
        text("SELECT id FROM repos WHERE name = :n"), {"n": REPO}
    ).scalar_one()
    config = SelectorConfig(known_test_runs=3, recent_main_runs=2)
    after_everything = ev.TargetRun(10**9, None, "f" * 40, "x", T0 + timedelta(days=1))

    from_index = ev.HistoryIndex(db_session, repo_id).history_before(after_everything, config)
    from_db = load_history(db_session, REPO, config)

    assert from_db is not None
    assert set(from_index.tests) == set(from_db.tests) == {"t::a", "t::b", "t::c"}
    # b: failing on linux main since commit 1; a: its latest macos main result failed (3).
    assert (
        from_index.broken_on_main
        == from_db.broken_on_main
        == {
            "t::a": f"{3:040x}",
            "t::b": f"{1:040x}",
        }
    )
    assert from_index.recent_main_failures == from_db.recent_main_failures
    assert set(from_index.recent_main_failures) == {"t::a", "t::b"}
    assert from_index.failed_runs == tuple(from_db.failed_runs)


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, check=True, text=True,
    )  # fmt: skip
    return proc.stdout.strip()


def test_deleted_files_come_from_the_commit_file_list_and_are_cached(tmp_path: Path) -> None:
    repo, cache = tmp_path / "repo", tmp_path / "cache"
    (repo / "tests").mkdir(parents=True)
    cache.mkdir()
    git(repo, "init", "-q")
    (repo / "tests" / "test_old.py").write_text("")
    (repo / "tests" / "test_keep.py").write_text("")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "one")
    git(repo, "rm", "-q", "tests/test_old.py")
    git(repo, "commit", "-q", "-m", "two")
    sha = git(repo, "rev-parse", "HEAD")
    changed = ["tests/test_keep.py", "tests/test_old.py"]

    assert ev.GraphProvider(repo, "go", cache).deleted_at(sha, changed) == ("tests/test_old.py",)
    # Cached: no checkout needed the second time.
    assert ev.GraphProvider(None, "go", cache).deleted_at(sha, changed) == ("tests/test_old.py",)
    # No checkout and no cache: unknown.
    assert ev.GraphProvider(None, "go", cache).deleted_at("b" * 40, changed) is None


def test_evaluate_never_runs_a_deleted_test_file(db_session: Session, tmp_path: Path) -> None:
    def upload(ci: int, hour: int, changed: list[str]) -> None:
        results = [
            ParsedTestResult(f"tests.{m}::t", f"tests.{m}", "t", None, P, 1, 1)
            for m in ("test_old", "test_keep")
        ]
        meta = RunMetadata(
            repo=REPO, commit_sha=f"{ci:040x}", branch="feature", is_main=False,
            ci_run_id=str(ci), started_at=T0 + timedelta(hours=hour), changed_files=changed,
        )  # fmt: skip
        create_run(db_session, meta, results)

    upload(1, 1, ["src/a.py"])
    # The target deletes tests/test_old.py and edits tests/test_keep.py.
    upload(2, 2, ["tests/test_keep.py", "tests/test_old.py"])
    repo_id = db_session.execute(
        text("SELECT id FROM repos WHERE name = :n"), {"n": REPO}
    ).scalar_one()
    target = next(t for t in ev.pr_targets(db_session, repo_id, 10, False) if t.ci_run_id == "2")
    (tmp_path / f"files-{target.commit_sha}.json").write_text(json.dumps(["tests/test_keep.py"]))
    graphs = ev.GraphProvider(None, "go", tmp_path)

    result = ev.evaluate(
        db_session, ev.HistoryIndex(db_session, repo_id), target, SelectorConfig(), graphs, {},
        go=False,
    )  # fmt: skip

    assert result.selection.mode is Mode.SELECTIVE
    assert result.selection.python_files_run_whole == ("tests/test_keep.py",)
    assert result.selection.command == "pytest tests/test_keep.py"


def iso(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def zipball(files: dict[str, str], sha: str) -> bytes:
    """A GitHub-style zipball: every entry under one "<owner>-<repo>-<sha>/" folder."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in files.items():
            zf.writestr(f"acme-shop-{sha[:7]}/{name}", content)
    return buffer.getvalue()


@respx.mock(assert_all_mocked=True)
def test_github_source_gives_graphs_and_file_lists_without_git(
    respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    sha = "c" * 40
    route = respx_mock.get(f"https://api.github.com/repos/acme/shop/zipball/{sha}").respond(
        content=zipball({"shop/cart.py": "import shop.money\n", "shop/money.py": "",
                         "tests/test_cart.py": "import shop.cart\n"}, sha))  # fmt: skip
    graphs = ev.GraphProvider(None, "go", tmp_path, ev.GitHubSource("t", "acme/shop"))

    graph = graphs.py_graph_at(sha)
    assert graph is not None
    assert graph.affected(["shop/money.py"]) == {"tests/test_cart.py": ["shop/money.py"]}
    # The file list comes from the same zipball (one download) and is cached.
    assert graphs.deleted_at(sha, ["shop/cart.py", "shop/gone.py"]) == ("shop/gone.py",)
    assert route.call_count == 1
    assert ev.GraphProvider(None, "go", tmp_path).deleted_at(sha, ["shop/gone.py"]) == (
        "shop/gone.py",
    )


@respx.mock(assert_all_mocked=True)
def test_merged_pr_targets_cut_history_at_the_head_commit(
    respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    gh = "https://api.github.com/repos/acme/shop"
    now = datetime.now(UTC)
    respx_mock.get(f"{gh}/pulls").respond(json=[
        {"number": 2, "merged_at": iso(now - timedelta(days=1)), "updated_at": iso(now),
         "head": {"sha": "b" * 40}, "base": {"sha": "a" * 40}},
        {"number": 3, "merged_at": None, "updated_at": iso(now),  # closed, not merged
         "head": {"sha": "d" * 40}, "base": {"sha": "a" * 40}},
        {"number": 1, "merged_at": iso(now - timedelta(days=200)), "updated_at": iso(now),
         "head": {"sha": "e" * 40}, "base": {"sha": "a" * 40}},  # outside the window
    ])  # fmt: skip
    pushed = now - timedelta(days=2)
    respx_mock.get(f"{gh}/commits/{'b' * 40}").respond(
        json={"commit": {"committer": {"date": iso(pushed)}}})  # fmt: skip
    renamed = {"filename": "src/new.py", "previous_filename": "src/old.py"}
    respx_mock.get(f"{gh}/compare/{'a' * 40}...{'b' * 40}").respond(json={"files": [renamed]})

    [target] = ev.merged_pr_targets("t", "acme/shop", 90, ev.GitHubCompare("t", tmp_path), now)

    assert (target.commit_sha, target.branch, target.run_id) == ("b" * 40, "#2", 0)
    assert abs(target.run_at - pushed) < timedelta(seconds=1)  # head commit, not merge time
    assert target.changed_files == ("src/new.py", "src/old.py")


def zipball_with_links(files: dict[str, str], links: dict[str, str], sha: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in files.items():
            zf.writestr(f"acme-shop-{sha[:7]}/{name}", content)
        for name, target in links.items():
            info = zipfile.ZipInfo(f"acme-shop-{sha[:7]}/{name}")
            info.external_attr = 0o120777 << 16  # a symlink, as GitHub's zipballs store them
            zf.writestr(info, target)
    return buffer.getvalue()


@respx.mock(assert_all_mocked=True)
def test_zipball_symlinks_become_copies_of_their_targets(
    respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    sha = "d" * 40
    respx_mock.get(f"https://api.github.com/repos/acme/shop/zipball/{sha}").respond(
        content=zipball_with_links(
            {"real/pkg/a.go": "package pkg", "real/x.txt": "x"},
            {"cmd/pkg": "../real/pkg",  # a directory link, like substrate's
             "cmd/x.txt": "../real/x.txt",
             "chain": "cmd/pkg",  # a link to a link
             "outside": "../../etc", "dangling": "nope"},
            sha,
        )
    )  # fmt: skip
    root = tmp_path / "tree"

    files = ev.GitHubSource("t", "acme/shop").extract(sha, root)

    assert files is not None and "cmd/pkg" in files  # listed like git lists a link
    assert (root / "cmd/pkg/a.go").read_text() == "package pkg"
    assert (root / "cmd/x.txt").read_text() == "x"
    assert (root / "chain/a.go").is_file()
    assert not (root / "outside").exists() and not (root / "dangling").exists()


@respx.mock(assert_all_mocked=True)
def test_providers_unpack_into_private_directories(
    respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    # Two evals side by side used to share one directory and wipe each other's trees.
    a, b = "a" * 40, "b" * 40
    for sha, module in ((a, "first"), (b, "second")):
        respx_mock.get(f"https://api.github.com/repos/acme/shop/zipball/{sha}").respond(
            content=zipball({f"{module}.py": "", "tests/test_x.py": f"import {module}\n"}, sha)
        )
    one = ev.GraphProvider(None, "go", tmp_path, ev.GitHubSource("t", "acme/shop"))
    two = ev.GraphProvider(None, "go", tmp_path, ev.GitHubSource("t", "acme/shop"))

    root_one = one._root_at(a)
    root_two = two._root_at(b)
    assert root_one is not None and root_two is not None and root_one != root_two
    assert (root_one / "first.py").exists()  # not wiped by the other provider
    one.close()
    two.close()
    assert not root_one.exists() and not root_two.exists()


def test_replay_main_last_n(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    for ci in range(1, 6):
        ingest(db_session, ci, ci, {"t::a": P}, variant=None, is_main=True)
    repo_id = db_session.execute(
        text("SELECT id FROM repos WHERE name = :n"), {"n": REPO}
    ).scalar_one()
    compare = ev.GitHubCompare("t", Path("."))
    monkeypatch.setattr(compare, "files", lambda repo, base, head: ("src/a.py",))

    every = ev.main_replay_targets(db_session, repo_id, REPO, compare)
    last_two = ev.main_replay_targets(db_session, repo_id, REPO, compare, last=2)

    assert len(every) == 4
    assert [t.commit_sha for t in last_two] == [t.commit_sha for t in every[-2:]]


def test_resume_skips_finished_targets_and_ignores_a_torn_line(tmp_path: Path) -> None:
    done = ev.TargetRun(0, None, "a" * 40, "#1", T0, changed_files=(), diff_label="PR #1")
    other = ev.TargetRun(0, None, "b" * 40, "#2", T0, changed_files=(), diff_label="PR #2")
    log = tmp_path / "results.jsonl"
    log.write_text(json.dumps({"key": ev.target_key(done)}) + "\n" + '{"key": "PR #2|bbb')  # crash

    keys = ev.resumed_keys(log)

    assert ev.target_key(done) in keys
    assert ev.target_key(other) not in keys  # its line was cut off: it runs again
    assert ev.resumed_keys(tmp_path / "missing.jsonl") == set()


# --- --triage --------------------------------------------------------------------------------


def triage_setup(db_session: Session, tmp_path: Path) -> tuple[int, ev.RunChanges]:
    repo_id = db_session.execute(
        text("SELECT id FROM repos WHERE name = :n"), {"n": REPO}
    ).scalar_one()
    meta = ev.load_run_meta(db_session, repo_id)
    graphs = ev.GraphProvider(None, "go", tmp_path)
    changes = ev.RunChanges(graphs, SelectorConfig(), False, meta,
                            ev.changed_files_of(db_session, list(meta)))  # fmt: skip
    # No import graph needed: the stored change (src/a.py) links by path mapping alone.
    changes.graph_links = lambda commit, files: {  # type: ignore[method-assign]
        "affected_packages": {}, "affected_files": {}, "depends": [], "go_module": None,
        "generated": [],
    }  # fmt: skip
    return repo_id, changes


def run_triage_eval(
    db_session: Session, tmp_path: Path, resume: Path | None = None
) -> dict[tuple[int, str], tl.Scored]:
    repo_id, changes = triage_setup(db_session, tmp_path)
    fixes = lambda test, failed, passed: True  # noqa: E731  (every fix diff is linked)
    scored = ev.triage_eval(db_session, REPO, repo_id, changes, fixes, 90, resume, (), False)  # type: ignore[arg-type]
    return {(s.run_id, s.test): s for s in scored}


def test_triage_eval_verdicts_never_see_later_runs(db_session: Session, tmp_path: Path) -> None:
    ingest(db_session, 1, 1, {"tests.test_a::t": P, "tests.test_b::t": P}, variant=None,
           is_main=True)  # fmt: skip
    pr = ingest(db_session, 2, 2, {"tests.test_a::t": F, "tests.test_b::t": F}, variant=None)
    before = run_triage_eval(db_session, tmp_path)

    # Later: the same commit passes test_b (a re-run), a new commit fixes test_a.
    ingest(db_session, 2, 3, {"tests.test_a::t": F, "tests.test_b::t": P}, variant=None,
           run_attempt=2)  # fmt: skip
    ingest(db_session, 3, 4, {"tests.test_a::t": P, "tests.test_b::t": P}, variant=None)
    after = run_triage_eval(db_session, tmp_path)

    for key in ((pr, "tests.test_a::t"), (pr, "tests.test_b::t")):
        assert (after[key].verdict, after[key].confidence) == (
            before[key].verdict, before[key].confidence)  # fmt: skip
    # Labels may use the future: test_b turned out flaky, test_a a regression.
    assert after[(pr, "tests.test_b::t")].label.kind == tl.FLAKY
    assert after[(pr, "tests.test_b::t")].label.when == "after"
    assert after[(pr, "tests.test_a::t")].label.kind == tl.REGRESSION
    assert before[(pr, "tests.test_a::t")].label.kind is None  # nothing after it yet


def test_triage_eval_resume_skips_done_runs(db_session: Session, tmp_path: Path) -> None:
    ingest(db_session, 1, 1, {"tests.test_a::t": P}, variant=None, is_main=True)
    pr = ingest(db_session, 2, 2, {"tests.test_a::t": F}, variant=None)
    log = tmp_path / "triage.jsonl"

    first = run_triage_eval(db_session, tmp_path, log)
    lines = log.read_text().splitlines()
    second = run_triage_eval(db_session, tmp_path, log)

    assert [json.loads(line)["key"] for line in lines] == [f"triage|{pr}"]
    assert log.read_text().splitlines() == lines  # nothing triaged again
    assert second == first  # read back from the log
    # src/a.py changed: path mapping links tests/test_a.py, main passed before -> regression.
    assert first[(pr, "tests.test_a::t")].verdict == "new_regression"


def test_triage_eval_unknown_diff_is_an_unknown_change(db_session: Session, tmp_path: Path) -> None:
    ingest(db_session, 1, 1, {"tests.test_a::t": P}, variant=None, is_main=True)
    run_id = ingest(db_session, 2, 2, {"tests.test_a::t": F}, variant=None)
    db_session.execute(text("UPDATE runs SET changed_files_known = false WHERE id = :id"),
                       {"id": run_id})  # fmt: skip
    _, changes = triage_setup(db_session, tmp_path)

    assert changes.links(run_id) is None
    assert changes.change(run_id).changed_files_known is False
    assert run_triage_eval(db_session, tmp_path)[(run_id, "tests.test_a::t")].verdict == "unknown"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("GITHUB_TOKEN=abc\n", "abc"),
        ("# comment\nexport GITHUB_TOKEN='a b'\n", "a b"),
        ('GITHUB_TOKEN="x"\nOTHER=1\n', "x"),
        ("GITHUB_TOKEN=abc # note\n", "abc"),
        ("OTHER=1\n", None),
    ],
)
def test_dotenv_value(tmp_path: Path, content: str, expected: str | None) -> None:
    env = tmp_path / ".env"
    env.write_text(content)
    assert ev._dotenv_value(env, "GITHUB_TOKEN") == expected


def test_github_token_prefers_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("GITHUB_TOKEN=from-file\n")
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    assert ev._github_token() == "from-env"
    monkeypatch.delenv("GITHUB_TOKEN")
    assert ev._github_token() == "from-file"


def test_triage_cli_startup_path(
    db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_eval --triage`` through ``main``: run metadata, the change and path maps, the
    triage, the resume log and the summary. The unit tests above call ``triage_eval`` directly
    and missed a crash in main's own setup."""
    ingest(db_session, 1, 1, {"tests.test_a::t": P}, variant=None, is_main=True)
    pr = ingest(db_session, 2, 2, {"tests.test_a::t": F}, variant=None)
    ingest(db_session, 3, 3, {"tests.test_a::t": F}, variant=None)  # filtered out by --commits
    cache = tmp_path / "cache"
    cache.mkdir()
    (tmp_path / "empty").mkdir()
    # src/a.py changed: the commit's Python graph is needed. Cached, so nothing is downloaded.
    graph = build_py_graph(tmp_path / "empty").to_json()
    (cache / f"pygraph-{2:040x}.json").write_text(json.dumps(graph))
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")  # never the real one from .env
    connection = db_session.connection()
    monkeypatch.setattr(ev, "get_sessionmaker", lambda: lambda: Session(
        bind=connection, join_transaction_mode="create_savepoint"))  # fmt: skip
    app = typer.Typer()
    app.command()(ev.main)
    log = tmp_path / "triage.jsonl"

    with respx.mock(assert_all_called=False):  # no routes: any GitHub call fails the test
        result = CliRunner().invoke(app, [
            "--repo", REPO, "--language", "python", "--source", "github", "--triage",
            "--commits", f"{2:040x}", "--cache-dir", str(cache), "--resume", str(log),
        ])  # fmt: skip

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert "# triage: 2 runs with failures, 1 failing tests" in result.output
    assert "=== triage: acme/shop ===" in result.output
    assert "failures: 1; labelled 0 (0.0%)" in result.output
    assert "excluded, never passed again on its branch: 1" in result.output
    [line] = [json.loads(text_line) for text_line in log.read_text().splitlines()]
    assert (line["key"], line["change"]) == (f"triage|{pr}", "known")
    assert line["failures"][0]["verdict"] == "new_regression"


# --- --replay-prs window ----------------------------------------------------------------------


def pr(number: int, merged: str | None, updated: str = "2026-10-20T00:00:00Z") -> dict[str, object]:
    return {"number": number, "merged_at": merged, "updated_at": updated,
            "head": {"sha": f"{number:040x}"}, "base": {"sha": "b" * 40}}  # fmt: skip


def test_merged_prs_window_ends_with_the_recorded_history(
    respx_mock: respx.MockRouter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    until = datetime(2026, 10, 7, 21, 0, tzinfo=UTC)
    respx_mock.get("https://api.github.com/repos/acme/shop/pulls").respond(json=[
        pr(1, "2026-07-01T00:00:00Z"),  # before the window (90 days before until)
        pr(2, "2026-07-10T00:00:00Z"),  # first day inside it
        pr(3, "2026-10-07T20:59:00Z"),  # just before the history ends
        pr(4, "2026-10-07T21:01:00Z"),  # after it: its history would be stale
        pr(5, None),  # closed without merging
    ])  # fmt: skip
    respx_mock.get(url__regex=r".*/commits/.*").respond(
        json={"commit": {"committer": {"date": "2026-10-01T00:00:00Z"}}}
    )
    compare = ev.GitHubCompare("t", tmp_path)
    monkeypatch.setattr(compare, "files", lambda repo, base, head: ("src/a.py",))

    targets = ev.merged_pr_targets("t", "acme/shop", 90, compare, until)

    assert [t.branch for t in targets] == ["#2", "#3"]


def test_history_end_is_the_newest_run(db_session: Session) -> None:
    ingest(db_session, 1, 1, {"t::a": P}, variant=None)
    ingest(db_session, 2, 30, {"t::a": P}, variant=None)
    repo_id = db_session.execute(
        text("SELECT id FROM repos WHERE name = :n"), {"n": REPO}
    ).scalar_one()
    assert ev.history_end(db_session, repo_id) == T0 + timedelta(hours=30)
    assert ev.history_end(db_session, -1) is None


def test_replay_prs_prints_the_window_it_used(
    db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest(db_session, 1, 1, {"t::a": P}, variant=None, is_main=True)
    ingest(db_session, 2, 48, {"t::a": P}, variant=None, is_main=True)  # history ends Jan 3
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    connection = db_session.connection()
    monkeypatch.setattr(ev, "get_sessionmaker", lambda: lambda: Session(
        bind=connection, join_transaction_mode="create_savepoint"))  # fmt: skip
    app = typer.Typer()
    app.command()(ev.main)

    with respx.mock() as github:
        pulls = github.get("https://api.github.com/repos/acme/shop/pulls").respond(json=[])
        args = ["--repo", REPO, "--language", "python", "--replay-prs", "--days", "10"]
        result = CliRunner().invoke(app, [*args, "--cache-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert ("merged PRs window: 2025-12-24 00:00 to 2026-01-03 00:00 UTC (10 days up to the "
            "end of the recorded history)") in result.output  # fmt: skip
    assert pulls.called


# --- network errors -------------------------------------------------------------------------


@pytest.fixture
def no_waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)
    return waits


def test_github_calls_retry_network_errors(
    respx_mock: respx.MockRouter, tmp_path: Path, no_waits: list[float]
) -> None:
    gh = "https://api.github.com/repos/acme/shop"
    respx_mock.get(f"{gh}/compare/{'a' * 40}...{'b' * 40}").mock(side_effect=[
        httpx.ConnectError("getaddrinfo failed"),  # the DNS blip that stopped the substrate eval
        httpx.ReadTimeout("timed out"),
        httpx.Response(200, json={"files": [{"filename": "src/a.py"}]}),
    ])  # fmt: skip
    sha = "c" * 40
    respx_mock.get(f"{gh}/zipball/{sha}").mock(side_effect=[
        httpx.ConnectError("getaddrinfo failed"),
        httpx.Response(200, content=zipball({"a.py": ""}, sha)),
    ])  # fmt: skip

    files = ev.GitHubCompare("t", tmp_path).files("acme/shop", "a" * 40, "b" * 40)
    tree = ev.GitHubSource("t", "acme/shop").extract(sha, tmp_path / "tree")

    assert files == ("src/a.py",)
    assert tree == ["a.py"]
    assert no_waits == [0, 10, 30, 0, 10]


def test_a_network_outage_stops_the_eval_instead_of_skewing_it(
    respx_mock: respx.MockRouter, tmp_path: Path, no_waits: list[float]
) -> None:
    # "Unavailable" (None) would be recorded as a real answer: no graph, an unknown fix diff.
    gh = "https://api.github.com/repos/acme/shop"
    respx_mock.get(url__startswith=gh).mock(side_effect=httpx.ConnectError("getaddrinfo failed"))

    with pytest.raises(httpx.ConnectError):
        ev.GitHubCompare("t", tmp_path).files("acme/shop", "a" * 40, "b" * 40)
    with pytest.raises(httpx.ConnectError):
        ev.GitHubSource("t", "acme/shop").extract("c" * 40, tmp_path / "tree")
    assert no_waits == [*ev.NETWORK_RETRY_WAITS, *ev.NETWORK_RETRY_WAITS]
    assert not list(tmp_path.glob("compare-*"))  # nothing cached


def test_http_errors_still_mean_unavailable(
    respx_mock: respx.MockRouter, tmp_path: Path, no_waits: list[float]
) -> None:
    gh = "https://api.github.com/repos/acme/shop"
    respx_mock.get(url__startswith=gh).respond(404)

    assert ev.GitHubCompare("t", tmp_path).files("acme/shop", "a" * 40, "b" * 40) is None
    assert ev.GitHubSource("t", "acme/shop").extract("c" * 40, tmp_path / "tree") is None
    assert no_waits == [0, 0]  # answered: not retried


def test_each_retry_is_logged_without_the_token(
    respx_mock: respx.MockRouter,
    tmp_path: Path,
    no_waits: list[float],
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "ghp_secret-token-never-logged"
    gh = "https://api.github.com/repos/acme/shop"
    respx_mock.get(f"{gh}/compare/{'a' * 40}...{'b' * 40}").mock(side_effect=[
        httpx.ConnectError(f"getaddrinfo failed (Bearer {token})"),  # even if a message quotes it
        httpx.ReadTimeout("timed out"),
        httpx.Response(200, json={"files": []}),
    ])  # fmt: skip
    respx_mock.get(url__startswith=f"{gh}/zipball/").mock(side_effect=httpx.ConnectError("down"))

    ev.GitHubCompare(token, tmp_path).files("acme/shop", "a" * 40, "b" * 40)
    with pytest.raises(httpx.ConnectError):
        ev.GitHubSource(token, "acme/shop").extract("c" * 40, tmp_path / "tree")

    err = capsys.readouterr().err
    compare = f"/repos/acme/shop/compare/{'a' * 40}...{'b' * 40}"
    zipball = f"/repos/acme/shop/zipball/{'c' * 40}"
    assert err.splitlines() == [
        f"github: attempt 1/4 failed (ConnectError) on GET {compare}; retrying in 10s",
        f"github: attempt 2/4 failed (ReadTimeout) on GET {compare}; retrying in 30s",
        f"github: attempt 1/4 failed (ConnectError) on GET {zipball}; retrying in 10s",
        f"github: attempt 2/4 failed (ConnectError) on GET {zipball}; retrying in 30s",
        f"github: attempt 3/4 failed (ConnectError) on GET {zipball}; retrying in 60s",
        f"github: attempt 4/4 failed (ConnectError) on GET {zipball}; giving up",
    ]
    assert token not in err and "Bearer" not in err
