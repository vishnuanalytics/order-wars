"""Engine/session setup for the Postgres persistence layer.

Follows the same "skip/fail clearly if unset" pattern as `agents/llm.py`:
nothing here connects at import time, so importing `db.models`/`db.session`
(e.g. for Alembic autogenerate) never requires a live database. A connection
is only attempted when `get_engine()`/`session_scope()` is actually called.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

# All Order Wars tables live under this Postgres schema, not `public` — the
# Neon database this connects to is shared with unrelated projects (verified
# by inspection: it already had tables like `documents`, `materials`,
# `up_orders` with no relation to this app). Applied via
# `schema_translate_map` rather than hardcoding `schema="order_wars"` onto
# the models in db/models.py, so those models — and the SQLite-backed tests
# that build them with a plain `create_engine("sqlite://")` — stay
# schema-agnostic; only a real Postgres connection gets translated.
DB_SCHEMA = "order_wars"

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _normalized_url(raw_url: str) -> str:
    """Ensure the psycopg3 driver is used even if DATABASE_URL is a bare
    `postgresql://...` string (e.g. pasted directly from the Neon console).
    """
    if raw_url.startswith("postgresql://"):
        return raw_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return raw_url


def get_engine() -> Engine:
    """Return a lazily-created, process-wide SQLAlchemy engine.

    Raises if DATABASE_URL isn't set — unlike the LLM provider keys, there's
    no fallback for "no database configured" once persistence work depends
    on it.
    """
    global _engine
    if _engine is None:
        load_dotenv()
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError(
                "DATABASE_URL is not set. Copy the Postgres connection string "
                "from the Neon console into .env as DATABASE_URL (this is "
                "separate from NEON_API_KEY, which only manages Neon projects)."
            )
        _engine = create_engine(
            _normalized_url(database_url),
            execution_options={"schema_translate_map": {None: DB_SCHEMA}},
        )
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional session: commits on success, rolls back on error."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
