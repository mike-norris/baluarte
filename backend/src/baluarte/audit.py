"""Append-only audit trail. Structured audit logging is extended in BAL-010."""

import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from baluarte.db.models import AuditEvent


def details_digest(details: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON form: sorted keys, no insignificant whitespace, UTF-8."""
    canonical = json.dumps(details, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def record(
    db: Session,
    *,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Add an audit event to the session. The caller commits it with the action it records."""
    details = details or {}
    db.add(
        AuditEvent(
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            details_digest=details_digest(details),
        )
    )
