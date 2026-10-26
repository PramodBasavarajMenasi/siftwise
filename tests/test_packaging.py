"""The install split: the light CLI never needs the server extra, and server-only entry points
say how to install it. CI's package job checks the same against real wheels."""

import importlib
import importlib.resources
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
from siftwise import extras
from siftwise.cli import server
from siftwise.cli.main import EXIT_USAGE, app

ROOT = Path(__file__).resolve().parent.parent
# Everything only the server and MCP extras bring, including what they pull in.
NOT_IN_THE_LIGHT_INSTALL = (*extras.SERVER_MODULES, *extras.MCP_MODULES, "starlette")
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
    (ModuleNotFoundError) and ``importlib.util.find_spec`` (None)."""
    prelude = f"import sys; sys.modules.update(dict.fromkeys({NOT_IN_THE_LIGHT_INSTALL!r}))\n"
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

        for args in {LIGHT_COMMANDS!r}:
            outcome = CliRunner().invoke(app, [*args, "--help"])
            assert outcome.exit_code == 0, (args, outcome.output, outcome.exception)
        loaded = {{m.split(".")[0] for m, module in sys.modules.items() if module is not None}}
        assert not loaded & set({NOT_IN_THE_LIGHT_INSTALL!r}), loaded
        print("ok")
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


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
