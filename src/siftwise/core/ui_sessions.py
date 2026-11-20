"""Dashboard sessions (``/ui``): created at login from an API token, checked on every page.

The browser's cookie holds a random session ID (32 bytes); the database keeps only its
SHA-256 (``ui_sessions.id_hash``), so a database dump holds no usable session. A session
records which token signed in: a stored token by its ID (looked up again on every request,
so revoking it ends the session at once), or the admin token by a salted SHA-256 of it (so a
changed ``SIFTWISE_API_TOKEN`` ends every admin session). The token itself is never stored.

Why server-side rather than a signed cookie: the API's workers are stateless and keep every
state in Postgres; a signed cookie can't be revoked (a copied cookie would outlive logout
until it expired) and would need a new shared signing secret.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from siftwise.core.models import ApiToken, UiSession
from siftwise.core.tokens import ADMIN, Principal, Revoked, Scope, resolve

LIFETIME = timedelta(hours=4)  # absolute: a session never outlives this, however busy


@dataclass(frozen=True)
class Login:
    """A new session: the cookie value (shown to the browser once) and its CSRF token."""

    session_id: str
    csrf: str
    principal: Principal


@dataclass(frozen=True)
class Active:
    """A valid session, as a page sees it."""

    principal: Principal
    csrf: str
    expires_at: datetime


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _admin_digest(salt: str, admin_token: str) -> str:
    return _digest(f"{salt}:{admin_token}")


def login(session: Session, token: str, admin_token: str | None) -> Login | None:
    """A session for ``token`` (the admin token or a stored, unrevoked one), or None."""
    token = token.strip()
    if not token:
        return None
    now = session.scalar(select(func.now()))
    assert now is not None
    row = UiSession(csrf=secrets.token_urlsafe(32), expires_at=now + LIFETIME)
    if admin_token and secrets.compare_digest(token.encode(), admin_token.encode()):
        principal = ADMIN
        row.admin_salt = secrets.token_hex(16)
        row.admin_digest = _admin_digest(row.admin_salt, admin_token)
    else:
        found = resolve(session, token)
        if not isinstance(found, Principal):
            return None
        principal = found
        row.api_token_id = found.token_id
    session_id = secrets.token_urlsafe(32)
    row.id_hash = _digest(session_id)
    row.repo, row.scope = principal.repo, principal.scope.value
    session.execute(delete(UiSession).where(UiSession.expires_at <= now))  # tidy expired rows
    session.add(row)
    session.commit()
    return Login(session_id, row.csrf, principal)


def check(session: Session, session_id: str, admin_token: str | None) -> Active | Revoked | None:
    """The session ``session_id`` is, checked against its token as it is now: ``Revoked()``
    if that token was revoked (or the admin token changed), None if unknown or expired. An
    ended session's row is deleted."""
    if not session_id:
        return None
    row = session.get(UiSession, _digest(session_id))
    if row is None:
        return None
    now = session.scalar(select(func.now()))
    assert now is not None
    if row.expires_at <= now:
        _drop(session, row)
        return None
    if row.api_token_id is None:
        same = (
            admin_token is not None
            and row.admin_salt is not None
            and row.admin_digest is not None
            and secrets.compare_digest(_admin_digest(row.admin_salt, admin_token), row.admin_digest)
        )
        if not same:
            _drop(session, row)
            return Revoked()
        return Active(ADMIN, row.csrf, row.expires_at)
    token = session.get(ApiToken, row.api_token_id)
    if token is None or token.revoked_at is not None:
        _drop(session, row)
        return Revoked()
    principal = Principal(token.id, token.name, token.repo, Scope(token.scope))
    return Active(principal, row.csrf, row.expires_at)


def logout(session: Session, session_id: str) -> None:
    """End a session (idempotent)."""
    if session_id:
        session.execute(delete(UiSession).where(UiSession.id_hash == _digest(session_id)))
        session.commit()


def _drop(session: Session, row: UiSession) -> None:
    session.delete(row)
    session.commit()
