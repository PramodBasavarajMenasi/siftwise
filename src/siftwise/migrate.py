"""Database migrations from the installed package (``siftwise server migrate`` / ``start``).

The Alembic scripts ship inside the package (``siftwise/migrations``), so a pip install of
``siftwise[server]`` can migrate without a checkout; ``alembic.ini`` at the repo root points
at the same scripts for development.
"""

import logging

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, pool, text

SCRIPT_LOCATION = "siftwise:migrations"

# Two-int advisory lock (namespace, key). Namespace 1 is the rollup's per-repo lock
# (core/history.py); 2 is this one, so they never collide.
MIGRATION_LOCK = (2, 0)


def alembic_config(url: str | None = None) -> Config:
    """Alembic config for the packaged scripts, without an ini file. ``url``: the database,
    else ``SIFTWISE_DATABASE_URL`` (read by migrations/env.py)."""
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    if url:
        config.set_main_option("sqlalchemy.url", url)
    return config


def upgrade(url: str, revision: str = "head") -> None:
    """``alembic upgrade`` under a session advisory lock, so replicas starting together run the
    migrations once: the others wait, then find nothing left to do."""
    logging.getLogger("alembic").setLevel(logging.INFO)
    if not logging.getLogger().handlers:
        logging.basicConfig(format="%(levelname)-5.5s [%(name)s] %(message)s")
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            namespace, key = MIGRATION_LOCK
            params = {"ns": namespace, "key": key}
            connection.execute(text("SELECT pg_advisory_lock(:ns, :key)"), params)
            connection.commit()  # the lock is the session's; alembic runs its own transaction
            try:
                config = alembic_config(url)
                config.attributes["connection"] = connection
                command.upgrade(config, revision)
                connection.commit()
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(:ns, :key)"), params)
                connection.commit()
    finally:
        engine.dispose()
