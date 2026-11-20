"""The dashboard's session check, before any request body is read.

Every ``/ui`` path but the login page, the login form's POST and the stylesheet needs a
valid session cookie (``core/ui_sessions.py``); without one the browser is sent to the login
page (303), never answered with data. The session's token is checked again on every request
(revoked: the session ends). A page of a repo the token can't see answers the same 404 as a
repo that doesn't exist. The login POST is the one body read before auth: urlencoded, at most
``LOGIN_MAX_BYTES``. The API never accepts this cookie, only Bearer tokens (``api/auth.py``),
so a browser session can't be used for a cross-site API call.

Every ``/ui`` response gets the security headers (``SECURITY_HEADERS``).
"""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from http.cookies import CookieError, SimpleCookie

from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from siftwise.core import ui_sessions
from siftwise.core.ui_sessions import Active

logger = logging.getLogger(__name__)

PREFIX = "/ui"
SESSION_COOKIE = "siftwise_session"
CSRF_COOKIE = "siftwise_csrf"
LOGIN_PATH = "/ui/login"
STATIC_PREFIX = "/ui/static/"
LOGIN_MAX_BYTES = 4096
REPOS_PREFIX = "/ui/repos/"
SUBPAGE = "/-/"  # separates a repo name (which has "/") from its sub-page

CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)
SECURITY_HEADERS = {
    "content-security-policy": CSP,
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    # Not "no-referrer": with it, browsers send `Origin: null` even on a same-origin form post
    # (Fetch, "serializing a request origin"), and the login's Origin check refuses every
    # sign-in. "same-origin" sends no referrer to any other site and keeps Origin real here.
    "referrer-policy": "same-origin",
    "cross-origin-opener-policy": "same-origin",
}


def is_dashboard_path(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


def path_repo(path: str) -> str | None:
    """The repo a ``/ui/repos/<repo>[/-/<page>]`` path is about."""
    if not path.startswith(REPOS_PREFIX):
        return None
    rest = path[len(REPOS_PREFIX) :]
    repo = rest.split(SUBPAGE, 1)[0].rstrip("/")
    return repo or None


def cookie(scope: Scope, name: str) -> str:
    raw = Headers(scope=scope).get("cookie", "")
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(raw)
    except CookieError:  # a malformed Cookie header: no cookies
        return ""
    morsel = jar.get(name)
    return morsel.value if morsel else ""


class DashboardAuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        admin_token: str | None,
        sessions: Callable[[], AbstractContextManager[Session]],
    ) -> None:
        self.app = app
        self.admin_token = admin_token
        self.sessions = sessions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_dashboard_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        send = _with_headers(send)
        path, method = scope["path"], scope["method"]
        if path.startswith(STATIC_PREFIX) or (path == LOGIN_PATH and method == "GET"):
            await self.app(scope, receive, send)
            return
        if path == LOGIN_PATH and method == "POST":
            response = _login_body_problem(scope)
            if response is not None:
                await response(scope, receive, send)
                return
            await self.app(scope, _limited(receive), send)
            return
        found = await self._check(cookie(scope, SESSION_COOKIE))
        if not isinstance(found, Active):
            redirect = RedirectResponse(LOGIN_PATH, status_code=303)
            if found is not None or cookie(scope, SESSION_COOKIE):
                redirect.delete_cookie(SESSION_COOKIE, path=PREFIX)
            await redirect(scope, receive, send)
            return
        repo = path_repo(path)
        if repo is not None and not found.principal.can_access(repo):
            from siftwise.api.dashboard.pages import not_found

            await not_found(found)(scope, receive, send)
            return
        scope["state"] = {**scope.get("state", {}), "principal": found.principal,
                          "ui_session": found}  # fmt: skip
        await self.app(scope, receive, send)

    async def _check(self, session_id: str) -> object:
        if not session_id:
            return None
        try:
            return await run_in_threadpool(self._check_sync, session_id)
        except Exception:  # the session store is the database: down means no one gets in
            logger.exception("dashboard session lookup failed")
            return None

    def _check_sync(self, session_id: str) -> object:
        with self.sessions() as session:
            return ui_sessions.check(session, session_id, self.admin_token)


def _login_body_problem(scope: Scope) -> Response | None:
    headers = Headers(scope=scope)
    length = headers.get("content-length")
    if length is not None and (not length.isdigit() or int(length) > LOGIN_MAX_BYTES):
        return PlainTextResponse("login form too large", status_code=413)
    kind = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if kind != "application/x-www-form-urlencoded":
        return PlainTextResponse("expected a form", status_code=415)
    return None


def _limited(receive: Receive) -> Receive:
    """``receive`` that stops a body over ``LOGIN_MAX_BYTES`` (chunked, no Content-Length)."""
    received = 0

    async def limited() -> Message:
        nonlocal received
        message = await receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > LOGIN_MAX_BYTES:
                raise HTTPException(413, "login form too large")
        return message

    return limited


def _with_headers(send: Send) -> Send:
    async def sending(message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = MutableHeaders(scope=message)
            for name, value in SECURITY_HEADERS.items():
                headers[name] = value
            if "cache-control" not in headers:
                headers["cache-control"] = "no-store"
        await send(message)

    return sending
