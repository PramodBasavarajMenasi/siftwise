"""Per-repo API tokens: create, list, revoke, and resolve a bearer token to what it may do.

A token is ``sw_`` plus 32 random bytes (``secrets.token_urlsafe``). Only its SHA-256 is
stored (``api_tokens.token_hash``), so a database dump holds no usable token, and a lookup is
an equality match on the hash: comparing a hash leaks nothing about the token through timing.

Scopes: ``read`` (select, triage, explain, recall, history and lookups) and ``write`` (also
uploads, rollups and shadow compares). ``SIFTWISE_API_TOKEN`` is the admin token: every repo,
write, never stored here.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from siftwise.core.models import ApiToken, UiSession

PREFIX = "sw_"
ALL_REPOS = "*"
# last_used_at is written at most once per token per this interval, so a busy CI token costs
# one UPDATE every 10 minutes, not one per request: the column says "used in the last 10
# minutes", which is enough to spot unused tokens.
LAST_USED_RESOLUTION = timedelta(minutes=10)


class Scope(StrEnum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class Principal:
    """Who a request is: the admin token (``token_id`` None) or a stored token."""

    token_id: int | None
    name: str
    repo: str  # a repo name or ALL_REPOS
    scope: Scope

    @property
    def is_admin(self) -> bool:
        return self.token_id is None

    def can_access(self, repo: str) -> bool:
        return self.repo in (ALL_REPOS, repo)

    def can_write(self) -> bool:
        return self.scope is Scope.WRITE


ADMIN = Principal(token_id=None, name="SIFTWISE_API_TOKEN", repo=ALL_REPOS, scope=Scope.WRITE)


@dataclass(frozen=True)
class Revoked:
    """A stored token that was revoked: rejected, but the client is told why."""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return PREFIX + secrets.token_urlsafe(32)


def create_token(session: Session, repo: str, scope: Scope, name: str = "") -> tuple[int, str]:
    """Store a new token and return ``(id, token)``: the token is never readable again."""
    if not repo.strip():
        raise ValueError("repo must be a repo name or '*'")
    token = new_token()
    row = ApiToken(name=name, token_hash=hash_token(token), repo=repo.strip(), scope=scope.value)
    session.add(row)
    session.commit()
    return row.id, token


def list_tokens(session: Session) -> list[ApiToken]:
    return list(session.scalars(select(ApiToken).order_by(ApiToken.id)))


def revoke_token(session: Session, token_id: int) -> ApiToken | None:
    """Revoke a token (idempotent: an already revoked one keeps its first revocation time)
    and end its dashboard sessions now (every /ui request also checks the token, so they were
    already refused; this leaves none behind). None if there is no such token."""
    row = session.get(ApiToken, token_id)
    if row is None:
        return None
    if row.revoked_at is None:
        row.revoked_at = session.scalar(select(func.now()))
    session.execute(delete(UiSession).where(UiSession.api_token_id == token_id))
    session.commit()
    return row


def any_active(session: Session) -> bool:
    """Whether any token is usable: with no admin token and none of these, the API fails
    closed (503) instead of answering 401 to everyone."""
    return (
        session.scalar(select(ApiToken.id).where(ApiToken.revoked_at.is_(None)).limit(1))
        is not None
    )


def resolve(
    session: Session, token: str, now: datetime | None = None
) -> Principal | Revoked | None:
    """The stored token ``token`` is, ``Revoked()`` if it was revoked, None if unknown. Marks
    it used (at most once per ``LAST_USED_RESOLUTION``)."""
    if not token.startswith(PREFIX):
        return None
    digest = hash_token(token)
    row = session.scalars(select(ApiToken).where(ApiToken.token_hash == digest)).one_or_none()
    if row is None or not secrets.compare_digest(row.token_hash, digest):
        return None
    if row.revoked_at is not None:
        return Revoked()
    current = now or session.scalar(select(func.now()))
    assert current is not None
    cutoff = current - LAST_USED_RESOLUTION
    if row.last_used_at is None or row.last_used_at < cutoff:
        session.execute(
            update(ApiToken)
            .where(ApiToken.id == row.id)
            .where(or_(ApiToken.last_used_at.is_(None), ApiToken.last_used_at < cutoff))
            .values(last_used_at=current)
        )
        session.commit()
    return Principal(token_id=row.id, name=row.name, repo=row.repo, scope=Scope(row.scope))
