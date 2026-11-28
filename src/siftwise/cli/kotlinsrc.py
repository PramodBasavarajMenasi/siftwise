"""Kotlin source files as the Java graph reads them (no compiler, no JVM).

``tokenize`` drops comments (nested ``/* /* */ */`` too), keeps strings and raw strings as
one token each, and reads string templates (``$name``, ``${...}``) as the code they are.
Newlines are tokens: an ``import`` or ``package`` ends at one.

``parse_kotlin`` reads what dependencies need:

* the ``package`` and the imports: a class or a member (``import a.b.C``,
  ``import a.b.topLevelFun``, ``import a.b.C.CONSTANT``), an alias (``import a.b.C as D``:
  the edge is to C) and a star import (``import a.b.*``);
* the top-level declarations: types (``class``, ``interface``, ``object``, ``enum class``,
  ``data``/``sealed``/``value``/``annotation class``, ``typealias``), several per file, and
  top-level functions and properties (extension ones by their own name), which the JVM
  sees as members of the file's facade class ``FileNameKt`` or its ``@file:JvmName``;
* top-level ``operator`` functions (``a + b`` calls ``plus`` without naming it);
* identifiers, dotted chains and string literals in code, as the Java parser does.

A file the tokenizer or the brace count can't close (an unterminated string or comment,
unbalanced braces) is ``ok=False``: what was read is kept, and the graph treats the file as
one it can't fully see (``javagraph``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Token:
    kind: str  # "id", "str" (a string's or text block's content), "punct"; Kotlin: "tstr", "nl"
    value: str


_IDENT = re.compile(r"[^\W\d]\w*")
_NUMBER = re.compile(r"0[xXbB][\w]*|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?[\w]*")
_FQN = re.compile(r"[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)+")
_TYPE_WORDS = frozenset({"class", "interface", "object", "typealias"})
# A class with one of these modifiers can't be a JUnit test class.
_NOT_RUNNABLE = frozenset({"enum", "annotation"})


class _Scanner:
    def __init__(self, source: str) -> None:
        self.s = source
        self.n = len(source)
        self.tokens: list[Token] = []
        self.ok = True

    def code(self, i: int, in_template: bool = False) -> int:
        """Code tokens from ``i``; in a ``${...}`` template, up to its closing brace (the
        index after it)."""
        s, n = self.s, self.n
        depth = 0
        while i < n:
            c = s[i]
            if c == "\n":
                self.tokens.append(Token("nl", "\n"))
                i += 1
            elif c.isspace():
                i += 1
            elif s.startswith("//", i):
                end = s.find("\n", i)
                i = n if end < 0 else end
            elif s.startswith("/*", i):
                i = self._block_comment(i)
            elif s.startswith('"""', i):
                i = self._string(i + 3, raw=True)
            elif c == '"':
                i = self._string(i + 1, raw=False)
            elif c == "'":
                i = self._char(i + 1)
            elif c == "`":
                end = s.find("`", i + 1)
                if end < 0 or "\n" in s[i + 1 : end]:
                    self.ok = False
                    end = n if end < 0 else end
                self.tokens.append(Token("id", s[i + 1 : end]))
                i = end + 1
            elif c.isdigit():
                m = _NUMBER.match(s, i)
                i = m.end() if m else i + 1
            elif (m := _IDENT.match(s, i)) is not None:
                self.tokens.append(Token("id", m.group()))
                i = m.end()
            elif s.startswith("::", i):
                self.tokens.append(Token("punct", "::"))
                i += 2
            else:
                if in_template:
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        if depth == 0:
                            return i + 1
                        depth -= 1
                self.tokens.append(Token("punct", c))
                i += 1
        if in_template:
            self.ok = False
        return i

    def _block_comment(self, i: int) -> int:
        s, n = self.s, self.n
        depth, i = 1, i + 2
        while i < n and depth:
            if s.startswith("/*", i):
                depth, i = depth + 1, i + 2
            elif s.startswith("*/", i):
                depth, i = depth - 1, i + 2
            else:
                i += 1
        if depth:
            self.ok = False
        return i

    def _string(self, i: int, raw: bool) -> int:
        """A string's literal text as one token (kind "str", or "tstr" with templates: its
        value is then only the literal parts), the templates' code before it."""
        s, n = self.s, self.n
        text: list[str] = []
        templated = False
        while i < n:
            if raw and s.startswith('"""', i):
                while i + 3 < n and s[i + 3] == '"':  # """"" ends with the last three
                    text.append('"')
                    i += 1
                i += 3
                break
            c = s[i]
            if not raw and c == '"':
                i += 1
                break
            if not raw and c == "\n":
                self.ok = False
                break
            if not raw and c == "\\" and i + 1 < n:
                text.append(s[i : i + 2])
                i += 2
                continue
            if c == "$" and i + 1 < n:
                if s[i + 1] == "{":
                    templated = True
                    i = self.code(i + 2, in_template=True)
                    continue
                m = _IDENT.match(s, i + 1)
                if m is not None:
                    templated = True
                    self.tokens.append(Token("id", m.group()))
                    i = m.end()
                    continue
            text.append(c)
            i += 1
        else:
            self.ok = False
        self.tokens.append(Token("tstr" if templated else "str", "".join(text)))
        return i

    def _char(self, i: int) -> int:
        s, n = self.s, self.n
        while i < n and s[i] not in "'\n":
            i += 2 if s[i] == "\\" else 1
        if i >= n or s[i] != "'":
            self.ok = False
            return i
        return i + 1


def tokenize(source: str) -> tuple[list[Token], bool]:
    """(tokens, whether everything closed): ids, "str"/"tstr" strings, punctuation, "nl"."""
    scanner = _Scanner(source)
    scanner.code(0)
    return scanner.tokens, scanner.ok


@dataclass(frozen=True)
class KotlinImport:
    name: str  # a.b.C, a.b.fn, a.b (a star import's package or class)
    star: bool = False
    alias: str | None = None


@dataclass(frozen=True)
class KotlinSource:
    package: str
    imports: tuple[KotlinImport, ...]
    types: tuple[str, ...]  # top-level type names (classes, objects, interfaces, aliases)
    classes: tuple[str, ...]  # the top-level classes and objects a test runner could run
    members: tuple[str, ...]  # top-level functions and properties
    operators: bool  # a top-level operator function (called without its name)
    jvm_name: str | None  # @file:JvmName
    identifiers: frozenset[str]
    dotted: tuple[tuple[str, ...], ...]
    strings: tuple[str, ...]  # string literals that look like class names
    ok: bool = True

    def facade(self, stem: str) -> str:
        """The JVM class holding the top-level members: ``@file:JvmName`` or ``StemKt``."""
        return self.jvm_name or f"{stem[:1].upper()}{stem[1:]}Kt"


def parse_kotlin(source: str) -> KotlinSource:
    tokens, ok = tokenize(source)
    n = len(tokens)

    def skip_nl(j: int) -> int:
        while j < n and tokens[j].kind == "nl":
            j += 1
        return j

    package = ""
    imports: list[KotlinImport] = []
    types: list[str] = []
    classes: list[str] = []
    members: list[str] = []
    operators = False
    jvm_name: str | None = None
    identifiers: set[str] = set()
    dotted: list[tuple[str, ...]] = []
    strings: list[str] = []
    depth = paren = 0
    modifiers: list[str] = []  # the identifiers since the last declaration boundary
    i = 0
    while i < n:
        t = tokens[i]
        if t.kind == "nl":
            i += 1
            continue
        top = depth == 0 and paren == 0
        if t.kind == "punct":
            if t.value == "{":
                depth += 1
            elif t.value == "}":
                depth -= 1
                ok = ok and depth >= 0
            elif t.value in "([":
                paren += 1
            elif t.value in ")]":
                paren -= 1
                ok = ok and paren >= 0
            if t.value == "@" and top:
                # @file:JvmName("Name")
                j = skip_nl(i + 1)
                if (
                    j + 5 < n
                    and tokens[j].value == "file"
                    and tokens[j + 1].value == ":"
                    and tokens[j + 2].value == "JvmName"
                    and tokens[j + 3].value == "("
                    and tokens[j + 4].kind == "str"
                    and tokens[j + 5].value == ")"
                ):
                    jvm_name = tokens[j + 4].value
                    i = j + 6
                    continue
            elif t.value != "@":
                modifiers = []
            i += 1
            continue
        if t.kind in ("str", "tstr"):
            if t.kind == "str" and _FQN.fullmatch(t.value):
                strings.append(t.value)
            i += 1
            continue
        if top and t.value in ("package", "import"):
            j, alias, star = i + 1, None, False
            parts: list[str] = []
            while j < n and tokens[j].kind != "nl" and tokens[j].value != ";":
                v = tokens[j]
                if v.kind == "id" and v.value == "as" and parts:
                    k = j + 1
                    if k < n and tokens[k].kind == "id":
                        alias, j = tokens[k].value, k
                elif v.kind == "id" and alias is None:
                    parts.append(v.value)
                elif v.value == "*":
                    star = True
                j += 1
            if t.value == "package":
                package = ".".join(parts)
            elif parts:
                imports.append(KotlinImport(".".join(parts), star, alias))
            modifiers = []
            i = j
            continue
        prev = tokens[i - 1].value if i else ""
        if top and t.value in _TYPE_WORDS and prev != "::" and prev != ".":
            j = skip_nl(i + 1)
            if j < n and tokens[j].kind == "id":
                name = tokens[j].value
                types.append(name)
                if t.value in ("class", "object") and not _NOT_RUNNABLE & set(modifiers):
                    classes.append(name)
                identifiers.add(name)
                modifiers = []
                i = j + 1
                continue
        if top and t.value in ("fun", "val", "var") and prev not in ("::", "."):
            j = _skip_angles(tokens, i + 1)
            if t.value == "fun" and j < n and tokens[j].value == "interface":  # fun interface X
                k = skip_nl(j + 1)
                if k < n and tokens[k].kind == "id":
                    types.append(tokens[k].value)
                    identifiers.add(tokens[k].value)
                    i = k + 1
                    modifiers = []
                    continue
            declared, _ = _declared_name(tokens, j)
            if declared:
                members.append(declared)
                if t.value == "fun" and "operator" in modifiers:
                    operators = True
            modifiers = []
            identifiers.add(t.value)
            i = i + 1
            continue
        if t.kind == "id":
            modifiers.append(t.value)
        identifiers.add(t.value)
        chain = [t.value]
        j = i
        while True:
            k = skip_nl(j + 1)
            if k + 1 < n and tokens[k].value == "." and tokens[skip_nl(k + 1)].kind == "id":
                nxt = skip_nl(k + 1)
                chain.append(tokens[nxt].value)
                identifiers.add(tokens[nxt].value)
                j = nxt
            else:
                break
        if len(chain) > 1:
            dotted.append(tuple(chain))
        i = j + 1
    ok = ok and depth == 0 and paren == 0
    return KotlinSource(
        package,
        tuple(imports),
        tuple(dict.fromkeys(types)),
        tuple(dict.fromkeys(classes)),
        tuple(dict.fromkeys(members)),
        operators,
        jvm_name,
        frozenset(identifiers),
        tuple(dotted),
        tuple(strings),
        ok,
    )


def _skip_angles(tokens: list[Token], j: int) -> int:
    """Past newlines and a ``<...>`` type parameter list at ``j`` (if there is one)."""
    n = len(tokens)
    while j < n and tokens[j].kind == "nl":
        j += 1
    if j >= n or tokens[j].value != "<":
        return j
    level, start = 0, j
    while j < n:
        if tokens[j].value == "<":
            level += 1
        elif tokens[j].value == ">":
            level -= 1
            if level == 0:
                return j + 1
        elif tokens[j].value in ("{", "}", ";", "=", "("):
            return start  # not a type parameter list after all
        j += 1
    return start


def _declared_name(tokens: list[Token], j: int) -> tuple[str | None, int]:
    """A ``fun``'s or property's name: the last identifier of ``Receiver.name``
    (``String.ext``, ``List<T>.second``, ``Foo?.bar``), or just ``name``."""
    n = len(tokens)
    name: str | None = None
    while j < n:
        while j < n and tokens[j].kind == "nl":
            j += 1
        if j >= n or tokens[j].kind != "id":
            break
        name = tokens[j].value
        j = _skip_angles(tokens, j + 1)
        while j < n and tokens[j].value == "?":
            j += 1
        if j < n and tokens[j].value == ".":
            j += 1
            continue
        break
    return name, j
