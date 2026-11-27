"""untested_changes (#103): which tests selection links to each changed file
(``selector.file_links``, ``POST /links``), the nearest test file to extend
(``cli/nearest.py``), the MCP tool, and that ``select_tests`` is unchanged by the sink.

"Linked" is what selection uses (imports, paths, rules, fixture references), never line
coverage: the wording test keeps "covered" and "untested code" out of every output."""

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from mcp import Client
from sqlalchemy.orm import Session

from siftwise.api.main import create_app
from siftwise.cli.nearest import nearest_test
from siftwise.config import Settings
from siftwise.core.ingest import create_run
from siftwise.core.junit import ParsedTestResult, Status
from siftwise.core.schemas import RunMetadata
from siftwise.core.selector import (
    DependsRule,
    FileLink,
    FullReason,
    KnownTest,
    LinkStatus,
    Mode,
    NotChecked,
    RepoHistory,
    SelectorConfig,
    file_links,
    select_tests,
)
from siftwise.db import get_session
from siftwise.mcp.server import MAX_OUTPUT_CHARS, UNTESTED_DESCRIPTION, build_server

ROOT = Path(__file__).resolve().parents[1]
SERVER = "http://siftwise.test"
TOKEN = "s3cret-token-value"
ENV = {"SIFTWISE_SERVER": SERVER, "SIFTWISE_API_TOKEN": TOKEN}

CART = "tests.test_cart::test_total"
PRICES = "tests.test_prices::test_price"
GO_MODULE = "example.com/shop"
GO_TEST = f"{GO_MODULE}/api::TestServe"


def history(*test_ids: str, **paths: str) -> RepoHistory:
    tests = {t: KnownTest(t, file_path=paths.get(t.replace(" ", "_").split("::")[0]),
                          avg_duration_ms=10, runs=5, failures=0) for t in test_ids}  # fmt: skip
    return RepoHistory(tests=tests)


def by_path(links: list[FileLink]) -> dict[str, FileLink]:
    return {f.path: f for f in links}


# --- selector.file_links ------------------------------------------------------------------


def test_files_with_and_without_linked_tests() -> None:
    found = by_path(file_links(
        history(CART, PRICES), ["shop/cart.py", "shop/prices.py", "shop/db.py"], True,
        affected_files={"tests/test_cart.py": ["shop/cart.py"]},
    ))  # fmt: skip
    # cart: by the import graph; prices: by path mapping (module name); db: nothing.
    assert found["shop/cart.py"] == FileLink("shop/cart.py", LinkStatus.LINKED, tests=(CART,))
    assert found["shop/prices.py"].status is LinkStatus.LINKED
    assert found["shop/prices.py"].tests == (PRICES,)
    assert found["shop/db.py"] == FileLink("shop/db.py", LinkStatus.NO_LINKED_TEST)


def test_no_linked_test_is_exactly_what_selection_calls_unmapped() -> None:
    changed = ["shop/cart.py", "shop/db.py", "shop/orders.py"]
    hist = history(CART)
    kwargs: dict[str, Any] = {"affected_files": {"tests/test_cart.py": ["shop/cart.py"]}}
    selection = select_tests(hist, changed, True, **kwargs)
    assert (selection.mode, selection.reason_code) == (Mode.FULL, FullReason.UNMAPPED_FILE)
    unlinked = [f.path for f in file_links(hist, changed, True, **kwargs)
                if f.status is LinkStatus.NO_LINKED_TEST]  # fmt: skip
    assert unlinked == list(selection.reason_items) == ["shop/db.py", "shop/orders.py"]


def test_go_files_are_linked_through_the_packages_dependents() -> None:
    config = SelectorConfig(go_module=GO_MODULE)
    found = by_path(file_links(
        history(GO_TEST), ["store/db.go", "store/cache.go", "other/x.go"], True, config,
        affected_packages={f"{GO_MODULE}/api": [f"{GO_MODULE}/store"]},
    ))  # fmt: skip
    assert found["store/db.go"].tests == found["store/cache.go"].tests == (GO_TEST,)
    assert found["other/x.go"].status is LinkStatus.NO_LINKED_TEST


def test_a_declared_rule_links_even_an_unsupported_language() -> None:
    config = SelectorConfig(depends=(DependsRule("tests/**", ("native/**",)),))
    found = by_path(file_links(history(CART), ["native/lib.rs", "other/lib.rs"], True, config))
    assert found["native/lib.rs"].status is LinkStatus.LINKED  # [[depends]]: linked
    assert found["other/lib.rs"].status is LinkStatus.NOT_CHECKED
    assert found["other/lib.rs"].reason_code is NotChecked.LANGUAGE_NOT_SUPPORTED
    assert "language not supported" in found["other/lib.rs"].reason


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("README.md", NotChecked.IGNORABLE),
        ("docs/guide.rst", NotChecked.IGNORABLE),
        ("pyproject.toml", NotChecked.BUILD_FILE),
        (".github/workflows/ci.yml", NotChecked.BUILD_FILE),
        ("web/package.json", NotChecked.NESTED_BUILD_FILE),
        ("junit.xml", NotChecked.GENERATED),
        ("app/src/main/kotlin/Cart.kt", NotChecked.JAVA_BUILD),  # until #102
    ],
)
def test_non_source_files_are_not_checked_with_the_selectors_reason(
    path: str, code: NotChecked
) -> None:
    [found] = file_links(history(CART), [path], True)
    assert (found.status, found.reason_code) == (LinkStatus.NOT_CHECKED, code)
    assert found.reason


def test_a_fixture_and_a_test_file_and_a_deleted_file() -> None:
    found = by_path(file_links(
        history(CART), ["tests/data/cart.json", "tests/test_cart.py", "shop/gone.py"], True,
        fixture_refs={"tests/data/cart.json": ["tests/test_cart.py"]},
        deleted_files=["shop/gone.py"],
    ))  # fmt: skip
    assert found["tests/data/cart.json"].reason_code is NotChecked.FIXTURE
    assert found["tests/test_cart.py"].status is LinkStatus.TEST_FILE
    assert found["tests/test_cart.py"].tests == (CART,)  # it runs itself
    assert found["shop/gone.py"].reason_code is NotChecked.DELETED


def test_jest_files() -> None:
    hist = history("cart::adds", cart="src/cart.test.ts")
    found = by_path(file_links(hist, ["src/cart.ts", "src/orders.ts"], True,
                               affected_files={"src/cart.test.ts": ["src/cart.ts"]}))  # fmt: skip
    assert found["src/cart.ts"].tests == ("cart::adds",)
    assert found["src/orders.ts"].status is LinkStatus.NO_LINKED_TEST


def test_java_files() -> None:
    from siftwise.core.selector import JavaBuildInfo, JavaModuleInfo

    java = JavaBuildInfo("maven", modules=(JavaModuleInfo("m", "m"),),
                         test_classes={"a.FooTest": "m"})  # fmt: skip
    found = by_path(file_links(
        history("a.FooTest::t"),
        ["m/src/main/java/a/Foo.java", "m/src/main/java/a/Bar.java",
         "m/src/main/resources/app.properties"],
        True, java=java,
    ))  # fmt: skip
    assert found["m/src/main/java/a/Foo.java"].tests == ("a.FooTest::t",)  # FooTest by name
    assert found["m/src/main/java/a/Bar.java"].status is LinkStatus.NO_LINKED_TEST
    resources = found["m/src/main/resources/app.properties"]
    assert resources.reason_code is NotChecked.JAVA_BUILD
    assert "main resources" in resources.reason


def test_without_history_or_a_known_diff_nothing_is_checked() -> None:
    [none] = file_links(None, ["shop/cart.py"], True)
    [unknown] = file_links(history(CART), ["shop/cart.py"], False)
    assert none.reason_code is NotChecked.NO_HISTORY
    assert unknown.reason_code is NotChecked.CHANGED_FILES_UNKNOWN


def test_select_tests_is_unchanged_by_the_sink_over_the_selector_test_corpus(
    tmp_path: Path,
) -> None:
    """Every _select call of the selector's own test files runs with and without the sink
    (tests/_sink_check.py) and must give equal selections."""
    count = tmp_path / "count"
    files = ["tests/test_selector.py", "tests/test_budget.py", "tests/test_javagraph.py",
             "tests/test_jsgraph.py", "tests/test_jsgraph_contexts.py", "tests/test_risk.py",
             "tests/test_fixtures.py", "tests/test_java_build_scopes.py"]  # fmt: skip
    env = {**os.environ, "SIFTWISE_SINK_CHECK_COUNT": str(count), "SIFTWISE_TEST_DATABASE_URL": ""}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p",
         "tests._sink_check", "-x", *files],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=900, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    assert int(count.read_text()) > 300  # selections compared


# --- nearest_test -------------------------------------------------------------------------


FILES = [
    "shop/cart.py", "shop/test_cart.py", "shop/orders.py", "shop/test_misc.py",
    "lib/util.py", "tests/lib/test_util.py", "tests/test_util.py", "other/thing.py",
    "store/db.go", "store/db_test.go", "store/cache.go", "api/serve.go",
    "web/src/cart.ts", "web/src/__tests__/cart.test.ts", "web/src/orders.ts",
    "core/src/main/java/a/b/Cart.java", "core/src/test/java/a/b/CartTests.java",
    "core/src/main/java/a/b/Orders.java", "core/src/main/java/a/c/Lonely.java",
]  # fmt: skip


@pytest.mark.parametrize(
    ("path", "nearest"),
    [
        ("shop/cart.py", "shop/test_cart.py"),  # convention, same folder
        ("shop/orders.py", "shop/test_cart.py"),  # another test file in the folder
        ("lib/util.py", "tests/lib/test_util.py"),  # convention elsewhere: the nearest path
        ("other/thing.py", None),
        ("store/cache.go", "store/db_test.go"),  # the package's tests
        ("api/serve.go", None),
        ("web/src/cart.ts", "web/src/__tests__/cart.test.ts"),
        ("web/src/orders.ts", None),
        ("core/src/main/java/a/b/Cart.java", "core/src/test/java/a/b/CartTests.java"),
        ("core/src/main/java/a/b/Orders.java", "core/src/test/java/a/b/CartTests.java"),
        ("core/src/main/java/a/c/Lonely.java", None),
        ("app/src/main/kotlin/Cart.kt", None),  # not analysed (#102)
    ],
)
def test_nearest_test(path: str, nearest: str | None) -> None:
    assert nearest_test(path, FILES) == nearest


# --- POST /links --------------------------------------------------------------------------

API_TOKEN = "test-token"


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app = create_app(Settings(_env_file=None, api_token=API_TOKEN))
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seeded(db_session: Session) -> None:
    results = [ParsedTestResult(test_id=t, classname=t.split("::")[0], name=t.split("::")[1],
                                file_path=None, status=Status.PASSED, duration_ms=5, attempt=1)
               for t in (CART, PRICES)]  # fmt: skip
    started = datetime(2026, 11, 1, tzinfo=UTC) + timedelta(hours=1)
    meta = RunMetadata(repo="acme/shop", commit_sha="a" * 40, branch="main", is_main=True,
                       started_at=started)  # fmt: skip
    create_run(db_session, meta, results)


@pytest.mark.usefixtures("seeded")
def test_links_api(client: TestClient) -> None:
    response = client.post("/links", headers={"Authorization": f"Bearer {API_TOKEN}"}, json={
        "repo": "acme/shop",
        "changed_files": ["shop/prices.py", "shop/cart.py", "shop/db.py", "README.md",
                          "pyproject.toml"],
        "affected_files": {"tests/test_cart.py": ["shop/cart.py"]},
    })  # fmt: skip
    assert response.status_code == 200, response.text
    body = response.json()
    files = [(f["path"], f["status"], f["linked_tests"], f["reason_code"]) for f in body["files"]]
    assert files == [
        ("shop/db.py", "no_linked_test", 0, None),  # what to act on first
        ("shop/cart.py", "linked", 1, None),
        ("shop/prices.py", "linked", 1, None),
        ("README.md", "not_checked", 0, "ignorable"),
        ("pyproject.toml", "not_checked", 0, "build_file"),
    ]  # fmt: skip
    assert body["counts"] == {"no_linked_test": 1, "linked": 2, "test_file": 0, "not_checked": 2}


def test_links_for_an_unknown_repo_is_404(client: TestClient) -> None:
    response = client.post("/links", headers={"Authorization": f"Bearer {API_TOKEN}"},
                           json={"repo": "acme/none", "changed_files": ["a.py"]})  # fmt: skip
    assert response.status_code == 404
    assert "unknown repo" in response.json()["detail"]


# --- the MCP tool -------------------------------------------------------------------------


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A feature branch changing shop/cart.py and adding shop/db.py; shop/ has a test."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "commit.gpgsign", "false")
    git(tmp_path, "remote", "add", "origin", "git@github.com:acme/shop.git")
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "cart.py").write_text("x = 1\n")
    (tmp_path / "shop" / "test_cart.py").write_text("def test_x():\n    pass\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "checkout", "-q", "-b", "feature")
    (tmp_path / "shop" / "cart.py").write_text("x = 2\n")
    (tmp_path / "shop" / "db.py").write_text("y = 1\n")  # untracked: in the change
    (tmp_path / "lone").mkdir()
    (tmp_path / "lone" / "thing.py").write_text("z = 1\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


def call(tool: str, args: dict[str, Any] | None = None, env: dict[str, str] | None = None) -> Any:
    async def go() -> Any:
        async with Client(build_server(ENV if env is None else env)) as client:
            result = await client.call_tool(tool, args or {})
            assert not result.is_error, result.content
            text = result.content[0].text  # type: ignore[union-attr]
            assert len(text) <= MAX_OUTPUT_CHARS and TOKEN not in text
            return result.structured_content

    return asyncio.run(go())


LINKS = {
    "repo": "acme/shop",
    "files": [
        {"path": "lone/thing.py", "status": "no_linked_test", "linked_tests": 0,
         "reason_code": None, "reason": ""},
        {"path": "shop/db.py", "status": "no_linked_test", "linked_tests": 0,
         "reason_code": None, "reason": ""},
        {"path": "shop/cart.py", "status": "linked", "linked_tests": 3, "reason_code": None,
         "reason": ""},
        {"path": "README.md", "status": "not_checked", "linked_tests": 0,
         "reason_code": "ignorable", "reason": "not code (docs, licenses, lint-only configs)"},
    ],
    "counts": {"no_linked_test": 2, "linked": 1, "test_file": 0, "not_checked": 1},
}  # fmt: skip


@pytest.mark.usefixtures("checkout")
def test_the_tool_lists_files_with_no_linked_test_and_the_test_to_extend(
    api: respx.MockRouter,
) -> None:
    route = api.post(f"{SERVER}/links").respond(json=LINKS)

    out = call("untested_changes")

    body = json.loads(route.calls.last.request.content)
    assert body["repo"] == "acme/shop"
    assert {"shop/cart.py", "shop/db.py", "lone/thing.py"} <= set(body["changed_files"])
    assert out["available"] is True
    assert out["no_linked_test"] == [
        {"file": "lone/thing.py", "nearest_test": None},  # no test nearby
        {"file": "shop/db.py", "nearest_test": "shop/test_cart.py"},  # same folder
    ]
    assert out["linked"] == [{"file": "shop/cart.py", "linked_tests": 3}]
    assert out["not_checked"][0]["reason_code"] == "ignorable"
    assert out["summary"].startswith("2 changed source file(s) with no linked test, 1 with")
    assert "not line coverage" in out["note"]
    assert "nearest_test" in out["next_step"]


@pytest.mark.usefixtures("checkout")
@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ({"status_code": 404, "json": {"detail": "Not Found"}}, "too old for untested_changes"),
        ({"status_code": 404, "json": {"detail": "unknown repo 'acme/shop'"}}, "no test history"),
        ({"status_code": 500, "json": {"detail": "boom"}}, "siftwise unavailable"),
    ],
)
def test_the_tool_fails_open(api: respx.MockRouter, answer: dict[str, Any], message: str) -> None:
    api.post(f"{SERVER}/links").respond(**answer)
    out = call("untested_changes")
    assert out["available"] is False and message in out["message"]


@pytest.mark.usefixtures("checkout")
def test_without_a_token_nothing_is_sent(api: respx.MockRouter) -> None:
    out = call("untested_changes", env={"SIFTWISE_SERVER": SERVER})
    assert out["available"] is False and "SIFTWISE_API_TOKEN" in out["message"]
    assert not api.calls


@pytest.mark.usefixtures("checkout")
def test_the_wording_says_linked_never_covered(api: respx.MockRouter) -> None:
    api.post(f"{SERVER}/links").respond(json=LINKS)
    texts = [UNTESTED_DESCRIPTION, json.dumps(call("untested_changes"))]
    from siftwise.core.schemas import FileLinkOut, LinksResponse

    texts += [
        json.dumps(FileLinkOut.model_json_schema()),
        json.dumps(LinksResponse.model_json_schema()),
    ]
    for text in texts:
        lowered = text.lower()
        assert "covered" not in lowered and "untested code" not in lowered, text[:200]
        assert "coverage" not in lowered or "not line coverage" in lowered
    assert "not line coverage" in UNTESTED_DESCRIPTION.replace("\n", " ")
