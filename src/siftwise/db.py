"""Database engine and session factory."""

from collections.abc import Iterator
from functools import lru_cache

from fastapi import Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from siftwise.config import get_settings


@lru_cache
def sessionmaker_for(url: str) -> sessionmaker[Session]:
    """One engine (and connection pool) per database URL, for the life of the process."""
    return sessionmaker(bind=create_engine(url, pool_pre_ping=True), expire_on_commit=False)


def get_engine() -> Engine:
    """The process settings' engine (the CLI and scripts; the API uses its app's)."""
    engine = get_sessionmaker().kw["bind"]
    assert isinstance(engine, Engine)
    return engine


def get_sessionmaker() -> sessionmaker[Session]:
    """Sessions on the process settings' database (``SIFTWISE_DATABASE_URL``)."""
    return sessionmaker_for(get_settings().database_url)


def app_sessionmaker(app_state: object) -> sessionmaker[Session]:
    """The sessions of an app made by ``create_app``: on the database URL it was given."""
    factory = getattr(app_state, "sessionmaker", None)
    return factory if factory is not None else get_sessionmaker()


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session, on the database of the app
    serving the request (``create_app``'s settings), never another one."""
    with app_sessionmaker(request.app.state)() as session:
        yield session
