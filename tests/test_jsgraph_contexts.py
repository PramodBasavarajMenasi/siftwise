"""Bounded non-literal JS imports (#51): templates and concatenations with a static
directory, ``import.meta.glob`` and ``require.context`` become edges to the files they can
load; anything siftwise can't bound stays dynamic."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from siftwise.cli import jsgraph
from siftwise.cli import main as cli
from siftwise.cli.jsgraph import MAX_CONTEXT_FILES, build_js_graph
from siftwise.core.selector import JsImports, KnownTest, Mode, RepoHistory, select_tests

APP = {
    "package.json": json.dumps({"devDependencies": {"jest": "29.7.0"}}),
    "tsconfig.json": json.dumps({"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}),
    "src/locales/en.json": "{}",
    "src/locales/fr.json": "{}",
    "src/locales/sub/de.json": "{}",
    "src/locales/README.md": "not json",
    "src/plugins/a.ts": "export const a = 1;",
    "src/plugins/b/index.ts": "export const b = 1;",
    "src/pages/Home.tsx": "export default 1;",
    "src/pages/About.tsx": "export default 2;",
    "src/pages/util.ts": "export const u = 1;",
    "src/pages/deep/Deep.tsx": "export default 3;",
    "src/icons/a.svg": "<svg/>",
    "src/icons/b.png": "png",
    "src/icons/sub/c.svg": "<svg/>",
    "src/other.ts": "export const other = 1;",
}


def write(root: Path, files: dict[str, str]) -> Path:
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content, encoding="utf-8")
    return root


def edges_of(root: Path, source: str, at: str = "src/loader.ts") -> set[str] | None:
    """The importer's edges, or None if it's dynamic."""
    write(root, {**APP, at: source})
    graph = build_js_graph(root)
    return None if at in graph.dynamic_files else set(graph.edges[at])


LOCALES = {"src/locales/en.json", "src/locales/fr.json", "src/locales/sub/de.json"}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # A template with a static directory and suffix: every .json below it, at any depth.
        ("export const t = (l: string) => import(`./locales/${l}.json`);", LOCALES),
        # Concatenation, no suffix: every file below the directory (extensions, index files).
        ("export const p = (n: string) => require('./plugins/' + n);",
         {"src/plugins/a.ts", "src/plugins/b/index.ts"}),
        # A parent directory, and a static start within the name.
        ("export const r = (p: string) => import(`../pages/${p}`);",
         {"src/pages/Home.tsx", "src/pages/About.tsx", "src/pages/util.ts",
          "src/pages/deep/Deep.tsx"}),
        ("export const h = (p: string) => import(`./pages/Ho${p}`);", {"src/pages/Home.tsx"}),
        # TS ESM: a ".js" suffix names the .ts source (`${n}` may be "b/index").
        ("export const j = (n: string) => import(`./plugins/${n}.js`);",
         {"src/plugins/a.ts", "src/plugins/b/index.ts"}),
        # tsconfig paths: @/* -> src/*.
        ("export const a = (l: string) => import(`@/locales/${l}.json`);", LOCALES),
        # require(require.resolve(X)) loads what X names.
        ("export const q = (n: string) => require(require.resolve(`./plugins/${n}`));",
         {"src/plugins/a.ts", "src/plugins/b/index.ts"}),
    ],
)  # fmt: skip
def test_bounded_imports_are_edges_to_every_file_they_can_load(
    tmp_path: Path, source: str, expected: set[str]
) -> None:
    at = "src/app/router.ts" if "../pages" in source else "src/loader.ts"
    assert edges_of(tmp_path, source, at) == expected


def test_vite_globs_and_webpack_contexts(tmp_path: Path) -> None:
    assert edges_of(tmp_path, "const pages = import.meta.glob('./pages/*.tsx');") == {
        "src/pages/Home.tsx", "src/pages/About.tsx"}  # fmt: skip
    assert edges_of(tmp_path, "const all = import.meta.glob(['./pages/**/*.tsx', '!./x']);") == {
        "src/pages/Home.tsx", "src/pages/About.tsx", "src/pages/deep/Deep.tsx"}  # fmt: skip
    assert edges_of(tmp_path, r"const i = require.context('./icons', false, /\.svg$/);") == {
        "src/icons/a.svg"}  # fmt: skip
    assert edges_of(tmp_path, r"const i = require.context('./icons', true, /\.SVG$/i);") == {
        "src/icons/a.svg", "src/icons/sub/c.svg"}  # fmt: skip
    # No regex (webpack's default: every file), and a regex Python can't read: every file,
    # never fewer.
    every = {"src/icons/a.svg", "src/icons/b.png", "src/icons/sub/c.svg"}
    assert edges_of(tmp_path, "const i = require.context('./icons');") == every
    assert edges_of(tmp_path, "const i = require.context('./icons', true, /(?<n>x)/);") == every


@pytest.mark.parametrize(
    "source",
    [
        "require(`./${name}`);",  # the directory is just ./
        "require('../' + name);",  # just ../
        "require(`./${a}/${b}.json`);",
        "require('lodash/' + name);",  # a bare package (node_modules is never read)
        "require(`./missing/${name}`);",  # not a directory of the checkout
        "require(`../../../outside/${name}`);",  # outside the checkout
        "require(`./locales/${name}.yaml`);",  # no file matches: something's off, don't guess
        "require(`#/locales/${name}`);",  # an alias siftwise can't resolve
        "import.meta.glob('/src/pages/*.tsx');",  # root-relative: Vite's root isn't known
        "import.meta.glob('./pages/{a,b}.tsx');",  # braces: not read
        "require.context('@/icons');",  # a webpack alias
        "require(name);",
    ],
)
def test_what_cant_be_bounded_stays_dynamic(tmp_path: Path, source: str) -> None:
    assert edges_of(tmp_path, source) is None


def test_a_bound_over_the_file_limit_stays_dynamic(tmp_path: Path) -> None:
    many = {f"src/many/m{i}.json": "{}" for i in range(MAX_CONTEXT_FILES + 1)}
    write(tmp_path, many)
    assert edges_of(tmp_path, "require(`./many/${n}.json`);") is None
    (tmp_path / "src/many/m0.json").unlink()  # exactly the limit: bounded
    assert len(edges_of(tmp_path, "require(`./many/${n}.json`);") or ()) == MAX_CONTEXT_FILES


def test_a_jest_mapper_on_the_prefix_keeps_it_dynamic(tmp_path: Path) -> None:
    jest = {"jest": {"moduleNameMapper": {"^@/(.*)$": "<rootDir>/src/$1"}}}
    write(tmp_path, {**APP, "package.json": json.dumps(jest)})
    write(tmp_path, {"src/loader.ts": "import(`@/locales/${l}.json`);"})
    assert "src/loader.ts" in build_js_graph(tmp_path).dynamic_files


def test_deleted_files_count_and_contexts_compose_with_literals(tmp_path: Path) -> None:
    write(
        tmp_path,
        {**APP, "src/loader.ts": "import x from './other';\nimport(`./locales/${l}.json`);"},
    )
    (tmp_path / "src/locales/fr.json").unlink()
    graph = build_js_graph(tmp_path, deleted=["src/locales/fr.json"])
    assert graph.edges["src/loader.ts"] == LOCALES | {"src/other.ts"}
    assert graph.dynamic_files == frozenset()


def test_one_unbounded_import_still_makes_the_file_dynamic(tmp_path: Path) -> None:
    write(tmp_path, {**APP, "src/loader.ts": "import(`./locales/${l}.json`);\nrequire(name);"})
    graph = build_js_graph(tmp_path)
    assert "src/loader.ts" in graph.dynamic_files
    assert graph.edges["src/loader.ts"] == LOCALES  # the bounded part is an edge too


def test_the_resolver_finds_every_directory(tmp_path: Path) -> None:
    write(tmp_path, {"a/b/c/d.ts": ""})
    resolver = jsgraph.Resolver(tmp_path, frozenset(jsgraph._walk(tmp_path)))
    assert resolver._all_dirs == {"a", "a/b", "a/b/c"}
    assert resolver._under("a/b") == ["a/b/c/d.ts"]
    assert resolver._under("a/b/c/d.ts") == []


# --- the CLI and the selector: a change outside the bound no longer selects the test -------


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    write(
        tmp_path,
        {
            **APP,
            "src/i18n.ts": "export const t = (l: string) => import(`./locales/${l}.json`);",
            "src/i18n.test.ts": "import { t } from './i18n';\ntest('t', () => {});",
            "src/other.test.ts": "import { other } from './other';\ntest('o', () => {});",
        },
    )
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def change(root: Path, path: str) -> dict[str, Any]:
    (root / path).write_text((root / path).read_text() + "\n", encoding="utf-8")
    git(root, "commit", "-qam", f"change {path}")
    body = cli.selection_request("acme/web", "HEAD~1", "HEAD")
    git(root, "reset", "-q", "--hard", "HEAD~1")
    return body


HISTORY = RepoHistory({t.test_id: t for t in (
    KnownTest("src/i18n.test.ts::t", "src/i18n.test.ts"),
    KnownTest("src/other.test.ts::o", "src/other.test.ts"),
)})  # fmt: skip


def selected(body: dict[str, Any]) -> set[str]:
    js = body.get("js_graph") or {}
    selection = select_tests(HISTORY, body["changed_files"], True,
                             affected_files=body.get("affected_files"),
                             js_imports=JsImports(dynamic=dict(js.get("dynamic", {}))))  # fmt: skip
    assert selection.mode is Mode.SELECTIVE, selection.reason
    return {t.test_id for t in selection.tests}


def test_a_change_inside_the_bound_selects_the_test_and_one_outside_no_longer_does(
    repo: Path,
) -> None:
    inside = change(repo, "src/locales/en.json")
    assert inside["affected_files"] == {"src/i18n.test.ts": ["src/locales/en.json"]}
    assert selected(inside) == {"src/i18n.test.ts::t"}

    outside = change(repo, "src/other.ts")
    assert "src/i18n.test.ts" not in (outside.get("js_graph") or {}).get("dynamic", {})
    # Before #51, i18n.ts was dynamic: its test ran for this change too.
    assert selected(outside) == {"src/other.test.ts::o"}
