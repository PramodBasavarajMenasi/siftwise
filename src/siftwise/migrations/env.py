from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, engine_from_config, pool

from siftwise.config import get_settings
from siftwise.core.models import Base

config = context.config
# Callers (siftwise.migrate, the test suite) may preset the URL; else SIFTWISE_DATABASE_URL.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", get_settings().database_url)

# alembic.ini (development) configures logging; siftwise.migrate has no ini file.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_on(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # siftwise.migrate passes the connection that holds its advisory lock.
    connection = config.attributes.get("connection")
    if connection is not None:
        run_on(connection)
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        run_on(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
