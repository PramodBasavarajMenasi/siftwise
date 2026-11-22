"""Shared FastAPI dependencies."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from sqlalchemy.orm import Session

from siftwise.api.auth import repo_forbidden, write_forbidden
from siftwise.config import Settings
from siftwise.core import tokens
from siftwise.core.tokens import Principal, Revoked
from siftwise.db import app_sessionmaker, get_session


def get_app_settings(request: Request) -> Settings:
    """The settings the app was created with (see ``create_app``)."""
    settings: Settings = request.app.state.settings
    return settings


def get_principal(request: Request) -> Principal:
    """The token the auth middleware resolved for this request."""
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal):  # only if a route were reachable without auth
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing bearer token")
    return principal


Caller = Annotated[Principal, Depends(get_principal)]


def authorize(principal: Principal, repo: str, *, write: bool = False, what: str = "") -> None:
    """Every route calls this with its repo before any lookup: 403 for a token of another
    repo (the same answer whether the repo exists or not), or for a read token doing ``what``,
    a write."""
    if not principal.can_access(repo):
        raise HTTPException(status.HTTP_403_FORBIDDEN, repo_forbidden(repo))
    if write and not principal.can_write():
        raise HTTPException(status.HTTP_403_FORBIDDEN, write_forbidden(what))


class SessionTokenStore:
    """``auth.TokenStore`` over the database, one short session per lookup."""

    def __init__(self, sessions: Callable[[], AbstractContextManager[Session]]) -> None:
        self.sessions = sessions

    def resolve(self, token: str) -> Principal | Revoked | None:
        with self.sessions() as session:
            return tokens.resolve(session, token)

    def any_active(self) -> bool:
        with self.sessions() as session:
            return tokens.any_active(session)


def app_sessions(app: FastAPI) -> Callable[[], AbstractContextManager[Session]]:
    """Sessions on the app's database (``create_app``'s settings), or ``get_session``'s
    override (tests), so the middleware reads tokens from the same database as the routes."""

    @contextmanager
    def session_scope() -> Iterator[Session]:
        override = app.dependency_overrides.get(get_session)
        if override is None:
            with app_sessionmaker(app.state)() as session:
                yield session
            return
        made = override()
        if isinstance(made, Session):
            yield made
            return
        try:
            yield next(made)
        finally:
            made.close()

    return session_scope
