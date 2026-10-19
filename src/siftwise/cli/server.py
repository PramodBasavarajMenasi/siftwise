"""``siftwise server start|migrate``: run the API server. Needs the server extra.

The light CLI registers these commands too, so they import the server modules only when run,
after checking they're installed: without the extra they exit 2 with the install command.
"""

from typing import Annotated

import typer

from siftwise import extras

EXIT_USAGE = 2

app = typer.Typer(
    # The backslash escapes rich markup, which would eat "[server]".
    help="Run the siftwise API server (needs the server extra: pip install 'siftwise\\[server]').",
    no_args_is_help=True,
)


def require_server() -> None:
    missing = extras.missing(extras.SERVER_MODULES)
    if missing:
        typer.echo(
            "error: `siftwise server` needs the server dependencies "
            f"(missing: {', '.join(missing)}): {extras.install_hint('server')}",
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)


@app.command("migrate")
def migrate_command() -> None:
    """Apply the database migrations (SIFTWISE_DATABASE_URL).

    Safe to run from several replicas at once: an advisory lock serializes them.
    """
    require_server()
    from siftwise import migrate
    from siftwise.config import get_settings

    migrate.upgrade(get_settings().database_url)


@app.command("start")
def start_command(
    host: Annotated[str, typer.Option(help="Address to listen on.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8000,
    run_migrations: Annotated[
        bool, typer.Option("--migrate/--no-migrate", help="Apply migrations first.")
    ] = True,
) -> None:
    """Apply the migrations, then serve the API.

    Reads SIFTWISE_DATABASE_URL and SIFTWISE_API_TOKEN (also from .env).
    """
    require_server()
    import uvicorn

    from siftwise import migrate
    from siftwise.config import get_settings

    settings = get_settings()
    if not settings.api_token:
        typer.echo(
            "warning: SIFTWISE_API_TOKEN is not set: every request except /healthz gets 503",
            err=True,
        )
    if run_migrations:
        migrate.upgrade(settings.database_url)
    uvicorn.run("siftwise.api.main:app", host=host, port=port)
