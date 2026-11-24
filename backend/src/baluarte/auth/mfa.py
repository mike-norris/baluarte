"""Built-in TOTP enrollment, verification, recovery codes and reset
(docs/SECURITY.md#built-in-totp). The RFC 6238 arithmetic is in ``totp``."""

import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from baluarte.auth import totp
from baluarte.auth.sessions import utcnow
from baluarte.configuration.crypto import Sealed, SecretBox
from baluarte.db.models import AppUser, UserRecoveryCode, UserSession, UserTotp


def secret_name(user_id: int) -> str:
    """The name each sealed TOTP secret is bound to, so it cannot be moved to another user."""
    return f"totp:{user_id}"


def _seal(box: SecretBox, row: UserTotp, secret: str) -> None:
    sealed = box.seal(secret_name(row.user_id), secret)
    row.key_id, row.nonce, row.ciphertext = sealed.key_id, sealed.nonce, sealed.ciphertext


def _open(box: SecretBox, row: UserTotp) -> str:
    return box.open(
        secret_name(row.user_id), Sealed(row.key_id, bytes(row.nonce), bytes(row.ciphertext))
    )


def enrolled(db: Session, user_id: int) -> bool:
    return (
        db.scalar(
            select(UserTotp.user_id).where(
                UserTotp.user_id == user_id, UserTotp.confirmed_at.is_not(None)
            )
        )
        is not None
    )


@dataclass(frozen=True)
class Enrollment:
    secret: str
    uri: str
    qr: list[str]


def start_enrollment(db: Session, box: SecretBox, user: AppUser) -> Enrollment | None:
    """A new secret for a user without a confirmed authenticator, replacing any unconfirmed one;
    None when the user already has one."""
    row = db.get(UserTotp, user.id, with_for_update=True)
    if row is not None and row.confirmed_at is not None:
        return None
    if row is None:
        row = UserTotp(user_id=user.id)
        db.add(row)
    secret = totp.new_secret()
    _seal(box, row, secret)
    row.created_at = utcnow()
    row.last_step = None
    uri = totp.otpauth_uri(secret, user.email or user.display_name or f"user {user.id}")
    return Enrollment(secret=secret, uri=uri, qr=totp.qr_rows(uri))


@dataclass(frozen=True)
class Verified:
    # "totp" or "recovery".
    method: str
    # Recovery codes made by confirming a new authenticator, shown once; else empty.
    recovery_codes: list[str]
    # Recovery codes left unused, after a recovery code was used; else None.
    recovery_left: int | None = None

    @property
    def enrolled(self) -> bool:
        return bool(self.recovery_codes)


class NotEnrolled(Exception):
    """There is no authenticator to check a code against."""


def verify(
    db: Session, box: SecretBox, user_id: int, code: str, now: float | None = None
) -> Verified | None:
    """Check a code from the user's authenticator, or one of their recovery codes. The first code
    from a new authenticator confirms it and makes the recovery codes. None when it is wrong."""
    row = db.get(UserTotp, user_id, with_for_update=True)
    if row is None:
        raise NotEnrolled
    entered = code.strip()
    at = time.time() if now is None else now
    if row.confirmed_at is None:
        step = totp.verify(_open(box, row), entered, at, None)
        if step is None:
            return None
        row.confirmed_at, row.last_step = utcnow(), step
        return Verified("totp", replace_recovery_codes(db, user_id))
    if not totp.looks_like_recovery_code(entered):
        step = totp.verify(_open(box, row), entered, at, row.last_step)
        if step is None:
            return None
        row.last_step = step
        return Verified("totp", [])
    used = db.execute(
        update(UserRecoveryCode)
        .where(
            UserRecoveryCode.user_id == user_id,
            UserRecoveryCode.digest == totp.recovery_digest(entered),
            UserRecoveryCode.used_at.is_(None),
        )
        .values(used_at=utcnow())
        .returning(UserRecoveryCode.id)
    ).first()
    if used is None:
        return None
    return Verified("recovery", [], recovery_codes_left(db, user_id))


def replace_recovery_codes(db: Session, user_id: int) -> list[str]:
    db.execute(delete(UserRecoveryCode).where(UserRecoveryCode.user_id == user_id))
    codes = totp.new_recovery_codes()
    db.add_all(
        UserRecoveryCode(user_id=user_id, digest=totp.recovery_digest(code)) for code in codes
    )
    return codes


def recovery_codes_left(db: Session, user_id: int) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(UserRecoveryCode)
            .where(UserRecoveryCode.user_id == user_id, UserRecoveryCode.used_at.is_(None))
        )
        or 0
    )


@dataclass(frozen=True)
class EnrolledUser:
    user_id: int
    email: str | None
    display_name: str | None
    confirmed_at: datetime
    recovery_codes_left: int


def enrolled_users(db: Session) -> list[EnrolledUser]:
    left = (
        select(func.count())
        .select_from(UserRecoveryCode)
        .where(UserRecoveryCode.user_id == UserTotp.user_id, UserRecoveryCode.used_at.is_(None))
        .scalar_subquery()
    )
    rows = db.execute(
        select(AppUser, UserTotp.confirmed_at, left)
        .join(UserTotp, UserTotp.user_id == AppUser.id)
        .where(UserTotp.confirmed_at.is_not(None))
        .order_by(AppUser.display_name, AppUser.id)
    ).all()
    return [
        EnrolledUser(user.id, user.email, user.display_name, confirmed, count)
        for user, confirmed, count in rows
        if confirmed is not None
    ]


def reset(db: Session, user_id: int) -> int | None:
    """Remove a user's authenticator and recovery codes, and end their live sessions, so their
    next sign-in sets up a new one. The number of sessions ended; None when not enrolled."""
    removed = db.execute(
        delete(UserTotp).where(UserTotp.user_id == user_id).returning(UserTotp.user_id)
    ).first()
    db.execute(delete(UserRecoveryCode).where(UserRecoveryCode.user_id == user_id))
    if removed is None:
        return None
    ended = db.execute(
        update(UserSession)
        .where(
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > func.now(),
        )
        .values(revoked_at=utcnow())
        .returning(UserSession.id)
    ).all()
    return len(ended)


def reseal(db: Session, box: SecretBox) -> int:
    """Seal every TOTP secret under the current key, for key rotation. The number resealed."""
    count = 0
    for row in db.scalars(select(UserTotp).with_for_update()):
        if row.key_id == box.key_id:
            continue
        _seal(box, row, _open(box, row))
        count += 1
    return count
