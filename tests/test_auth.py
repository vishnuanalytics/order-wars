"""backend/auth.py in isolation — google_id_token.verify_oauth2_token is
never called against real Google servers here; only the parts around it
(user upsert, this app's own session token issue/decode) get exercised
directly, with GOOGLE_CLIENT_ID/SESSION_SECRET set via monkeypatch so the
test doesn't depend on a real .env.
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import (
    _user_id_from_session_token,
    get_current_user_optional,
    issue_session_token,
    upsert_user_from_google_claims,
)
from db.models import Base, User


@pytest.fixture(autouse=True)
def _session_secret(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret-not-for-real-use-padded-to-32-bytes")


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


GOOGLE_CLAIMS = {
    "sub": "108234098234098234",
    "email": "player@example.com",
    "name": "Test Player",
    "picture": "https://example.com/pic.jpg",
}


def test_upsert_creates_a_new_user_from_claims(session):
    user = upsert_user_from_google_claims(session, GOOGLE_CLAIMS)
    session.commit()

    assert user.google_sub == GOOGLE_CLAIMS["sub"]
    assert user.email == "player@example.com"
    assert user.name == "Test Player"
    assert user.picture_url == "https://example.com/pic.jpg"
    assert session.query(User).count() == 1


def test_upsert_updates_the_same_user_on_repeat_sign_in(session):
    first = upsert_user_from_google_claims(session, GOOGLE_CLAIMS)
    session.commit()
    first_id = first.id

    changed_claims = {**GOOGLE_CLAIMS, "name": "New Display Name", "email": "newmail@example.com"}
    second = upsert_user_from_google_claims(session, changed_claims)
    session.commit()

    assert second.id == first_id  # same account, looked up by sub — not a duplicate
    assert session.query(User).count() == 1
    assert second.name == "New Display Name"
    assert second.email == "newmail@example.com"


def test_upsert_falls_back_to_email_when_google_omits_name(session):
    claims = {"sub": "abc", "email": "noname@example.com", "picture": None}
    user = upsert_user_from_google_claims(session, claims)
    assert user.name == "noname@example.com"


def test_issue_and_decode_session_token_round_trips(session):
    user = upsert_user_from_google_claims(session, GOOGLE_CLAIMS)
    session.commit()

    token = issue_session_token(user.id)
    assert _user_id_from_session_token(token) == user.id


def test_decode_rejects_a_garbage_token():
    assert _user_id_from_session_token("not-a-real-token") is None


def test_decode_rejects_a_token_signed_with_a_different_secret(monkeypatch):
    token = issue_session_token(uuid.uuid4())
    monkeypatch.setenv("SESSION_SECRET", "a-different-secret")
    assert _user_id_from_session_token(token) is None


def test_get_current_user_optional_returns_none_without_a_header(session):
    assert get_current_user_optional(session, authorization=None) is None


def test_get_current_user_optional_returns_none_for_a_malformed_header(session):
    assert get_current_user_optional(session, authorization="not-bearer-scheme") is None


def test_get_current_user_optional_returns_the_real_user_for_a_valid_token(session):
    user = upsert_user_from_google_claims(session, GOOGLE_CLAIMS)
    session.commit()
    token = issue_session_token(user.id)

    found = get_current_user_optional(session, authorization=f"Bearer {token}")
    assert found is not None
    assert found.id == user.id
