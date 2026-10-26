import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from siftwise import migrate


def alembic_config(url: str) -> Config:
    """The packaged migrations (what `siftwise server migrate` runs)."""
    return migrate.alembic_config(url)


@pytest.fixture(autouse=True)
def isolated_from_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never see the CI job they run in. In GitHub Actions, earlier steps export
    ``GITHUB_*``/``SIFTWISE_*`` variables (action-e2e's select step sets SIFTWISE_JUNIT_DIR),
    and the job sets GITHUB_OUTPUT and the like: a test reading them would depend on where it
    runs, or write into the real job's outputs. GitLab CI's are ``CI_*``."""
    for name in list(os.environ):
        if (name.startswith(("GITHUB_", "SIFTWISE_", "CI_", "GITLAB_"))
                and name != "SIFTWISE_TEST_DATABASE_URL"):  # fmt: skip
            monkeypatch.delenv(name)


@pytest.fixture(scope="session")
def db_url() -> str:
    url = os.environ.get("SIFTWISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("SIFTWISE_TEST_DATABASE_URL not set")
    return url


@pytest.fixture(scope="session")
def db_engine(db_url: str) -> Iterator[Engine]:
    """Engine for a freshly migrated test database (rebuilt once per session)."""
    config = alembic_config(db_url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    """Session whose work, including commits, is rolled back after each test."""
    with db_engine.connect() as connection, connection.begin() as transaction:
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        yield session
        session.close()
        transaction.rollback()
