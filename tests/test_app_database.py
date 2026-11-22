"""create_app(settings) serves every request from settings.database_url: the routes' sessions
and the middleware's token lookups, never the process-wide SIFTWISE_DATABASE_URL."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from siftwise.api.main import create_app
from siftwise.config import Settings, get_settings
from siftwise.core import tokens
from siftwise.core.tokens import Scope

ADMIN = "test-token"
# Nothing listens there (connect_timeout: Windows retries a refused connection for seconds).
NOWHERE = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none?connect_timeout=1"


@pytest.fixture
def process_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Lets a test point the process-wide settings somewhere (get_settings is cached)."""
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest.fixture
def stored_token(db_engine: Engine) -> Iterator[str]:
    """A read token committed to the test database (other connections must see it)."""
    with Session(db_engine) as session:
        token_id, token = tokens.create_token(session, "*", Scope.READ, "app-database-test")
        session.commit()
    yield token
    with Session(db_engine) as session:
        session.execute(text("DELETE FROM api_tokens WHERE id = :id"), {"id": token_id})
        session.commit()


def app_client(url: str) -> TestClient:
    app = create_app(Settings(_env_file=None, database_url=url, api_token=ADMIN))
    return TestClient(app, raise_server_exceptions=False)


def test_requests_use_the_database_url_given_to_create_app(
    db_url: str, process_url: pytest.MonkeyPatch, stored_token: str
) -> None:
    # The process settings point nowhere; the app was given the test database.
    process_url.setenv("SIFTWISE_DATABASE_URL", NOWHERE)
    with app_client(db_url) as client:
        route = client.get("/repos/nobody/x/ci", headers={"Authorization": f"Bearer {ADMIN}"})
        assert route.status_code == 404, route.text  # the route read the app's database
        # The middleware found the stored token there too (404: accepted, unknown repo).
        found = client.get(
            "/repos/nobody/x/ci", headers={"Authorization": f"Bearer {stored_token}"}
        )
        assert found.status_code == 404, found.text


def test_requests_never_fall_back_to_the_process_database(
    db_url: str, process_url: pytest.MonkeyPatch, stored_token: str
) -> None:
    # The other way round: the process settings hold a working database, the app's points
    # nowhere, so every request that needs a database fails instead of using another one.
    process_url.setenv("SIFTWISE_DATABASE_URL", db_url)
    with app_client(NOWHERE) as client:
        route = client.get("/repos/nobody/x/ci", headers={"Authorization": f"Bearer {ADMIN}"})
        assert route.status_code == 500
        # The stored token is in the process database only: the lookup goes to the app's
        # (unreachable: 503, fail closed), never finds it there (404 would mean it did).
        found = client.get(
            "/repos/nobody/x/ci", headers={"Authorization": f"Bearer {stored_token}"}
        )
        assert found.status_code == 503 and found.json()["detail"] == "API token lookup failed"
