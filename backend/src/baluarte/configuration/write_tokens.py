"""GitLab write tokens and the deployment switches that allow their use (ADR-0009, ADR-0012).

Each write token is a sealed secret (ADR-0008) entered by an administrator, with its own
deployment switch set only in the environment (``true`` or ``false``, default ``false``). While
the switch is false nothing is written with that token, whatever is stored. A token is never
returned by the API and never logged; the API shows only whether one is stored and what GitLab
reports about it (``GET /personal_access_tokens/self`` with that token): its name, scopes and
expiry. Only the applier that owns a token decrypts it (``load``); collection keeps using the
read-only token.

Storing, replacing and removing a token are audited as ``settings.<name>_set``, ``_replaced``
and ``_removed``, without the token itself.
"""

from dataclasses import dataclass
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
from baluarte.integrations.gitlab.settings import GitLabSettings
from baluarte.integrations.gitlab.writer import GitLabWriter
from baluarte.settings import ConfigurationError, env

MAX_TOKEN = 1024


class TokenStatus(BaseModel):
    set: bool
    writes_enabled: bool
    scopes: list[str] | None = None
    expires_at: str | None = None
    token_name: str | None = None
    checked_at: str | None = None
    check_error: str | None = None


class TokenInvalid(ValueError):
    pass


def checked(token: str) -> str:
    token = token.strip()
    if not token or len(token) > MAX_TOKEN or any(c.isspace() for c in token):
        raise TokenInvalid(
            f"The token must be 1 to {MAX_TOKEN} characters without spaces or line breaks"
        )
    return token


@dataclass(frozen=True)
class WriteToken:
    # The stored secret's and setting's name, also the audit action's stem.
    name: str
    # The deployment switch, an environment variable.
    switch: str
    # What the token writes, for messages: "label writes", "remediation writes".
    writes: str

    def writes_enabled(self) -> bool:
        """Whether the deployment allows this token to be used (its switch)."""
        raw = env(self.switch).lower()
        if raw in ("", "false"):
            return False
        if raw == "true":
            return True
        raise ConfigurationError(f"{self.switch} must be true or false")

    def status(self, db: Session) -> TokenStatus:
        stored = secret_stored(db, self.name)
        known = (read_setting(db, self.name) or {}) if stored else {}
        return TokenStatus(
            set=stored,
            writes_enabled=self.writes_enabled(),
            scopes=known.get("scopes"),
            expires_at=known.get("expires_at"),
            token_name=known.get("token_name"),
            checked_at=known.get("checked_at"),
            check_error=known.get("check_error"),
        )

    def check(
        self,
        gitlab: GitLabSettings | None,
        token: str,
        transport: httpx.BaseTransport | None = None,
    ) -> dict[str, Any]:
        """What GitLab reports about ``token``; ``check_error`` says why nothing could be read."""
        if gitlab is None:
            return {"check_error": "GitLab is not configured, so the token could not be checked."}
        with GitLabWriter(gitlab, token, transport=transport) as writer:
            result = writer.token_self()
        body = result.body if isinstance(result.body, dict) else {}
        if not result.ok:
            reason = result.error or f"GitLab answered {result.status}"
            return {"check_error": f"GET /personal_access_tokens/self failed: {reason}."}
        scopes = body.get("scopes")
        found: dict[str, Any] = {
            "token_name": body.get("name") if isinstance(body.get("name"), str) else None,
            "scopes": [s for s in scopes if isinstance(s, str)]
            if isinstance(scopes, list)
            else None,
            "expires_at": body.get("expires_at")
            if isinstance(body.get("expires_at"), str)
            else None,
            "check_error": None,
        }
        if body.get("active") is False or body.get("revoked") is True:
            found["check_error"] = "GitLab reports the token as revoked or inactive."
        elif found["scopes"] is not None and "api" not in found["scopes"]:
            found["check_error"] = (
                f"The token has no api scope, so GitLab will refuse {self.writes}."
            )
        return found

    def save(
        self,
        db: Session,
        token: str,
        box: SecretBox,
        gitlab: GitLabSettings | None,
        actor: str,
        transport: httpx.BaseTransport | None = None,
    ) -> TokenStatus:
        """Seal and store the token with what GitLab reports about it. The caller commits."""
        token = checked(token)
        replaced = secret_stored(db, self.name)
        known = self.check(gitlab, token, transport)
        now: datetime = db.execute(select(func.now())).scalar_one()
        known["checked_at"] = now.isoformat().replace("+00:00", "Z")
        write_secret(db, self.name, token, box, actor)
        write_setting(db, self.name, known, actor)
        audit.record(
            db,
            actor=actor,
            action=f"settings.{self.name}_{'replaced' if replaced else 'set'}",
            resource_type="setting",
            resource_id=self.name,
            details=known,
        )
        return self.status(db)

    def remove(self, db: Session, actor: str) -> bool:
        """Remove the token; False when none was stored. The caller commits."""
        if not secret_stored(db, self.name):
            return False
        delete_secret(db, self.name)
        delete_setting(db, self.name)
        audit.record(
            db,
            actor=actor,
            action=f"settings.{self.name}_removed",
            resource_type="setting",
            resource_id=self.name,
        )
        return True

    def load(self, db: Session, box: SecretBox | None) -> str | None:
        """Decrypt the token for its applier; None when none is stored.

        Raises SecretUnavailable when one is stored but cannot be decrypted.
        """
        return read_secret_value(db, self.name, box)
