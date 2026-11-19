"""Mutants of changed source lines, for the mutation-based recall eval (``mutation.py``).

A mutant is one small edit of a line a commit changed: a source span and its replacement.
The rest of the file is untouched (no re-formatting), so line numbers, comments and
everything else stay as they were. Operators:

* ``flip_comparison``: the first operator of a comparison, inverted (``<`` -> ``>=``,
  ``==`` -> ``!=``, ``is`` -> ``is not``, ``in`` -> ``not in`` and back).
* ``negate_condition``: an ``if`` / ``while`` / conditional-expression test, negated
  (``not (...)``), or a ``not x`` test unwrapped to ``x``.
* ``int_plus_one``: an integer literal ``n`` -> ``n + 1`` (not booleans, not in annotations).
* ``replace_return``: ``return <value>`` -> ``return None`` (``True`` <-> ``False``).
* ``remove_call``: a call statement -> ``pass``. Calls that only log, print or warn are left
  alone: removing them changes no behaviour a test checks (equivalent mutants).

Python mutants come from ``ast`` (``python_mutants``) and must still compile. JavaScript and
TypeScript ones (``js_mutants``) are a limited, token-level approach with no parser:
comparisons, integer literals and ``return <value>;`` on the changed lines, with strings,
template literals and comments masked out. They are checked for parsing separately, with the
target repo's own TypeScript parser (``ts_parse_check.js``); the eval drops the ones that fail.
"""

from __future__ import annotations

import ast
import random
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass

FLIP_COMPARISON = "flip_comparison"
NEGATE_CONDITION = "negate_condition"
INT_PLUS_ONE = "int_plus_one"
REPLACE_RETURN = "replace_return"
REMOVE_CALL = "remove_call"
OPERATORS = (FLIP_COMPARISON, NEGATE_CONDITION, INT_PLUS_ONE, REPLACE_RETURN, REMOVE_CALL)

_FLIPPED: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.GtE: ast.Lt,
    ast.Gt: ast.LtE,
    ast.LtE: ast.Gt,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}
# Call statements left alone: removing them changes only output (equivalent mutants).
_QUIET_CALLS = re.compile(
    r"^(print|breakpoint|(warnings\.)?warn|(self\.|cls\.)?(_?log(ger)?|logging|LOG(GER)?)"
    r"\.\w+)$"
)


@dataclass(frozen=True)
class Mutant:
    """``replacement`` in place of ``original``, at bytes ``start``..``end`` of the file's UTF-8
    encoding (``line``: 1-based, where ``original`` starts)."""

    path: str
    operator: str
    line: int
    start: int
    end: int
    original: str
    replacement: str

    @property
    def id(self) -> str:
        return f"{self.path}:{self.line}:{self.start}:{self.operator}"

    def apply(self, source: str) -> str:
        data = source.encode("utf-8")
        found = data[self.start : self.end].decode("utf-8")
        if found != self.original:
            raise ValueError(f"{self.id}: expected {self.original!r}, found {found!r}")
        return (data[: self.start] + self.replacement.encode("utf-8") + data[self.end :]).decode(
            "utf-8"
        )

    def describe(self) -> str:
        return f"{self.path}:{self.line} {self.operator}: {_short(self.original)} -> " + _short(
            self.replacement
        )

    def to_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "operator": self.operator,
            "line": self.line,
            "start": self.start,
            "end": self.end,
            "original": self.original,
            "replacement": self.replacement,
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> Mutant:
        return cls(
            str(data["path"]),
            str(data["operator"]),
            int(str(data["line"])),
            int(str(data["start"])),
            int(str(data["end"])),
            str(data["original"]),
            str(data["replacement"]),
        )


def _short(text: str, limit: int = 60) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 3] + "..."


# --- changed lines ------------------------------------------------------------------------

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines(diff: str) -> dict[str, set[int]]:
    """The new-side line numbers each file's hunks add or change, from a ``git diff -U0``
    (paths from the ``+++ b/`` lines; deleted files have none)."""
    lines: dict[str, set[int]] = defaultdict(set)
    path: str | None = None
    for row in diff.splitlines():
        if row.startswith("+++ "):
            target = row[4:].strip()
            path = target[2:] if target.startswith("b/") else None
            continue
        match = _HUNK.match(row)
        if match and path is not None:
            first, count = int(match[1]), int(match[2]) if match[2] is not None else 1
            lines[path].update(range(first, first + count))
    return {p: s for p, s in lines.items() if s}


# --- Python -------------------------------------------------------------------------------


class _Offsets:
    """(line, column) from ``ast`` (columns in UTF-8 bytes) -> byte offsets in the file."""

    def __init__(self, data: bytes) -> None:
        self.starts = [0]
        for row in data.splitlines(keepends=True):
            self.starts.append(self.starts[-1] + len(row))

    def __call__(self, line: int, col: int) -> int:
        return self.starts[line - 1] + col


def python_mutants(path: str, source: str, lines: Iterable[int]) -> list[Mutant]:
    """Every mutant of ``source`` on ``lines`` that still compiles, in file order. A node is
    on a line when it starts there. Empty if ``source`` doesn't parse itself."""
    wanted = set(lines)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    data = source.encode("utf-8")
    offset = _Offsets(data)
    skip = _annotation_nodes(tree) | _guard_tests(tree)

    def span(node: ast.AST) -> tuple[int, int, str]:
        start = offset(node.lineno, node.col_offset)  # type: ignore[attr-defined]
        end = offset(node.end_lineno, node.end_col_offset)  # type: ignore[attr-defined]
        return start, end, data[start:end].decode("utf-8")

    found: list[Mutant] = []
    for node, operator, replacement in _candidates(tree, skip, span):
        if node.lineno not in wanted:  # type: ignore[attr-defined]
            continue
        start, end, original = span(node)
        if replacement == original:
            continue
        found.append(Mutant(path, operator, node.lineno, start, end, original, replacement))  # type: ignore[attr-defined]
    unique = {(m.start, m.end, m.replacement): m for m in found}
    return sorted(
        (m for m in unique.values() if compiles(m.apply(source), path)),
        key=lambda m: (m.start, m.end, OPERATORS.index(m.operator)),
    )


def compiles(source: str, path: str = "<mutant>") -> bool:
    try:
        compile(source, path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        return False
    return True


def _candidates(
    tree: ast.AST, skip: set[int], span: Callable[[ast.AST], tuple[int, int, str]]
) -> Iterator[tuple[ast.AST, str, str]]:
    """(node to replace, operator, replacement) for every mutable node of ``tree``."""
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Compare) and type(node.ops[0]) in _FLIPPED:
            flipped = ast.Compare(
                left=node.left,
                ops=[_FLIPPED[type(node.ops[0])](), *node.ops[1:]],
                comparators=node.comparators,
            )
            yield node, FLIP_COMPARISON, f"({ast.unparse(flipped)})"
        elif isinstance(node, (ast.If, ast.While, ast.IfExp)):
            test = node.test
            if id(test) in skip:
                continue
            if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                yield test, NEGATE_CONDITION, f"({span(test.operand)[2]})"
            else:
                yield test, NEGATE_CONDITION, f"(not ({span(test)[2]}))"
        elif isinstance(node, ast.Constant) and type(node.value) is int and hasattr(node, "lineno"):
            yield node, INT_PLUS_ONE, str(node.value + 1)
        elif isinstance(node, ast.Return) and node.value is not None:
            value = node.value
            if isinstance(value, ast.Constant) and value.value is None:
                continue
            if isinstance(value, ast.Constant) and type(value.value) is bool:
                yield value, REPLACE_RETURN, str(not value.value)
            else:
                yield value, REPLACE_RETURN, "None"
        elif (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and not _QUIET_CALLS.match(_call_name(node.value.func))
        ):
            yield node, REMOVE_CALL, "pass"


def _call_name(func: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
    elif isinstance(func, ast.Call):
        parts.append(_call_name(func.func) + "()")
    return ".".join(reversed(parts))


def _annotation_nodes(tree: ast.AST) -> set[int]:
    """Every node inside a type annotation: mutating one changes no behaviour."""
    roots: list[ast.expr | None] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.arg, ast.AnnAssign)):
            roots.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            roots.append(node.returns)
    return {id(n) for root in roots if root is not None for n in ast.walk(root)}


def _guard_tests(tree: ast.AST) -> set[int]:
    """``if __name__ == "__main__"`` and ``if TYPE_CHECKING`` tests (and their parts): tests
    never run the first, and the second only guards imports for type checkers."""
    guards: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        text = ast.unparse(node.test)
        if ("__name__" in text and "__main__" in text) or text.endswith("TYPE_CHECKING"):
            guards.update(id(n) for n in ast.walk(node.test))
    return guards


# --- JavaScript / TypeScript (token-level) -------------------------------------------------

_JS_FLIPPED = {
    "===": "!==",
    "!==": "===",
    "==": "!=",
    "!=": "==",
    "<": ">=",
    ">=": "<",
    ">": "<=",
    "<=": ">",
}
# A comparison operator surrounded by whitespace (generics and arrows never are: `a<b>`,
# `=>`), an integer literal, or `return <value>;` on one line.
_JS_COMPARISON = re.compile(r"(?<=\s)(===|!==|==|!=|<=|>=|<|>)(?=\s)")
_JS_INT = re.compile(r"(?<![\w.$])(\d+)(?![\w.$])")
_JS_RETURN = re.compile(r"\breturn\s+([^;{}]+?)\s*;\s*$")


def js_mutants(path: str, source: str, lines: Iterable[int]) -> list[Mutant]:
    """Token-level mutants of JS/TS ``lines``: comparisons flipped, integers + 1, and
    ``return <value>;`` -> ``return undefined;`` (``true`` <-> ``false``). Limited on purpose
    (no parser): strings, template literals and comments are masked out, and a line one of
    them crosses is skipped. Regular-expression literals aren't recognised, so a mutant can
    land in one: the caller's parse check drops it if that breaks the file, and otherwise it
    is an ordinary mutant of the regex."""
    wanted = set(lines)
    data = source.encode("utf-8")
    masked = _mask_js(source)
    found: list[Mutant] = []
    line_start = 0
    for number, (row, mask) in enumerate(
        zip(source.splitlines(keepends=True), masked.splitlines(keepends=True), strict=True),
        start=1,
    ):
        base = len(source[:line_start].encode("utf-8"))
        line_start += len(row)
        if number not in wanted or mask.strip() == "" or "\0" in mask.rstrip("\r\n"):
            continue

        def mutant(
            operator: str,
            begin: int,
            end: int,
            text: str,
            row: str = row,
            base: int = base,
            number: int = number,
        ) -> Mutant:
            start = base + len(row[:begin].encode("utf-8"))
            stop = base + len(row[:end].encode("utf-8"))
            return Mutant(path, operator, number, start, stop, row[begin:end], text)

        for m in _JS_COMPARISON.finditer(mask):
            found.append(mutant(FLIP_COMPARISON, m.start(), m.end(), _JS_FLIPPED[m[1]]))
        for m in _JS_INT.finditer(mask):
            found.append(mutant(INT_PLUS_ONE, m.start(), m.end(), str(int(m[1]) + 1)))
        ret = _JS_RETURN.search(mask)
        if ret:
            value = ret[1].strip()
            if value not in ("undefined", "null"):
                replacement = {"true": "false", "false": "true"}.get(value, "undefined")
                begin = ret.start(1)
                found.append(mutant(REPLACE_RETURN, begin, begin + len(value), replacement))
    assert all(data[m.start : m.end].decode("utf-8") == m.original for m in found)
    return sorted(found, key=lambda m: (m.start, OPERATORS.index(m.operator)))


def _mask_js(source: str) -> str:
    """``source`` with comments and the inside of strings and template literals blanked: by
    spaces on a line they don't cross, by NUL on lines they cross (which the caller skips).
    Same length, same line breaks."""
    out = list(source)
    i, n = 0, len(source)

    def blank(begin: int, end: int, keep_quotes: bool) -> None:
        crosses = "\n" in source[begin:end]
        fill = "\0" if crosses else " "
        lo, hi = (begin + 1, end - 1) if keep_quotes else (begin, end)
        for k in range(lo, hi):
            if out[k] not in "\r\n":
                out[k] = fill
        if crosses:  # mark the opening line too, which might show only the quote
            for k in (begin, max(begin, end - 1)):
                if out[k] not in "\r\n":
                    out[k] = "\0"

    while i < n:
        c = source[i]
        if source.startswith("//", i):
            end = source.find("\n", i)
            end = n if end < 0 else end
            blank(i, end, False)
            i = end
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = n if end < 0 else end + 2
            blank(i, end, False)
            i = end
        elif c in "'\"`":
            j = i + 1
            while j < n and source[j] != c:
                j += 2 if source[j] == "\\" else 1
            end = min(j + 1, n)
            blank(i, end, True)
            i = end
        else:
            i += 1
    return "".join(out)


# --- choosing ------------------------------------------------------------------------------


def choose(mutants: Sequence[Mutant], k: int, seed: str) -> list[Mutant]:
    """Up to ``k`` of ``mutants``, spread over operators: operators in a seeded random order,
    one mutant each in turn (each operator's mutants seeded-shuffled). Same seed, same pick."""
    rng = random.Random(seed)
    by_operator: dict[str, list[Mutant]] = defaultdict(list)
    for m in sorted(mutants, key=lambda m: (m.path, m.start, m.end, m.operator)):
        by_operator[m.operator].append(m)
    order = sorted(by_operator)
    rng.shuffle(order)
    for operator in order:
        rng.shuffle(by_operator[operator])
    chosen: list[Mutant] = []
    while len(chosen) < k and any(by_operator.values()):
        for operator in order:
            if by_operator[operator] and len(chosen) < k:
                chosen.append(by_operator[operator].pop(0))
    return chosen
