"""Per-repo API tokens (#71): scopes on every route, repo binding, revocation, the admin token,
no tokens at all, auth before the body, last_used_at and the `siftwise token` commands."""

import asyncio
import contextlib
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.types import Message, Receive, Scope, Send
from typer.testing import CliRunner

from siftwise.api.auth import DEFAULT_PUBLIC_PATHS, ROUTE_SCOPES, BearerAuthMiddleware, route_scope
from siftwise.api.main import create_app
from siftwise.cli import token as token_cli
from siftwise.cli.main import app as cli_app
from siftwise.config import Settings
from siftwise.core import tokens
from siftwise.core.models import ApiToken
from siftwise.core.tokens import ADMIN, Principal, Revoked
from siftwise.core.tokens import Scope as TokenScope
from siftwise.db import get_session

REPO = "acme/shop"
OTHER = "acme/other"
ADMIN_TOKEN = "admin-s3cret"
SHA = "a" * 40


# --- the route table ----------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """One request per route, with a body valid enough to reach the handler."""

    method: str
    path: str  # "{repo}" is filled in
    write: bool
    json: dict[str, Any] | None = None
    query: dict[str, str] = field(default_factory=dict)
    upload: bool = False

    def send(self, client: TestClient, repo: str, token: str | None) -> Any:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        path = self.path.format(repo=repo)
        params = {k: v.format(repo=repo) for k, v in self.query.items()}
        if self.upload:
            meta = {
                "repo": repo,
                "commit_sha": SHA,
                "branch": "main",
                "is_main": True,
                "ci_run_id": "1",
            }
            return client.post(
                path,
                files=[("files", ("j.xml", b"<testsuite/>", "application/xml"))],
                data={"metadata": json.dumps(meta)},
                headers=headers,
            )
        body = (
            None if self.json is None else json.loads(json.dumps(self.json).replace("{repo}", repo))
        )
        return client.request(self.method, path, json=body, params=params, headers=headers)


CALLS = {
    "select": Call("POST", "/select", False, json={"repo": "{repo}"}),
    "triage": Call(
        "POST",
        "/triage",
        False,
        json={"repo": "{repo}", "commit_sha": SHA, "failures": [{"test_id": "t::a"}]},
    ),
    "run triage": Call("POST", "/repos/{repo}/triage", False, json={"ci_run_id": "1"}),
    "explain": Call("POST", "/explain", False, json={"repo": "{repo}", "test_id": "t::a"}),
    "recall": Call("GET", "/repos/{repo}/recall", False),
    "history": Call("GET", "/tests/t::a/history", False, query={"repo": "{repo}"}),
    "lookup": Call("GET", "/runs/lookup", False, query={"repo": "{repo}", "ci_run_id": "1"}),
    "variants": Call("GET", "/runs/variants", False, query={"repo": "{repo}", "ci_run_id": "1"}),
    "upload": Call("POST", "/runs", True, upload=True),
    "rollup": Call("POST", "/repos/{repo}/rollup", True),
    "shadow compare": Call("POST", "/repos/{repo}/shadow", True, json={"ci_run_id": "1"}),
}


def test_every_route_has_an_explicit_scope() -> None:
    """A new route must be added to ROUTE_SCOPES (and to CALLS here): otherwise it would
    silently need a write token, the fail-closed default."""
    app = create_app(Settings(_env_file=None, api_token=ADMIN_TOKEN))
    routes = {
        (method.upper(), path)
        for path, item in app.openapi()["paths"].items()
        if path not in DEFAULT_PUBLIC_PATHS
        for method in item
    }  # OpenAPI paths drop the ":path" convertor
    assert routes == {(m, t.replace(":path", "")) for m, t in ROUTE_SCOPES}
    assert {(c.method, _template(c.path)) for c in CALLS.values()} == routes
    assert {
        (m, t.replace(":path", "")) for (m, t), s in ROUTE_SCOPES.items() if s is TokenScope.WRITE
    } == {(c.method, _template(c.path)) for c in CALLS.values() if c.write}


def _template(path: str) -> str:
    return path.replace("t::a", "{test_id}")


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/repos/acme/shop/triage", (TokenScope.READ, "acme/shop")),
        ("POST", "/repos/a/b/c/shadow", (TokenScope.WRITE, "a/b/c")),
        ("GET", "/repos/acme/shop/recall", (TokenScope.READ, "acme/shop")),
        ("GET", "/repos/acme/shop/rollup", (TokenScope.WRITE, None)),  # no such GET route
        ("GET", "/tests/a/b::c/history", (TokenScope.READ, None)),
        ("POST", "/select", (TokenScope.READ, None)),
        ("POST", "/somewhere-new", (TokenScope.WRITE, None)),
    ],
)
def test_route_scope(method: str, path: str, expected: tuple[TokenScope, str | None]) -> None:
    assert route_scope(method, path) == expected


# --- the middleware alone: no database ----------------------------------------------------


class FakeStore:
    def __init__(self, known: dict[str, Principal | Revoked], active: bool = True) -> None:
        self.known = known
        self.active = active
        self.lookups = 0

    def resolve(self, token: str) -> Principal | Revoked | None:
        self.lookups += 1
        return self.known.get(token)

    def any_active(self) -> bool:
        return self.active


class BrokenStore(FakeStore):
    def resolve(self, token: str) -> Principal | Revoked | None:
        raise RuntimeError("database down")


READ_TOKEN = "sw_read"
WRITE_TOKEN = "sw_write"
OTHER_TOKEN = "sw_other"
REVOKED_TOKEN = "sw_revoked"
STORE: dict[str, Principal | Revoked] = {
    READ_TOKEN: Principal(1, "read", REPO, TokenScope.READ),
    WRITE_TOKEN: Principal(2, "write", REPO, TokenScope.WRITE),
    OTHER_TOKEN: Principal(3, "other", OTHER, TokenScope.WRITE),
    REVOKED_TOKEN: Revoked(),
}


async def body_reader(scope: Scope, receive: Receive, send: Send) -> None:
    """Stand-in for the real app: reads the whole body, like a multipart upload would."""
    while (await receive()).get("more_body", False):
        pass
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send(
        {
            "type": "http.response.body",
            "body": json.dumps(scope.get("state", {}).get("principal").__class__.__name__).encode(),
        }
    )


def drive(
    middleware: BearerAuthMiddleware, method: str, path: str, token: str | None
) -> tuple[int, int, dict[str, Any]]:
    """A 50-chunk request through the middleware: (status, body chunks read, JSON reply)."""
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    scope: Scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers,
        "root_path": "",
    }
    chunks = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal chunks
        chunks += 1
        return {"type": "http.request", "body": b"x" * 1024, "more_body": chunks < 50}

    async def send(message: Message) -> None:
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return sent[0]["status"], chunks, json.loads(body) if body else {}


def middleware(
    admin: str | None = ADMIN_TOKEN, store: FakeStore | None = None
) -> BearerAuthMiddleware:
    return BearerAuthMiddleware(body_reader, admin, tokens=store or FakeStore(STORE))


@pytest.mark.parametrize(
    ("token", "path", "status", "detail"),
    [
        (None, "/runs", 401, "missing bearer token"),
        ("sw_nope", "/runs", 401, "unknown token"),
        ("not-a-stored-token", "/runs", 401, "unknown token"),
        (REVOKED_TOKEN, "/select", 401, "token revoked"),
        (READ_TOKEN, "/runs", 403, "read-only token: POST /runs needs a write token"),
        (READ_TOKEN, f"/repos/{REPO}/shadow", 403, "read-only token"),
        (READ_TOKEN, f"/repos/{REPO}/rollup", 403, "read-only token"),
        (READ_TOKEN, "/no-such-route", 403, "read-only token"),  # unmapped = write
        (WRITE_TOKEN, f"/repos/{OTHER}/shadow", 403, f"no access to repo '{OTHER}'"),
        (OTHER_TOKEN, f"/repos/{REPO}/rollup", 403, f"no access to repo '{REPO}'"),
    ],
)
def test_rejected_requests_never_have_their_body_read(
    token: str | None, path: str, status: int, detail: str
) -> None:
    code, chunks, reply = drive(middleware(), "POST", path, token)

    assert (code, chunks) == (status, 0)
    assert detail in reply["detail"]
    assert OTHER not in reply["detail"] or OTHER in path  # never names another token's repo


@pytest.mark.parametrize(
    ("token", "method", "path"),
    [
        (READ_TOKEN, "POST", "/select"),
        (READ_TOKEN, "GET", f"/repos/{REPO}/recall"),
        (READ_TOKEN, "POST", f"/repos/{REPO}/triage"),
        (WRITE_TOKEN, "POST", "/runs"),
        (WRITE_TOKEN, "POST", f"/repos/{REPO}/shadow"),
        (ADMIN_TOKEN, "POST", f"/repos/{OTHER}/rollup"),
    ],
)
def test_allowed_requests_reach_the_app(token: str, method: str, path: str) -> None:
    code, chunks, reply = drive(middleware(), method, path, token)

    assert (code, chunks) == (200, 50)
    assert reply == "Principal"  # type: ignore[comparison-overlap]  # the stand-in replies a string


def test_the_admin_token_never_touches_the_store() -> None:
    store = BrokenStore(STORE)
    code, _, _ = drive(middleware(store=store), "POST", "/runs", ADMIN_TOKEN)

    assert code == 200


def test_tokens_without_the_prefix_are_not_looked_up() -> None:
    store = FakeStore(STORE)
    drive(middleware(store=store), "POST", "/select", "some-old-shared-secret")

    assert store.lookups == 0


def test_no_admin_token_and_no_stored_tokens_fails_closed() -> None:
    code, chunks, reply = drive(
        middleware(None, FakeStore({}, active=False)), "POST", "/runs", "sw_anything"
    )

    assert (code, chunks) == (503, 0)
    assert "SIFTWISE_API_TOKEN" in reply["detail"]


def test_stored_tokens_work_without_an_admin_token() -> None:
    mw = middleware(None, FakeStore(STORE))

    assert drive(mw, "POST", "/select", READ_TOKEN)[0] == 200
    assert drive(mw, "POST", "/select", "sw_nope")[0] == 401


def test_a_failing_token_store_fails_closed() -> None:
    code, chunks, reply = drive(middleware(store=BrokenStore(STORE)), "POST", "/runs", "sw_x")

    assert (code, chunks) == (503, 0)
    assert reply["detail"] == "API token lookup failed"


# --- the real app and database ------------------------------------------------------------


@pytest.fixture
def make_client(db_session: Session) -> Iterator[Callable[[str | None], TestClient]]:
    clients: list[TestClient] = []

    def make(admin: str | None = None) -> TestClient:
        app = create_app(Settings(_env_file=None, api_token=admin))
        app.dependency_overrides[get_session] = lambda: db_session
        client = TestClient(app)
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


@pytest.fixture
def made(db_session: Session) -> dict[str, str]:
    """Tokens in the database: read and write for REPO, write for OTHER, read for every repo,
    and a revoked write token for REPO."""
    out = {
        "read": tokens.create_token(db_session, REPO, TokenScope.READ, "agent")[1],
        "write": tokens.create_token(db_session, REPO, TokenScope.WRITE, "ci")[1],
        "other": tokens.create_token(db_session, OTHER, TokenScope.WRITE)[1],
        "all-read": tokens.create_token(db_session, "*", TokenScope.READ)[1],
    }
    revoked_id, out["revoked"] = tokens.create_token(db_session, REPO, TokenScope.WRITE)
    tokens.revoke_token(db_session, revoked_id)
    return out


AUTH_STATUSES = (401, 403, 503)


@pytest.mark.parametrize("name", list(CALLS))
def test_scopes_on_every_route(
    name: str, made: dict[str, str], make_client: Callable[[str | None], TestClient]
) -> None:
    call, client = CALLS[name], make_client(None)

    read = call.send(client, REPO, made["read"])
    write = call.send(client, REPO, made["write"])
    everywhere = call.send(client, REPO, made["all-read"])

    assert write.status_code not in AUTH_STATUSES, write.text
    if call.write:
        assert (read.status_code, everywhere.status_code) == (403, 403)
        assert "read-only token" in read.json()["detail"]
    else:
        assert read.status_code not in AUTH_STATUSES, read.text
        assert everywhere.status_code not in AUTH_STATUSES, everywhere.text


@pytest.mark.parametrize("name", list(CALLS))
def test_a_token_for_another_repo_gets_403_whether_the_repo_exists_or_not(
    name: str, made: dict[str, str], make_client: Callable[[str | None], TestClient]
) -> None:
    call, client = CALLS[name], make_client(ADMIN_TOKEN)
    CALLS["upload"].send(client, REPO, ADMIN_TOKEN)  # REPO exists now; "acme/none" doesn't

    for repo in (REPO, "acme/none"):
        response = call.send(client, repo, made["other"])
        assert response.status_code == 403, (repo, response.text)
        assert response.json() == {"detail": f"this token has no access to repo {repo!r}"}


@pytest.mark.parametrize("name", list(CALLS))
def test_revoked_and_unknown_tokens_get_401(
    name: str, made: dict[str, str], make_client: Callable[[str | None], TestClient]
) -> None:
    client = make_client(ADMIN_TOKEN)

    revoked = CALLS[name].send(client, REPO, made["revoked"])
    unknown = CALLS[name].send(client, REPO, "sw_" + "x" * 43)

    assert revoked.status_code == unknown.status_code == 401
    assert revoked.json()["detail"].endswith("token revoked")
    assert unknown.json()["detail"].endswith("unknown token")


@pytest.mark.parametrize("name", list(CALLS))
def test_the_admin_token_has_full_access(
    name: str, made: dict[str, str], make_client: Callable[[str | None], TestClient]
) -> None:
    client = make_client(ADMIN_TOKEN)

    for repo in (REPO, OTHER):
        assert CALLS[name].send(client, repo, ADMIN_TOKEN).status_code not in AUTH_STATUSES


def test_no_admin_token_and_no_tokens_is_503_and_a_revoked_one_does_not_count(
    db_session: Session, make_client: Callable[[str | None], TestClient]
) -> None:
    client = make_client(None)
    assert CALLS["select"].send(client, REPO, None).status_code == 503

    token_id, token = tokens.create_token(db_session, REPO, TokenScope.READ)
    assert CALLS["select"].send(client, REPO, None).status_code == 401
    assert CALLS["select"].send(client, REPO, token).status_code == 404  # unknown repo

    tokens.revoke_token(db_session, token_id)
    assert CALLS["select"].send(client, REPO, token).status_code == 401  # told it's revoked
    response = CALLS["select"].send(client, REPO, None)
    assert response.status_code == 503  # no usable token left: fail closed again
    assert "SIFTWISE_API_TOKEN" in response.json()["detail"]


def test_storing_a_shadow_selection_needs_a_write_token(
    made: dict[str, str], make_client: Callable[[str | None], TestClient]
) -> None:
    client = make_client(ADMIN_TOKEN)
    CALLS["upload"].send(client, REPO, ADMIN_TOKEN)
    body = {
        "repo": REPO,
        "changed_files": ["a.py"],
        "shadow": {"ci_run_id": "77", "run_attempt": 1, "commit_sha": SHA},
    }

    read = client.post("/select", json=body, headers={"Authorization": f"Bearer {made['read']}"})
    write = client.post("/select", json=body, headers={"Authorization": f"Bearer {made['write']}"})

    assert read.status_code == 403
    assert (
        read.json()["detail"] == "read-only token: storing a shadow selection needs a write token"
    )
    assert write.status_code == 200


def test_only_the_hash_is_stored(db_session: Session) -> None:
    token_id, token = tokens.create_token(db_session, REPO, TokenScope.READ, "x")
    row = db_session.get(ApiToken, token_id)

    assert token.startswith("sw_") and len(token) >= 40
    assert row is not None and row.token_hash == tokens.hash_token(token)
    assert token not in {row.token_hash, row.name, row.repo}


def test_last_used_is_written_at_most_once_per_interval(db_session: Session) -> None:
    token_id, token = tokens.create_token(db_session, REPO, TokenScope.READ)
    first = tokens.resolve(db_session, token)
    row = db_session.get(ApiToken, token_id)
    assert isinstance(first, Principal) and row is not None and row.last_used_at is not None
    used = row.last_used_at

    tokens.resolve(db_session, token, now=used + timedelta(minutes=9))
    db_session.refresh(row)
    assert row.last_used_at == used  # within the interval: no write

    later = used + tokens.LAST_USED_RESOLUTION + timedelta(seconds=1)
    tokens.resolve(db_session, token, now=later)
    db_session.refresh(row)
    assert row.last_used_at == later


def test_revoking_twice_keeps_the_first_time(db_session: Session) -> None:
    token_id, _ = tokens.create_token(db_session, REPO, TokenScope.READ)
    first = tokens.revoke_token(db_session, token_id)
    assert first is not None
    revoked_at = first.revoked_at

    again = tokens.revoke_token(db_session, token_id)

    assert again is not None and again.revoked_at == revoked_at
    assert tokens.revoke_token(db_session, 999_999) is None


def test_the_admin_principal() -> None:
    assert ADMIN.is_admin and ADMIN.can_write() and ADMIN.can_access("any/repo")


# --- the CLI ------------------------------------------------------------------------------


@pytest.fixture
def cli_db(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Session:
    monkeypatch.setattr(token_cli, "_session", lambda: contextlib.nullcontext(db_session))
    return db_session


def test_token_create_prints_the_token_once(cli_db: Session) -> None:
    result = CliRunner().invoke(
        cli_app, ["token", "create", "--repo", REPO, "--scope", "write", "--name", "ci"]
    )

    assert result.exit_code == 0, result.output
    token = result.stdout.strip()
    assert token.startswith("sw_")
    assert "shown only once" in result.stderr
    found = tokens.resolve(cli_db, token)
    assert isinstance(found, Principal)
    assert (found.repo, found.scope, found.name) == (REPO, TokenScope.WRITE, "ci")


@pytest.mark.parametrize(
    "args",
    [["--repo", REPO, "--scope", "admin"], ["--repo", " "]],
    ids=["bad-scope", "blank-repo"],
)
def test_token_create_rejects_bad_options(cli_db: Session, args: list[str]) -> None:
    result = CliRunner().invoke(cli_app, ["token", "create", *args])

    assert result.exit_code == 2
    assert cli_db.scalars(select(ApiToken)).first() is None


def test_token_list_never_prints_tokens(cli_db: Session) -> None:
    _, token = tokens.create_token(cli_db, REPO, TokenScope.READ, "agent")
    tokens.create_token(cli_db, "*", TokenScope.WRITE)

    result = CliRunner().invoke(cli_app, ["token", "list"])

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["ID", "NAME", "REPO", "SCOPE", "CREATED", "LAST", "USED", "REVOKED"]
    assert "agent" in lines[1] and REPO in lines[1] and "read" in lines[1]
    assert "*" in lines[2] and "write" in lines[2]
    assert token not in result.output and tokens.hash_token(token) not in result.output


def test_token_list_with_no_tokens(cli_db: Session) -> None:
    result = CliRunner().invoke(cli_app, ["token", "list"])

    assert result.exit_code == 0
    assert "no tokens" in result.stderr


def test_token_revoke(cli_db: Session) -> None:
    token_id, token = tokens.create_token(cli_db, REPO, TokenScope.READ)

    result = CliRunner().invoke(cli_app, ["token", "revoke", str(token_id)])
    missing = CliRunner().invoke(cli_app, ["token", "revoke", "999999"])

    assert result.exit_code == 0, result.output
    assert f"revoked token {token_id}" in result.stdout
    assert isinstance(tokens.resolve(cli_db, token), Revoked)
    assert missing.exit_code == 1
    assert "no token with id 999999" in missing.stderr
