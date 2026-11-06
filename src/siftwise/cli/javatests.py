"""JUnit 5 test methods from Java source, for re-runs: which method a report's test name is.

Gradle's JUnit XML names a test by its display name (``@DisplayName("adds up the total")``,
or a parameterized invocation's ``[1] 1`` / ``1 plus 2``), and Surefire's phrased reporter
(``usePhrasedTestCaseMethodName``) does too, with the class's display name as the classname.
Maven and Gradle filter by method (``-Dtest=a.FooTest#total``, ``--tests a.FooTest.total``),
so a re-run needs the method that carries the name. Read from the class's source:

* a method whose ``@DisplayName`` is the name (a string literal, or literals joined by +);
* a ``@ParameterizedTest`` / ``@RepeatedTest`` whose invocation names (its ``name`` template,
  else JUnit's default) produce it, alone (Gradle) or after the method's own display name
  (Surefire's phrased names);
* the class: its fully qualified name (``a.FooTest$Nested`` for a nested class), or a phrased
  classname (the class's display names, outer first, joined by spaces).

Never a guess: a name two methods could carry, a method whose name siftwise can't read (a
constant, a ``@DisplayNameGeneration``, dynamic tests) or two source files that disagree give
a reason instead. Pure: the caller reads the files.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from siftwise.cli.javagraph import Token, tokenize

# A method's or template's text siftwise can't read (a constant, a generator).
UNKNOWN = "\0unknown"
_CLASS_WORDS = frozenset({"class", "interface", "enum", "record"})
_NOT_METHODS = frozenset({
    "if", "for", "while", "switch", "catch", "synchronized", "return", "new", "throw", "else",
    "try", "do", "assert", "super", "this",
})  # fmt: skip
# JUnit's default invocation names (5.11: "[{index}] {argumentSetNameOrArgumentsWithNames}").
_DEFAULT_PARAMETERIZED = "[{index}] {arguments}"
_DEFAULT_REPEATED = "repetition {currentRepetition} of {totalRepetitions}"
_TEMPLATED = {"ParameterizedTest": _DEFAULT_PARAMETERIZED, "RepeatedTest": _DEFAULT_REPEATED}
# Tests whose invocations are named at run time (DynamicTest.dynamicTest("...")).
_DYNAMIC = frozenset({"TestFactory", "TestTemplate"})
_GENERATORS = frozenset({"DisplayNameGeneration", "IndicativeSentencesGeneration"})
_COUNTERS = frozenset({"index", "currentRepetition", "totalRepetitions"})
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "s": " ", "0": "\0"}


@dataclass
class JavaMethod:
    name: str
    display: str | None = None  # @DisplayName, UNKNOWN if it isn't a literal
    template: str | None = None  # invocation names of a parameterized/repeated test


@dataclass
class JavaClass:
    qualified: str  # a.FooTest, a.FooTest$Nested
    phrased: str  # display names, outer first, joined by spaces
    generated: bool  # a @DisplayNameGeneration applies: undecorated names are unknown
    methods: list[JavaMethod] = field(default_factory=list)
    extends: bool = False  # it may inherit test methods


def parse_test_classes(source: str) -> list[JavaClass]:
    """Every class in a Java file (nested ones too) with its methods' names."""
    tokens = tokenize(source)
    package = _package(tokens)
    classes: list[JavaClass] = []
    stack: list[tuple[JavaClass, int]] = []  # (class, its body's brace depth)
    pending: list[tuple[str, list[Token]]] = []  # annotations before a declaration
    depth = paren = 0
    i, n = 0, len(tokens)
    while i < n:
        t = tokens[i]
        body = bool(stack) and depth == stack[-1][1] and paren == 0
        top = not stack and paren == 0
        if t.value == "@" and t.kind == "punct" and (body or top):
            if i + 1 < n and tokens[i + 1].value == "interface":
                i += 1  # @interface: an annotation type, declared like a class
                continue
            name, i = _qualified(tokens, i + 1)
            args: list[Token] = []
            if i < n and tokens[i].value == "(":
                args, i = _group(tokens, i)
            pending.append((name.rsplit(".", 1)[-1], args))
            continue
        if (
            t.kind == "id"
            and t.value in _CLASS_WORDS
            and (body or top)
            and i + 1 < n
            and tokens[i + 1].kind == "id"
            and (i == 0 or tokens[i - 1].value != ".")
        ):
            outer = stack[-1][0] if stack else None
            simple = tokens[i + 1].value
            qualified = (f"{outer.qualified}${simple}" if outer
                         else f"{package}.{simple}" if package else simple)  # fmt: skip
            display = _display(pending)
            own = simple if display is None or display == UNKNOWN else display
            names = {a for a, _ in pending}
            cls = JavaClass(
                qualified,
                f"{outer.phrased} {own}" if outer else own,
                bool(names & _GENERATORS) or (outer.generated if outer else False),
            )
            if display == UNKNOWN:
                cls.phrased = UNKNOWN
            pending = []
            j = i + 2
            while j < n and tokens[j].value not in ("{", ";"):
                if tokens[j].value == "extends":
                    cls.extends = True
                j += 1
            if j < n and tokens[j].value == "{":
                classes.append(cls)
                depth += 1
                stack.append((cls, depth))
            i = j + 1
            continue
        if (
            body
            and t.kind == "id"
            and t.value not in _NOT_METHODS
            and i + 1 < n
            and tokens[i + 1].value == "("
            and tokens[i - 1].value not in (".", "new")
        ):
            stack[-1][0].methods.append(_method(t.value, pending, stack[-1][0].generated))
            pending = []
        elif t.value == "(":
            paren += 1
        elif t.value == ")":
            paren = max(paren - 1, 0)
        elif t.value == "{":
            if body:
                pending = []
            depth += 1
        elif t.value == "}":
            if stack and depth == stack[-1][1]:
                stack.pop()
            depth = max(depth - 1, 0)
        elif t.value in (";", "=") and body:
            pending = []
        i += 1
    return classes


def find_method(
    classes: Sequence[JavaClass], classname: str, name: str
) -> tuple[str, str] | str | None:
    """(class to filter by, method) for a reported test, a reason it can't be told, or None
    when the source says nothing (no such class: the caller falls back to the name itself)."""
    if _is_fqn(classname):
        found = [c for c in classes if c.qualified == classname]
    else:
        found = [c for c in classes if c.phrased == classname]
    if not found:
        return None
    if len(found) > 1:
        return f"{len(found)} classes are named {classname!r}"
    cls = found[0]
    exact = [m for m in cls.methods if m.display == name]
    # Names siftwise can't read, and dynamic tests (any name): they may carry this one too.
    unknown = [m for m in cls.methods if UNKNOWN in (m.display, m.template)]
    plain = _plain_method(name)
    if plain is not None and any(m.name == plain for m in cls.methods) and not exact:
        return cls.qualified, plain  # the report names the method itself
    if len(exact) == 1 and not unknown:
        return cls.qualified, exact[0].name
    if exact or unknown:
        names = sorted({m.name for m in exact + unknown})
        return (f"{cls.qualified}: more than one method may be named {name!r} "
                f"({', '.join(names)})")  # fmt: skip
    invoked = [m for m in cls.methods if m.template is not None and _invokes(m, name)]
    if len(invoked) == 1 and invoked[0].template != UNKNOWN:
        return cls.qualified, invoked[0].name
    if invoked:
        names = sorted({m.name for m in invoked})
        return (f"{cls.qualified}: {name!r} may be an invocation of more than one method "
                f"({', '.join(names)})")  # fmt: skip
    if plain is not None:
        return None  # an inherited method, or one siftwise didn't read: the name decides
    return f"no method of {cls.qualified} is named {name!r} (display names: @DisplayName)"


def outer_class(classname: str) -> str | None:
    """The top-level class of a fully qualified name (``a.Foo`` for ``a.Foo$Bar``), or None
    for a phrased classname."""
    return classname.split("$", 1)[0] if _is_fqn(classname) else None


# --- helpers ---------------------------------------------------------------------------------

_FQN = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\Z", re.ASCII)
_PLAIN = re.compile(r"([A-Za-z_$][\w$]*)(?:\([^()]*\))?(?:\[[^\[\]]*\])?\Z", re.ASCII)


def _is_fqn(classname: str) -> bool:
    return bool(_FQN.match(classname))


def _plain_method(name: str) -> str | None:
    m = _PLAIN.match(name)
    return m[1] if m else None


def _package(tokens: Sequence[Token]) -> str:
    for k, t in enumerate(tokens):
        if t.kind == "id" and t.value == "package":
            name, _ = _qualified(tokens, k + 1)
            return name
        if t.kind == "id" and t.value in _CLASS_WORDS | {"import"}:
            break
    return ""


def _qualified(tokens: Sequence[Token], i: int) -> tuple[str, int]:
    parts = []
    while i < len(tokens) and tokens[i].kind == "id":
        parts.append(tokens[i].value)
        if i + 2 < len(tokens) and tokens[i + 1].value == "." and tokens[i + 2].kind == "id":
            i += 2
        else:
            i += 1
            break
    return ".".join(parts), i


def _group(tokens: Sequence[Token], i: int) -> tuple[list[Token], int]:
    """The tokens inside the parentheses at ``i``, and the index after them."""
    level, start = 0, i
    while i < len(tokens):
        if tokens[i].value == "(":
            level += 1
        elif tokens[i].value == ")":
            level -= 1
            if level == 0:
                return list(tokens[start + 1 : i]), i + 1
        i += 1
    return list(tokens[start + 1 :]), i


def _string(args: Sequence[Token], key: str) -> str | None:
    """An annotation argument's string value (``"a"``, ``key = "a"``, ``"a" + "b"``), None
    without one, UNKNOWN if it isn't made of literals."""
    values: list[Token] = []
    k = 0
    while k < len(args):
        if args[k].kind == "id" and k + 1 < len(args) and args[k + 1].value == "=":
            end = k + 2
            while end < len(args) and args[end].value != ",":
                end += 1
            if args[k].value == key:
                values = list(args[k + 2 : end])
            k = end + 1
            continue
        if key == "value" and k == 0:
            end = 0
            while end < len(args) and args[end].value != ",":
                end += 1
            values = list(args[:end])
            k = end
            continue
        k += 1
    if not values:
        return None
    parts = values[::2]
    if all(p.kind == "str" for p in parts) and all(s.value == "+" for s in values[1::2]):
        return " ".join("".join(_unescape(p.value) for p in parts).split())
    return UNKNOWN


def _unescape(text: str) -> str:
    return re.sub(r"\\(u+[0-9a-fA-F]{4}|.)", lambda m: _escape(m[1]), text)


def _escape(code: str) -> str:
    if code.startswith("u"):
        return chr(int(code.lstrip("u"), 16))
    return _ESCAPES.get(code, code)


def _display(annotations: Sequence[tuple[str, list[Token]]]) -> str | None:
    for name, args in annotations:
        if name == "DisplayName":
            return _string(args, "value") or UNKNOWN
    return None


def _method(name: str, annotations: Sequence[tuple[str, list[Token]]], generated: bool
            ) -> JavaMethod:  # fmt: skip
    display = _display(annotations)
    names = {a for a, _ in annotations}
    if display is None and generated and names & {"Test", *_TEMPLATED, *_DYNAMIC}:
        display = UNKNOWN  # a generator names it
    method = JavaMethod(name, display)
    for annotation, args in annotations:
        if annotation in _TEMPLATED:
            template = _string(args, "name")
            method.template = _TEMPLATED[annotation] if template is None else template
        elif annotation in _DYNAMIC:
            method.template = UNKNOWN
    return method


def _invokes(method: JavaMethod, name: str) -> bool:
    """Whether ``name`` may be one of the method's invocations: the invocation's name alone
    (Gradle), or after the method's display name (Surefire's phrased names)."""
    if method.template == UNKNOWN:
        return True
    assert method.template is not None
    invocation = _template_regex(method.template)
    if re.fullmatch(invocation, name):
        return True
    own = method.display if method.display not in (None, UNKNOWN) else None
    if own:
        return bool(re.fullmatch(re.escape(own) + "(?: " + invocation + ")?", name))
    # Surefire's phrased name of an undecorated one: "param(int)[1] 1".
    return bool(re.fullmatch(re.escape(method.name) + r"\([^()]*\)\[\d+\](?: .*)?", name))


def _template_regex(template: str) -> str:
    """A JUnit name template (``{index}``, ``{0}``, ``{arguments}``...) as a regex."""
    out: list[str] = []
    for part in re.split(r"(\{[^{}]*\})", " ".join(template.split())):
        if part.startswith("{") and part.endswith("}"):
            out.append(r"\d+" if part[1:-1] in _COUNTERS else ".*?")
        else:
            out.append(re.escape(part).replace(r"\ ", " ?").replace(" ", " ?"))
    return "".join(out)
