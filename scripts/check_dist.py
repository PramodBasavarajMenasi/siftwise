"""Check built distributions before publishing: run after ``uv build`` (CI's package job,
release.yml).

    python scripts/check_dist.py [dist]

- ``twine check --strict`` on the sdist and wheel;
- the sdist holds only ``src/siftwise``, README.md, LICENSE and pyproject.toml; the wheel holds
  the migrations;
- the wheel in fresh venvs: light (``siftwise --help`` and ``version``, none of the extras'
  modules importable, ``siftwise server start`` exits 2 with the install command), ``[server]``
  (the API imports, the migrations resolve) and ``[mcp]``; prints each install's size.

Needs only the standard library and ``uv`` on PATH. Exit 1 on the first failed check.
"""

import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

SERVER_MODULES = ("fastapi", "uvicorn", "sqlalchemy", "alembic", "psycopg", "pydantic")
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
        if run(siftwise, "version").strip() != version:
            fail(f"`siftwise version` doesn't print {version}")
        importable = run(
            bin_dir / "python", "-c",
            "import importlib.util as u; "
            f"print([m for m in {(*SERVER_MODULES, 'mcp')!r} if u.find_spec(m)])",
        ).strip()  # fmt: skip
        if importable != "[]":
            fail(f"the light install has extras' modules: {importable}")
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
