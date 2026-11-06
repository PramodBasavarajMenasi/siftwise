"""Fake ``go list -deps -test -json ./...`` output for a small module.

    example.com/m          (root, version.go)
    example.com/m/a        imports nothing
    example.com/m/b        imports a
    example.com/m/c        imports b; its _test package (c_test) also imports d
    example.com/m/d        imports nothing
    example.com/m/notests  imports a (has no tests of its own)

Each package lists its direct imports as go list does: ``Imports`` (non-test files),
``TestImports`` (in-package _test.go files) and ``XTestImports`` (the _test package, which
imports the package itself).

Plus what go list really emits alongside: test variants ("p [p.test]"), generated test
mains ("p.test"), a standard-library package and a third-party module package.
"""

import json
from pathlib import Path
from typing import Any

M = "example.com/m"
MAIN = {"Path": M, "Main": True}


def go_list_packages(root: Path) -> list[dict[str, Any]]:
    def pkg(rel: str, deps: list[str], **extra: Any) -> dict[str, Any]:
        path = f"{M}/{rel}" if rel else M
        return {
            "ImportPath": path,
            "Name": rel.rsplit("/", 1)[-1] or "m",
            "Dir": str(root / rel) if rel else str(root),
            "Module": MAIN,
            "Deps": deps,
            **extra,
        }

    def test_variant(rel: str, deps: list[str]) -> dict[str, Any]:
        base = f"{M}/{rel}" if rel else M
        return {**pkg(rel, deps), "ImportPath": f"{base} [{base}.test]", "ForTest": base}

    def test_main(rel: str, deps: list[str]) -> dict[str, Any]:
        base = f"{M}/{rel}" if rel else M
        return {**pkg(rel, deps), "ImportPath": f"{base}.test", "Name": "main"}

    a, b, c, d = (f"{M}/{x}" for x in "abcd")
    return [
        {"ImportPath": "fmt", "Name": "fmt", "Standard": True, "Dir": "/goroot/src/fmt"},
        {
            "ImportPath": "github.com/x/y",
            "Name": "y",
            "Dir": "/gomod/github.com/x/y",
            "Module": {"Path": "github.com/x/y"},
        },
        pkg("", ["fmt"], Imports=["fmt"]),
        pkg("a", ["fmt"], Imports=["fmt"], TestImports=["testing"]),
        test_variant("a", ["fmt", "testing"]),
        test_main("a", [f"{a} [{a}.test]", "testing"]),
        pkg("b", [a, "fmt"], Imports=[a, "fmt"], TestImports=["testing"]),
        test_variant("b", [a, "fmt", "testing"]),
        pkg(
            "c",
            [a, b],
            Imports=[b],
            TestImports=["testing"],
            XTestImports=[c, d, "github.com/x/y", "testing"],
        ),
        test_variant("c", [a, b, "testing"]),
        {  # external test package c_test, importing d only in tests
            **pkg("c", [a, b, f"{c} [{c}.test]", d, "github.com/x/y"]),
            "ImportPath": f"{c}_test [{c}.test]",
            "Name": "c_test",
            "ForTest": c,
        },
        test_main("c", [f"{c} [{c}.test]", f"{c}_test [{c}.test]"]),
        pkg("d", []),
        pkg("notests", [a], Imports=[a]),
    ]


def go_list_output(root: Path) -> str:
    """As printed by go list: concatenated, indented JSON objects."""
    return "\n".join(json.dumps(p, indent="\t") for p in go_list_packages(root)) + "\n"
