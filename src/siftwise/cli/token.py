"""``siftwise token create|list|revoke``: manage per-repo API tokens. Needs the server extra.

These talk to the database directly (SIFTWISE_DATABASE_URL), like ``siftwise server
migrate``, not to the API: managing tokens takes the database credentials, which only the
server's operator has. An API endpoint would let anyone holding the admin token (the one CI
jobs use) mint tokens, so a leaked CI secret could create new, longer-lived ones.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

import typer

from siftwise.cli.server import EXIT_USAGE, require_server

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

EXIT_NOT_FOUND = 1

app = typer.Typer(
    # The backslash escapes rich markup, which would eat "[server]".
    help="Manage per-repo API tokens in the server's database (SIFTWISE_DATABASE_URL; needs "
    "the server extra: pip install 'siftwise\\[server]').",
    no_args_is_help=True,
)


@app.command("create")
def create_command(
    repo: Annotated[
        str | None,
        typer.Option(help="The repo it is for, e.g. acme/shop ('*' = all repos: see --all-repos)."),
    ] = None,
    all_repos: Annotated[
        bool,
        typer.Option(
            "--all-repos",
            help="For every repo, the same as --repo '*'. Use it on Windows, where Click "
            "expands a '*' argument into file names even when it is quoted.",
        ),
    ] = False,
    scope: Annotated[
        str, typer.Option(help="read: select, triage, explain, report. write: also uploads.")
    ] = "read",
    name: Annotated[str, typer.Option(help="A label for `siftwise token list`.")] = "",
) -> None:
    """Create a token and print it once: only its hash is stored."""
    require_server("siftwise token")
    from siftwise.core.tokens import ALL_REPOS, Scope, create_token

    if scope not in {s.value for s in Scope}:
        typer.echo(f"error: --scope must be read or write, not {scope!r}", err=True)
        raise typer.Exit(EXIT_USAGE)
    if all_repos and repo is not None:
        typer.echo("error: give --repo or --all-repos, not both", err=True)
        raise typer.Exit(EXIT_USAGE)
    if all_repos:
        repo = ALL_REPOS
    if repo is None or not repo.strip():
        typer.echo("error: --repo must be a repo name (or use --all-repos)", err=True)
        raise typer.Exit(EXIT_USAGE)
    with _session() as session:
        token_id, token = create_token(session, repo, Scope(scope), name)
    typer.echo(
        f"created token {token_id} ({scope}, repo {repo.strip()}). It is shown only once:",
        err=True,
    )
    typer.echo(token)


@app.command("list")
def list_command() -> None:
    """List tokens: id, name, repo, scope, created, last used (to 10 minutes), revoked. Never
    the tokens themselves."""
    require_server("siftwise token")
    from siftwise.core.tokens import list_tokens

    with _session() as session:
        rows = list_tokens(session)
    if not rows:
        typer.echo("no tokens (SIFTWISE_API_TOKEN, the admin token, isn't stored here)", err=True)
        return
    header = ("ID", "NAME", "REPO", "SCOPE", "CREATED", "LAST USED", "REVOKED")
    lines = [
        (str(r.id), r.name or "-", r.repo, r.scope, _when(r.created_at), _when(r.last_used_at),
         _when(r.revoked_at))
        for r in rows
    ]  # fmt: skip
    widths = [max(len(row[i]) for row in (header, *lines)) for i in range(len(header))]
    for row in (header, *lines):
        typer.echo(
            "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        )


@app.command("revoke")
def revoke_command(token_id: Annotated[int, typer.Argument(help="The token's id.")]) -> None:
    """Revoke a token: it gets 401 from then on (the next request; nothing is cached)."""
    require_server("siftwise token")
    from siftwise.core.tokens import revoke_token

    with _session() as session:
        row = revoke_token(session, token_id)
        if row is None:
            typer.echo(f"error: no token with id {token_id}", err=True)
            raise typer.Exit(EXIT_NOT_FOUND)
        typer.echo(
            f"revoked token {row.id} ({row.scope}, repo {row.repo}) at {_when(row.revoked_at)}"
        )


def _session() -> "Session":
    from siftwise.db import get_sessionmaker

    return get_sessionmaker()()


def _when(value: datetime | None) -> str:
    return "-" if value is None else value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
