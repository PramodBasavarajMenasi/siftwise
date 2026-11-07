"""Java builds and a static Java dependency graph, for selecting JUnit test classes.

``siftwise select`` reads the build at the repo root and sends it with ``/select`` as ``java``
(``JavaChange.payload``): the build tool, whether a wrapper is there, its modules and their
test classes, so the server can run selected classes per module
(``./mvnw -B -pl <module> -am test -Dtest=...``, ``./gradlew :<module>:test --tests ...``).
When a ``.java`` file changed it also builds the dependency graph, and every test class that
uses a changed class goes into ``affected_files`` (test file -> changed files), with the
nearest change, the file on the way and the depth in ``java``.

No JVM, Maven or Gradle runs: the build files are read as text.

* **Maven:** ``pom.xml`` and its ``<modules>`` (profiles' too), recursively.
* **Gradle:** ``settings.gradle[.kts]``'s ``include`` of string literals; a single project
  without one.
* Each module's sources are the conventional ``src/main/java`` and ``src/test/java``. A
  module whose build sets other source directories (``<sourceDirectory>``, ``sourceSets``),
  uses annotation processors (generated sources), or has Kotlin, Groovy or Scala sources is
  *not analysed*: a change to its Java sources runs the full suite. Lombok is the exception:
  it only adds members to the annotated class, never new types other code depends on.
* A build siftwise can't read (an ``include`` of anything but literals, ``includeBuild``,
  ``projectDir``, Android, both Maven and Gradle with no wrapper to decide) is *unsupported*:
  every Java change runs the full suite.

The graph (``JavaGraph``): a tokenizer skips comments, strings, text blocks and char literals
(``tokenize``); each file's ``package``, imports (single-type, static, on-demand), top-level
types, identifiers and dotted names are read (``parse_java``) and resolved to the repo's
classes: imports, same-package simple names, on-demand imports' simple names,
fully-qualified names in code, and string literals naming a class (``Class.forName``). An
import of a class the sources don't declare in one of their packages (generated code) makes
the file *dynamic*: tests that reach it run for any Java change, never counted as covering it.
Reflection by computed name (Spring component scanning, ``ServiceLoader``) can't be seen:
declare such dependencies with ``[[depends]]`` rules.
"""

from __future__ import annotations

import os
import posixpath
import re
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element

from defusedxml import ElementTree

from siftwise.core.selector import java_class_of

MAVEN = "maven"
GRADLE = "gradle"
MAIN_JAVA = "src/main/java"
TEST_JAVA = "src/test/java"
OTHER_LANGUAGES = ("kotlin", "groovy", "scala")
GRADLE_BUILD_FILES = ("build.gradle", "build.gradle.kts")
GRADLE_SETTINGS = ("settings.gradle", "settings.gradle.kts")
# Annotation processors that generate types other code uses (by artifact ID). Lombok isn't
# one: it adds members to the annotated class only.
PROCESSOR_ARTIFACTS = (
    "mapstruct-processor", "dagger-compiler", "hilt-compiler", "auto-value", "auto-factory",
    "immutables", "querydsl-apt", "hibernate-jpamodelgen", "jpamodelgen", "micronaut-inject-java",
    "record-builder-processor", "jmh-generator-annprocess", "picocli-codegen",
    "avaje-inject-generator", "dagger-android-processor", "room-compiler", "auto-service",
)  # fmt: skip
SKIP_DIRS = frozenset({"target", "build", "out", "node_modules", "bin"})
MAX_NOTES = 5


# --- the build model ------------------------------------------------------------------------


@dataclass(frozen=True)
class JavaModule:
    path: str  # repo-relative directory; "" = the root project
    name: str  # Maven: the -pl argument (the directory); Gradle: the project path (":a:b")
    unanalysed: tuple[str, ...] = ()  # why its Java sources aren't analysed (full suite)

    @property
    def main_root(self) -> str:
        return posixpath.join(self.path, MAIN_JAVA) if self.path else MAIN_JAVA

    @property
    def test_root(self) -> str:
        return posixpath.join(self.path, TEST_JAVA) if self.path else TEST_JAVA


@dataclass(frozen=True)
class JavaBuild:
    tool: str  # MAVEN or GRADLE
    wrapper: bool
    modules: tuple[JavaModule, ...] = ()
    unsupported: str = ""  # why the build couldn't be read: every Java change runs everything
    # For scoping a module's build file change (``build_scopes``): module path -> the modules
    # that depend on it directly (Maven: their POM names its artifactId; Gradle: they name it
    # in project(':x')), and module path -> why a change to its build file can't be scoped.
    # ``scopes_known`` False (a build whose dependencies can't be read, or an older eval
    # snapshot): no build file change is scoped.
    dependents: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    unscoped: Mapping[str, str] = field(default_factory=dict)
    scopes_known: bool = False


def detect_build(root: Path) -> JavaBuild | None:
    """The checkout's Java build, or None without one."""
    has_maven = (root / "pom.xml").is_file()
    has_gradle = any((root / f).is_file() for f in (*GRADLE_SETTINGS, *GRADLE_BUILD_FILES))
    if not (has_maven or has_gradle):
        return None
    mvnw, gradlew = _wrapper(root, "mvnw"), _wrapper(root, "gradlew")
    if has_maven and has_gradle and mvnw == gradlew:
        return JavaBuild(MAVEN, mvnw, unsupported="both a Maven and a Gradle build, and no "
                         "wrapper tells which one CI uses")  # fmt: skip
    if has_maven and not (has_gradle and gradlew):
        return _maven_build(root, mvnw)
    return _gradle_build(root, gradlew)


def _wrapper(root: Path, name: str) -> bool:
    return any((root / f).is_file() for f in (name, f"{name}.cmd", f"{name}.bat"))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: Element, *names: str) -> Element | None:
    """A descendant by local tag names (the POM namespace is ignored)."""
    current: Element | None = element
    for name in names:
        if current is None:
            return None
        current = next((c for c in current if _local(str(c.tag)) == name), None)
    return current


def _children(element: Element | None, name: str) -> list[Element]:
    return [] if element is None else [c for c in element if _local(str(c.tag)) == name]


_DEFAULT_SOURCE_DIRS = {
    name: (default, f"${{project.basedir}}/{default}", f"${{basedir}}/{default}")
    for name, default in (("sourceDirectory", MAIN_JAVA), ("testSourceDirectory", TEST_JAVA))
}


def _maven_build(root: Path, wrapper: bool) -> JavaBuild:
    poms: dict[str, tuple[Element, str]] = {}  # directory -> (project, text)

    def pom(directory: str) -> tuple[Element, str] | None:
        if directory not in poms:
            path = root / directory / "pom.xml" if directory else root / "pom.xml"
            text = path.read_text(encoding="utf-8")
            poms[directory] = (ElementTree.fromstring(text), text)
        return poms[directory]

    flags_memo: dict[str, list[str]] = {}

    def flags_of(directory: str, seen: frozenset[str] = frozenset()) -> list[str]:
        """A POM's own flags and those it inherits from its <parent> in the checkout (its
        relativePath, by default ../pom.xml): plugin configuration is inherited."""
        if directory in flags_memo:
            return flags_memo[directory]
        found = pom(directory)
        assert found is not None
        project, text = found
        flags = _pom_flags(project, text)
        parent_dir = _parent_dir(project, directory)
        if (
            parent_dir is not None
            and parent_dir not in seen
            and (root / parent_dir / "pom.xml").is_file()
        ):
            flags += [f for f in flags_of(parent_dir, seen | {directory}) if f not in flags]
        flags_memo[directory] = flags
        return flags

    modules: list[JavaModule] = []
    queue = deque([""])
    seen: set[str] = set()
    try:
        while queue:
            directory = queue.popleft()
            if directory in seen:
                continue
            seen.add(directory)
            found = pom(directory)
            assert found is not None
            project, _ = found
            flags = flags_of(directory) + _language_dirs(root, directory)
            modules.append(JavaModule(directory, directory or ".", tuple(dict.fromkeys(flags))))
            entries = _children(_child(project, "modules"), "module")
            for profile in _children(_child(project, "profiles"), "profile"):
                entries += _children(_child(profile, "modules"), "module")
            for entry in entries:
                target = (entry.text or "").strip()
                if target.endswith(".xml"):
                    target = posixpath.dirname(target)
                child = posixpath.normpath(posixpath.join(directory, target)) if target else ""
                if child.startswith("..") or not (root / child / "pom.xml").is_file():
                    where = f"{directory or '.'}/pom.xml"
                    why = f"module {target!r} of {where} isn't in the checkout"
                    return JavaBuild(MAVEN, wrapper, unsupported=why)
                queue.append("" if child == "." else child)
    except (OSError, UnicodeDecodeError, ElementTree.ParseError) as exc:
        return JavaBuild(MAVEN, wrapper, unsupported=f"a pom.xml can't be read ({exc})")
    dependents, unscoped, known = _maven_scoping({d: poms[d] for d in seen})
    return JavaBuild(MAVEN, wrapper, tuple(sorted(modules, key=lambda m: m.path)),
                     dependents=dependents, unscoped=unscoped, scopes_known=known)  # fmt: skip


_ARTIFACT = re.compile(r"<artifactId>\s*([^<]*?)\s*</artifactId>")


def _maven_scoping(
    poms: Mapping[str, tuple[Element, str]],
) -> tuple[dict[str, tuple[str, ...]], dict[str, str], bool]:
    """(module -> modules naming its artifactId, module -> why its POM can't be scoped,
    whether dependencies could be read). Any ``<artifactId>`` in a POM counts (dependencies,
    plugins, profiles, exclusions): more dependents, never fewer. One built from a property
    (``${...}``) may name any module: nothing is scoped."""
    artifact: dict[str, str] = {}
    unscoped: dict[str, str] = {}
    refs: dict[str, set[str]] = {}
    for directory, (project, text) in poms.items():
        own = _child(project, "artifactId")
        name = (own.text or "").strip() if own is not None else ""
        # Versions a POM manages (a BOM's list) aren't dependencies: a module's POM change
        # doesn't change the BOM. A change to the managing POM itself is never scoped.
        used = re.sub(r"<dependencyManagement\b.*?</dependencyManagement>", "",
                      _xml_without_comments(text), flags=re.S)  # fmt: skip
        found = set(_ARTIFACT.findall(used))
        if any("${" in ref for ref in found):
            return {}, {}, False
        refs[directory] = found - {name}
        if not name or "${" in name:
            unscoped[directory] = "its pom.xml names no plain artifactId"
            continue
        artifact[directory] = name
        packaging = _child(project, "packaging")
        if not directory:
            unscoped[directory] = "the root pom.xml"
        elif (packaging is not None and (packaging.text or "").strip() == "pom") or (
            re.search(r"<modules\b", text)
        ):
            unscoped[directory] = "a parent or aggregator POM (packaging pom, <modules>)"
        elif re.search(r"<(dependencyManagement|pluginManagement)\b", text):
            unscoped[directory] = "its POM manages versions or plugins others may use"
    by_artifact: dict[str, list[str]] = defaultdict(list)
    for directory, name in artifact.items():
        by_artifact[name].append(directory)
    dependents: dict[str, set[str]] = defaultdict(set)
    for directory, (project, _) in poms.items():
        parent = _child(project, "parent", "artifactId")
        parent_name = (parent.text or "").strip() if parent is not None else ""
        for target in [*by_artifact.get(parent_name, ()), _parent_dir(project, directory)]:
            if target is not None and target in poms and target != directory:
                unscoped.setdefault(target, f"the parent POM of {directory or '.'}")
        for ref in refs[directory]:
            for target in by_artifact.get(ref, ()):
                if target != directory:
                    dependents[target].add(directory)
    return {m: tuple(sorted(d)) for m, d in dependents.items()}, unscoped, True


def _xml_without_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def _parent_dir(project: Element, directory: str) -> str | None:
    """The directory of a POM's <parent> (its relativePath, ../pom.xml by default), or None
    for none or one outside the checkout."""
    parent = _child(project, "parent")
    if parent is None:
        return None
    relative = _child(parent, "relativePath")
    if relative is not None and not (relative.text or "").strip():
        return None  # <relativePath/>: from a repository, not the checkout
    target = (relative.text or "").strip() if relative is not None else "../pom.xml"
    if target.endswith(".xml"):
        target = posixpath.dirname(target) or "."
    resolved = posixpath.normpath(posixpath.join(directory, target))
    if resolved.startswith(".."):
        return None
    return "" if resolved == "." else resolved


# Annotation processors that check code without generating any (javac plugins, linters):
# configuring only these keeps a module analysed. Any other configured processor may
# generate types other code uses.
CHECKERS = ("lombok", "error_prone", "errorprone", "nullaway", "checker", "jspecify")


def _pom_flags(project: Element, text: str) -> list[str]:
    flags = []
    build = _child(project, "build")
    for name, defaults in _DEFAULT_SOURCE_DIRS.items():
        configured = _child(build, name) if build is not None else None
        if configured is not None and (configured.text or "").strip().rstrip("/") not in defaults:
            flags.append(f"custom {name} {(configured.text or '').strip()!r}")
    if "build-helper-maven-plugin" in text:
        flags.append("generated or added sources (build-helper-maven-plugin)")
    paths = re.findall(r"<annotationProcessorPaths>(.*?)</annotationProcessorPaths>", text, re.S)
    processors = [
        artifact
        for block in paths
        for artifact in re.findall(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", block)
    ]
    processors += [
        name.strip()
        for block in re.findall(r"<annotationProcessors>(.*?)</annotationProcessors>", text, re.S)
        for name in re.findall(r"<annotationProcessor>([^<]+)</annotationProcessor>", block)
    ]
    generating = [a for a in processors if not a.startswith(CHECKERS) and "lombok" not in a]
    generating += [
        a for a in PROCESSOR_ARTIFACTS if f"<artifactId>{a}" in text and a not in generating
    ]
    if generating:
        flags.append(f"annotation processors ({', '.join(generating)}: generated sources "
                     "aren't analysed)")  # fmt: skip
    return flags


def _language_dirs(root: Path, directory: str) -> list[str]:
    base = root / directory if directory else root
    return [
        f"{language.capitalize()} sources (src/{kind}/{language} isn't analysed)"
        for language in OTHER_LANGUAGES
        for kind in ("main", "test")
        if (base / "src" / kind / language).is_dir()
    ]


_INCLUDE_WORD = re.compile(r"\binclude\b(?!Build|Flat)")
_INCLUDE_CALL = re.compile(
    r"""\binclude\s*\(?\s*((?:(['"])[^'"\n$]*\2\s*,\s*)*(['"])[^'"\n$]*\3)\s*\)?"""
)
_STRING = re.compile(r"""(['"])([^'"\n]*)\1""")


def _gradle_build(root: Path, wrapper: bool) -> JavaBuild:
    settings_name = next((f for f in GRADLE_SETTINGS if (root / f).is_file()), None)
    settings = _without_comments(_read(root / settings_name)) if settings_name else ""
    for construct, why in (
        ("includeBuild", "a composite build (includeBuild)"),
        ("includeFlat", "includeFlat (projects outside the checkout)"),
        ("projectDir", "projects in custom directories (projectDir)"),
    ):
        if construct in settings:
            return JavaBuild(GRADLE, wrapper, unsupported=f"{settings_name} uses {why}")
    calls = list(_INCLUDE_CALL.finditer(settings))
    if len(calls) != len(_INCLUDE_WORD.findall(settings)):
        return JavaBuild(GRADLE, wrapper, unsupported=f"{settings_name} includes projects by "
                         "something other than string literals")  # fmt: skip
    paths = [""]
    for call in calls:
        for _, name in _STRING.findall(call.group(1)):
            path = name.strip(":").replace(":", "/")
            if path and path not in paths:
                paths.append(path)
    shared = _gradle_shared(root)
    if "com.android" in shared:
        return JavaBuild(GRADLE, wrapper, unsupported="an Android build (its source sets aren't "
                         "the standard ones)")  # fmt: skip
    modules = []
    for path in paths:
        own = "".join(_read(root / path / name) for name in GRADLE_BUILD_FILES)
        if "com.android" in own:
            return JavaBuild(GRADLE, wrapper, unsupported=f"{path or '.'} is an Android project")
        text = _without_comments(own) + shared
        flags = []
        if re.search(r"\bsourceSets\b", text) and re.search(r"\bsrcDirs?\b|setSrcDirs", text):
            flags.append("custom source sets")
        processors = [
            line.strip()
            for line in text.splitlines()
            if re.search(r"\b(annotationProcessor|kapt|ksp)\b", line)
            and not any(checker in line for checker in CHECKERS)
        ]
        if processors:
            flags.append("annotation processors (generated sources aren't analysed)")
        if re.search(r"org\.jetbrains\.kotlin|kotlin\(\s*\"jvm\"", own):
            flags.append("the Kotlin plugin")
        flags += _language_dirs(root, path)
        name = ":" + path.replace("/", ":") if path else ""
        modules.append(JavaModule(path, name, tuple(dict.fromkeys(flags))))
    dependents, unscoped, known = _gradle_scoping(root, paths, shared)
    return JavaBuild(GRADLE, wrapper, tuple(sorted(modules, key=lambda m: m.path)),
                     dependents=dependents, unscoped=unscoped, scopes_known=known)  # fmt: skip


_PROJECT_REF = re.compile(r"""\bproject\s*\(\s*(?:path\s*[:=]\s*)?(['"])(:[^'"]*)\1""")
# A project reference siftwise can't read: typesafe accessors (projects.core), a non-literal.
_UNREAD_PROJECT = re.compile(
    r"""\bprojects\.[A-Za-z]|\bproject\s*\(\s*(?!(?:path\s*[:=]\s*)?['"])"""
)
# Build logic in a project's build file that configures other projects.
_CONFIGURES_OTHERS = re.compile(
    r"\b(subprojects|allprojects|rootProject|childProjects|evaluationDependsOn|configure)\b"
)


def _gradle_scoping(
    root: Path, paths: Sequence[str], shared: str
) -> tuple[dict[str, tuple[str, ...]], dict[str, str], bool]:
    """Like ``_maven_scoping``, from ``project(':x')`` references in each project's build
    file; references in the root build file or buildSrc (``shared``) count for every project."""
    texts = {
        p: _without_comments("".join(_read(root / p / n) for n in GRADLE_BUILD_FILES))
        for p in paths
    }
    if any(_UNREAD_PROJECT.search(t) for t in [*texts.values(), shared]):
        return {}, {}, False

    def targets(text: str) -> set[str]:
        return {m.group(2).strip(":").replace(":", "/") for m in _PROJECT_REF.finditer(text)}

    everyone = targets(shared)
    dependents: dict[str, set[str]] = defaultdict(set)
    unscoped: dict[str, str] = {"": "the root build file"}
    for path, text in texts.items():
        if path and _CONFIGURES_OTHERS.search(text):
            unscoped[path] = "its build file configures other projects"
        for target in (targets(text) | everyone) - {path}:
            if target in texts:
                dependents[target].add(path)
    return {m: tuple(sorted(d)) for m, d in dependents.items()}, unscoped, True


def build_scopes(build: JavaBuild, changed: Iterable[str]) -> dict[str, list[str]]:
    """Changed module build file (a module's ``pom.xml`` or ``build.gradle[.kts]``, never the
    root's) -> the modules whose tests it can affect: its module and every module that
    depends on it, transitively. Left out (the full suite, as for any build file): the root,
    a module the build model doesn't place, an unscoped one (a parent POM, version or plugin
    management, configuring other projects), one whose scope holds a module with tests the
    class lists may miss (``_hides_tests``: other-language or custom test sources), and every
    one when the build's dependencies can't be read."""
    if not build.scopes_known or build.unsupported:
        return {}
    names = ("pom.xml",) if build.tool == MAVEN else GRADLE_BUILD_FILES
    modules = {m.path: m for m in build.modules}
    found: dict[str, list[str]] = {}
    for path in sorted(set(changed)):
        directory, base = posixpath.split(path)
        if base not in names or not directory or directory not in modules:
            continue
        if directory in build.unscoped:
            continue
        scope = {directory}
        queue = deque([directory])
        while queue:
            for user in build.dependents.get(queue.popleft(), ()):
                if user not in scope:
                    scope.add(user)
                    queue.append(user)
        if any(_hides_tests(modules[m]) for m in scope if m in modules):
            continue
        found[path] = sorted(scope)
    return found


def _hides_tests(module: JavaModule) -> bool:
    """Whether a module may have tests its ``src/test/java`` file names don't list: other
    languages' test sources, custom source directories or source sets, added sources. An
    annotation processor alone doesn't: it generates code, not test classes."""
    return any(not flag.startswith("annotation processors") for flag in module.unanalysed)


def _gradle_shared(root: Path) -> str:
    """Build logic every project gets: the root build file (``subprojects``/``allprojects``)
    and convention plugins in buildSrc."""
    parts = [_read(root / name) for name in GRADLE_BUILD_FILES]
    build_src = root / "buildSrc"
    if build_src.is_dir():
        for dirpath, dirnames, filenames in os.walk(build_src):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            parts += [
                _read(Path(dirpath) / f)
                for f in filenames
                if f.endswith((".gradle", ".gradle.kts", ".kt", ".groovy", ".java"))
            ]
    return _without_comments("\n".join(parts))


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except (OSError, UnicodeDecodeError):
        return ""


def _without_comments(text: str) -> str:
    """Groovy/Kotlin build text without // and /* */ comments (strings kept)."""
    return re.sub(r"""("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')|//[^\n]*|/\*.*?\*/""",
                  lambda m: m.group(1) or "", text, flags=re.S)  # fmt: skip


# --- the tokenizer and the parser -----------------------------------------------------------


_JAVA_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
    |(?P<line>//[^\n]*)
    |(?P<block>/\*.*?(?:\*/|\Z))
    |(?P<text>\"\"\"(?:[^\\]|\\.)*?(?:\"\"\"|\Z))
    |(?P<str>"(?:[^"\\\n]|\\.)*"?)
    |(?P<chr>'(?:[^'\\\n]|\\.)*'?)
    |(?P<num>\.?\d[\w.]*(?:[eEpP][+-]\d+)?)
    |(?P<id>[^\W\d]\w*)
    |(?P<punct>.)
    """,
    re.S | re.X,
)


@dataclass(frozen=True)
class Token:
    kind: str  # "id", "str" (a string's or text block's content), "punct"
    value: str


def tokenize(source: str) -> list[Token]:
    """Java tokens as far as dependencies need them: comments, whitespace, numbers and char
    literals dropped; strings and text blocks as one token each."""
    tokens = []
    for m in _JAVA_TOKEN.finditer(source):
        kind = m.lastgroup
        if kind in ("ws", "line", "block", "num", "chr"):
            continue
        value = m.group()
        if kind == "text":
            tokens.append(Token("str", value[3:-3]))
        elif kind == "str":
            tokens.append(Token("str", value[1:-1]))
        else:
            tokens.append(Token(kind or "punct", value))
    return tokens


@dataclass(frozen=True)
class JavaSource:
    package: str
    imports: tuple[tuple[str, bool, bool], ...]  # (name, static, on demand ".*")
    types: tuple[str, ...]  # top-level type names
    identifiers: frozenset[str]
    dotted: tuple[tuple[str, ...], ...]  # a.b.C.x chains in code
    strings: tuple[str, ...]  # string literals that look like class names


_TYPE_KEYWORDS = frozenset({"class", "interface", "enum", "record"})
_FQN = re.compile(r"[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)+")


def parse_java(source: str) -> JavaSource:
    tokens = tokenize(source)
    package = ""
    imports: list[tuple[str, bool, bool]] = []
    types: list[str] = []
    identifiers: set[str] = set()
    dotted: list[tuple[str, ...]] = []
    strings: list[str] = []
    depth = 0
    i, n = 0, len(tokens)
    while i < n:
        t = tokens[i]
        if t.kind == "punct":
            depth += 1 if t.value == "{" else -1 if t.value == "}" else 0
            i += 1
            continue
        if t.kind == "str":
            if _FQN.fullmatch(t.value):
                strings.append(t.value)
            i += 1
            continue
        if depth == 0 and t.value in ("package", "import"):
            j, static = i + 1, False
            if t.value == "import" and j < n and tokens[j].value == "static":
                static, j = True, j + 1
            parts: list[str] = []
            while j < n and tokens[j].value != ";":
                if tokens[j].kind == "id" or tokens[j].value == "*":
                    parts.append(tokens[j].value)
                j += 1
            if t.value == "package":
                package = ".".join(parts)
            elif parts:
                wildcard = parts[-1] == "*"
                imports.append((".".join(parts[:-1] if wildcard else parts), static, wildcard))
            i = j + 1
            continue
        if depth == 0 and t.value in _TYPE_KEYWORDS and i + 1 < n and tokens[i + 1].kind == "id":
            types.append(tokens[i + 1].value)
        identifiers.add(t.value)
        # a dotted chain: a . b . C ...
        j = i
        chain = [t.value]
        while j + 2 < n and tokens[j + 1].value == "." and tokens[j + 2].kind == "id":
            chain.append(tokens[j + 2].value)
            identifiers.add(tokens[j + 2].value)
            j += 2
        if len(chain) > 1:
            dotted.append(tuple(chain))
        i = j + 1
    return JavaSource(package, tuple(imports), tuple(types), frozenset(identifiers),
                      tuple(dotted), tuple(strings))  # fmt: skip


# --- the graph ------------------------------------------------------------------------------


# (module directory, class, is a test) of a Java file under src/main/java or src/test/java.
class_of = java_class_of


@dataclass(frozen=True)
class Nearest:
    module: str  # the nearest changed file the test uses
    via: str | None  # the file the test uses on the way; None: it uses it directly
    depth: int


@dataclass
class Notes:
    items: list[str] = field(default_factory=list)

    def add(self, note: str) -> None:
        if note not in self.items and len(self.items) < MAX_NOTES:
            self.items.append(note)


@dataclass(frozen=True)
class JavaGraph:
    edges: Mapping[str, frozenset[str]]  # file -> files it uses
    tests: frozenset[str]  # files under test roots
    dynamic_files: Mapping[str, str]  # file -> the generated class it imports
    notes: tuple[str, ...] = ()

    def affected(self, changed_files: Iterable[str]) -> tuple[
        dict[str, list[str]], dict[str, Nearest], dict[str, str]
    ]:  # fmt: skip
        """(test file -> changed files it uses, test file -> its nearest change, test file ->
        a dynamic file it reaches)."""
        users: dict[str, set[str]] = defaultdict(set)
        for file, used in self.edges.items():
            for target in used:
                users[target].add(file)
        reached: dict[str, set[str]] = defaultdict(set)
        nearest: dict[str, Nearest] = {}
        for changed in sorted(set(changed_files)):
            if changed not in users and changed not in self.edges:
                continue
            next_hop: dict[str, str] = {}
            depth = {changed: 0}
            queue = deque([changed])
            while queue:
                current = queue.popleft()
                for user in sorted(users.get(current, ())):
                    if user not in depth:
                        depth[user] = depth[current] + 1
                        next_hop[user] = current
                        queue.append(user)
            for file, d in depth.items():
                if not d or file not in self.tests:
                    continue
                reached[file].add(changed)
                hop = next_hop[file]
                candidate = Nearest(changed, None if hop == changed else hop, d)
                old = nearest.get(file)
                if old is None or (candidate.depth, candidate.module) < (old.depth, old.module):
                    nearest[file] = candidate
        dynamic: dict[str, str] = {}
        if self.dynamic_files:
            source = {f: f for f in self.dynamic_files}
            queue = deque(sorted(source))
            while queue:
                current = queue.popleft()
                for user in sorted(users.get(current, ())):
                    if user not in source:
                        source[user] = source[current]
                        queue.append(user)
            dynamic = {f: s for f, s in sorted(source.items()) if f in self.tests}
        files = {f: sorted(m) for f, m in sorted(reached.items())}
        return files, dict(sorted(nearest.items())), dynamic


def _java_files(root: Path, directory: str) -> list[str]:
    base = root / directory
    found: list[str] = []
    if not base.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        rel = Path(dirpath).relative_to(root).as_posix()
        found += [f"{rel}/{f}" for f in filenames if f.endswith(".java")]
    return sorted(found)


def build_java_graph(root: Path, build: JavaBuild, deleted: Iterable[str] = ()) -> JavaGraph:
    """The graph of every module's ``src/main/java`` and ``src/test/java``. ``deleted`` Java
    files still count as classes (named after their path), so their users are found."""
    files: list[str] = []
    for module in build.modules:
        files += _java_files(root, module.main_root) + _java_files(root, module.test_root)
    index: dict[str, str] = {}  # class -> file
    by_package: dict[str, dict[str, str]] = defaultdict(dict)  # package -> simple name -> file
    parsed: dict[str, JavaSource] = {}
    for file in files:
        try:
            source = parse_java((root / file).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        parsed[file] = source
        names = set(source.types)
        located = class_of(file)
        if located:  # the file's name is its public class's, by Java convention
            names.add(located[1].rsplit(".", 1)[-1])
        for name in names:
            fqn = f"{source.package}.{name}" if source.package else name
            index.setdefault(fqn, file)
            by_package[source.package].setdefault(name, file)
    for file in deleted:
        located = class_of(file)
        if located:
            package, _, name = located[1].rpartition(".")
            index.setdefault(located[1], file)
            by_package[package].setdefault(name, file)
    packages = set(by_package)
    notes = Notes()
    edges: dict[str, frozenset[str]] = {}
    dynamic: dict[str, str] = {}
    for file, source in parsed.items():
        targets: set[str] = set()
        on_demand: list[str] = []
        for name, static, wildcard in source.imports:
            if wildcard and not static and name not in index:
                on_demand.append(name)  # import a.b.*
                continue
            target = _resolve(name if wildcard or not static else name.rsplit(".", 1)[0], index)
            if target:
                targets.add(target)
            elif _in_repo_package(name, packages) and not static:
                dynamic.setdefault(file, name)  # a generated class, most likely
        same = by_package.get(source.package, {})
        for identifier in source.identifiers:
            if identifier in same:
                targets.add(same[identifier])
            for package in on_demand:
                found = by_package.get(package, {}).get(identifier)
                if found:
                    targets.add(found)
        for chain in source.dotted:
            target = _resolve(".".join(chain), index)
            if target:
                targets.add(target)
        for literal in source.strings:
            if literal in index:
                targets.add(index[literal])
        targets.discard(file)
        edges[file] = frozenset(targets)
    for file, name in sorted(dynamic.items())[:2]:
        notes.add(f"{file} imports {name}, which the sources don't declare (generated?): tests "
                  "that use it run for any Java change")  # fmt: skip
    tests = frozenset(f for f in files if (c := class_of(f)) is not None and c[2])
    return JavaGraph(edges, tests, dynamic, tuple(notes.items))


def _resolve(name: str, index: Mapping[str, str]) -> str | None:
    """The file of ``a.b.C`` or of the longest prefix of ``a.b.C.Inner.member`` that's a class."""
    parts = name.split(".")
    for end in range(len(parts), 0, -1):
        found = index.get(".".join(parts[:end]))
        if found:
            return found
    return None


def _in_repo_package(name: str, packages: set[str]) -> bool:
    package = name.rpartition(".")[0]
    return bool(package) and package in packages


# --- what /select gets ----------------------------------------------------------------------


@dataclass(frozen=True)
class JavaChange:
    build: JavaBuild
    test_classes: Mapping[str, Sequence[str]]  # module path -> classes under its test root
    files: dict[str, list[str]] = field(default_factory=dict)  # affected_files' Java part
    nearest: dict[str, Nearest] = field(default_factory=dict)
    dynamic: dict[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    # Changed module build file -> the modules to run (``build_scopes``).
    build_scopes: dict[str, list[str]] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        """``/select``'s ``java``."""
        return {
            "tool": self.build.tool,
            "wrapper": self.build.wrapper,
            "unsupported": self.build.unsupported,
            "modules": [
                {"path": m.path, "name": m.name, "unanalysed": list(m.unanalysed)}
                for m in self.build.modules
            ],
            "test_classes": {m: list(c) for m, c in self.test_classes.items()},
            "nearest": {
                test: {"module": n.module, "via": n.via, "depth": n.depth}
                for test, n in self.nearest.items()
            },
            "dynamic": dict(self.dynamic),
            "notes": list(self.notes),
            # Only when there is one: an older server rejects the key (and then all of
            # `java`), but it runs the full suite for any build file change anyway.
            **({"build_scopes": dict(self.build_scopes)} if self.build_scopes else {}),
        }


def java_change(
    root: Path, changed: Sequence[str], deleted: Sequence[str] = ()
) -> JavaChange | None:
    """The checkout's Java build and the change's effect on its test classes (the graph only
    when a ``.java`` file changed); None without a Java build. Raises OSError, ValueError."""
    build = detect_build(root)
    if build is None:
        return None
    test_classes = test_classes_of(root, build)
    if build.unsupported or not any(p.endswith(".java") for p in changed):
        return change_from(build, test_classes, None, changed)
    graph = build_java_graph(root, build, [p for p in deleted if p.endswith(".java")])
    return change_from(build, test_classes, graph, changed)


def test_classes_of(root: Path, build: JavaBuild) -> dict[str, list[str]]:
    """Module path -> the classes under its src/test/java (named after their files)."""
    found: dict[str, list[str]] = {}
    for module in build.modules:
        classes = [
            located[1]
            for file in _java_files(root, module.test_root)
            if (located := class_of(file)) is not None
        ]
        if classes:
            found[module.path] = classes
    return found


def change_from(
    build: JavaBuild,
    test_classes: Mapping[str, Sequence[str]],
    graph: JavaGraph | None,
    changed: Sequence[str],
) -> JavaChange:
    """What a change does to the test classes, given the build's graph (None: no Java file
    changed, so no graph is needed)."""
    scopes = build_scopes(build, changed)
    if graph is None or build.unsupported or not any(p.endswith(".java") for p in changed):
        return JavaChange(build, test_classes, build_scopes=scopes)
    files, nearest, dynamic = graph.affected(changed)
    return JavaChange(build, test_classes, files, nearest, dynamic, graph.notes, scopes)


def snapshot_json(
    build: JavaBuild, test_classes: Mapping[str, Sequence[str]], graph: JavaGraph | None
) -> dict[str, Any]:
    """A commit's build, test classes and graph, as JSON (the eval caches one per commit)."""
    return {
        "build": {
            "tool": build.tool,
            "wrapper": build.wrapper,
            "unsupported": build.unsupported,
            "modules": [[m.path, m.name, list(m.unanalysed)] for m in build.modules],
            "dependents": {m: list(d) for m, d in build.dependents.items()},
            "unscoped": dict(build.unscoped),
            "scopes_known": build.scopes_known,
        },
        "test_classes": {m: list(c) for m, c in test_classes.items()},
        "graph": None
        if graph is None
        else {
            "edges": {f: sorted(e) for f, e in graph.edges.items()},
            "tests": sorted(graph.tests),
            "dynamic": dict(graph.dynamic_files),
            "notes": list(graph.notes),
        },
    }


def snapshot_from_json(
    data: Mapping[str, Any],
) -> tuple[JavaBuild, dict[str, list[str]], JavaGraph | None]:
    b = data["build"]
    build = JavaBuild(
        b["tool"],
        b["wrapper"],
        tuple(JavaModule(path, name, tuple(flags)) for path, name, flags in b["modules"]),
        b["unsupported"],
        {m: tuple(d) for m, d in (b.get("dependents") or {}).items()},
        dict(b.get("unscoped") or {}),
        bool(b.get("scopes_known")),  # an older snapshot: unknown, nothing scoped
    )
    g = data["graph"]
    graph = (
        None
        if g is None
        else JavaGraph(
            {f: frozenset(e) for f, e in g["edges"].items()},
            frozenset(g["tests"]),
            dict(g["dynamic"]),
            tuple(g["notes"]),
        )
    )
    return build, {m: list(c) for m, c in data["test_classes"].items()}, graph
