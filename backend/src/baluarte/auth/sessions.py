"""Database-backed sign-in state, users and sessions.

Bearer values (session tokens, OIDC state) are random and only their SHA-256 digests are
stored, so a database read does not yield a usable cookie.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from baluarte.auth.oidc import PendingLogin
from baluarte.auth.roles import Role
from baluarte.auth.settings import AuthSettings
from baluarte.db.models import AppUser, OidcLogin, UserSession

LOGIN_TTL = timedelta(minutes=10)
# Avoid a write on every request; idle expiry is accurate to within this interval.
LAST_SEEN_RESOLUTION = timedelta(minutes=1)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def csrf_token(session_token: str) -> str:
    """Per-session CSRF token, derived so it never needs storing and cannot reveal the cookie."""
    return sha256_hex("baluarte-csrf:" + session_token)


def utcnow() -> datetime:
    return datetime.now(UTC)


def save_pending_login(db: Session, pending: PendingLogin, return_to: str) -> None:
    now = utcnow()
    db.execute(delete(OidcLogin).where(OidcLogin.expires_at <= now))
    db.add(
        OidcLogin(
            state_digest=sha256_hex(pending.state),
            nonce=pending.nonce,
            code_verifier=pending.code_verifier,
            return_to=return_to,
            expires_at=now + LOGIN_TTL,
        )
    )


def take_pending_login(db: Session, state: str) -> OidcLogin | None:
    """Remove and return an unexpired pending sign-in, so each state is usable once."""
    return db.execute(
        delete(OidcLogin)
        .where(OidcLogin.state_digest == sha256_hex(state), OidcLogin.expires_at > utcnow())
        .returning(OidcLogin)
    ).scalar_one_or_none()


def upsert_user(
    db: Session, issuer: str, subject: str, email: str | None, display_name: str | None
) -> AppUser:
    now = utcnow()
    values = {"email": email, "display_name": display_name, "last_login_at": now}
    statement = (
        insert(AppUser)
        .values(issuer=issuer, subject=subject, **values)
        .on_conflict_do_update(index_elements=["issuer", "subject"], set_=values)
        .returning(AppUser)
    )
    return db.execute(statement).scalar_one()


def create_session(
    db: Session, user: AppUser, roles: frozenset[Role], settings: AuthSettings
) -> str:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    db.add(
        UserSession(
            token_digest=sha256_hex(token),
            user_id=user.id,
            roles=sorted(roles),
            created_at=now,
            last_seen_at=now,
            expires_at=now + settings.session_lifetime,
        )
    )
    return token


@dataclass(frozen=True)
class ActiveSession:
    session: UserSession
    user: AppUser


def resolve_session(db: Session, token: str, settings: AuthSettings) -> ActiveSession | None:
    """Return the live session for a cookie value, refreshing its idle timer."""
    now = utcnow()
    row = db.execute(
        select(UserSession, AppUser)
        .join(AppUser, AppUser.id == UserSession.user_id)
        .where(
            UserSession.token_digest == sha256_hex(token),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
            UserSession.last_seen_at > now - settings.session_idle,
        )
    ).one_or_none()
    if row is None:
        return None
    session, user = row
    if now - session.last_seen_at > LAST_SEEN_RESOLUTION:
        session.last_seen_at = now
        db.commit()
    return ActiveSession(session=session, user=user)


def revoke_session(db: Session, session: UserSession) -> None:
    session.revoked_at = utcnow()


def revoke_token(db: Session, token: str) -> None:
    """Revoke the session behind a cookie value, if it is still live."""
    db.execute(
        update(UserSession)
        .where(UserSession.token_digest == sha256_hex(token), UserSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
