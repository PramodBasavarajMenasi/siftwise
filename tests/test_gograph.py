import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from siftwise.cli.gograph import (
    GoGraph,
    build_graph,
    find_go_modules,
    merge_graphs,
    parse_go_list,
    readonly_goflags,
    run_go_list,
)
from tests.gofixture import M, go_list_output, go_list_packages

A, B, C, D = (f"{M}/{x}" for x in "abcd")


@pytest.fixture
def graph(tmp_path: Path) -> GoGraph:
    return build_graph(go_list_packages(tmp_path), tmp_path)


def test_parse_concatenated_json_objects(tmp_path: Path) -> None:
    packages = parse_go_list(go_list_output(tmp_path))
    assert [p["ImportPath"] for p in packages][:3] == ["fmt", "github.com/x/y", M]
    assert parse_go_list("  \n") == []
    with pytest.raises(ValueError):
        parse_go_list('{"ImportPath": "a"} {broken')


def test_graph_maps_dirs_to_main_module_packages_only(graph: GoGraph) -> None:
    assert graph.module == M
    # No std/third-party packages, no test variants or generated test mains.
    assert dict(graph.dirs) == {
        "": M,
        "a": A,
        "b": B,
        "c": C,
        "d": D,
        "notests": f"{M}/notests",
    }


def test_graph_deps_include_test_imports(graph: GoGraph) -> None:
    assert graph.deps[A] == {A}
    assert graph.deps[B] == {A, B}
    assert graph.deps[C] == {A, B, C, D}  # d comes from the external c_test package


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        (["a/a.go"], {A: [A], B: [A], C: [A], f"{M}/notests": [A]}),
        (["d/d.go"], {C: [D], D: [D]}),  # c's tests import d
        (["b/b.go", "d/d.go"], {B: [B], C: [B, D], D: [D]}),
        (["version.go"], {M: [M]}),
        (["a/a_test.go"], {}),  # test files can't be imported by other packages
        (["vendor/x/x.go", "README.md", "a/data.json"], {}),
    ],
    ids=["a", "test-only-import", "two-packages", "root", "test-file", "not-go-packages"],
)
def test_affected_packages(
    graph: GoGraph, changed: list[str], expected: dict[str, list[str]]
) -> None:
    assert graph.affected(changed) == expected


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        # b imports a; c only through b (c -> b -> a); notests imports a.
        (["a/a.go"], {A: 1, B: 1, C: 2, f"{M}/notests": 1}),
        (["d/d.go"], {C: 1, D: 1}),  # c's _test package imports d itself
        (["b/b.go", "d/d.go"], {B: 1, C: 1, D: 1}),
        (["version.go"], {M: 1}),
        (["a/a_test.go"], {}),
    ],
    ids=["a", "test-only-import", "two-packages", "root", "test-file"],
)
def test_import_depths(graph: GoGraph, changed: list[str], expected: dict[str, int]) -> None:
    assert graph.depths(changed) == expected
    assert set(graph.depths(changed)) == set(graph.affected(changed))


def test_direct_edges_leave_out_tests_variants_and_other_modules(graph: GoGraph) -> None:
    assert graph.imports[C] == {B}  # not fmt
    assert graph.test_imports[C] == {D}  # XTestImports without c itself or github.com/x/y
    assert graph.test_imports[A] == frozenset()
    assert set(graph.imports) == set(graph.dirs.values())


def test_json_round_trip(graph: GoGraph) -> None:
    assert GoGraph.from_json(graph.to_json()) == graph
    # A graph cached before the direct edges were kept: no depths, the rest as before.
    old = {key: value for key, value in graph.to_json().items()
           if key not in ("imports", "test_imports")}  # fmt: skip
    assert GoGraph.from_json(old).affected(["a/a.go"]) == graph.affected(["a/a.go"])
    assert GoGraph.from_json(old).depths(["a/a.go"]) == {}


def test_run_go_list_returns_none_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Missing binary, non-zero exit (python can't open a file named "list") and timeout.
    assert run_go_list(tmp_path, go=str(tmp_path / "no-such-go")) is None
    assert run_go_list(tmp_path, go=sys.executable) is None

    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("go", 1)

    monkeypatch.setattr("siftwise.cli.gograph.subprocess.run", timeout)
    assert run_go_list(tmp_path) is None


def test_run_go_list_parses_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    flags: list[object] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        if args[1] == "env":  # `go env -w GOFLAGS=-mod=mod`: go.mod may be rewritten
            return subprocess.CompletedProcess(args, 0, b"-mod=mod -tags=e2e\n", b"")
        flags.append(kwargs["env"]["GOFLAGS"])  # type: ignore[index]
        return subprocess.CompletedProcess(args, 0, go_list_output(tmp_path).encode(), b"")

    monkeypatch.setattr("siftwise.cli.gograph.subprocess.run", fake_run)
    packages = run_go_list(tmp_path)

    assert calls == [["go", "env", "GOFLAGS"], ["go", "list", "-deps", "-test", "-json", "./..."]]
    assert flags == ["-mod=readonly -tags=e2e"]  # never edits go.mod; other flags kept
    assert packages is not None and len(packages) == len(go_list_packages(tmp_path))


def test_run_go_list_rejects_garbage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "siftwise.cli.gograph.subprocess.run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, b"{not json", b""),
    )
    assert run_go_list(tmp_path) is None


# --- several modules (go.work, nested go.mod) ----------------------------------------------


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_find_go_modules(tmp_path: Path) -> None:
    write(tmp_path / "go.mod", "module example.com/root\n")
    write(tmp_path / "tools" / "go.mod", "module example.com/tools // a comment\n")
    write(tmp_path / "a" / "b" / "go.mod", 'module "example.com/ab"\n')
    for ignored in ("vendor/x", "x/testdata", "_old", ".cache/m", "node_modules/m"):
        write(tmp_path / ignored / "go.mod", "module example.com/ignored\n")
    write(tmp_path / "go.work", "go 1.21\n\nuse (\n\t.\n\t./tools // tools\n\t../outside\n)\n"
          "use ./_old\n")  # fmt: skip

    assert find_go_modules(tmp_path) == {
        "": "example.com/root",
        "_old": "example.com/ignored",  # named by go.work: Go uses it
        "a/b": "example.com/ab",
        "tools": "example.com/tools",
    }
    assert find_go_modules(tmp_path / "a" / "b") == {"": "example.com/ab"}


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ("", ""),
        ("-mod=mod", "-mod=readonly"),
        ("--mod=mod -tags=e2e", "-mod=readonly -tags=e2e"),
        ("-mod=vendor", "-mod=vendor"),  # an explicit choice, and it writes nothing
        ("-mod=readonly -modcacherw", "-mod=readonly -modcacherw"),
    ],
)
def test_go_list_never_writes_go_mod(flags: str, expected: str) -> None:
    assert readonly_goflags(flags) == expected


def test_packages_of_other_modules_in_the_checkout_link_across_modules(tmp_path: Path) -> None:
    """A nested module's go list (run in its directory) shows the root module's packages it
    imports through `replace ../` as non-main packages in the checkout: they link."""
    lib = {"ImportPath": "example.com/root/a", "Dir": str(tmp_path / "a"),
           "Module": {"Path": "example.com/root", "Version": "v0.0.0",
                      "Replace": {"Path": "../", "Dir": str(tmp_path)}}}  # fmt: skip
    downloaded = {"ImportPath": "github.com/x/y", "Dir": str(tmp_path / ".cache" / "y"),
                  "Module": {"Path": "github.com/x/y", "Version": "v1.0.0"}}  # fmt: skip
    vendored = {"ImportPath": "github.com/v/w", "Dir": str(tmp_path / "tools" / "vendor" / "w"),
                "Module": {"Path": "github.com/v/w", "Version": "v1.0.0",
                           "Replace": {"Path": "./vendor/w"}}}  # fmt: skip
    tools_main = {"Path": "example.com/tools", "Main": True, "Dir": str(tmp_path / "tools")}
    x = {
        "ImportPath": "example.com/tools/x",
        "Dir": str(tmp_path / "tools" / "x"),
        "Module": tools_main,
        "Deps": ["example.com/root/a", "github.com/x/y", "github.com/v/w"],
    }
    tools = build_graph([lib, downloaded, vendored, x], tmp_path)
    root = GoGraph("example.com/root", {"a": "example.com/root/a"},
                   {"example.com/root/a": frozenset({"example.com/root/a"})})  # fmt: skip

    graph = merge_graphs([root, tools], "example.com/root")

    assert dict(graph.dirs) == {"a": "example.com/root/a", "tools/x": "example.com/tools/x"}
    assert graph.affected(["a/a.go"]) == {
        "example.com/root/a": ["example.com/root/a"],
        "example.com/tools/x": ["example.com/root/a"],
    }
    assert graph.module == "example.com/root"


GO = shutil.which("go")


X_GO = 'package x\n\nimport "example.com/root/a"\n\nfunc X() int { return a.A() }\n'
X_TEST = 'package x\n\nimport "testing"\n\nfunc TestX(t *testing.T) { if X() != 1 { t.Fail() } }\n'
A_TEST = 'package a\n\nimport "testing"\n\nfunc TestA(t *testing.T) {}\n'


@pytest.mark.skipif(os.environ.get("SIFTWISE_REAL_TOOL_TESTS") != "1", reason="real Go")
def test_nested_modules_and_workspaces_for_real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """go list per module, with real Go: a nested module importing the root through a
    `replace`, with and without go.work; `go test -C` runs the nested package; and with
    GOFLAGS=-mod=mod, go list leaves a go.mod it would have edited alone (#52)."""
    if GO is None:
        pytest.fail("SIFTWISE_REAL_TOOL_TESTS=1, but go isn't on PATH")
    for name, value in {"GOFLAGS": "-mod=mod", "GOPROXY": "off", "GOTOOLCHAIN": "local"}.items():
        monkeypatch.setenv(name, value)
    write(tmp_path / "go.mod", "module example.com/root\n\ngo 1.21\n")
    write(tmp_path / "a" / "a.go", "package a\n\nfunc A() int { return 1 }\n")
    write(tmp_path / "a" / "a_test.go", A_TEST)
    write(tmp_path / "tools" / "x" / "x.go", X_GO)
    write(tmp_path / "tools" / "x" / "x_test.go", X_TEST)
    # No `require` for the replaced module: `go list` with -mod=mod would add one.
    missing = "module example.com/tools\n\ngo 1.21\n\nreplace example.com/root => ../\n"
    write(tmp_path / "tools" / "go.mod", missing)
    run_go_list(tmp_path / "tools")  # may fail (the fallbacks apply): it must not write
    assert (tmp_path / "tools" / "go.mod").read_text() == missing

    tools_mod = missing.replace("\nreplace", "\nrequire example.com/root v0.0.0\n\nreplace")
    write(tmp_path / "tools" / "go.mod", tools_mod)
    for work in (False, True):
        if work:
            write(tmp_path / "go.work", "go 1.21\n\nuse (\n\t.\n\t./tools\n)\n")
        modules = find_go_modules(tmp_path)
        graphs = []
        for directory in modules:
            packages = run_go_list(tmp_path / directory if directory else tmp_path)
            assert packages is not None, (work, directory)
            graphs.append(build_graph(packages, tmp_path))
        graph = merge_graphs(graphs, modules.get(""))

        assert modules == {"": "example.com/root", "tools": "example.com/tools"}
        assert graph.affected(["a/a.go"]) == {
            "example.com/root/a": ["example.com/root/a"],
            "example.com/tools/x": ["example.com/root/a"],
        }, work
        # x imports a (in the other module) itself; a is the changed package.
        assert graph.depths(["a/a.go"]) == {"example.com/root/a": 1, "example.com/tools/x": 1}
        assert (tmp_path / "tools" / "go.mod").read_text() == tools_mod
    monkeypatch.setenv("GOFLAGS", "")
    proc = subprocess.run([GO, "test", "-C", "tools", "./x"], cwd=tmp_path,
                          capture_output=True, text=True, check=False)  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(os.environ.get("SIFTWISE_REAL_TOOL_TESTS") != "1", reason="real Go")
def test_import_depths_for_real(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Depths from real go list output: b imports a, c imports b, d's in-package test imports
    a, e's _test package imports a, f's _test package imports c."""
    if GO is None:
        pytest.fail("SIFTWISE_REAL_TOOL_TESTS=1, but go isn't on PATH")
    for name, value in {"GOFLAGS": "", "GOPROXY": "off", "GOTOOLCHAIN": "local"}.items():
        monkeypatch.setenv(name, value)
    m = "example.com/depth"
    files = {
        "go.mod": f"""module {m}

go 1.21
""",
        "a/a.go": """package a

func A() int { return 1 }
""",
        "b/b.go": f"""package b

import "{m}/a"

func B() int {{ return a.A() }}
""",
        "b/b_test.go": """package b

import "testing"

func TestB(t *testing.T) {}
""",
        "c/c.go": f"""package c

import "{m}/b"

func C() int {{ return b.B() }}
""",
        "c/c_test.go": """package c

import "testing"

func TestC(t *testing.T) {}
""",
        "d/d.go": "package d",
        "d/d_test.go": f"""package d

import (
    "testing"
    "{m}/a"
)

func TestD(t *testing.T) {{ _ = a.A() }}
""",
        "e/e.go": "package e",
        "e/e_test.go": f"""package e_test

import (
    "testing"
    "{m}/a"
)

func TestE(t *testing.T) {{ _ = a.A() }}
""",
        "f/f.go": "package f",
        "f/f_test.go": f"""package f_test

import (
    "testing"
    "{m}/c"
)

func TestF(t *testing.T) {{ _ = c.C() }}
""",
    }
    for name, text in files.items():
        write(tmp_path / name, text)

    packages = run_go_list(tmp_path)
    assert packages is not None
    graph = build_graph(packages, tmp_path)

    assert graph.affected(["a/a.go"]).keys() == {f"{m}/{p}" for p in "abcdef"}
    assert graph.depths(["a/a.go"]) == {
        f"{m}/a": 1, f"{m}/b": 1, f"{m}/c": 2, f"{m}/d": 1, f"{m}/e": 1, f"{m}/f": 3}  # fmt: skip
