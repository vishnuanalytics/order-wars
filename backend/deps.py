"""Shared FastAPI dependencies — split out of backend/main.py specifically
so backend/auth.py can depend on get_session too without a circular import
(main.py imports auth.py's route handlers/dependencies; auth.py needs a DB
session the same way every other route does).
"""

from fastapi import Depends
from sqlalchemy.orm import Session, sessionmaker

from db.session import get_sessionmaker


def _default_session_factory() -> sessionmaker[Session]:
    return get_sessionmaker()


def get_session(
    session_factory: sessionmaker[Session] = Depends(_default_session_factory),
):
    """Per-request read/write session — commits on success, always closes.
    `create_game`/`play_game` manage their own short-lived sessions instead
    of reusing this one, since they outlive a single request.
    """
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
