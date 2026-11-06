"""The label write token and the deployment switch that allows label writes (ADR-0009).

- ``BALUARTE_LABEL_WRITES`` (``true`` or ``false``, default ``false``) is set only in the
  deployment's environment. While it is false no label plan can be applied, whatever is stored.
- The token is a sealed secret (ADR-0008) entered by an administrator. It is never returned by
  the API and never logged; the API shows only whether one is stored and what GitLab reports
  about it (``GET /personal_access_tokens/self`` with that token): its name, scopes and expiry.
  Only the label plan applier decrypts it (``load``); collection keeps using the read-only token.

Storing, replacing and removing it are audited as ``settings.label_write_token_*`` without the
token itself.
"""

from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.configuration.crypto import SecretBox
from baluarte.configuration.store import (
    delete_secret,
    delete_setting,
    read_secret_value,
    read_setting,
    secret_stored,
    write_secret,
    write_setting,
)
from baluarte.integrations.gitlab.label_writer import LabelWriter
from baluarte.integrations.gitlab.settings import GitLabSettings
from baluarte.settings import ConfigurationError, env

LABEL_WRITES = "BALUARTE_LABEL_WRITES"
SECRET = "label_write_token"  # noqa: S105 (the name of a stored secret)
SETTING = "label_write_token"
MAX_TOKEN = 1024


def writes_enabled() -> bool:
    """Whether the deployment allows label plans to be applied (``BALUARTE_LABEL_WRITES``)."""
    raw = env(LABEL_WRITES).lower()
    if raw in ("", "false"):
        return False
    if raw == "true":
        return True
    raise ConfigurationError(f"{LABEL_WRITES} must be true or false")


class TokenStatus(BaseModel):
    set: bool
    writes_enabled: bool
    scopes: list[str] | None = None
    expires_at: str | None = None
    token_name: str | None = None
    checked_at: str | None = None
    check_error: str | None = None


def status(db: Session) -> TokenStatus:
    stored = secret_stored(db, SECRET)
    known = (read_setting(db, SETTING) or {}) if stored else {}
    return TokenStatus(
        set=stored,
        writes_enabled=writes_enabled(),
        scopes=known.get("scopes"),
        expires_at=known.get("expires_at"),
        token_name=known.get("token_name"),
        checked_at=known.get("checked_at"),
        check_error=known.get("check_error"),
    )


class TokenInvalid(ValueError):
    pass


def checked(token: str) -> str:
    token = token.strip()
    if not token or len(token) > MAX_TOKEN or any(c.isspace() for c in token):
        raise TokenInvalid(
            f"The token must be 1 to {MAX_TOKEN} characters without spaces or line breaks"
        )
    return token


def check(
    gitlab: GitLabSettings | None, token: str, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    """What GitLab reports about ``token``; ``check_error`` says why nothing could be read."""
    if gitlab is None:
        return {"check_error": "GitLab is not configured, so the token could not be checked."}
    with LabelWriter(gitlab, token, transport=transport) as writer:
        result = writer.token_self()
    body = result.body if isinstance(result.body, dict) else {}
    if not result.ok:
        reason = result.error or f"GitLab answered {result.status}"
        return {"check_error": f"GET /personal_access_tokens/self failed: {reason}."}
    scopes = body.get("scopes")
    found: dict[str, Any] = {
        "token_name": body.get("name") if isinstance(body.get("name"), str) else None,
        "scopes": [s for s in scopes if isinstance(s, str)] if isinstance(scopes, list) else None,
        "expires_at": body.get("expires_at") if isinstance(body.get("expires_at"), str) else None,
        "check_error": None,
    }
    if body.get("active") is False or body.get("revoked") is True:
        found["check_error"] = "GitLab reports the token as revoked or inactive."
    elif found["scopes"] is not None and "api" not in found["scopes"]:
        found["check_error"] = "The token has no api scope, so GitLab will refuse label writes."
    return found


def save(
    db: Session,
    token: str,
    box: SecretBox,
    gitlab: GitLabSettings | None,
    actor: str,
    transport: httpx.BaseTransport | None = None,
) -> TokenStatus:
    """Seal and store the token with what GitLab reports about it. The caller commits."""
    token = checked(token)
    replaced = secret_stored(db, SECRET)
    known = check(gitlab, token, transport)
    now: datetime = db.execute(select(func.now())).scalar_one()
    known["checked_at"] = now.isoformat().replace("+00:00", "Z")
    write_secret(db, SECRET, token, box, actor)
    write_setting(db, SETTING, known, actor)
    audit.record(
        db,
        actor=actor,
        action="settings.label_write_token_replaced"
        if replaced
        else "settings.label_write_token_set",
        resource_type="setting",
        resource_id=SETTING,
        details=known,
    )
    return status(db)


def remove(db: Session, actor: str) -> bool:
    """Remove the token; False when none was stored. The caller commits."""
    if not secret_stored(db, SECRET):
        return False
    delete_secret(db, SECRET)
    delete_setting(db, SETTING)
    audit.record(
        db,
        actor=actor,
        action="settings.label_write_token_removed",
        resource_type="setting",
        resource_id=SETTING,
    )
    return True


def load(db: Session, box: SecretBox | None) -> str | None:
    """Decrypt the token for the applier; None when none is stored.

    Raises SecretUnavailable when one is stored but cannot be decrypted.
    """
    return read_secret_value(db, SECRET, box)
