"""The optional extras: the modules each one brings, and how to install it when missing.

``pip install siftwise`` is the light CLI (select, record, triage, report, rollup, gha):
typer, httpx and defusedxml only. ``siftwise[server]`` adds the API server and its database;
``siftwise[mcp]`` the MCP server for coding agents. Pure (no imports of either), so the light
CLI can check before importing.
"""

import importlib.util

SERVER_MODULES = (
    "fastapi",
    "uvicorn",
    "sqlalchemy",
    "alembic",
    "psycopg",
    "pydantic",
    "pydantic_settings",
)
MCP_MODULES = ("mcp",)


def missing(modules: tuple[str, ...]) -> list[str]:
    """The ``modules`` that can't be imported."""
    return [name for name in modules if importlib.util.find_spec(name) is None]


def install_hint(extra: str) -> str:
    return (
        f"pip install 'siftwise[{extra}]' "
        f'(from a siftwise checkout: uv tool install ".[{extra}]")'
    )
