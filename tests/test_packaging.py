"""The install split: the light CLI never needs the server extra, and server-only entry points
say how to install it. CI's package job checks the same against real wheels."""

import importlib
import importlib.resources
import json
import re
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path
from typing import Any

import pytest
from packaging.version import Version
from typer.testing import CliRunner

import siftwise
from scripts import check_dist
from siftwise import extras
from siftwise.cli import server
from siftwise.cli.main import EXIT_USAGE, app

ROOT = Path(__file__).resolve().parent.parent
# Everything only the server and MCP extras bring, including what they pull in. pydantic is
# among them, but typer 0.29+ requires it and imports it to build a command's parameters, so
# `without_extras` lets typer (only) import it, as scripts/check_dist.py does.
NOT_IN_THE_LIGHT_INSTALL = (*extras.SERVER_MODULES, *extras.MCP_MODULES, "starlette")
BLOCKED = tuple(m for m in NOT_IN_THE_LIGHT_INSTALL if m != "pydantic")
LIGHT_COMMANDS = [
    [],
    ["select"],
    ["record"],
    ["triage"],
    ["report"],
    ["rollup"],
    ["mcp"],
    ["server"],
    ["gha", "select"],
    ["gha", "record"],
    ["gha", "rollup"],
    ["gitlab", "select"],
    ["gitlab", "record"],
    ["gitlab", "rollup"],
]


def without_extras(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter where none of the extras' modules can be imported:
    a ``None`` entry in ``sys.modules`` is what a missing package looks like to both ``import``
    (ModuleNotFoundError) and ``importlib.util.find_spec`` (None). pydantic is the exception:
    only typer may import it (typer 0.29+ does); for anyone else, siftwise included, it is
    missing (``check_dist.PYDANTIC_ONLY_FOR_TYPER``)."""
    prelude = (
        check_dist.IMPORT_RULE
        + check_dist.PYDANTIC_ONLY_FOR_TYPER
        + f"sys.modules.update(dict.fromkeys({BLOCKED!r}))\n"
    )
    return subprocess.run(
        [sys.executable, "-c", prelude + textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_light_cli_imports_no_server_or_mcp_dependency() -> None:
    result = without_extras(
        f"""
        from typer.testing import CliRunner
        from siftwise.cli.main import app
        import siftwise.cli.gha, siftwise.core.selector, siftwise.core.junit

        for args in [[*a, "--help"] for a in {LIGHT_COMMANDS!r}] + [["version"]]:
            outcome = CliRunner().invoke(app, args)
            assert outcome.exit_code == 0, (args, outcome.output, outcome.exception)
        loaded = {{m.split(".")[0] for m, module in sys.modules.items() if module is not None}}
        # pydantic may be here only through typer: the gate refuses every other importer.
        assert not loaded & set({BLOCKED!r}), loaded
        print("ok")
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_siftwise_modules_import_no_extras_module_not_even_pydantic() -> None:
    """Importing (not running) the light CLI: nothing of the extras, pydantic included, whatever
    typer's version."""
    result = without_extras(
        f"""
        import importlib
        for module in {check_dist.LIGHT_MODULES!r}:
            importlib.import_module(module)
        loaded = {{m.split(".")[0] for m, module in sys.modules.items() if module is not None}}
        assert not loaded & set({NOT_IN_THE_LIGHT_INSTALL!r}), loaded
        print("ok")
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_without_extras_refuses_pydantic_to_anyone_but_typer() -> None:
    result = without_extras("import pydantic")

    assert result.returncode != 0
    assert "No module named 'pydantic' (blocked: imported by <string>)" in result.stderr


def run_light_cli_check(code: str) -> dict[str, list[str]]:
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr
    found: dict[str, list[str]] = json.loads(result.stdout)
    return found


def test_the_light_cli_loads_pydantic_only_through_typer() -> None:
    """check_dist's check, here with the dev install: nothing blocked, so it holds whatever
    typer does (0.29+ imports pydantic to build a command's parameters)."""
    found = run_light_cli_check(check_dist.light_cli_check())

    assert found == {"on_import": [], "blamed": []}


def test_the_light_cli_check_blames_a_command_that_imports_a_server_module() -> None:
    code = check_dist.light_cli_check().replace(
        "for args in [",
        "@app.command('zz')\ndef zz() -> None:\n    import fastapi\nfor args in [['zz'], ",
        1,
    )

    found = run_light_cli_check(code)

    assert found["on_import"] == []
    assert found["blamed"][0] == "fastapi (imported by <string>)"


@pytest.mark.parametrize("command", ["start", "migrate"])
def test_server_commands_without_the_extra_say_how_to_install_it(command: str) -> None:
    result = without_extras(
        f"""
        from typer.testing import CliRunner
        from siftwise.cli.main import app

        outcome = CliRunner().invoke(app, ["server", "{command}"])
        print(outcome.exit_code)
        print(outcome.output)
        """
    )

    assert result.returncode == 0, result.stderr
    code, output = result.stdout.split("\n", 1)
    assert int(code) == EXIT_USAGE
    assert "`siftwise server` needs the server dependencies" in output
    assert "pip install 'siftwise[server]'" in output
    assert "uv tool install 'siftwise[server]'" in output


def test_importing_the_api_without_the_extra_says_how_to_install_it() -> None:
    result = without_extras("import siftwise.api")

    assert result.returncode != 0
    assert "ImportError: the siftwise API server needs the server dependencies" in result.stderr
    assert "pip install 'siftwise[server]'" in result.stderr


def test_server_start_migrates_then_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    from siftwise import migrate

    calls: list[tuple[str, Any]] = []
    monkeypatch.setenv("SIFTWISE_DATABASE_URL", "postgresql+psycopg://u:p@db:5432/x")
    monkeypatch.setenv("SIFTWISE_API_TOKEN", "t")
    monkeypatch.setattr(migrate, "upgrade", lambda url: calls.append(("migrate", url)))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(("serve", (app, kw))))
    get_settings = importlib.import_module("siftwise.config").get_settings
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(app, ["server", "start", "--host", "0.0.0.0", "--port", "9"])
        no_migrate = CliRunner().invoke(app, ["server", "start", "--no-migrate"])
    finally:
        get_settings.cache_clear()

    assert result.exit_code == 0 and no_migrate.exit_code == 0, result.output
    assert "warning" not in result.output
    assert calls == [
        ("migrate", "postgresql+psycopg://u:p@db:5432/x"),
        ("serve", ("siftwise.api.main:app", {"host": "0.0.0.0", "port": 9})),
        ("serve", ("siftwise.api.main:app", {"host": "127.0.0.1", "port": 8000})),
    ]


def test_server_start_warns_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    monkeypatch.chdir(ROOT / "tests")  # no .env here
    get_settings = importlib.import_module("siftwise.config").get_settings
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(app, ["server", "start", "--no-migrate"])
    finally:
        get_settings.cache_clear()

    assert result.exit_code == 0
    assert "SIFTWISE_API_TOKEN is not set" in result.output


def test_require_server_passes_with_the_extra_installed() -> None:
    server.require_server()  # the dev environment has every extra


def test_the_version_has_one_source() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]

    assert "version" not in project and project["dynamic"] == ["version"]
    assert pyproject["tool"]["hatch"]["version"]["path"] == "src/siftwise/__init__.py"
    assert Version(siftwise.__version__) >= Version("0.1.0.dev0")
    result = CliRunner().invoke(app, ["version"])
    assert result.output.strip() == siftwise.__version__


def test_version_flag_prints_the_version() -> None:
    flag = CliRunner().invoke(app, ["--version"])
    command = CliRunner().invoke(app, ["version"])

    assert flag.exit_code == 0, flag.output
    assert flag.output == command.output == f"{siftwise.__version__}\n"


def test_version_flag_in_the_light_install() -> None:
    result = without_extras(
        f"""
        from typer.testing import CliRunner
        from siftwise.cli.main import app

        outcome = CliRunner().invoke(app, ["--version"])
        assert outcome.exit_code == 0, (outcome.output, outcome.exception)
        loaded = {{m.split(".")[0] for m, module in sys.modules.items() if module is not None}}
        assert not loaded & set({NOT_IN_THE_LIGHT_INSTALL!r}), loaded
        print(outcome.output, end="")
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{siftwise.__version__}\n"


def test_the_quickstart_uses_the_released_image() -> None:
    # The quickstart (docker-compose.yml) and the README name one published image; a release
    # (not a dev or pre-release version) must point them at its own version.
    image = re.compile(r"ghcr\.io/pramodbasavarajmenasi/siftwise:([\w.\-]+)")
    compose = image.findall((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    readme = image.findall((ROOT / "README.md").read_text(encoding="utf-8"))

    assert len(compose) == 1 and readme
    assert set(readme) == set(compose)
    if not Version(siftwise.__version__).is_prerelease:
        assert compose == [siftwise.__version__]


def test_the_readme_links_are_absolute_for_pypi() -> None:
    # README.md is the PyPI project page, where relative links and images break.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    prose = re.sub(r"^```.*?^```", "", readme, flags=re.MULTILINE | re.DOTALL)
    links = re.findall(r"\]\(([^)\s]+)\)", prose)
    links += re.findall(r'(?:src|href|srcset)="([^"]+)"', prose)

    relative = [link for link in links if not link.startswith(("https://", "#", "mailto:"))]
    assert links and not relative


def test_the_light_dependencies_are_only_the_cli_ones() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    names = {dep.split(">")[0].split("[")[0] for dep in project["dependencies"]}

    assert names == {"typer", "httpx", "defusedxml"}
    assert set(project["optional-dependencies"]) == {"server", "mcp"}


def test_the_migrations_ship_in_the_package() -> None:
    migrations = importlib.resources.files("siftwise") / "migrations"

    assert (migrations / "env.py").is_file()
    assert (migrations / "script.py.mako").is_file()
    assert any(f.name.startswith("0001_") for f in (migrations / "versions").iterdir())


def test_server_help_names_the_extra() -> None:
    # Rich markup would eat an unescaped "[server]" and print "pip install 'siftwise'".
    result = CliRunner().invoke(app, ["server", "--help"])

    assert result.exit_code == 0
    assert "'siftwise[server]'" in result.output
