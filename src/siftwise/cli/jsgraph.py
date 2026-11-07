"""Static JS/TS import graph of a checkout, for selecting Jest tests that import changed code.

``siftwise select`` builds it at the repo root, like the Python graph, and sends
``JsGraph.affected(...)``: every Jest test file that imports a changed file, directly or
transitively (``affected_files``), the nearest such file with the one the test imports on the
way and the import depth, the tests that reach an import siftwise can't resolve, and notes
on what it couldn't read (``js_graph``).

No Node.js: a small tokenizer (``scan``) reads each ``.js .jsx .ts .tsx .mjs .cjs .mts .cts``
file, skipping comments, strings, template literals, regular expressions and JSX text, and
``imports`` finds what it imports: ``import ... from``, ``import '...'``, ``import type``
(type changes break compiles), ``export ... from``, ``require('...')``, ``import('...')`` and
``jest.mock('...')``-style calls. A ``require`` or ``import()`` of anything but a literal can
load any file: the file is *dynamic*, and every test that reaches it is selected for any
JS change (never counted as covering the change). Unless it is bounded (``ContextImport``):
a template or concatenation with a static directory (``import(`./locales/${l}.json`)``),
``import.meta.glob`` or ``require.context`` with literals is an edge to every file below the
directory that it can match (``Resolver.context``; dynamic again when it can't be bounded).

Resolution, first that applies (``Resolver``):

* Jest ``moduleNameMapper`` from a JSON config (``jest.config.json`` or the package.json
  ``jest`` key). A ``jest.config.js``/``.ts`` can't be read without Node: a note says so.
* Relative paths: the exact file, TS ESM's ``.js`` -> ``.ts`` mapping, the extensions in
  ``EXTENSIONS`` order, then ``dir/index.*``.
* tsconfig.json / jsconfig.json ``paths`` and ``baseUrl`` (nearest config up the tree,
  ``extends`` chains within the repo; JSONC: comments and trailing commas).
* Workspace packages (package.json ``workspaces``, pnpm-workspace.yaml, lerna.json): ``@s/pkg``
  and ``@s/pkg/sub`` resolve to the package's source (``exports``/``source``/``module``/
  ``main`` when they name a file in the checkout, else ``src/index.*``, ``index.*``); the
  package's package.json is an edge too, since its entry points decide what is imported.
* Anything else is an external package (node_modules is never read) or an asset that isn't
  in the checkout: no edge, never a failure. An asset that IS in the checkout (a JSON file, a
  stylesheet) is an edge, so changing it selects its importers.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import posixpath
import re
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from siftwise.core.selector import JS_EXTENSIONS, glob_match, is_jest_test_file

# Node/TypeScript resolution order for a path without (or with an unknown) extension.
EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs", ".json")
# TypeScript ESM imports name the emitted file: ``./x.js`` is ``x.ts`` in the source.
_EMITTED = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}
SKIP_DIRS = frozenset(
    {"node_modules", "bower_components", "dist", "build", "coverage", "out", "__pycache__"}
)
JEST_CALLS = frozenset(
    {"mock", "unmock", "doMock", "dontMock", "requireActual", "requireMock", "setMock",
     "createMockFromModule"}
)  # fmt: skip
JEST_JS_CONFIGS = tuple(
    f"jest.config{ext}" for ext in (".js", ".ts", ".mjs", ".cjs", ".mts", ".cts")
)
# After these keywords a ``/`` starts a regular expression and a ``<`` a JSX element.
_EXPRESSION_KEYWORDS = frozenset(
    {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case",
     "do", "else", "yield", "await", "extends"}
)  # fmt: skip
_IDENT_START = re.compile(r"[A-Za-z_$\u0080-\uffff]")
_IDENT = re.compile(r"[\w$\u0080-\uffff]+")
# Whitespace, with a byte order mark and a no-break space.
_BLANK = frozenset(" \t\r\n\ufeff\u00a0")
_JSX_NAME = re.compile(r"[A-Za-z_$][\w$.:-]*")
MAX_NOTES = 5
# A bounded non-literal import (a context module) matching more files than this stays dynamic:
# past it the "bound" is most of a project, and its edges would cost more than they save.
MAX_CONTEXT_FILES = 500


# --- tokenizer ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Token:
    kind: str  # "id", "num", "str" (a string or a template without ${}), "tpl" (with ${}),
    # "re" (a regular expression literal), "punct", "jsx"
    value: str
    # A "tpl": its static pieces around each ${} (one more than the substitutions), and how
    # many tokens its ${} expressions left just before it.
    parts: tuple[str, ...] = ()
    inner: int = 0


class _Scanner:
    """Tokens of JS/TS source, as far as imports need them. ``jsx``: ``<`` in an expression
    position may start a JSX element (not in .ts files, where it's a type assertion)."""

    def __init__(self, source: str, jsx: bool) -> None:
        self.s = source
        self.n = len(source)
        self.i = 2 if source.startswith("#!") else 0  # a hashbang line: skipped below
        if self.i:
            self._skip_line()
        self.jsx = jsx
        self.tokens: list[Token] = []

    # Expression position: a ``/`` here is a regex, a ``<`` may be JSX.
    def _operand_expected(self) -> bool:
        if not self.tokens:
            return True
        last = self.tokens[-1]
        if last.kind in ("num", "str", "tpl", "re", "jsx"):
            return False
        if last.kind == "id":
            return last.value in _EXPRESSION_KEYWORDS
        return last.value not in (")", "]")

    def _skip_line(self) -> None:
        end = self.s.find("\n", self.i)
        self.i = self.n if end < 0 else end

    def code(self, until_brace: bool = False) -> None:
        """Scan code; with ``until_brace``, stop after the ``}`` closing the current ``{``
        (a template's ``${``, a JSX ``{expression}``)."""
        s, depth = self.s, 0
        while self.i < self.n:
            c = s[self.i]
            if c in _BLANK:
                self.i += 1
            elif c == "/" and s.startswith("//", self.i):
                self._skip_line()
            elif c == "/" and s.startswith("/*", self.i):
                end = s.find("*/", self.i + 2)
                self.i = self.n if end < 0 else end + 2
            elif c == "/" and self._operand_expected():
                self._regex()
            elif c in "'\"":
                self.tokens.append(Token("str", self._string(c)))
            elif c == "`":
                self._template()
            elif c == "{":
                depth += 1
                self._punct(c)
            elif c == "}":
                if until_brace and depth == 0:
                    self.i += 1
                    return
                depth -= 1
                self._punct(c)
            elif c == "<" and self.jsx and self._operand_expected() and self._jsx_element():
                self.tokens.append(Token("jsx", ""))
            elif c.isdigit():
                m = _IDENT.match(s, self.i)
                self.i = m.end() if m else self.i + 1
                self.tokens.append(Token("num", ""))
            elif _IDENT_START.match(c):
                m = _IDENT.match(s, self.i)
                assert m is not None
                self.tokens.append(Token("id", m.group()))
                self.i = m.end()
            else:
                self._punct(c)

    def _punct(self, c: str) -> None:
        self.tokens.append(Token("punct", c))
        self.i += 1

    def _string(self, quote: str) -> str:
        s, start = self.s, self.i + 1
        i = start
        out = []
        while i < self.n:
            c = s[i]
            if c == "\\":
                out.append(s[i + 1 : i + 2])
                i += 2
            elif c == quote:
                self.i = i + 1
                return "".join(out)
            elif c == "\n":  # unterminated: stop at the line end
                break
            else:
                out.append(c)
                i += 1
        self.i = i
        return "".join(out)

    def _template(self) -> None:
        s = self.s
        self.i += 1
        out: list[str] = []
        parts: list[str] = []  # the static pieces before each ${}
        piece: list[str] = []
        start = len(self.tokens)
        while self.i < self.n:
            c = s[self.i]
            if c == "\\":
                out.append(s[self.i + 1 : self.i + 2])
                piece.append(s[self.i + 1 : self.i + 2])
                self.i += 2
            elif c == "`":
                self.i += 1
                break
            elif c == "$" and s.startswith("${", self.i):
                parts.append("".join(piece))
                piece = []
                self.i += 2
                # The expression's own tokens matter (a require inside it), but not as part
                # of the template: keep them (before the template's token, which counts
                # them in `inner`), then mark the template.
                self.code(until_brace=True)
            else:
                out.append(c)
                piece.append(c)
                self.i += 1
        if not parts:
            self.tokens.append(Token("str", "".join(out)))
            return
        inner = len(self.tokens) - start
        self.tokens.append(Token("tpl", "".join(out), (*parts, "".join(piece)), inner))

    def _regex(self) -> None:
        s, i = self.s, self.i + 1
        in_class = False
        while i < self.n:
            c = s[i]
            if c == "\\":
                i += 2
                continue
            if c == "\n":
                break
            if in_class:
                in_class = c != "]"
            elif c == "[":
                in_class = True
            elif c == "/":
                i += 1
                break
            i += 1
        m = _IDENT.match(s, i)  # flags
        start, self.i = self.i, m.end() if m else i
        self.tokens.append(Token("re", s[start : self.i]))  # an operand, like a string

    # --- JSX: skipped as an operand; its {expressions} are scanned as code ----------------

    def _jsx_element(self) -> bool:
        """Skip a JSX element at ``<``; False (and nothing consumed) if it isn't one, e.g. a
        TS generic arrow ``<T,>(x: T) => x`` in a .tsx file."""
        start, count = self.i, len(self.tokens)
        try:
            ok = self._jsx_tag()
        except RecursionError:
            ok = False
        if not ok:
            self.i = start
            del self.tokens[count:]
        return ok

    def _skip_ws(self) -> None:
        while self.i < self.n and self.s[self.i] in " \t\r\n":
            self.i += 1

    def _jsx_tag(self) -> bool:
        s = self.s
        self.i += 1  # <
        self._skip_ws()
        if s.startswith(">", self.i):  # a fragment
            self.i += 1
            return self._jsx_children()
        m = _JSX_NAME.match(s, self.i)
        if not m:
            return False
        self.i = m.end()
        while True:
            self._skip_ws()
            if self.i >= self.n:
                return False
            if s.startswith("/>", self.i):
                self.i += 2
                return True
            c = s[self.i]
            if c == ">":
                self.i += 1
                return self._jsx_children()
            if c == "{":  # {...spread}
                self.i += 1
                self.code(until_brace=True)
                continue
            attr = _JSX_NAME.match(s, self.i)
            if not attr:
                return False
            self.i = attr.end()
            self._skip_ws()
            if not s.startswith("=", self.i):
                continue  # a boolean attribute
            self.i += 1
            self._skip_ws()
            if self.i >= self.n:
                return False
            c = s[self.i]
            if c in "'\"":
                end = s.find(c, self.i + 1)  # JSX attribute strings have no escapes
                if end < 0:
                    return False
                self.i = end + 1
            elif c == "{":
                self.i += 1
                self.code(until_brace=True)
            elif c == "<":
                if not self._jsx_tag():
                    return False
            else:
                return False

    def _jsx_children(self) -> bool:
        s = self.s
        while self.i < self.n:
            c = s[self.i]
            if c == "{":
                self.i += 1
                self.code(until_brace=True)
            elif c == "<":
                if s.startswith("</", self.i):
                    end = s.find(">", self.i)
                    if end < 0:
                        return False
                    self.i = end + 1
                    return True
                if not self._jsx_tag():
                    return False
            else:
                self.i += 1  # text
        return False


def scan(source: str, jsx: bool = True) -> list[Token]:
    scanner = _Scanner(source, jsx)
    scanner.code()
    return scanner.tokens


@dataclass(frozen=True)
class ContextImport:
    """A ``require``/``import()`` whose target isn't a literal but is bounded by a static
    directory, like a bundler's context module (``Resolver.context`` finds its files):

    * ``prefix``: a template or concatenation, ``pattern`` = its static start
      (``./locales/``), ``suffix`` = its static end (``.json``, may be empty);
    * ``glob``: ``import.meta.glob`` (Vite), ``pattern`` = one glob (``./pages/*.tsx``);
    * ``context``: ``require.context`` (webpack), ``pattern`` = the directory, with
      ``recursive`` and the ``regex`` literal (``/\\.js$/i``; None: every file).
    """

    kind: str
    pattern: str
    suffix: str = ""
    recursive: bool = True
    regex: str | None = None


@dataclass(frozen=True)
class Imports:
    specifiers: tuple[str, ...]  # what the file imports, in order, de-duplicated
    dynamic: bool  # a require/import() siftwise can't bound: it may load any file
    contexts: tuple[ContextImport, ...] = ()  # bounded non-literal imports, de-duplicated


def _call_args(tokens: Sequence[Token], k: int) -> list[list[Token]] | None:
    """The arguments of the call whose ``(`` is at ``k``, each as its tokens (a template's
    ``${}`` tokens left out: the template token stands for them); None if it doesn't close."""
    args: list[list[Token]] = []
    current: list[Token] = []
    depth = 0
    for j in range(k + 1, min(len(tokens), k + 2000)):
        t = tokens[j]
        if t.kind == "punct" and t.value in "([{":
            depth += 1
        elif t.kind == "punct" and t.value in ")]}":
            if depth == 0:
                if t.value != ")":
                    return None
                if current or args:
                    args.append(current)
                return [_collapse(arg) for arg in args]
            depth -= 1
        elif t.kind == "punct" and t.value == "," and depth == 0:
            args.append(current)
            current = []
            continue
        current.append(t)
    return None


def _collapse(tokens: Sequence[Token]) -> list[Token]:
    """``tokens`` without the tokens of templates' ``${}`` expressions (just before each)."""
    out: list[Token] = []
    i = len(tokens) - 1
    while i >= 0:
        out.append(tokens[i])
        i -= 1 + (tokens[i].inner if tokens[i].kind == "tpl" else 0)
    return out[::-1]


# Punctuation a concatenation may hold outside brackets: `'./a/' + x.y + '.js'`. Anything
# else there (?:, ||, &&, -, !, ??) could make the value something else entirely.
_CONCAT_PUNCT = frozenset("+.()[]{}")


def _target(arg: Sequence[Token]) -> str | ContextImport | None:
    """What a require/import() argument loads: a literal, a bounded pattern, or None (can't
    tell: dynamic)."""
    if len(arg) == 1 and arg[0].kind == "str":
        return arg[0].value
    if len(arg) == 1 and arg[0].kind == "tpl":
        parts = arg[0].parts  # `${base}/x`: no static start, nothing bounds it
        return ContextImport("prefix", parts[0], parts[-1]) if parts[0] else None
    if (
        len(arg) >= 5
        and [t.value for t in arg[:4]] == ["require", ".", "resolve", "("]
        and _closes_last(arg, 3)
    ):
        inner = _call_args(arg, 3)  # require(require.resolve(X)) loads what X names
        return _target(inner[0]) if inner else None
    terms: list[list[Token]] = [[]]
    depth = 0
    for t in arg:
        if t.kind == "punct":
            if depth == 0 and t.value not in _CONCAT_PUNCT:
                return None
            if t.value in "([{":
                depth += 1
            elif t.value in ")]}":
                depth -= 1
            elif t.value == "+" and depth == 0:
                terms.append([])
                continue
        terms[-1].append(t)
    if len(terms) < 2 or not all(terms):
        return None

    def literal(term: list[Token]) -> bool:
        return len(term) == 1 and term[0].kind == "str"

    if all(literal(term) for term in terms):
        return "".join(term[0].value for term in terms)
    first, last = terms[0], terms[-1]
    if literal(first):
        lead = len(list(_takewhile(literal, terms)))
        prefix = "".join(term[0].value for term in terms[:lead])
    elif len(first) == 1 and first[0].kind == "tpl":
        prefix = first[0].parts[0]
    else:
        return None
    if not prefix:
        return None
    if literal(last):
        trail = len(list(_takewhile(literal, terms[::-1])))
        suffix = "".join(term[0].value for term in terms[len(terms) - trail :])
    elif len(last) == 1 and last[0].kind == "tpl":
        suffix = last[0].parts[-1]
    else:
        suffix = ""
    return ContextImport("prefix", prefix, suffix)


def _closes_last(tokens: Sequence[Token], k: int) -> bool:
    """Whether the bracket opened at ``k`` closes at the last token (nothing follows it)."""
    depth = 0
    for j in range(k, len(tokens)):
        if tokens[j].kind == "punct" and tokens[j].value in "([{":
            depth += 1
        elif tokens[j].kind == "punct" and tokens[j].value in ")]}":
            depth -= 1
            if depth == 0:
                return j == len(tokens) - 1
    return False


def _takewhile(test: Any, items: Sequence[list[Token]]) -> Iterable[list[Token]]:
    for item in items:
        if not test(item):
            return
        yield item


def _glob_patterns(arg: Sequence[Token]) -> list[str] | None:
    """``import.meta.glob``'s first argument: a literal or an array of literals."""
    if len(arg) == 1 and arg[0].kind == "str":
        return [arg[0].value]
    if len(arg) < 2 or arg[0].value != "[" or arg[-1].value != "]":
        return None
    found = []
    for n, t in enumerate(arg[1:-1]):
        if n % 2 == 0 and t.kind == "str":
            found.append(t.value)
        elif n % 2 == 1 and t.kind == "punct" and t.value == ",":
            continue
        else:
            return None
    return found or None


def imports(source: str, jsx: bool = True) -> Imports:
    """What a JS/TS file imports (see the module docstring)."""
    tokens = scan(source, jsx)
    found: list[str] = []
    contexts: list[ContextImport] = []
    dynamic = False

    def load(target: str | ContextImport | None) -> None:
        nonlocal dynamic
        if isinstance(target, str):
            found.append(target)
        elif target is not None:
            contexts.append(target)
        else:
            dynamic = True

    def at(k: int) -> Token:
        return tokens[k] if k < len(tokens) else Token("eof", "")

    def is_(k: int, kind: str, value: str | None = None) -> bool:
        t = at(k)
        return t.kind == kind and (value is None or t.value == value)

    for k, token in enumerate(tokens):
        if token.kind != "id":
            continue
        after_dot = k > 0 and is_(k - 1, "punct", ".")
        name = token.value
        if name == "jest" and not after_dot:
            # jest.mock('m') / jest.requireActual('m'): a non-literal says nothing
            call = is_(k + 1, "punct", ".") and at(k + 2).value in JEST_CALLS
            if call and is_(k + 3, "punct", "(") and is_(k + 4, "str"):
                found.append(at(k + 4).value)
            continue
        if after_dot:
            continue
        if name == "require":
            if k > 0 and at(k - 1).value == "function":
                continue  # a declaration: `declare function require(id: string)`
            if is_(k + 1, "punct", "("):
                args = _call_args(tokens, k + 1)
                if args:
                    load(_target(args[0]))
                elif args is None:
                    dynamic = True  # never closes: can't tell what it loads
            elif is_(k + 1, "punct", ".") and is_(k + 3, "punct", "("):
                method = at(k + 2).value
                args = _call_args(tokens, k + 3) if method in ("resolve", "context") else None
                if method == "resolve" and args:
                    # require.resolve('m'): a path, an edge when bounded; else nothing
                    target = _target(args[0])
                    if target is not None:
                        load(target)
                elif method == "context":
                    load(_require_context(args))
        elif name == "import":
            if is_(k + 1, "punct", "("):
                args = _call_args(tokens, k + 1)
                load(_target(args[0]) if args else None)
            elif is_(k + 1, "punct", "."):
                # import.meta.glob('./pages/*.ts') (Vite); any other import.meta: nothing
                glob = at(k + 2).value == "meta" and is_(k + 3, "punct", ".")
                if glob and at(k + 4).value in ("glob", "globEager"):
                    args = _call_args(tokens, k + 5) if is_(k + 5, "punct", "(") else None
                    patterns = _glob_patterns(args[0]) if args else None
                    wanted = [p for p in patterns or () if not p.startswith("!")]
                    if wanted:
                        contexts.extend(ContextImport("glob", p) for p in wanted)
                    else:
                        dynamic = True
                continue
            elif is_(k + 1, "str"):
                found.append(at(k + 1).value)  # import 'side-effect'
            else:
                spec = _from_clause(tokens, k + 1, stop_at_equals=True)
                if spec is not None:
                    found.append(spec)
        elif name == "export":
            nxt = at(k + 1)
            if nxt.value in ("*", "{") or (nxt.value == "type" and at(k + 2).value in ("*", "{")):
                spec = _from_clause(tokens, k + 1, stop_at_equals=False)
                if spec is not None:
                    found.append(spec)
    return Imports(tuple(dict.fromkeys(found)), dynamic, tuple(dict.fromkeys(contexts)))


def _require_context(args: list[list[Token]] | None) -> ContextImport | None:
    """``require.context(dir, recursive = true, regex = /^\\.\\/.*$/)`` (webpack) with a
    literal directory; None otherwise."""
    if not args or len(args[0]) != 1 or args[0][0].kind != "str":
        return None
    recursive = not (len(args) > 1 and [t.value for t in args[1]] == ["false"])
    literal = len(args) > 2 and len(args[2]) == 1 and args[2][0].kind == "re"
    regex = args[2][0].value if literal else None
    return ContextImport("context", args[0][0].value, recursive=recursive, regex=regex)


def _from_clause(tokens: Sequence[Token], k: int, stop_at_equals: bool) -> str | None:
    """The module of an ``import ... from 'm'`` / ``export {...} from 'm'`` clause that starts
    at ``k``, or None (an ``export {a}`` with no ``from``, ``import x = require(...)``)."""
    depth = 0
    for j in range(k, min(len(tokens), k + 4000)):
        t = tokens[j]
        if t.kind == "punct":
            if t.value == "{":
                depth += 1
            elif t.value == "}":
                depth -= 1
            elif depth == 0 and (t.value == ";" or (t.value == "=" and stop_at_equals)):
                return None
        elif t.kind == "id" and depth == 0:
            if t.value == "from" and j + 1 < len(tokens) and tokens[j + 1].kind == "str":
                return tokens[j + 1].value
            if t.value in ("import", "export", "const", "let", "var", "function", "class"):
                return None
        elif t.kind == "str" and depth == 0 and j > k:
            return None
    return None


# --- configs --------------------------------------------------------------------------------


def jsonc(text: str) -> Any:
    """JSON with comments and trailing commas (tsconfig.json, jsconfig.json)."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))


@dataclass(frozen=True)
class TsPaths:
    """A tsconfig's resolution settings, ``extends`` applied."""

    base_url: str | None  # repo-relative directory, or None
    paths: tuple[tuple[str, tuple[str, ...]], ...]  # pattern -> targets, repo-relative
    paths_dir: str  # what ``paths`` targets are relative to without baseUrl


@dataclass(frozen=True)
class JestConfig:
    directory: str  # the config's directory: the project it applies to
    root_dir: str
    mappers: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...]


@dataclass(frozen=True)
class Package:
    name: str
    directory: str
    manifest: Mapping[str, Any]


@dataclass
class Notes:
    items: list[str] = field(default_factory=list)

    def add(self, note: str) -> None:
        if note not in self.items and len(self.items) < MAX_NOTES:
            self.items.append(note)


# --- the graph ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Nearest:
    module: str  # the nearest changed file the test imports
    via: str | None  # the file the test imports on the way; None: it imports it directly
    depth: int  # 1 = a direct import


@dataclass(frozen=True)
class Affected:
    files: dict[str, list[str]]  # test file -> changed files it imports (affected_files)
    nearest: dict[str, Nearest]
    dynamic: dict[str, str]  # test file -> a file it reaches that has a dynamic import
    notes: list[str]

    def js_graph(self) -> dict[str, Any] | None:
        """The ``js_graph`` field of ``/select``: None when there is nothing to say."""
        if not (self.nearest or self.dynamic or self.notes):
            return None
        return {
            "nearest": {
                test: {"module": n.module, "via": n.via, "depth": n.depth}
                for test, n in self.nearest.items()
            },
            "dynamic": dict(self.dynamic),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class JsGraph:
    edges: Mapping[str, frozenset[str]]  # file -> files it imports
    dynamic_files: frozenset[str]  # files with a require/import() of a non-literal
    notes: tuple[str, ...] = ()

    def affected(self, changed_files: Iterable[str]) -> Affected:
        importers: dict[str, set[str]] = defaultdict(set)
        for file, imported in self.edges.items():
            for target in imported:
                importers[target].add(file)
        reached: dict[str, set[str]] = defaultdict(set)
        nearest: dict[str, Nearest] = {}
        for changed in sorted(set(changed_files)):
            if changed not in importers:
                continue
            # Reverse BFS: next_hop[x] = the file x imports on its shortest way to `changed`.
            next_hop: dict[str, str] = {}
            depth = {changed: 0}
            queue = deque([changed])
            while queue:
                current = queue.popleft()
                for importer in sorted(importers.get(current, ())):
                    if importer not in depth:
                        depth[importer] = depth[current] + 1
                        next_hop[importer] = current
                        queue.append(importer)
            for file, d in depth.items():
                if not d or not is_jest_test_file(file):
                    continue
                reached[file].add(changed)
                hop = next_hop[file]
                candidate = Nearest(changed, None if hop == changed else hop, d)
                old = nearest.get(file)
                if old is None or (candidate.depth, candidate.module) < (old.depth, old.module):
                    nearest[file] = candidate
        dynamic: dict[str, str] = {}
        if self.dynamic_files:
            queue = deque(sorted(self.dynamic_files))
            source = {f: f for f in queue}
            while queue:
                current = queue.popleft()
                for importer in sorted(importers.get(current, ())):
                    if importer not in source:
                        source[importer] = source[current]
                        queue.append(importer)
            dynamic = {f: src for f, src in sorted(source.items()) if is_jest_test_file(f)}
        files = {f: sorted(m) for f, m in sorted(reached.items())}
        return Affected(files, dict(sorted(nearest.items())), dynamic, list(self.notes))


@dataclass
class _Cache:
    """Parsed imports by content hash, so a file read twice (or unchanged since the last
    build in this process) is tokenized once."""

    by_hash: dict[str, Imports] = field(default_factory=dict)

    def imports(self, content: bytes, jsx: bool) -> Imports:
        key = hashlib.sha1(content + (b"\x01" if jsx else b"\x00")).hexdigest()
        found = self.by_hash.get(key)
        if found is None:
            found = imports(content.decode("utf-8", "replace"), jsx)
            self.by_hash[key] = found
        return found


_CACHE = _Cache()


def _walk(root: Path) -> list[str]:
    """Every file under ``root`` (repo-relative, sorted), node_modules, dot-dirs and build
    output pruned while walking."""
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        rel = Path(dirpath).relative_to(root).as_posix()
        prefix = "" if rel == "." else rel + "/"
        files.extend(prefix + name for name in filenames)
    return sorted(files)


def build_js_graph(root: Path, deleted: Iterable[str] = ()) -> JsGraph:
    """The checkout's import graph. ``deleted`` files count as existing, so an import of a
    file the change deletes is still an edge (its importers break)."""
    files = _walk(root)
    resolver = Resolver(root, frozenset(files) | frozenset(deleted))
    edges: dict[str, frozenset[str]] = {}
    dynamic: set[str] = set()
    for file in files:
        ext = posixpath.splitext(file)[1]
        if ext not in JS_EXTENSIONS:
            continue
        try:
            content = (root / file).read_bytes()
        except OSError:
            continue
        # .ts/.mts/.cts can't hold JSX (`<T>x` is a type assertion there).
        found = _CACHE.imports(content, jsx=ext not in (".ts", ".mts", ".cts"))
        if found.dynamic:
            dynamic.add(file)
        targets: set[str] = set()
        for spec in found.specifiers:
            targets.update(resolver.resolve(spec, file))
        for context in found.contexts:
            matched = resolver.context(context, file)
            if matched is None:
                dynamic.add(file)  # can't bound it: it may load any file
            else:
                targets.update(matched)
        targets.discard(file)
        edges[file] = frozenset(targets)
    return JsGraph(edges, frozenset(dynamic), tuple(resolver.notes.items))


class Resolver:
    """Import specifier -> repo files (see the module docstring for the order)."""

    def __init__(self, root: Path, files: frozenset[str]) -> None:
        self.root = root
        self.files = files
        self.notes = Notes()
        self._dirs = {posixpath.dirname(f) for f in files}
        self._ts_cache: dict[str, TsPaths | None] = {}
        self._ts_for_dir: dict[str, TsPaths | None] = {}
        self._jest_for_dir: dict[str, JestConfig | None] = {}
        self._every_dir: set[str] | None = None
        self._sorted: list[str] | None = None
        self.jest_configs = self._jest_configs()
        self.packages = self._workspace_packages()

    # --- reading the checkout ---

    def _read_json(self, path: str, comments: bool = False) -> Any:
        try:
            text = (self.root / path).read_text(encoding="utf-8")
            return jsonc(text) if comments else json.loads(text)
        except (OSError, ValueError, UnicodeDecodeError):
            return None

    def _jest_configs(self) -> dict[str, JestConfig]:
        found: dict[str, JestConfig] = {}
        for file in sorted(self.files):
            directory, base = posixpath.split(file)
            data: Any = None
            if base == "jest.config.json":
                data = self._read_json(file)
            elif base == "package.json":
                manifest = self._read_json(file)
                data = manifest.get("jest") if isinstance(manifest, dict) else None
            elif base in JEST_JS_CONFIGS:
                self.notes.add(
                    f"{file} is JavaScript/TypeScript and can't be read without Node: its "
                    "moduleNameMapper isn't applied (tsconfig and relative resolution are)"
                )
                continue
            if not isinstance(data, dict) or directory in found:
                continue
            root_dir = posixpath.normpath(posixpath.join(directory, str(data.get("rootDir", "."))))
            root_dir = "" if root_dir == "." else root_dir
            mappers = []
            for pattern, target in (data.get("moduleNameMapper") or {}).items():
                try:
                    compiled = re.compile(str(pattern))
                except re.error:
                    self.notes.add(f"{file}: moduleNameMapper pattern {pattern!r} isn't a "
                                   "regular expression siftwise can read")  # fmt: skip
                    continue
                targets = tuple(str(t) for t in (target if isinstance(target, list) else [target]))
                mappers.append((compiled, targets))
            found[directory] = JestConfig(directory, root_dir, tuple(mappers))
        return found

    def _workspace_packages(self) -> dict[str, Package]:
        patterns: list[str] = []
        manifest = self._read_json("package.json") if "package.json" in self.files else None
        if isinstance(manifest, dict):
            workspaces = manifest.get("workspaces")
            if isinstance(workspaces, dict):
                workspaces = workspaces.get("packages")
            if isinstance(workspaces, list):
                patterns += [str(w) for w in workspaces]
        if "pnpm-workspace.yaml" in self.files:
            patterns += _pnpm_packages(self.root / "pnpm-workspace.yaml")
        lerna = self._read_json("lerna.json") if "lerna.json" in self.files else None
        if isinstance(lerna, dict) and isinstance(lerna.get("packages"), list):
            patterns += [str(p) for p in lerna["packages"]]
        include = [p.strip("/").removeprefix("./") for p in patterns if not p.startswith("!")]
        exclude = [p[1:].strip("/").removeprefix("./") for p in patterns if p.startswith("!")]
        packages: dict[str, Package] = {}
        for file in sorted(self.files):
            directory, base = posixpath.split(file)
            if base != "package.json" or not directory:
                continue
            if not any(glob_match(directory, p) for p in include):
                continue
            if any(glob_match(directory, p) for p in exclude):
                continue
            data = self._read_json(file)
            if isinstance(data, dict) and isinstance(data.get("name"), str):
                packages.setdefault(data["name"], Package(data["name"], directory, data))
        return packages

    def _tsconfig(self, path: str, seen: frozenset[str] = frozenset()) -> TsPaths | None:
        if path in self._ts_cache:
            return self._ts_cache[path]
        data = self._read_json(path, comments=True)
        if not isinstance(data, dict):
            self._ts_cache[path] = None
            return None
        directory = posixpath.dirname(path)
        inherited: TsPaths | None = None
        extends = data.get("extends")
        for parent in [extends] if isinstance(extends, str) else (extends or []):
            parent = str(parent)
            if not parent.startswith("."):
                self.notes.add(f"{path} extends {parent!r} from a package: not followed "
                               "(node_modules is never read)")  # fmt: skip
                continue
            target = posixpath.normpath(posixpath.join(directory, parent))
            if target not in self.files and target + ".json" in self.files:
                target += ".json"
            if target in self.files and target not in seen:
                found = self._tsconfig(target, seen | {path})
                inherited = found or inherited
        options = data.get("compilerOptions") or {}
        base_url = inherited.base_url if inherited else None
        paths = inherited.paths if inherited else ()
        paths_dir = inherited.paths_dir if inherited else directory
        if isinstance(options, dict):
            if isinstance(options.get("baseUrl"), str):
                base_url = _clean(posixpath.join(directory, options["baseUrl"]))
            if isinstance(options.get("paths"), dict):
                paths = tuple(
                    (str(pattern), tuple(str(t) for t in targets))
                    for pattern, targets in options["paths"].items()
                    if isinstance(targets, list)
                )
                paths_dir = directory
        result = TsPaths(base_url, paths, paths_dir)
        self._ts_cache[path] = result
        return result

    def _tsconfig_for(self, directory: str) -> TsPaths | None:
        if directory in self._ts_for_dir:
            return self._ts_for_dir[directory]
        found: TsPaths | None = None
        for name in ("tsconfig.json", "jsconfig.json"):
            path = posixpath.join(directory, name) if directory else name
            if path in self.files:
                found = self._tsconfig(path)
                break
        else:
            found = self._tsconfig_for(posixpath.dirname(directory)) if directory else None
        self._ts_for_dir[directory] = found
        return found

    def _jest_for(self, directory: str) -> JestConfig | None:
        if directory in self._jest_for_dir:
            return self._jest_for_dir[directory]
        found = self.jest_configs.get(directory)
        if found is None and directory:
            found = self._jest_for(posixpath.dirname(directory))
        self._jest_for_dir[directory] = found
        return found

    # --- resolving ---

    def resolve(self, spec: str, importer: str) -> set[str]:
        """The repo files ``spec`` imported from ``importer`` names (empty: external, or not
        in the checkout)."""
        spec = spec.split("?", 1)[0].split("#", 1)[0] if not spec.startswith("#") else spec
        if not spec:
            return set()
        directory = posixpath.dirname(importer)
        jest = self._jest_for(directory)
        if jest:
            for pattern, targets in jest.mappers:
                m = pattern.search(spec)
                if not m:
                    continue
                for target in targets:
                    mapped = _substitute(target, m).replace("<rootDir>", "/" + jest.root_dir)
                    if mapped.startswith("/"):
                        found = self._path(_clean(mapped.lstrip("/")))
                    else:
                        found = self._bare(mapped, directory)
                    if found:
                        return found
                return set()  # mapped to something outside the checkout (a stub package)
            # no pattern matched: resolve as usual
        if spec.startswith(("./", "../")) or spec in (".", ".."):
            return self._path(_clean(posixpath.join(directory, spec)))
        return self._bare(spec, directory)

    def _bare(self, spec: str, directory: str) -> set[str]:
        ts = self._tsconfig_for(directory)
        if ts is not None:
            for pattern, targets in sorted(ts.paths, key=lambda p: -len(p[0].split("*")[0])):
                star = _match_star(pattern, spec)
                if star is None:
                    continue
                base = ts.base_url if ts.base_url is not None else ts.paths_dir
                for target in targets:
                    path = _clean(posixpath.join(base, target.replace("*", star, 1)))
                    found = self._path(path) if path is not None else set()
                    if found:
                        return found
            if ts.base_url is not None:
                path = _clean(posixpath.join(ts.base_url, spec))
                found = self._path(path) if path is not None else set()
                if found:
                    return found
        name, sub = _package_name(spec)
        package = self.packages.get(name)
        if package is not None:
            return self._package(package, sub)
        return set()  # an external package: node_modules is never read

    def _package(self, package: Package, sub: str) -> set[str]:
        manifest_path = posixpath.join(package.directory, "package.json")
        entries: list[str] = []
        exports = package.manifest.get("exports")
        key = "." if not sub else "./" + sub
        if isinstance(exports, str) and not sub:
            entries.append(exports)
        elif isinstance(exports, dict):
            entry = exports.get(key)
            if isinstance(entry, dict):
                conditions = ("source", "import", "require", "default", "types")
                entries += [str(entry[c]) for c in conditions if isinstance(entry.get(c), str)]
            elif isinstance(entry, str):
                entries.append(entry)
        if not sub:
            entries += [str(package.manifest[k]) for k in ("source", "module", "main", "types")
                        if isinstance(package.manifest.get(k), str)]  # fmt: skip
        for entry in entries:
            path = _clean(posixpath.join(package.directory, entry))
            found = self._path(path) if path is not None else set()
            if not found and path is not None:
                found = self._path(_source_of(path, package.directory))
            if found:
                return found | {manifest_path}
        fallbacks = [
            posixpath.join(package.directory, "src", sub),
            posixpath.join(package.directory, sub),
        ]
        if not sub:
            fallbacks = [posixpath.join(package.directory, "src", "index"),
                         posixpath.join(package.directory, "index")]  # fmt: skip
        for fallback in fallbacks:
            found = self._path(_clean(fallback))
            if found:
                return found | {manifest_path}
        return {manifest_path}

    # --- bounded non-literal imports (context modules) ---

    def context(self, context: ContextImport, importer: str) -> set[str] | None:
        """Every file of the checkout a bounded import from ``importer`` could load, at any
        depth below its static directory (as bundlers' context modules include), or None
        when siftwise can't bound it (the importer is then dynamic): no static directory, a
        directory of just ``./`` or ``../``, a bare package, one outside the checkout or not
        in it, an alias siftwise can't resolve to one directory, no file matching, or more
        than ``MAX_CONTEXT_FILES`` files."""
        directory = posixpath.dirname(importer)
        found: set[str] = set()
        if context.kind == "prefix":
            head, slash, start = context.pattern.rpartition("/")
            if not slash or (not start and all(p in (".", "..") for p in head.split("/"))):
                return None  # `${x}`, 'pkg' + x, `./${x}`, '../' + x: anything may follow
            bases = self._context_dirs(head, directory)
            if not bases:
                return None
            suffix = context.suffix.split("?", 1)[0].split("#", 1)[0]
            for base in bases:
                found.update(f for f in self._under(base)
                             if _bounded_match(f[len(base) + 1 :], start, suffix))  # fmt: skip
        elif context.kind == "glob":
            pattern = context.pattern
            if not pattern.startswith(("./", "../")) or any(c in pattern for c in "{}[]()!+@"):
                return None  # root-relative, an alias (from the Vite config) or extglobs
            path = _clean(posixpath.join(directory, pattern))
            if path is None:
                return None
            static = []
            for part in path.split("/"):
                if "*" in part or "?" in part:
                    break
                static.append(part)
            base = "/".join(static[:-1] if len(static) == len(path.split("/")) else static)
            if not base or base not in self._all_dirs:
                return None
            found = {f for f in self._under(base) if glob_match(f, path)}
        elif context.kind == "context":
            if not context.pattern.startswith((".", "/")) or context.pattern.startswith("/"):
                return None  # webpack's own aliases can't be read
            base_dir = _clean(posixpath.join(directory, context.pattern))
            if not base_dir or base_dir not in self._all_dirs:
                return None
            regex = _js_regex(context.regex)
            for f in self._under(base_dir):
                rel = f[len(base_dir) + 1 :]
                if not context.recursive and "/" in rel:
                    continue
                if regex is None or regex.search("./" + rel):
                    found.add(f)
        else:
            return None
        if not found or len(found) > MAX_CONTEXT_FILES:
            return None
        return found

    def _context_dirs(self, head: str, directory: str) -> list[str]:
        """The checkout directories a bounded import's static directory names: relative, or
        through tsconfig ``paths``/``baseUrl``; [] when it can't be told (a bare package, a
        Jest moduleNameMapper match, not a directory of the checkout, the repo root)."""
        if head.startswith(("./", "../")) or head in (".", ".."):
            candidates = [_clean(posixpath.join(directory, head))]
        else:
            jest = self._jest_for(directory)
            if jest and any(p.search(head + "/") for p, _ in jest.mappers):
                return []  # the mapper decides where it goes: can't bound it here
            candidates = []
            ts = self._tsconfig_for(directory)
            if ts is not None:
                for pattern, targets in sorted(ts.paths, key=lambda p: -len(p[0].split("*")[0])):
                    star = _match_star(pattern, head + "/") if "*" in pattern else (
                        "" if pattern == head else None)  # fmt: skip
                    if star is None:
                        continue
                    base = ts.base_url if ts.base_url is not None else ts.paths_dir
                    # TypeScript tries every target in turn: a file may come from any of them.
                    candidates = [_clean(posixpath.join(base, t.replace("*", star, 1)))
                                  for t in targets]  # fmt: skip
                    break
                if not candidates and ts.base_url is not None:
                    candidates = [_clean(posixpath.join(ts.base_url, head))]
        dirs = [d.rstrip("/") for d in candidates if d]
        if not dirs or any(d not in self._all_dirs for d in dirs):
            return []
        return dirs

    @property
    def _all_dirs(self) -> set[str]:
        if self._every_dir is None:
            found: set[str] = set()
            for f in self.files:
                d = posixpath.dirname(f)
                while d and d not in found:
                    found.add(d)
                    d = posixpath.dirname(d)
            self._every_dir = found
        return self._every_dir

    def _under(self, base: str) -> list[str]:
        """The checkout's files below ``base`` (at any depth)."""
        if self._sorted is None:
            self._sorted = sorted(self.files)
        start = bisect.bisect_left(self._sorted, base + "/")
        end = bisect.bisect_left(self._sorted, base + "0")  # "0" sorts right after "/"
        return self._sorted[start:end]

    def _path(self, path: str | None) -> set[str]:
        """The file a path names: exact, TS ESM's emitted-name mapping, an extension, an
        index file."""
        if path is None:
            return set()
        stem, ext = posixpath.splitext(path)
        for source_ext in _EMITTED.get(ext, ()):
            if stem + source_ext in self.files:
                return {stem + source_ext}
        if path in self.files:
            return {path}
        for candidate_ext in EXTENSIONS:
            if path + candidate_ext in self.files:
                return {path + candidate_ext}
        if path in self._dirs or not path:
            for candidate_ext in EXTENSIONS:
                index = (
                    posixpath.join(path, "index" + candidate_ext)
                    if path
                    else "index" + candidate_ext
                )
                if index in self.files:
                    return {index}
        return set()


def _bounded_match(rest: str, start: str, suffix: str) -> bool:
    """Whether a file at ``rest`` below a bounded import's directory can be what it loads:
    it starts with the import's static name start and ends with its static suffix, as Node
    and TypeScript resolve it (an extension added, ``/index.*``, TS ESM's ``.js`` -> ``.ts``)."""
    if not rest.startswith(start):
        return False
    tail = rest[len(start) :]
    if not suffix or tail.endswith(suffix):
        return True
    if any(tail.endswith(suffix + ext) or tail.endswith(f"{suffix}/index{ext}")
           for ext in EXTENSIONS):  # fmt: skip
        return True
    dot = suffix.rfind(".")  # not posixpath.splitext: it reads ".js" as a name, no extension
    has_ext = dot >= 0 and "/" not in suffix[dot:]
    stem, ext = (suffix[:dot], suffix[dot:]) if has_ext else (suffix, "")
    return any(tail.endswith(stem + source) for source in _EMITTED.get(ext, ()))


_JS_REGEX = re.compile(r"/(.*)/([a-z]*)", re.DOTALL)


def _js_regex(literal: str | None) -> re.Pattern[str] | None:
    r"""A JS regex literal (``/\.js$/i``) as a Python pattern; None (every file: never fewer)
    for none, unknown flags or a pattern Python can't compile."""
    m = _JS_REGEX.fullmatch(literal or "")
    if not m or set(m.group(2)) - set("gimsuy"):
        return None
    flags = (re.IGNORECASE if "i" in m.group(2) else 0) | (re.DOTALL if "s" in m.group(2) else 0)
    flags |= re.MULTILINE if "m" in m.group(2) else 0
    try:
        return re.compile(m.group(1), flags)
    except re.error:
        return None


def _clean(path: str) -> str | None:
    """A normalized repo-relative path, or None outside the checkout."""
    cleaned = posixpath.normpath(path)
    if cleaned == ".":
        return ""
    if cleaned.startswith(("../", "/")) or cleaned == "..":
        return None
    return cleaned


def _match_star(pattern: str, spec: str) -> str | None:
    """What ``*`` stands for when a tsconfig ``paths`` pattern matches ``spec`` (or "")."""
    if "*" not in pattern:
        return "" if pattern == spec else None
    prefix, _, suffix = pattern.partition("*")
    if spec.startswith(prefix) and spec.endswith(suffix) and len(spec) >= len(prefix) + len(suffix):
        return spec[len(prefix) : len(spec) - len(suffix)]
    return None


def _substitute(target: str, match: re.Match[str]) -> str:
    """A Jest moduleNameMapper target with ``$1``... filled in."""

    def group(m: re.Match[str]) -> str:
        n = int(m.group(1))
        return (match.group(n) or "") if n <= (match.re.groups or 0) else ""

    return re.sub(r"\$(\d+)", group, target)


def _package_name(spec: str) -> tuple[str, str]:
    """``@s/pkg/sub/x`` -> (``@s/pkg``, ``sub/x``); ``pkg/sub`` -> (``pkg``, ``sub``)."""
    parts = spec.split("/")
    count = 2 if spec.startswith("@") else 1
    return "/".join(parts[:count]), "/".join(parts[count:])


def _source_of(path: str, package_dir: str) -> str:
    """A built entry point's source: ``pkg/dist/index.js`` -> ``pkg/src/index``."""
    rest = path[len(package_dir) :].lstrip("/") if package_dir else path
    first, _, tail = rest.partition("/")
    if first in ("dist", "lib", "build", "out", "esm", "cjs") and tail:
        tail = posixpath.splitext(tail)[0]
        if tail.endswith(".d"):
            tail = tail[:-2]
        return posixpath.join(package_dir, "src", tail)
    return path


def _pnpm_packages(path: Path) -> list[str]:
    """The ``packages:`` globs of a pnpm-workspace.yaml (a flat list; no YAML library)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    found, inside = [], False
    for line in lines:
        stripped = line.split("#", 1)[0].rstrip()
        if not stripped:
            continue
        if not line.startswith((" ", "\t", "-")):
            inside = stripped.startswith("packages:")
            continue
        if inside and stripped.lstrip().startswith("-"):
            found.append(stripped.lstrip()[1:].strip().strip("'\""))
    return found
