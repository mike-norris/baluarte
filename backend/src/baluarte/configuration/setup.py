"""First-run setup: the one-time setup code, setup sessions and completion (ADR-0008).

Before anyone can sign in, the web UI is unlocked by a setup code. Every API start while setup is
incomplete issues a new code and logs it once; ``baluarte-setup-code`` issues one on demand. Only
the code's SHA-256 digest is stored, and failed attempts are limited in the database so that
every API replica shares the limit.

Setup completes when the first user with the admin role signs in through the identity provider
entered during setup. From then on the setup endpoints refuse every request and settings change
only through the admin Settings page. When the deployment's environment configures sign-in,
there is nothing to set up and the state is always ``complete``.
"""

import hashlib
import hmac
import logging
import secrets
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import Engine, delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from baluarte.auth.settings import deployment_auth_present
from baluarte.configuration.store import OIDC, read_setting
from baluarte.db.models import SetupSession, SetupState

logger = logging.getLogger(__name__)

State = Literal["needs_setup", "awaiting_admin_sign_in", "complete"]
CodeCheck = Literal["ok", "invalid", "locked"]

SESSION_TTL = timedelta(minutes=60)
MAX_FAILED_ATTEMPTS = 5
FAILURE_WINDOW = timedelta(minutes=15)
# No 0/O or 1/I/L, so a code read from a log is hard to mistype. 12 characters: about 59 bits.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_GROUPS = 3
CODE_GROUP_LENGTH = 4


def utcnow() -> datetime:
    return datetime.now(UTC)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_code() -> str:
    return "-".join(
        "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_GROUP_LENGTH))
        for _ in range(CODE_GROUPS)
    )


def normalise_code(raw: str) -> str:
    """Accept the code in any case, with or without dashes or spaces."""
    compact = "".join(char for char in raw.upper() if char.isalnum())
    return "-".join(
        compact[start : start + CODE_GROUP_LENGTH]
        for start in range(0, len(compact), CODE_GROUP_LENGTH)
    )


def setup_csrf_token(session_token: str) -> str:
    return sha256_hex("baluarte-setup-csrf:" + session_token)


def _row(db: Session, *, lock: bool = False) -> SetupState:
    row = db.get(SetupState, 1, with_for_update=lock)
    if row is None:  # the migration inserts it; recreate it if someone deleted it
        row = SetupState(id=1, failed_attempts=0)
        db.add(row)
        db.flush()
    return row


def setup_state(db: Session) -> State:
    if deployment_auth_present():
        return "complete"
    if _row(db).completed_at is not None:
        return "complete"
    if read_setting(db, OIDC) is not None:
        return "awaiting_admin_sign_in"
    return "needs_setup"


def issue_code(db: Session, *, revoke_sessions: bool = False) -> str | None:
    """Store a new setup code's digest and return the code; None once setup is complete.

    The previous code stops working. ``revoke_sessions`` also ends every setup session, for when
    the old code may have been seen by someone else.
    """
    row = _row(db, lock=True)
    if setup_state(db) == "complete":
        return None
    code = new_code()
    row.code_digest = sha256_hex(code)
    row.code_issued_at = utcnow()
    row.failed_attempts = 0
    row.failures_since = None
    if revoke_sessions:
        db.execute(delete(SetupSession))
    return code


def check_code(db: Session, code: str) -> CodeCheck:
    """Compare a code with the stored digest, counting failures. The caller commits."""
    row = _row(db, lock=True)
    now = utcnow()
    if row.failures_since is not None and now - row.failures_since >= FAILURE_WINDOW:
        row.failed_attempts = 0
        row.failures_since = None
    if row.failed_attempts >= MAX_FAILED_ATTEMPTS:
        return "locked"
    if row.code_digest is not None and hmac.compare_digest(
        row.code_digest, sha256_hex(normalise_code(code))
    ):
        row.failed_attempts = 0
        row.failures_since = None
        return "ok"
    if row.failures_since is None:
        row.failures_since = now
    row.failed_attempts += 1
    return "invalid"


def create_setup_session(db: Session) -> str:
    now = utcnow()
    db.execute(delete(SetupSession).where(SetupSession.expires_at <= now))
    token = secrets.token_urlsafe(32)
    db.add(
        SetupSession(token_digest=sha256_hex(token), created_at=now, expires_at=now + SESSION_TTL)
    )
    return token


def setup_session_valid(db: Session, token: str) -> bool:
    return (
        db.scalar(
            select(SetupSession.token_digest).where(
                SetupSession.token_digest == sha256_hex(token), SetupSession.expires_at > utcnow()
            )
        )
        is not None
    )


def end_setup_session(db: Session, token: str) -> None:
    db.execute(delete(SetupSession).where(SetupSession.token_digest == sha256_hex(token)))


def complete_setup(db: Session, user_id: int) -> bool:
    """Mark setup complete for the first admin sign-in; True only for that sign-in.

    Ends every setup session and clears the code. The caller audits and commits.
    """
    row = _row(db, lock=True)
    if row.completed_at is not None or setup_state(db) != "awaiting_admin_sign_in":
        return False
    row.completed_at = utcnow()
    row.completed_by_user_id = user_id
    row.code_digest = None
    row.failed_attempts = 0
    row.failures_since = None
    db.execute(delete(SetupSession))
    return True


def issue_startup_code(engine: Engine) -> bool:
    """Issue and log a setup code if setup is incomplete. False when the database is not ready."""
    try:
        with Session(engine) as db:
            code = issue_code(db)
            db.commit()
    except SQLAlchemyError as exc:
        logger.info("setup: database not ready for a setup code yet (%s)", type(exc).__name__)
        return False
    if code is not None:
        logger.warning(
            "setup: Baluarte is not set up yet. Open the web UI and enter the setup code %s. "
            "It works until setup completes or a new code is issued (on the next API start, "
            "or with baluarte-setup-code).",
            code,
        )
    return True


def issue_startup_code_when_ready(
    engine_provider: Callable[[], Engine | None],
    attempts: int = 60,
    interval: float = 5.0,
) -> threading.Thread:
    """Issue the startup code in the background, retrying until migrations have run."""

    def run() -> None:
        for _ in range(attempts):
            engine = engine_provider()
            if engine is None:
                return
            if issue_startup_code(engine):
                return
            time.sleep(interval)
        logger.warning("setup: no setup code issued; run baluarte-setup-code to issue one")

    thread = threading.Thread(target=run, name="setup-code", daemon=True)
    thread.start()
    return thread
