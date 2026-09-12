"""db/session.py's lazy singleton locking — mocked create_engine, never a
real DATABASE_URL/Neon connection. Module-level _engine/_SessionLocal are
reset around every test so this file's state can't leak into (or be leaked
into by) other tests or the real app.
"""

import threading
from unittest.mock import MagicMock

import pytest

import db.session as db_session_module


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch):
    monkeypatch.setattr(db_session_module, "_engine", None)
    monkeypatch.setattr(db_session_module, "_SessionLocal", None)


def _concurrent_calls(fn, n: int) -> list:
    """Call `fn()` from `n` threads released at (as close to) the same
    instant as possible, to actually exercise the race window a
    check-then-act singleton would otherwise have.
    """
    results = []
    barrier = threading.Barrier(n)

    def _call():
        barrier.wait(timeout=5)
        results.append(fn())

    threads = [threading.Thread(target=_call) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    return results


def test_get_engine_creates_exactly_one_engine_under_concurrent_first_calls(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake-host/fake-db")
    created = []

    def _fake_create_engine(*args, **kwargs):
        created.append(object())
        return MagicMock(name="engine")

    monkeypatch.setattr(db_session_module, "create_engine", _fake_create_engine)

    results = _concurrent_calls(db_session_module.get_engine, n=16)

    assert len(created) == 1
    assert len({id(r) for r in results}) == 1


def test_get_sessionmaker_is_a_singleton_under_concurrent_first_calls(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake-host/fake-db")
    monkeypatch.setattr(db_session_module, "create_engine", lambda *a, **k: MagicMock(name="engine"))
    made = []
    real_sessionmaker = db_session_module.sessionmaker

    def _counting_sessionmaker(*args, **kwargs):
        sm = real_sessionmaker(*args, **kwargs)
        made.append(sm)
        return sm

    monkeypatch.setattr(db_session_module, "sessionmaker", _counting_sessionmaker)

    results = _concurrent_calls(db_session_module.get_sessionmaker, n=16)

    assert len(made) == 1
    assert len({id(r) for r in results}) == 1


def test_get_engine_raises_clearly_when_database_url_unset(monkeypatch):
    # load_dotenv() (called inside get_engine()) would otherwise reload the
    # real DATABASE_URL from .env right back in, since it only fills in
    # vars that are absent — exactly the case delenv just created.
    monkeypatch.setattr(db_session_module, "load_dotenv", lambda: None)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        db_session_module.get_engine()
