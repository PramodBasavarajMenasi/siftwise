"""Check built distributions before publishing: run after ``uv build`` (CI's package job,
release.yml).

    python scripts/check_dist.py [dist]

- ``twine check --strict`` on the sdist and wheel;
- the sdist holds only ``src/siftwise``, README.md, LICENSE and pyproject.toml; the wheel holds
  the migrations;
- the wheel in fresh venvs: light (``siftwise --help``, ``version`` and ``--version``; none of
  the extras' modules installed but pydantic, which typer 0.29+ requires; siftwise's light
  modules import none of them, pydantic included, and its commands load pydantic only through
  typer (``light_cli_check``); ``siftwise server start`` exits 2 with the install command),
  ``[server]`` (the API imports, the migrations resolve) and ``[mcp]``; prints each install's
  size.

Needs only the standard library and ``uv`` on PATH. Exit 1 on the first failed check.
"""

import json
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

SERVER_MODULES = ("fastapi", "uvicorn", "sqlalchemy", "alembic", "psycopg", "pydantic_settings")
# Installed in the light venv since typer 0.29.0 requires it, and typer imports it whenever it
# builds a command's parameters (typer/_adapters.py). siftwise itself must never import it.
PYDANTIC = "pydantic"
# Nothing the light CLI may load: the extras' modules and what they pull in.
FORBIDDEN = (*SERVER_MODULES, PYDANTIC, "mcp", "starlette")
# The light commands whose imports are checked: every command's --help, plus `version`.
LIGHT_COMMANDS: tuple[list[str], ...] = (
    [], ["select"], ["record"], ["triage"], ["report"], ["rollup"], ["mcp"], ["server"],
    ["gha", "select"], ["gha", "record"], ["gha", "rollup"],
    ["gitlab", "select"], ["gitlab", "record"], ["gitlab", "rollup"],
)  # fmt: skip
LIGHT_MODULES = (
    "siftwise.cli.main", "siftwise.cli.gha", "siftwise.cli.gitlab", "siftwise.cli.reruns",
    "siftwise.core.selector", "siftwise.core.junit", "siftwise.core.risk", "siftwise.extras",
)  # fmt: skip
# The rule both checks share (this script on the installed wheel, tests/test_packaging.py on
# the dev install), as code for a fresh interpreter: `importer()` = the package whose code is
# importing (the innermost frame outside importlib and these hooks); `allowed(top, importer)`:
# an extras' module may only be imported by its own package, or pydantic by typer.
IMPORT_RULE = """
import os, sys, traceback

ALLOWED_IMPORTERS = {"pydantic": {"typer", "pydantic"}}

def package_of(filename):
    parts = filename.replace(os.sep, "/").split("/")
    if "site-packages" in parts:
        return parts[parts.index("site-packages") + 1].removesuffix(".py")
    return "siftwise" if "siftwise" in parts else filename

def _ours(frame):
    path = frame.filename.replace(os.sep, "/")
    return (frame.filename.startswith("<frozen importlib")
            or path.endswith("/importlib/__init__.py")
            or (frame.filename == "<string>" and frame.name in ("find_spec", "importer")))

def importer():
    frames = [f for f in traceback.extract_stack() if not _ours(f)]
    return package_of(frames[-1].filename) if frames else "?"

def probing():
    # importlib.util.find_spec (extras.missing) asks whether a module exists: not an import.
    # importlib.util is frozen in some Pythons ("<frozen importlib.util>"), a file in others.
    return any(f.name == "find_spec" and (f.filename == "<frozen importlib.util>"
               or f.filename.replace(os.sep, "/").endswith("/importlib/util.py"))
               for f in traceback.extract_stack())

def allowed(top, by):
    return by == top or by in ALLOWED_IMPORTERS.get(top, ())
"""
# Run in the interpreter being checked; prints JSON: `on_import` = forbidden modules loaded by
# importing siftwise's light modules (pydantic included), `blamed` = forbidden modules the
# commands then imported against IMPORT_RULE.
_CHECK = """
import importlib, json

FORBIDDEN = set({forbidden!r})
blamed = []

class Watch:
    def find_spec(self, name, path=None, target=None):
        top = name.split(".")[0]
        if top in FORBIDDEN and name not in sys.modules and not probing():
            by = importer()
            if not allowed(top, by):
                blamed.append(f"{{name}} (imported by {{by}})")
        return None

for module in {modules!r}:
    importlib.import_module(module)
on_import = sorted({{m.split(".")[0] for m in sys.modules}} & FORBIDDEN)
sys.meta_path.insert(0, Watch())
from typer.testing import CliRunner
from siftwise.cli.main import app
for args in {invocations!r}:
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, (args, result.output, result.exception)
print(json.dumps({{"on_import": on_import, "blamed": blamed}}))
"""


def light_cli_check() -> str:
    """Python code that imports siftwise's light modules and runs the light commands, and prints
    what it found as JSON (see ``_CHECK``); both lists must be empty."""
    return IMPORT_RULE + _CHECK.format(
        forbidden=FORBIDDEN,
        modules=LIGHT_MODULES,
        invocations=[[*args, "--help"] for args in LIGHT_COMMANDS] + [["version"]],
    )


# A prelude for code that runs with the extras missing (tests/test_packaging.py): pydantic
# (installed with typer 0.29+) is importable only by typer, as IMPORT_RULE says; for any other
# importer, siftwise included, its import fails as if it weren't installed.
PYDANTIC_ONLY_FOR_TYPER = """
class _PydanticGate:
    def find_spec(self, name, path=None, target=None):
        top = name.split(".")[0]
        if top == "pydantic" and not probing() and not allowed(top, importer()):
            raise ModuleNotFoundError(
                f"No module named {name!r} (blocked: imported by {importer()})", name=name)
        return None

sys.meta_path.insert(0, _PydanticGate())
"""


# hatch always adds .gitignore to an sdist.
SDIST_FILES = ("README.md", "LICENSE", "pyproject.toml", "PKG-INFO", ".gitignore")


def fail(message: str) -> None:
    print(f"check_dist: FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def run(*args: str | Path, expect: int = 0) -> str:
    result = subprocess.run(
        [str(a) for a in args], capture_output=True, text=True, check=False, timeout=600
    )
    if result.returncode != expect:
        fail(f"{' '.join(map(str, args))} exited {result.returncode} (expected {expect}):\n"
             f"{result.stdout}{result.stderr}")  # fmt: skip
    return result.stdout + result.stderr


def check_sdist(sdist: Path, version: str) -> None:
    root = f"siftwise-{version}/"
    with tarfile.open(sdist) as archive:
        names = [m.name for m in archive.getmembers() if m.isfile()]
    stray = [
        n for n in names
        if not n.startswith(root)
        or not (n[len(root):].startswith("src/siftwise/") or n[len(root):] in SDIST_FILES)
    ]  # fmt: skip
    if stray:
        fail(f"unexpected files in the sdist: {stray[:10]}")
    print(f"check_dist: sdist: {len(names)} files, only src/siftwise + {', '.join(SDIST_FILES)}")


def check_wheel(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    needed = ["siftwise/migrations/env.py", "siftwise/migrations/script.py.mako"]
    missing = [n for n in needed if n not in names]
    if missing or not any(n.startswith("siftwise/migrations/versions/0001_") for n in names):
        fail(f"the wheel lacks the migrations: {missing or 'versions/'}")
    if any(n.startswith(("tests/", "scripts/")) for n in names):
        fail("the wheel contains tests/ or scripts/")
    print(f"check_dist: wheel: {len(names)} files, migrations included")


def venv(base: Path, name: str, requirement: str) -> tuple[Path, float]:
    """A fresh venv with ``requirement`` installed: its bin dir, and its site-packages' size in
    MB as installed (before any import writes bytecode)."""
    path = base / name
    run("uv", "venv", "--quiet", "--python", sys.executable, path)
    bin_dir = path / ("Scripts" if os.name == "nt" else "bin")
    python = bin_dir / "python"
    run("uv", "pip", "install", "--quiet", "--python", path, requirement)
    site = run(python, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])")
    return bin_dir, size_mb(Path(site.strip()))


def size_mb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6


def main() -> None:
    dist = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    wheels, sdists = sorted(dist.glob("siftwise-*.whl")), sorted(dist.glob("siftwise-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        fail(f"expected one wheel and one sdist in {dist}, found {wheels + sdists}")
    wheel, sdist = wheels[0], sdists[0]
    version = wheel.name.split("-")[1]

    run("uvx", "twine", "check", "--strict", wheel, sdist)
    print("check_dist: twine check passed")
    check_sdist(sdist, version)
    check_wheel(wheel)

    with tempfile.TemporaryDirectory(prefix="siftwise-dist-") as tmp:
        base = Path(tmp)
        bin_dir, size = venv(base, "light", str(wheel.resolve()))
        siftwise = bin_dir / "siftwise"
        run(siftwise, "--help")
        for args in (["version"], ["--version"]):
            if run(siftwise, *args).strip() != version:
                fail(f"`siftwise {args[0]}` doesn't print {version}")
        importable = run(
            bin_dir / "python", "-c",
            "import importlib.util as u; "
            f"print([m for m in {(*SERVER_MODULES, 'mcp')!r} if u.find_spec(m)])",
        ).strip()  # fmt: skip
        if importable != "[]":
            fail(f"the light install has extras' modules: {importable}")
        found = json.loads(run(bin_dir / "python", "-c", light_cli_check()).strip())
        if found["on_import"]:
            fail(f"importing the light CLI loads extras' modules: {found['on_import']}")
        if found["blamed"]:
            fail(f"the light commands import extras' modules: {found['blamed']}")
        out = run(siftwise, "server", "start", expect=2)
        if "pip install 'siftwise[server]'" not in out:
            fail(f"`siftwise server start` without the extra doesn't name it:\n{out}")
        print(f"check_dist: light install OK, {size:.0f} MB")

        bin_dir, size = venv(base, "server", f"siftwise[server] @ {wheel.resolve().as_uri()}")
        run(bin_dir / "siftwise", "server", "--help")
        head = run(
            bin_dir / "python", "-c",
            "import siftwise.api.main; from siftwise import migrate; "
            "from alembic.script import ScriptDirectory; "
            "print(ScriptDirectory.from_config(migrate.alembic_config()).get_current_head())",
        ).strip().splitlines()[-1]  # fmt: skip
        if head in ("", "None"):
            fail("the server install finds no migration head")
        print(f"check_dist: server install OK (migrations at {head}), {size:.0f} MB")

        bin_dir, size = venv(base, "mcp", f"siftwise[mcp] @ {wheel.resolve().as_uri()}")
        run(bin_dir / "siftwise", "mcp", "--help")
        run(bin_dir / "python", "-c", "import siftwise.mcp.server")
        print(f"check_dist: mcp install OK, {size:.0f} MB")


if __name__ == "__main__":
    main()
