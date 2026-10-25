"""Re-running failing tests for ``actions/record``'s ``rerun-unknown``, one runner per tool.

A runner turns failing test IDs (as recorded) into commands that run exactly those tests again
and write a JUnit report whose test IDs match the recorded ones:

* pytest: ``[prefix] pytest --junitxml=<out> -p no:cacheprovider <node IDs>``;
* Go: ``gotestsum --junitfile <out> -- -count=1 -run '^(TestA|TestB)$' <package>`` per package
  when gotestsum is on PATH, else ``go test -json`` with the same flags, converted to JUnit
  here (``go_json_to_junit``). Subtests re-run through their top-level test;
* Jest: ``[prefix] jest --ci --runTestsByPath <file> -t '^(<full name>|...)$'`` per file, from
  the nearest directory above it whose package.json (or jest.config.*) has Jest, with the
  jest-junit reporter writing to <out> (it must be installed, as for the CI run itself);
* Maven: ``mvn -B -Dtest=Class#m1+m2,Other#m3 test`` (``./mvnw`` when present), reports from
  ``target/surefire-reports``; Gradle: ``gradle test --continue --tests Class.method ...``
  (``./gradlew`` when present), reports from ``build/test-results``. Their reports are the
  files those directories got during the re-run (new or rewritten), copied out; then the CI
  run's reports are put back (``KeptReports``), so a later step of the job reads what the CI
  run wrote. (Surefire's reports directory has no user property: ``-D`` can't move it.)

The runner of a test comes from its ID and file, and the checkout: a pytest node whose file
exists; a Go import path (go.mod at the root, the package in its module); a JS/TS file (from the
leg's own JUnit reports) under a Jest package; a Java class with pom.xml or build.gradle[.kts]
at the root. Anything else isn't re-run, with the reason.

``rerun-prefix`` goes in front of the pytest and Jest commands (``uv run``, ``npx``): those
tools usually live in a project environment. Go, Maven and Gradle never get it (a prefix meant
for one language would break another in a mixed repo); their launchers are found here.

Every command runs without a shell. Names that reach a regex are escaped; Java class and
method names must be plain identifiers (nothing else reaches Maven's or Gradle's arguments).
Pure except for the file checks in ``classify``, Maven/Gradle's report backups and
``Invocation.reports``/``restore``: tests need no tool.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from siftwise.cli.repoconfig import read_go_module
from siftwise.core.selector import KnownTest, Runner, _classify, pytest_node

PYTEST, GO, JEST, MAVEN, GRADLE = "pytest", "go", "jest", "maven", "gradle"
# Never walked for Maven/Gradle reports.
_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", ".tox", "__pycache__"})
_JEST_CONFIGS = ("jest.config.js", "jest.config.ts", "jest.config.mjs", "jest.config.cjs",
                 "jest.config.json")  # fmt: skip
_GRADLE_FILES = ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")
_JAVA_CLASS = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\Z", re.ASCII)
# A method name, then JUnit 5 parameter types and a parameterized index: m, m(), m(int)[1], m[2]
_JAVA_METHOD = re.compile(r"([A-Za-z_$][\w$]*)(?:\([^()]*\))?(?:\[[^\[\]]*\])?\Z", re.ASCII)
_GO_META = frozenset("\\.+*?()|[]{}^$")
_JS_META = frozenset("\\^$.*+?()[]{}|/")
# Re-run failure output kept per converted Go test.
_MAX_OUTPUT = 4096

# Overridable in tests: where a tool is installed (None if it isn't).
which: Callable[[str], str | None] = shutil.which


@dataclass(frozen=True)
class Target:
    """One failing test to re-run: its runner, what groups it into one command (``group``),
    and what the command names it by (``arg``)."""

    test_id: str
    runner: str
    arg: str
    group: tuple[str, ...] = ()


class RunOptions(TypedDict, total=False):
    """``run_command``'s keyword arguments."""

    cwd: Path
    env: dict[str, str]
    stdout: Path


@dataclass
class Invocation:
    """One command of a re-run round, and where its JUnit report comes from."""

    tool: str
    argv: list[str]
    report: Path | None = None  # the JUnit file it writes (pytest, Jest, gotestsum)
    cwd: Path | None = None
    env: dict[str, str] | None = None
    stdout: Path | None = None  # go test -json output, converted to ``report``
    kept: KeptReports | None = None  # Maven/Gradle: the checkout's reports, backed up

    def options(self) -> RunOptions:
        """``run_command``'s keyword arguments: only those this command needs."""
        options = RunOptions()
        if self.cwd is not None:
            options["cwd"] = self.cwd
        if self.env is not None:
            options["env"] = self.env
        if self.stdout is not None:
            options["stdout"] = self.stdout
        return options

    def reports(self) -> list[Path]:
        """The JUnit files the run wrote. Raises OSError if there are none."""
        if self.stdout is not None and self.report is not None:
            self.report.write_text(go_json_to_junit(self.stdout.read_bytes()), encoding="utf-8")
        if self.kept is not None:
            found = self.kept.collect()
            if not found:
                raise OSError(f"{self.tool} wrote no new reports")
            return found
        if self.report is None or not self.report.is_file():
            raise FileNotFoundError(f"no {self.report}")
        return [self.report]

    def restore(self) -> None:
        """Put back the checkout's reports the command changed (Maven/Gradle; else nothing):
        after ``reports``, whether the command ran, failed, timed out or never started.
        Raises OSError if some couldn't be."""
        if self.kept is not None:
            self.kept.restore()


@dataclass
class Checkout:
    """What the checkout says about its runners (``root``: the working directory)."""

    root: Path
    _go_module: str | None = None
    _go_read: bool = False
    _jest_dirs: dict[Path, Path | None] = field(default_factory=dict)
    _files: list[Path] | None = None  # the checkout's files, listed once (``find``)

    def __post_init__(self) -> None:
        self.root = self.root.resolve()  # the Jest search stops here

    @property
    def go_module(self) -> str | None:
        if not self._go_read:
            self._go_module, self._go_read = read_go_module(self.root), True
        return self._go_module

    def java_tool(self) -> str | None:
        """maven or gradle (from the root's build files), or None."""
        maven = (self.root / "pom.xml").is_file()
        gradle = any((self.root / name).is_file() for name in _GRADLE_FILES)
        if maven and gradle:
            if _wrapper(self.root, "gradlew"):
                return GRADLE
            return MAVEN if _wrapper(self.root, "mvnw") else None
        return MAVEN if maven else GRADLE if gradle else None

    def find(self, reported: str) -> Path | None:
        """A test file a report names: absolute, relative to the root, or (jest-junit writes
        paths relative to the Jest project, e.g. a monorepo's package) the only file in the
        checkout whose path ends with it."""
        path = Path(reported)
        if path.is_absolute():
            return path if path.is_file() else None
        if (self.root / path).is_file():
            return self.root / path
        if self._files is None:
            self._files = []
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
                self._files += [Path(dirpath) / name for name in filenames]
        suffix = "/" + path.as_posix().lstrip("/")
        matches = [p for p in self._files if p.as_posix().endswith(suffix)]
        return matches[0] if len(matches) == 1 else None

    def jest_dir(self, file: Path) -> Path | None:
        """The nearest directory at or above ``file`` (within the root) that has Jest."""
        directory = file.parent
        seen: list[Path] = []
        while True:
            if directory in self._jest_dirs:
                found = self._jest_dirs[directory]
                break
            seen.append(directory)
            if _has_jest(directory):
                found = directory
                break
            if directory == self.root or directory.parent == directory:
                found = None
                break
            directory = directory.parent
        for d in seen:
            self._jest_dirs[d] = found
        return found


def classify(test_id: str, file_path: str | None, checkout: Checkout) -> Target | str:
    """How to re-run ``test_id`` (its recorded file, if a JUnit report named one), or why it
    can't be."""
    node = pytest_node(test_id)
    if node is not None:
        if (checkout.root / node.partition("::")[0]).is_file():
            return Target(test_id, PYTEST, node)
        return "its file isn't in the checkout"
    test = _classify(KnownTest(test_id, file_path))
    if test.runner is Runner.GO:
        module = checkout.go_module
        if module is None:
            return "a Go test, but there's no go.mod at the checkout's root"
        package = test.classname
        if package != module and not package.startswith(module + "/"):
            return f"its Go package isn't in this module ({module})"
        return Target(test_id, GO, test.name.split("/", 1)[0], (package,))
    if test.runner is Runner.JEST and test.file_path:
        found = checkout.find(test.file_path)
        if found is None:
            return "its file isn't in the checkout"
        file = found
        directory = checkout.jest_dir(file.resolve())
        if directory is None:
            return "a JS/TS test with no Jest (package.json or jest.config) above its file"
        relative = file.resolve().relative_to(directory).as_posix()
        return Target(test_id, JEST, test.name, (str(directory), relative))
    classname, _, name = test_id.partition("::")
    method = _JAVA_METHOD.match(name)
    if _JAVA_CLASS.match(classname) and method:
        tool = checkout.java_tool()
        if tool is None:
            return "no runner found for it (pytest, Go, Jest, Maven or Gradle)"
        return Target(test_id, tool, f"{classname}\0{method[1]}")
    return "no runner found for it (pytest, Go, Jest, Maven or Gradle)"


def invocations(
    targets: Sequence[Target], out: Path, n: int, prefix: Sequence[str], root: Path
) -> list[Invocation]:
    """Round ``n``'s commands for ``targets`` (one runner's or several), reports under ``out``.
    The pytest command is exactly what record ran before other runners existed."""
    by_runner: dict[str, list[Target]] = {}
    for target in targets:
        by_runner.setdefault(target.runner, []).append(target)
    result: list[Invocation] = []
    for runner, group in by_runner.items():
        result += _BUILDERS[runner](group, out, n, tuple(prefix), root)
    return result


def _pytest(targets: list[Target], out: Path, n: int, prefix: tuple[str, ...], root: Path
            ) -> list[Invocation]:  # fmt: skip
    xml = out / f"rerun-{n}.xml"
    argv = [*prefix, "pytest", f"--junitxml={xml}", "-p", "no:cacheprovider",
            *(t.arg for t in targets)]  # fmt: skip
    return [Invocation(PYTEST, argv, report=xml)]


def _go(targets: list[Target], out: Path, n: int, prefix: tuple[str, ...], root: Path
        ) -> list[Invocation]:  # fmt: skip
    gotestsum = which("gotestsum")
    result = []
    for k, (package, names) in enumerate(_grouped(targets).items(), 1):
        xml = out / f"rerun-{n}-go-{k}.xml"
        flags = ["-count=1", "-run", go_run_pattern(names), package[0]]
        if gotestsum:
            argv = [gotestsum, "--junitfile", str(xml), "--format", "standard-verbose", "--",
                    *flags]  # fmt: skip
            result.append(Invocation(GO, argv, report=xml))
        else:
            argv = ["go", "test", "-json", *flags]
            stdout = out / f"rerun-{n}-go-{k}.json"
            result.append(Invocation(GO, argv, report=xml, stdout=stdout))
    return result


def _jest(targets: list[Target], out: Path, n: int, prefix: tuple[str, ...], root: Path
          ) -> list[Invocation]:  # fmt: skip
    result = []
    for k, ((directory, file), names) in enumerate(_grouped(targets).items(), 1):
        name = f"rerun-{n}-jest-{k}.xml"
        if prefix:
            tool = [*prefix, "jest"]
        else:
            local = Path(directory) / "node_modules" / ".bin" / "jest"
            tool = [str(local) if _exists_bin(local) else "jest"]
        argv = [*tool, "--ci", "--runTestsByPath", file, "-t", jest_name_pattern(names),
                "--reporters=default", "--reporters=jest-junit"]  # fmt: skip
        env = {"JEST_JUNIT_OUTPUT_DIR": str(out), "JEST_JUNIT_OUTPUT_NAME": name}
        result.append(Invocation(JEST, argv, report=out / name, cwd=Path(directory), env=env))
    return result


def _maven(targets: list[Target], out: Path, n: int, prefix: tuple[str, ...], root: Path
           ) -> list[Invocation]:  # fmt: skip
    methods: dict[str, list[str]] = {}
    for target in targets:
        classname, method = target.arg.split("\0")
        methods.setdefault(classname, [])
        if method not in methods[classname]:
            methods[classname].append(method)
    spec = surefire_filter(methods)
    tool = _wrapper(root, "mvnw") or "mvn"
    argv = [tool, "-B", f"-Dtest={spec}", "-Dsurefire.failIfNoSpecifiedTests=false",
            "-DfailIfNoTests=false", "test"]  # fmt: skip
    kept = KeptReports(root, ("target", "surefire-reports"), out / f"rerun-{n}-maven")
    return [Invocation(MAVEN, argv, kept=kept)]


def _gradle(targets: list[Target], out: Path, n: int, prefix: tuple[str, ...], root: Path
            ) -> list[Invocation]:  # fmt: skip
    filters: list[str] = []
    for target in targets:
        classname, method = target.arg.split("\0")
        if f"{classname}.{method}" not in filters:
            filters.append(f"{classname}.{method}")
    tool = _wrapper(root, "gradlew") or "gradle"
    # --continue: in a multi-project build, a project the filter matches nothing in fails its
    # test task; the others must still run.
    argv = [tool, "test", "--continue", *(a for f in filters for a in ("--tests", f))]
    kept = KeptReports(root, ("build", "test-results"), out / f"rerun-{n}-gradle")
    return [Invocation(GRADLE, argv, kept=kept)]


_BUILDERS = {PYTEST: _pytest, GO: _go, JEST: _jest, MAVEN: _maven, GRADLE: _gradle}


def _grouped(targets: Iterable[Target]) -> dict[tuple[str, ...], list[str]]:
    """Targets' ``arg``s by ``group``, both in first-seen order, without repeats."""
    grouped: dict[tuple[str, ...], list[str]] = {}
    for target in targets:
        args = grouped.setdefault(target.group, [])
        if target.arg not in args:
            args.append(target.arg)
    return grouped


def go_run_pattern(names: Sequence[str]) -> str:
    """``go test -run``'s regex for exactly these top-level tests (RE2, metacharacters
    escaped like Go's regexp.QuoteMeta)."""
    return "^(" + "|".join("".join("\\" + c if c in _GO_META else c for c in name)
                           for name in names) + ")$"  # fmt: skip


def jest_name_pattern(names: Sequence[str]) -> str:
    """``jest -t``'s regex for exactly these full test names (a JS RegExp: metacharacters
    escaped, and each space matches any run of whitespace, which the recorded name collapsed)."""

    def escape(name: str) -> str:
        escaped = "".join("\\" + c if c in _JS_META else c for c in name)
        return re.sub(r" +", r"\\s+", escaped)

    return "^(" + "|".join(escape(name) for name in names) + ")$"


def surefire_filter(methods: Mapping[str, Sequence[str]]) -> str:
    """Surefire's ``-Dtest``: ``Class#m1+m2,Other#m3``."""
    return ",".join(f"{cls}#{'+'.join(names)}" for cls, names in methods.items())


def go_json_to_junit(data: bytes) -> str:
    """``go test -json`` output as JUnit XML, named as go-junit-report and gotestsum name tests
    (classname = the package import path, name = the test, subtests included). A test that
    started but never finished (a panic, a timeout) is a failure. Non-JSON lines (build
    output) are ignored; a package that didn't build has no tests."""
    tests: dict[tuple[str, str], dict[str, object]] = {}
    for line in data.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or not event.get("Package") or not event.get("Test"):
            continue
        key = (str(event["Package"]), str(event["Test"]))
        entry = tests.setdefault(key, {"status": "fail", "time": 0.0, "output": []})
        action = event.get("Action")
        if action == "output":
            output = entry["output"]
            assert isinstance(output, list)
            output.append(str(event.get("Output") or ""))
        elif action in ("pass", "fail", "skip"):
            entry["status"] = action
            entry["time"] = float(event.get("Elapsed") or 0)
    root = ET.Element("testsuites")
    suites: dict[str, ET.Element] = {}
    for (package, test), entry in tests.items():
        if package not in suites:
            suites[package] = ET.SubElement(root, "testsuite", name=package)
        case = ET.SubElement(suites[package], "testcase", classname=package, name=test,
                             time=f"{entry['time']:.3f}")  # fmt: skip
        output = entry["output"]
        assert isinstance(output, list)
        if entry["status"] == "fail":
            ET.SubElement(case, "failure", message="Failed").text = "".join(output)[-_MAX_OUTPUT:]
        elif entry["status"] == "skip":
            ET.SubElement(case, "skipped")
    return ET.tostring(root, encoding="unicode")


def _wrapper(root: Path, name: str) -> str | None:
    """The build wrapper script at the root (``mvnw``/``gradlew``, ``.cmd``/``.bat`` on
    Windows), as an absolute path, or None."""
    names = (f"{name}.cmd", f"{name}.bat") if os.name == "nt" else (name,)
    for candidate in names:
        if (root / candidate).is_file():
            return str((root / candidate).resolve())
    return None


def _exists_bin(path: Path) -> bool:
    """An npm bin shim (``jest``, or ``jest.cmd`` on Windows)."""
    return path.is_file() or (os.name == "nt" and path.with_suffix(".cmd").is_file())


def _has_jest(directory: Path) -> bool:
    if any((directory / name).is_file() for name in _JEST_CONFIGS):
        return True
    try:
        package = json.loads((directory / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(package, dict):
        return False
    if "jest" in package:
        return True
    return any(isinstance(package.get(k), dict) and "jest" in package[k]
               for k in ("dependencies", "devDependencies", "peerDependencies"))  # fmt: skip


def _report_files(root: Path, under: tuple[str, ...]) -> dict[Path, tuple[int, int]]:
    """``TEST-*.xml`` files inside any ``<under>`` directory below ``root`` (e.g.
    target/surefire-reports, build/test-results/<task>), with their mtime and size."""
    found: dict[Path, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        parts = Path(dirpath).parts
        if not any(tuple(parts[i : i + len(under)]) == under for i in range(len(parts))):
            continue
        for name in filenames:
            if name.startswith("TEST-") and name.endswith(".xml"):
                path = Path(dirpath) / name
                try:
                    stat = path.stat()
                except OSError:
                    continue
                found[path] = (stat.st_mtime_ns, stat.st_size)
    return found


class KeptReports:
    """The checkout's ``TEST-*.xml`` reports under ``<under>`` directories, copied into
    ``store`` when created (before the re-run). ``collect`` copies out the reports the re-run
    wrote (new or rewritten: mtime or size changed); ``restore`` deletes the new ones and puts
    back every one that changed or went missing (Gradle empties its results directory, so a
    ``--tests`` run would leave only the re-run classes' reports). Raises OSError."""

    def __init__(self, root: Path, under: tuple[str, ...], store: Path) -> None:
        self.root, self.under, self.store = root, under, store
        self.before = _report_files(root, under)
        self.backup: dict[Path, Path] = {}
        self.restored = False
        (store / "backup").mkdir(parents=True, exist_ok=True)
        for k, path in enumerate(self.before):
            copy = store / "backup" / f"{k}-{path.name}"
            shutil.copy2(path, copy)  # with its mtime: put back, it looks untouched
            self.backup[path] = copy

    def collect(self) -> list[Path]:
        """Copies of the reports the re-run wrote (two modules can share a file name)."""
        after = _report_files(self.root, self.under)
        written = sorted(path for path, stamp in after.items() if self.before.get(path) != stamp)
        (self.store / "reports").mkdir(parents=True, exist_ok=True)
        copies = []
        for k, path in enumerate(written):
            copies.append(self.store / "reports" / f"{k}-{path.name}")
            shutil.copy2(path, copies[-1])
        return copies

    def restore(self) -> None:
        """Once. Tries every file, then raises OSError if any couldn't be put back."""
        if self.restored:
            return
        self.restored = True
        after = _report_files(self.root, self.under)
        failed = 0
        for path in after.keys() - self.before.keys():
            try:
                path.unlink()
            except OSError:
                failed += 1
        for path, stamp in self.before.items():
            if after.get(path) != stamp:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(self.backup[path], path)
                except OSError:
                    failed += 1
        if failed:
            raise OSError(f"{failed} report(s) in {'/'.join(self.under)} not restored")
