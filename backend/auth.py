"""Google Sign-In: verify a Google ID token server-side, issue this app's
own session token, and FastAPI dependencies to read that token back.

Deliberately optional everywhere it's used — signing in enriches ownership
and attribution (which scenarios are "yours", who annotated what) but
nothing in this app requires it, preserving every no-auth flow already
built (see CLAUDE.md Non-goals: auth was an explicit non-goal, and even
now this is additive, not a wall in front of the app). Bearer-token based,
not cookie-based, which is also why backend/main.py's wide-open CORS stays
safe with this added: a token only ever travels because this app's own
frontend JS reads it out of its own localStorage and attaches it
explicitly — nothing ambient a forged cross-origin request could ride
along on the way a cookie would.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Header
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from sqlalchemy.orm import Session

from db.models import User

# How long this app's own session token stays valid, independent of
# Google's ID token (which expires in ~1 hour and isn't meant to be a
# standing app session — re-prompting sign-in that often would be a bad
# experience for what's meant to be a lightweight ownership/attribution
# feature, not a bank).
SESSION_TOKEN_TTL = timedelta(days=30)


def _google_client_id() -> str:
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    if not client_id:
        raise RuntimeError(
            "GOOGLE_CLIENT_ID is not set — copy your Google Cloud Console "
            "OAuth 2.0 Client ID into .env (see .env.example)."
        )
    return client_id


def _session_secret() -> str:
    secret = os.environ.get("SESSION_SECRET")
    if not secret:
        raise RuntimeError(
            "SESSION_SECRET is not set — generate one with `python -c "
            '"import secrets; print(secrets.token_urlsafe(32))"` and put '
            "it in .env (see .env.example)."
        )
    return secret


def verify_google_id_token(token: str) -> dict:
    """Verifies signature, expiry, issuer, and audience (must match this
    app's own GOOGLE_CLIENT_ID — without checking `aud`, a token minted for
    a *different* Google app would still pass signature verification, since
    it's still a genuine token from Google, just not one meant for us).
    Raises ValueError if any of that fails — the /auth/google route turns
    that into a 401, not a 500, since a bad/expired token from the client
    is an auth failure, not a server bug.
    """
    return google_id_token.verify_oauth2_token(
        token, google_requests.Request(), audience=_google_client_id()
    )


def upsert_user_from_google_claims(session: Session, claims: dict) -> User:
    """Looks up by `sub` (Google's stable per-account id — not email, which
    a person can change) and updates name/email/picture from the latest
    sign-in, since those can drift over time and the claims are already
    freshly verified.
    """
    google_sub = claims["sub"]
    user = session.query(User).filter_by(google_sub=google_sub).one_or_none()
    if user is None:
        user = User(google_sub=google_sub, email=claims["email"], name=claims.get("name") or claims["email"])
        session.add(user)
    else:
        user.email = claims["email"]
        user.name = claims.get("name") or claims["email"]
    user.picture_url = claims.get("picture")
    session.flush()  # assigns user.id if newly created, without committing yet
    return user


def issue_session_token(user_id: uuid.UUID) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "iat": now, "exp": now + SESSION_TOKEN_TTL}
    return jwt.encode(payload, _session_secret(), algorithm="HS256")


def _user_id_from_session_token(token: str) -> uuid.UUID | None:
    """None for anything wrong with the token — including SESSION_SECRET
    itself not being configured (RuntimeError from _session_secret()), not
    just a malformed/expired/wrong-signature token (jwt.PyJWTError) or a
    malformed payload (KeyError/ValueError). Every caller of this treats
    None as "anonymous," never as an error — a server that simply hasn't
    had Google Sign-In configured yet should serve every request as
    anonymous, not 500 the moment any client happens to send an
    Authorization header. Caught live by a test hitting /auth/me with a
    bogus token and no SESSION_SECRET set at all — that route is supposed
    to answer "not signed in" (401), not crash (500).
    """
    try:
        payload = jwt.decode(token, _session_secret(), algorithms=["HS256"])
        return uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError, RuntimeError):
        return None


def get_current_user_optional(
    session: Session,
    authorization: str | None = Header(default=None),
) -> User | None:
    """None for an anonymous request — not an error. Every route using this
    must keep working with no `Authorization` header at all, the same as
    before this feature existed.

    Not itself a `Depends(...)`-decorated dependency (it takes `session`
    as a plain first argument, not `Depends(get_session)`) so a route can
    call it with the *same* session it's already using for the rest of the
    request, instead of opening a second one — see main.py's call sites.
    """
    if not authorization or not authorization.startswith("Bearer "):
        return None
    user_id = _user_id_from_session_token(authorization.removeprefix("Bearer "))
    if user_id is None:
        return None
    return session.get(User, user_id)
