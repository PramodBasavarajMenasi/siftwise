"""The JS/TS import graph (cli/jsgraph.py): the parser, resolution, the graph, and how the
selector, triage and the CLI use it."""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from siftwise.cli import jsgraph
from siftwise.cli import main as cli
from siftwise.cli.jsgraph import JsGraph, build_js_graph, imports, jsonc
from siftwise.core.risk import Order
from siftwise.core.selector import (
    LINK_JS_IMPORT,
    JsImport,
    JsImports,
    KnownTest,
    Mode,
    RepoHistory,
    Strength,
    change_links,
    is_build_file,
    js_import_reason,
    select_tests,
)

FIXTURE = Path(__file__).parent / "fixtures" / "js_monorepo"


def specs(source: str, jsx: bool = True) -> list[str]:
    return list(imports(source, jsx).specifiers)


# --- parser ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import x from './a';", ["./a"]),
        ('import x, { a as b } from "./a"', ["./a"]),
        ("import * as ns from '@s/pkg/sub';", ["@s/pkg/sub"]),
        ("import './side-effect.css';", ["./side-effect.css"]),
        ("import type { T } from './types';", ["./types"]),  # type changes break compiles
        ("import { type T, f } from './mixed';", ["./mixed"]),
        ("export { c } from './c.js';", ["./c.js"]),
        ("export * from './d';", ["./d"]),
        ("export * as e from './e';", ["./e"]),
        ("export type { F } from './f';", ["./f"]),
        ("const m = require('./m');", ["./m"]),
        ("const m = require('./m', 'ignored');", ["./m"]),
        ("const p = require.resolve('./p');", ["./p"]),
        ("const l = () => import('./lazy');", ["./lazy"]),
        ("const l = import(`./lazy`);", ["./lazy"]),  # a template with no ${}
        ("jest.mock('./mocked');", ["./mocked"]),
        ("const a = jest.requireActual('./actual');", ["./actual"]),
        ("jest.doMock('./later', () => ({}));", ["./later"]),
        ("import z = require('./ts-require');", ["./ts-require"]),
        ("import {\n  a,\n  b,\n} from './multi-line';", ["./multi-line"]),
    ],
)
def test_each_import_form(source: str, expected: list[str]) -> None:
    assert specs(source) == expected
    assert imports(source).dynamic is False


@pytest.mark.parametrize(
    "source",
    [
        "// import a from './comment';",
        "/* import a from './block';\n require('./b') */",
        "const s = \"import a from './string'\";",
        "const s = 'require(\\'./escaped\\')';",
        "const t = `import a from './template'`;",
        "const r = /import a from '.\\/regex'/g;",
        "export { local };\nexport const x = 1;",
        "import.meta.url;",
        "obj.require('./member'); x.import('./member');",
    ],
)
def test_comments_strings_templates_and_regexes_hold_no_imports(source: str) -> None:
    assert specs(source) == []


def test_a_template_substitution_is_code() -> None:
    assert specs("const t = `x ${require('./inside')} y`;") == ["./inside"]


def test_division_is_not_a_regex() -> None:
    source = "const half = width / 2; const q = (a) / (b) / c;\nimport x from './after';"
    assert specs(source) == ["./after"]


def test_jsx_text_with_apostrophes_doesnt_hide_later_imports() -> None:
    source = (
        'const el = <div className="x" title=\'y\' {...rest}>Don\'t {"stop"} '
        "{require('./in-jsx')} <b>it's</b><></></div>;\n"
        "export const later = require('./after-jsx');"
    )
    assert specs(source, jsx=True) == ["./in-jsx", "./after-jsx"]


def test_ts_generics_are_not_jsx() -> None:
    source = (
        "const g = useState<Array<number>>([]);\n"
        "function id<T,>(v: T): T { return v; }\n"
        "const f = <T,>(v: T) => v;\n"
        "if (a < b && c > d) {}\n"
        "import x from './after-generics';"
    )
    assert specs(source, jsx=True) == ["./after-generics"]
    # In a .ts file `<T>x` is a type assertion, not JSX.
    assert specs("const n = <number>value;\nimport y from './y';", jsx=False) == ["./y"]


@pytest.mark.parametrize(
    "source",
    [
        "const m = require(name);",
        "const m = require('./plugins/' + name);",
        "const m = await import(`./locale/${lang}.js`);",
        "const m = import(path);",
    ],
)
def test_a_non_literal_require_or_import_is_dynamic(source: str) -> None:
    assert imports(source).dynamic is True


def test_declarations_and_mocks_of_variables_are_not_dynamic() -> None:
    source = "declare function require(id: string): any;\njest.mock(name);\nrequire();"
    assert imports(source).dynamic is False


def test_a_hashbang_and_byte_order_mark_are_skipped() -> None:
    assert specs("#!/usr/bin/env node\nimport a from './a';") == ["./a"]
    assert specs("﻿import a from './a';") == ["./a"]


def test_jsonc_reads_comments_and_trailing_commas() -> None:
    text = '{\n // c\n "a": "http://x", /* b */ "b": [1, 2,],\n}'
    assert jsonc(text) == {"a": "http://x", "b": [1, 2]}


# --- resolution -----------------------------------------------------------------------------


def write(root: Path, files: dict[str, str]) -> Path:
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content, encoding="utf-8")
    return root


def resolver(root: Path) -> jsgraph.Resolver:
    return jsgraph.Resolver(root, frozenset(jsgraph._walk(root)))


def test_relative_paths_extensions_index_files_and_ts_esm(tmp_path: Path) -> None:
    r = resolver(write(tmp_path, {
        "src/a.ts": "", "src/b.tsx": "", "src/c.js": "", "src/lib/index.ts": "",
        "src/d.ts": "", "src/data.json": "{}", "src/x.mts": "",
    }))  # fmt: skip
    assert r.resolve("./a", "src/main.ts") == {"src/a.ts"}
    assert r.resolve("./b", "src/main.ts") == {"src/b.tsx"}
    assert r.resolve("./c", "src/main.ts") == {"src/c.js"}
    assert r.resolve("./lib", "src/main.ts") == {"src/lib/index.ts"}
    assert r.resolve("./d.js", "src/main.ts") == {"src/d.ts"}  # TS ESM names the emitted file
    assert r.resolve("./x.mjs", "src/main.ts") == {"src/x.mts"}
    assert r.resolve("./data.json", "src/main.ts") == {"src/data.json"}  # an asset in the repo
    assert r.resolve("../src/a", "test/t.test.ts") == {"src/a.ts"}
    assert r.resolve("./missing", "src/main.ts") == set()  # generated or broken: no edge
    assert r.resolve("react", "src/main.ts") == set()  # external: node_modules never read
    assert r.resolve("../../outside", "src/main.ts") == set()


def test_tsconfig_paths_and_base_url_through_extends(tmp_path: Path) -> None:
    r = resolver(write(tmp_path, {
        "tsconfig.base.json": '{ // base\n "compilerOptions": { "baseUrl": ".", "paths": '
                              '{ "@app/*": ["src/app/*"], "config": ["src/config.ts"], }, }, }',
        "web/tsconfig.json": '{ "extends": "../tsconfig.base" }',
        "jsconfig.json": '{ "compilerOptions": { "baseUrl": "lib" } }',
        "src/app/cart.ts": "", "src/config.ts": "", "lib/util.js": "",
        "base/tsconfig.json": '{ "extends": "@tsconfig/node20/tsconfig.json" }',
    }))  # fmt: skip
    assert r.resolve("@app/cart", "web/x.ts") == {"src/app/cart.ts"}
    assert r.resolve("config", "web/x.ts") == {"src/config.ts"}
    assert r.resolve("util", "top.js") == {"lib/util.js"}  # jsconfig baseUrl at the root
    assert r.resolve("anything", "base/x.ts") == set()
    assert any("@tsconfig/node20/tsconfig.json" in note for note in r.notes.items)


def test_workspace_packages_resolve_to_their_source(tmp_path: Path) -> None:
    r = resolver(write(tmp_path, {
        "package.json": json.dumps({"workspaces": {"packages": ["packages/*"]}}),
        "pnpm-workspace.yaml": "packages:\n  - 'tools/*'\n  - '!tools/skip'\n",
        "packages/a/package.json": json.dumps({"name": "@s/a", "main": "dist/index.js"}),
        "packages/a/src/index.ts": "", "packages/a/src/sub/deep.ts": "",
        "packages/b/package.json": json.dumps({"name": "b", "exports": "./lib/b.js"}),
        "packages/b/lib/b.js": "",
        "packages/c/package.json": json.dumps({"name": "c", "exports": {"./x": "./src/x.ts"}}),
        "packages/c/src/x.ts": "",
        "tools/t/package.json": json.dumps({"name": "t"}), "tools/t/index.js": "",
        "tools/skip/package.json": json.dumps({"name": "skip"}), "tools/skip/index.js": "",
    }))  # fmt: skip
    assert r.resolve("@s/a", "x.ts") == {"packages/a/src/index.ts", "packages/a/package.json"}
    assert r.resolve("@s/a/sub/deep", "x.ts") == {"packages/a/src/sub/deep.ts",
                                                  "packages/a/package.json"}  # fmt: skip
    assert r.resolve("b", "x.ts") == {"packages/b/lib/b.js", "packages/b/package.json"}
    assert r.resolve("c/x", "x.ts") == {"packages/c/src/x.ts", "packages/c/package.json"}
    assert r.resolve("t", "x.ts") == {"tools/t/index.js", "tools/t/package.json"}
    assert r.resolve("skip", "x.ts") == set()  # excluded with "!": an external package


def test_a_json_jest_config_maps_modules(tmp_path: Path) -> None:
    r = resolver(write(tmp_path, {
        "app/jest.config.json": json.dumps({"rootDir": "..", "moduleNameMapper": {
            "^@/(.*)$": "<rootDir>/src/$1", "\\.(css|svg)$": "identity-obj-proxy",
            "^cfg$": ["<rootDir>/missing", "<rootDir>/src/config"]}}),
        "src/util.ts": "", "src/config.ts": "", "app/styles.css": "",
    }))  # fmt: skip
    assert r.resolve("@/util", "app/a.test.ts") == {"src/util.ts"}
    assert r.resolve("cfg", "app/a.test.ts") == {"src/config.ts"}  # targets tried in order
    assert r.resolve("./styles.css", "app/a.test.ts") == set()  # mapped to a stub under Jest
    assert r.resolve("./styles.css", "other/a.ts") == set()  # outside the Jest project: as usual
    assert r.resolve("../app/styles.css", "other/a.ts") == {"app/styles.css"}


def test_a_javascript_jest_config_cant_be_read_and_says_so(tmp_path: Path) -> None:
    graph = build_js_graph(write(tmp_path, {"jest.config.ts": "export default {}", "a.ts": ""}))
    assert graph.notes == ("jest.config.ts is JavaScript/TypeScript and can't be read without "
                           "Node: its moduleNameMapper isn't applied (tsconfig and relative "
                           "resolution are)",)  # fmt: skip


def test_an_import_of_a_deleted_file_is_still_an_edge(tmp_path: Path) -> None:
    root = write(tmp_path, {"src/a.test.ts": "import { gone } from './gone';"})
    graph = build_js_graph(root, deleted=["src/gone.ts"])
    assert graph.edges["src/a.test.ts"] == {"src/gone.ts"}
    assert graph.affected(["src/gone.ts"]).files == {"src/a.test.ts": ["src/gone.ts"]}


def test_node_modules_and_build_output_are_never_read(tmp_path: Path) -> None:
    root = write(tmp_path, {"node_modules/x/index.js": "require(name)",
                            "dist/a.js": "require(name)", "src/a.ts": ""})  # fmt: skip
    graph = build_js_graph(root)
    assert set(graph.edges) == {"src/a.ts"} and not graph.dynamic_files


# --- the graph ------------------------------------------------------------------------------


def test_the_fixture_monorepo_graph() -> None:
    graph = build_js_graph(FIXTURE)
    edges = {f: sorted(e) for f, e in graph.edges.items() if e}
    assert edges == {
        "packages/core/src/format.ts": ["packages/core/src/money.ts",
                                        "packages/core/src/rates.json"],
        "packages/core/src/index.ts": ["packages/core/src/format.ts",
                                       "packages/core/src/money.ts"],  # './money.js' -> .ts
        "packages/core/src/sum.spec.ts": ["packages/core/src/money.ts"],
        # jest.mock('~/cart') via tsconfig paths; '@web/...' via the JSON moduleNameMapper
        "packages/web/src/__tests__/Price.test.tsx": ["packages/web/src/cart.ts",
                                                      "packages/web/src/components/Price.tsx"],
        "packages/web/src/__tests__/cart.test.ts": ["packages/web/src/cart.ts"],
        # '@shop/core' (exports -> dist, mapped back to src) and '@shared/*' (tsconfig extends)
        "packages/web/src/cart.ts": ["packages/core/package.json", "packages/core/src/index.ts",
                                     "packages/core/src/shared/ids.ts"],
        # './price.css' is mapped to a stub by Jest: no edge
        "packages/web/src/components/Price.tsx": ["packages/core/package.json",
                                                  "packages/core/src/index.ts"],
        "packages/web/src/plugins.test.ts": ["packages/web/src/plugins.ts"],
    }  # fmt: skip
    assert graph.dynamic_files == {"packages/web/src/plugins.ts"}
    assert len(graph.notes) == 1 and graph.notes[0].startswith("tools/jest.config.ts")


def test_affected_is_transitive_with_the_nearest_change_and_its_depth() -> None:
    affected = build_js_graph(FIXTURE).affected(["packages/core/src/money.ts"])
    assert affected.files == {
        "packages/core/src/sum.spec.ts": ["packages/core/src/money.ts"],
        "packages/web/src/__tests__/Price.test.tsx": ["packages/core/src/money.ts"],
        "packages/web/src/__tests__/cart.test.ts": ["packages/core/src/money.ts"],
    }
    nearest = affected.nearest
    assert nearest["packages/core/src/sum.spec.ts"] == jsgraph.Nearest(
        "packages/core/src/money.ts", None, 1)  # fmt: skip
    assert nearest["packages/web/src/__tests__/cart.test.ts"] == jsgraph.Nearest(
        "packages/core/src/money.ts", "packages/web/src/cart.ts", 3)  # fmt: skip
    # A test that reaches a dynamic import is reported apart.
    assert affected.dynamic == {"packages/web/src/plugins.test.ts": "packages/web/src/plugins.ts"}
    js_graph = affected.js_graph()
    assert js_graph is not None
    assert js_graph["nearest"]["packages/web/src/__tests__/cart.test.ts"] == {
        "module": "packages/core/src/money.ts",
        "via": "packages/web/src/cart.ts",
        "depth": 3,
    }


def test_an_asset_and_a_workspace_manifest_select_their_importers() -> None:
    graph = build_js_graph(FIXTURE)
    assert set(graph.affected(["packages/core/src/rates.json"]).files) == {
        "packages/web/src/__tests__/Price.test.tsx",
        "packages/web/src/__tests__/cart.test.ts",
    }
    assert set(graph.affected(["packages/core/package.json"]).files) == {
        "packages/web/src/__tests__/Price.test.tsx",
        "packages/web/src/__tests__/cart.test.ts",
    }


def test_only_jest_test_files_are_affected() -> None:
    graph = JsGraph({"src/a.ts": frozenset({"src/b.ts"}), "src/b.ts": frozenset(),
                     "src/a.test.ts": frozenset({"src/a.ts"}),
                     "src/__tests__/c.ts": frozenset({"src/b.ts"})}, frozenset())  # fmt: skip
    assert set(graph.affected(["src/b.ts"]).files) == {"src/a.test.ts", "src/__tests__/c.ts"}
    assert graph.affected(["src/unknown.ts"]).js_graph() is None


# --- selection, triage ----------------------------------------------------------------------


def jest(path: str, name: str = "works") -> KnownTest:
    return KnownTest(f"{path}::{name}", file_path=path, avg_duration_ms=10, runs=50, failures=0)


HISTORY = RepoHistory(tests={t.test_id: t for t in [
    jest("src/__tests__/cart.test.ts"),  # as jest-junit reports it: from the project's root
    jest("packages/core/src/sum.spec.ts"),  # imports money.ts; no name match
    jest("src/plugins.test.ts"),
    jest("src/unrelated.test.ts"),
]})  # fmt: skip
CART_TEST = "src/__tests__/cart.test.ts::works"
MONEY_TEST = "packages/core/src/sum.spec.ts::works"


def js_select(changed: list[str], **kwargs: Any) -> Any:
    return select_tests(HISTORY, changed, True, **kwargs)


def test_jest_tests_importing_a_changed_file_are_selected_with_the_way_in() -> None:
    affected = {
        "packages/web/src/__tests__/cart.test.ts": ["packages/core/src/money.ts"],
        "packages/core/src/sum.spec.ts": ["packages/core/src/money.ts"],
    }
    nearest = {"packages/web/src/__tests__/cart.test.ts": JsImport(
        "packages/core/src/money.ts", "packages/web/src/cart.ts", 3)}  # fmt: skip
    selection = js_select(["packages/core/src/money.ts"], affected_files=affected,
                          js_imports=JsImports(nearest=nearest))  # fmt: skip
    assert selection.mode is Mode.SELECTIVE  # covered by the graph: no fallback
    reasons = {t.test_id: t.reasons for t in selection.tests}
    assert reasons == {
        # the repo path ends with the project-relative path jest-junit reported
        CART_TEST: ("imports changed module packages/core/src/money.ts (via "
                    "packages/web/src/cart.ts)",),
        MONEY_TEST: ("imports changed module packages/core/src/money.ts",),
    }  # fmt: skip
    assert selection.commands == ("jest packages/core/src/sum.spec.ts src/__tests__/cart.test.ts",)


def test_without_the_graph_a_changed_js_file_with_no_test_by_name_runs_everything() -> None:
    assert js_select(["packages/core/src/money.ts"]).mode is Mode.FULL


def test_dynamic_tests_run_but_never_cover_a_change() -> None:
    dynamic = JsImports(dynamic={"packages/web/src/plugins.test.ts": "packages/web/src/plugins.ts"})
    # Covered by a static import: the dynamic test runs too.
    covered = js_select(
        ["packages/core/src/money.ts"],
        affected_files={"packages/core/src/sum.spec.ts": ["packages/core/src/money.ts"]},
        js_imports=dynamic,
    )
    assert {t.test_id for t in covered.tests} == {MONEY_TEST, "src/plugins.test.ts::works"}
    assert ("reaches packages/web/src/plugins.ts, whose dynamic import siftwise can't resolve: "
            "it may load changed code") in covered.tests[1].reasons  # fmt: skip
    # Reached only maybe, by a dynamic import: the fallback stays.
    assert js_select(["packages/core/src/money.ts"], js_imports=dynamic).mode is Mode.FULL


def test_notes_go_into_the_reason() -> None:
    reason = js_import_reason(["src/a.ts", "src/b.ts"], JsImport("src/b.ts", "src/c.ts", 2),
                              ["jest.config.ts can't be read"])  # fmt: skip
    assert reason == ("imports changed module src/b.ts (via src/c.ts) (+1 more); jest.config.ts "
                      "can't be read")  # fmt: skip


def test_the_risk_order_counts_a_js_import_as_indirect() -> None:
    """Import depth isn't a link tier yet (for any language): a direct import is indirect."""
    selection = js_select(
        ["packages/core/src/money.ts"],
        affected_files={"packages/core/src/sum.spec.ts": ["packages/core/src/money.ts"]},
    )
    [test] = selection.tests
    assert test.rank_reason.startswith("indirect link")


def test_a_deleted_jest_test_file_isnt_selected() -> None:
    """Before, its path still mapped and `jest <deleted file>` failed with "No tests found"."""
    selection = js_select(
        ["packages/core/src/money.ts", "packages/core/src/sum.spec.ts"],
        affected_files={"packages/core/src/sum.spec.ts": ["packages/core/src/money.ts"]},
        deleted_files=["packages/core/src/sum.spec.ts"],
    )
    assert MONEY_TEST not in {t.test_id for t in selection.tests}


def test_triage_links_what_selection_selects() -> None:
    affected = {"packages/web/src/__tests__/cart.test.ts": ["packages/core/src/money.ts"]}
    links = change_links(HISTORY.tests.values(), ["packages/core/src/money.ts"],
                         affected_files=affected)  # fmt: skip
    [link] = links[CART_TEST]
    assert (link.signal, link.changed, link.strength) == (
        LINK_JS_IMPORT, "packages/core/src/money.ts", Strength.MEDIUM)  # fmt: skip
    selected = {t.test_id for t in js_select(["packages/core/src/money.ts"],
                                             affected_files=affected).tests}  # fmt: skip
    assert selected == {test_id for test_id, found in links.items() if found}


@pytest.mark.parametrize(
    "path",
    ["jsconfig.json", "web/tsconfig.app.json", ".swcrc", "lerna.json", ".babelrc.json",
     "swc.config.js", "jest.config.ts", "pnpm-workspace.yaml", "package-lock.json"],
)  # fmt: skip
def test_js_resolution_and_transpiler_configs_force_the_full_suite(path: str) -> None:
    assert is_build_file(path)
    assert js_select([path]).mode is Mode.FULL


# --- the CLI --------------------------------------------------------------------------------


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def js_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(FIXTURE, root)
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    git(root, "checkout", "-b", "change")
    (root / "packages/core/src/money.ts").write_text("export const add = 1;\n", encoding="utf-8")
    git(root, "commit", "-am", "change money")
    monkeypatch.chdir(root)
    return root


@pytest.mark.usefixtures("js_repo")
def test_the_cli_sends_js_edges_and_details() -> None:
    body = cli.selection_request("acme/shop", "main", "HEAD")
    assert body["affected_files"] == {
        "packages/core/src/sum.spec.ts": ["packages/core/src/money.ts"],
        "packages/web/src/__tests__/Price.test.tsx": ["packages/core/src/money.ts"],
        "packages/web/src/__tests__/cart.test.ts": ["packages/core/src/money.ts"],
    }
    js_graph = body["js_graph"]
    assert js_graph["nearest"]["packages/web/src/__tests__/cart.test.ts"]["depth"] == 3
    assert body["import_depths"]["affected_files"] == {
        "packages/core/src/sum.spec.ts": 1,
        "packages/web/src/__tests__/Price.test.tsx": 3,
        "packages/web/src/__tests__/cart.test.ts": 3,
    }
    assert js_graph["dynamic"] == {
        "packages/web/src/plugins.test.ts": "packages/web/src/plugins.ts"
    }
    assert js_graph["notes"][0].startswith("tools/jest.config.ts")
    # Recorded runs (triage) get the edges, never js_graph: an older server's schema forbids it.
    change = cli.local_change("main", "HEAD")
    assert change.graph_complete and "js_graph" not in change.fields
    assert (change.graph() or {})["affected_files"] == body["affected_files"]


@pytest.mark.usefixtures("js_repo")
def test_no_js_imports_sends_no_graph_and_says_it_is_incomplete() -> None:
    body = cli.selection_request("acme/shop", "main", "HEAD", js_imports=False)
    assert "js_graph" not in body and not body.get("affected_files")
    assert not cli.local_change("main", "HEAD", js_imports=False).graph_complete


def test_a_python_only_change_builds_no_js_graph(tmp_path: Path) -> None:
    assert not cli.wants_js_graph(tmp_path, ["src/a.py", "README.md"])
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert not cli.wants_js_graph(tmp_path, ["src/a.py", "docs/x.md"])
    assert cli.wants_js_graph(tmp_path, ["src/data.json"])  # JS may import it
    assert cli.wants_js_graph(tmp_path.parent, ["web/a.tsx"])


SERVER = "http://siftwise.test"
# What a server from before the JS graph answers (its request schema forbids unknown fields).
EXTRA_JS_GRAPH = {"type": "extra_forbidden", "loc": ["body", "js_graph"], "input": {}}
OLD_SERVER_JS_422 = {"detail": [{**EXTRA_JS_GRAPH, "msg": "Extra inputs are not permitted"}]}


def test_an_older_server_gets_the_request_again_without_js_graph() -> None:
    body = {"repo": "acme/shop", "affected_files": {"a.test.ts": ["a.ts"]}, "js_graph": {}}
    with respx.mock(assert_all_called=True) as api:
        route = api.post(f"{SERVER}/select")
        route.side_effect = [httpx.Response(422, json=OLD_SERVER_JS_422),
                             httpx.Response(200, json={"mode": "selective"})]  # fmt: skip
        assert cli.post_select(SERVER, "t", body) == {"mode": "selective"}
        resent = json.loads(route.calls.last.request.content)
    assert "js_graph" not in resent and resent["affected_files"] == body["affected_files"]


def test_other_errors_are_not_retried() -> None:
    with respx.mock() as api:
        route = api.post(f"{SERVER}/select").respond(422, json={"detail": "bad repo"})
        with pytest.raises(cli.SiftwiseError):
            cli.post_select(SERVER, "t", {"repo": "x", "js_graph": {}})
        assert route.call_count == 1


# --- new Jest test files --------------------------------------------------------------------


def test_a_new_test_file_alone_runs_by_its_path() -> None:
    selection = js_select(["packages/core/src/new.test.ts"])
    assert selection.mode is Mode.SELECTIVE
    assert selection.commands == ("jest packages/core/src/new.test.ts",)
    assert selection.reason == (
        "1 changed test file(s) run whole, no known tests; new test file: "
        "packages/core/src/new.test.ts"
    )


def test_a_new_test_file_runs_first_next_to_a_changed_source_file() -> None:
    dynamic = {"packages/core/src/zz.test.ts": "packages/core/src/plugins.ts"}
    selection = js_select(
        ["packages/core/src/money.ts", "packages/core/src/zz.test.ts"],
        affected_files={"packages/core/src/sum.spec.ts": ["packages/core/src/money.ts"]},
        js_imports=JsImports(dynamic=dynamic),  # it reaches a dynamic import: it runs anyway
        order=Order.RISK,
    )
    assert selection.mode is Mode.SELECTIVE
    # The new file has no ranked test: it is the change itself, so it comes first.
    assert selection.commands == (
        "jest packages/core/src/zz.test.ts packages/core/src/sum.spec.ts",
    )
    assert [t.test_id for t in selection.tests] == [MONEY_TEST]
    assert selection.reason.endswith("; new test file: packages/core/src/zz.test.ts")


def test_a_new_file_in_tests_runs_from_its_jest_project() -> None:
    # Known tests are reported from the web package's root ("src/__tests__/cart.test.ts").
    selection = js_select(["packages/web/src/__tests__/checkout.ts"])
    assert selection.mode is Mode.SELECTIVE
    assert selection.commands == ("jest src/__tests__/checkout.ts",)


def test_a_new_file_that_isnt_a_test_keeps_the_fallback() -> None:
    assert js_select(["packages/web/src/checkout.ts"]).mode is Mode.FULL


def test_a_new_test_file_with_no_way_to_name_it_keeps_the_fallback() -> None:
    # No known Jest test lives in, below or above tools/: the path Jest wants can't be told.
    assert js_select(["tools/x.test.ts"]).mode is Mode.FULL
    # A deeper new directory under a known one: repo-relative, as the known tests' paths are.
    selection = js_select(["packages/core/src/feature/x.test.ts"])
    assert selection.commands == ("jest packages/core/src/feature/x.test.ts",)
    # No known Jest tests at all: no runner to trust.
    empty = select_tests(RepoHistory(tests={"t::a": KnownTest("tests.test_a::t")}),
                         ["src/a.test.ts"], True)  # fmt: skip
    assert empty.mode is Mode.FULL


def test_a_changed_known_test_file_reported_from_its_project_root_runs_its_tests() -> None:
    path = "packages/web/src/__tests__/cart.test.ts"
    selection = js_select([path])
    assert [(t.test_id, t.reasons) for t in selection.tests] == [
        (CART_TEST, (f"test file changed: {path}",))
    ]
    links = change_links(HISTORY.tests.values(), [path])
    assert [link.signal for link in links[CART_TEST]] == ["test file changed"]
