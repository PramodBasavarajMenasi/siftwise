"""Bearer-token auth, checked from headers before any request body is read.

Two kinds of token: the admin token (``SIFTWISE_API_TOKEN``: every repo, write) and stored
per-repo tokens (``siftwise token create``: one repo or ``*``, ``read`` or ``write``). The
middleware resolves the token, checks the route's scope and, for routes with the repo in the
path, the repo, all before the body is read. Routes whose repo is in the body or the query
check it after parsing (``deps.authorize``), before any lookup, so a token for another repo
learns nothing about whether a repo exists.
"""

import logging
import secrets
from collections.abc import Iterable
from typing import Protocol

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import compile_path
from starlette.types import ASGIApp, Receive, Scope, Send

from siftwise.core.tokens import ADMIN, PREFIX, Principal, Revoked
from siftwise.core.tokens import Scope as TokenScope

logger = logging.getLogger(__name__)

DEFAULT_PUBLIC_PATHS = frozenset({"/healthz", "/docs", "/redoc", "/openapi.json"})

READ, WRITE = TokenScope.READ, TokenScope.WRITE
# Every route's scope, by method and path template. A route missing here needs WRITE (fail
# closed); tests/test_tokens.py checks every route of the app is listed.
ROUTE_SCOPES: dict[tuple[str, str], TokenScope] = {
    # Storing a shadow selection needs WRITE (checked in the route); recording a CI selection
    # (``ci``) too, but a read token still gets its selection, not recorded.
    ("POST", "/select"): READ,
    ("POST", "/triage"): READ,
    ("POST", "/repos/{repo:path}/triage"): READ,
    ("POST", "/explain"): READ,
    ("GET", "/repos/{repo:path}/recall"): READ,
    ("GET", "/repos/{repo:path}/flaky"): READ,
    ("GET", "/repos/{repo:path}/ci"): READ,
    ("GET", "/tests/{test_id:path}/history"): READ,
    ("GET", "/runs/lookup"): READ,
    ("GET", "/runs/variants"): READ,
    ("POST", "/runs"): WRITE,
    ("POST", "/repos/{repo:path}/rollup"): WRITE,
    ("POST", "/repos/{repo:path}/shadow"): WRITE,
    ("POST", "/repos/{repo:path}/outcome"): WRITE,
    # The dashboard (SIFTWISE_DASHBOARD=1): read-only pages behind its own session cookie,
    # checked by api/dashboard/auth.py, never by a Bearer token. Login, logout and the
    # stylesheet are listed too: every /ui route is a read.
    ("GET", "/ui"): READ,
    ("GET", "/ui/"): READ,
    ("GET", "/ui/login"): READ,
    ("POST", "/ui/login"): READ,  # the one body read before auth: a form of at most 4 KB
    ("POST", "/ui/logout"): READ,
    ("GET", "/ui/static/{name}"): READ,
    ("GET", "/ui/repos/{repo:path}"): READ,
    ("GET", "/ui/repos/{repo:path}/-/runs"): READ,
    ("GET", "/ui/repos/{repo:path}/-/flaky"): READ,
    ("GET", "/ui/repos/{repo:path}/-/runs/{ci_run_id}/{attempt:int}"): READ,
}
DASHBOARD_PREFIX = "/ui"

NOT_CONFIGURED = "server has no SIFTWISE_API_TOKEN and no API tokens (siftwise token create)"


class TokenStore(Protocol):
    """Stored tokens, read synchronously (the middleware runs these in a thread)."""

    def resolve(self, token: str) -> Principal | Revoked | None: ...

    def any_active(self) -> bool: ...


def repo_forbidden(repo: str) -> str:
    """The 403 for a token of another repo: names only the repo the client asked for."""
    return f"this token has no access to repo {repo!r}"


def write_forbidden(what: str) -> str:
    return f"read-only token: {what} needs a write token"


# ROUTE_SCOPES' templates compiled the way Starlette routes them ({repo:path} spans "/"), so
# the middleware matches a request without running the router (or depending on its internals).
_COMPILED = [
    (method, compile_path(template)[0], compile_path(template)[2], needed)
    for (method, template), needed in ROUTE_SCOPES.items()
]


def route_scope(method: str, path: str) -> tuple[TokenScope, str | None]:
    """The scope a request needs and the repo in its path (None if it has none). A request
    matching no listed route needs WRITE."""
    for route_method, regex, convertors, needed in _COMPILED:
        found = regex.match(path) if method == route_method else None
        if found is not None:
            repo = found.groupdict().get("repo")
            return needed, convertors["repo"].convert(repo) if repo is not None else None
    return WRITE, None


class BearerAuthMiddleware:
    """Require ``Authorization: Bearer <token>`` on every path not in ``public_paths``.

    Runs as raw ASGI middleware so a rejected request never has its body read: a client
    without a valid token can't make the server spool a large upload. New routes are
    protected, and need a write token, by default. With no admin token and no stored token,
    protected paths fail closed (503).
    """

    def __init__(
        self,
        app: ASGIApp,
        token: str | None,
        tokens: TokenStore | None = None,
        public_paths: Iterable[str] = DEFAULT_PUBLIC_PATHS,
    ) -> None:
        self.app = app
        self.token = token.encode() if token else None
        self.tokens = tokens
        self.public_paths = frozenset(public_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.public_paths:
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if path == DASHBOARD_PREFIX or path.startswith(DASHBOARD_PREFIX + "/"):
            # The dashboard checks its own session cookie (api/dashboard/auth.py), and its
            # routes exist only with SIFTWISE_DASHBOARD=1: when off, /ui answers 404.
            await self.app(scope, receive, send)
            return

        response, principal = await self._authenticate(scope)
        if principal is not None:
            needed, path_repo = route_scope(scope["method"], scope["path"])
            if needed is WRITE and not principal.can_write():
                response = _error(403, write_forbidden(f"{scope['method']} {scope['path']}"))
            elif path_repo is not None and not principal.can_access(path_repo):
                response = _error(403, repo_forbidden(path_repo))
            else:
                scope["state"] = {**scope.get("state", {}), "principal": principal}
                await self.app(scope, receive, send)
                return
        assert response is not None
        await response(scope, receive, send)

    async def _authenticate(self, scope: Scope) -> tuple[JSONResponse | None, Principal | None]:
        credentials = _bearer(Headers(scope=scope).get("authorization", ""))
        if (
            credentials
            and self.token is not None
            and secrets.compare_digest(credentials.encode(), self.token)
        ):
            return None, ADMIN
        try:
            if credentials and self.tokens is not None and credentials.startswith(PREFIX):
                found = await run_in_threadpool(self.tokens.resolve, credentials)
                if isinstance(found, Principal):
                    return None, found
                if isinstance(found, Revoked):
                    return _unauthorized("token revoked"), None
            if self.token is None and (
                self.tokens is None or not await run_in_threadpool(self.tokens.any_active)
            ):
                return _error(503, NOT_CONFIGURED), None
        except Exception:  # the token store is the database: down means no one gets in
            logger.exception("API token lookup failed")
            return _error(503, "API token lookup failed"), None
        return _unauthorized("unknown token" if credentials else "missing bearer token"), None


def _bearer(header: str) -> str:
    scheme, _, credentials = header.partition(" ")
    return credentials.strip() if scheme.lower() == "bearer" else ""


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        {"detail": f"invalid or missing bearer token: {detail}"},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status_code)
