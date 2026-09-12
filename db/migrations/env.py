import os
from logging.config import fileConfig

from dotenv import load_dotenv
from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

from db.models import Base
from db.session import _normalized_url

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# DATABASE_URL comes from .env, not alembic.ini, so the same connection
# string used by the app (see db/session.py) drives migrations too.
load_dotenv()
database_url = os.environ.get("DATABASE_URL")
if not database_url:
    raise RuntimeError(
        "DATABASE_URL is not set. Copy the Postgres connection string from "
        "the Neon console into .env as DATABASE_URL before running Alembic "
        "(this is separate from NEON_API_KEY, which only manages Neon "
        "projects)."
    )
config.set_main_option("sqlalchemy.url", _normalized_url(database_url))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
