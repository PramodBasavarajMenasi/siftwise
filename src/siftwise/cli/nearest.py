"""The nearest existing test file to extend for a changed source file (MCP ``untested_changes``).

Pure: it gets the checkout's file list. The order, first that finds a file:

1. a test file named by the language's convention, in the source file's own folder (Java: the
   same package under ``src/test/java``; Jest also ``__tests__/``):
   Python ``test_x.py`` / ``x_test.py``, Go ``x_test.go``, JS/TS ``x.test.*`` / ``x.spec.*``,
   Java ``XTest.java`` / ``XTests.java``;
2. any other test file in that folder or package (Go: the package's tests);
3. a convention-named test file anywhere else, the one sharing the longest leading path
   with the source file (``src/shop/cart.py`` -> ``tests/shop/test_cart.py``).

Test files are recognised by the selector's own rules. Kotlin isn't analysed by the selector
yet (issue #102), so no suggestion is made for ``.kt`` files.
"""

import posixpath
from collections.abc import Callable, Iterable

from siftwise.core.selector import (
    JS_EXTENSIONS,
    is_jest_test_file,
    is_python_test_file,
    java_class_of,
)


def _is_java_test(path: str) -> bool:
    located = java_class_of(path)
    return located is not None and located[2]


def _rule(path: str) -> tuple[str, list[str], Callable[[str], bool]] | None:
    """(the folder its tests live in, convention names, is a test file) for ``path``."""
    folder, name = posixpath.split(path)
    stem, ext = posixpath.splitext(name)
    if ext == ".py":
        return folder, [f"test_{stem}.py", f"{stem}_test.py"], is_python_test_file
    if ext == ".go":
        return folder, [f"{stem}_test.go"], lambda p: p.endswith("_test.go")
    if ext in JS_EXTENSIONS:
        exts = [ext, *sorted(JS_EXTENSIONS - {ext})]
        names = [f"{stem}.{kind}{e}" for kind in ("test", "spec") for e in exts]
        return folder, names + [f"__tests__/{n}" for n in names], is_jest_test_file
    if ext == ".java":
        located = java_class_of(path)
        if located is None or located[2]:
            return None
        if "/src/main/java/" in f"/{folder}/":
            folder = f"/{folder}/".replace("/src/main/java/", "/src/test/java/", 1).strip("/")
        return folder, [f"{stem}Test.java", f"{stem}Tests.java"], _is_java_test
    return None


def _shared(a: str, b: str) -> int:
    """How many leading folders two paths share."""
    n = 0
    for x, y in zip(a.split("/")[:-1], b.split("/")[:-1], strict=False):
        if x != y:
            break
        n += 1
    return n


def nearest_test(path: str, files: Iterable[str]) -> str | None:
    """The test file to extend for ``path`` (repo-relative, ``/``), or None."""
    rule = _rule(path)
    if rule is None:
        return None
    folder, names, is_test = rule
    existing = sorted(set(files))
    present = set(existing)
    for name in names:
        candidate = posixpath.join(folder, name) if folder else name
        if candidate in present:
            return candidate
    here = [f for f in existing if posixpath.dirname(f) == folder and is_test(f)]
    if here:
        return here[0]
    basenames = {posixpath.basename(n) for n in names}
    elsewhere = [f for f in existing if posixpath.basename(f) in basenames and is_test(f)]
    if elsewhere:
        return sorted(elsewhere, key=lambda f: (-_shared(path, f), f))[0]
    return None
