import os
from logging.config import fileConfig

from dotenv import load_dotenv
from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

from sqlalchemy import text

from db.models import Base
from db.session import DB_SCHEMA, _normalized_url

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


def _include_name(name, type_, parent_names):
    """Extra safety net on top of NOT passing `include_schemas=True` below
    (see that comment for why) — keeps Alembic from ever comparing against
    other schemas' tables in this shared Neon database if some future
    Alembic/SQLAlchemy version changes the default enumeration behavior.

    Also explicitly excludes Alembic's own version table: providing *any*
    custom `include_name` apparently suppresses Alembic's normal default
    protection for it — verified live, autogenerate proposed
    `op.drop_table('alembic_version')` once this filter was added, which
    would have destroyed Alembic's own bookkeeping table on the next
    `upgrade`.
    """
    if type_ == "schema":
        return name in (DB_SCHEMA, None)
    if type_ == "table" and name == "alembic_version":
        return False
    return True


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
        # Order Wars' tables (and alembic_version itself) live under a
        # dedicated schema, not `public` — see db/session.py's DB_SCHEMA
        # docstring for why. Models stay schema-agnostic; this translate map
        # is what actually redirects every emitted DDL/DML statement.
        # Committed as its own transaction, explicitly — SQLAlchemy 2.0
        # "begin once" connections auto-begin a transaction on first
        # execute(), and leaving it open for context.begin_transaction() to
        # inherit made Alembic treat it as externally managed and never
        # commit it (verified live: the whole migration silently rolled
        # back on connection close, schema and all, with no error raised).
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {DB_SCHEMA}"))
        connection.commit()
        # schema_translate_map (below) only affects SQL Core-compiled
        # DDL/DML — it does NOT affect Alembic's raw information_schema
        # reflection queries used for autogenerate comparison, which follow
        # the connection's actual default schema (Postgres's search_path,
        # normally "public"). Verified live: without this, autogenerate
        # compared against `public` — a schema shared with unrelated
        # projects — and crashed trying to reflect their tables for a
        # potential "drop" op (several, e.g. up_orders, weather_snapshots,
        # material_properties, don't reflect cleanly, for reasons unrelated
        # to this project). Setting search_path makes `order_wars` the
        # default schema for reflection too, so comparison never sees
        # `public` at all — simpler and more robust than trying to filter
        # per-table after the fact.
        connection.execute(text(f"SET search_path TO {DB_SCHEMA}"))
        connection.commit()  # same "begin once" pitfall as CREATE SCHEMA above
        connection = connection.execution_options(
            schema_translate_map={None: DB_SCHEMA}
        )
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=DB_SCHEMA,
            include_name=_include_name,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
