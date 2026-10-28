"""Append-only audit trail, stored in ``audit_event`` and mirrored to the ``baluarte.audit`` log.

Details are redacted before they are hashed and stored. The log line is written only after the
transaction commits, so a rolled-back action never appears in the log stream.
"""

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

from baluarte.db.models import AuditEvent
from baluarte.logs import redact_value

audit_logger = logging.getLogger("baluarte.audit")

_PENDING = "baluarte_audit_pending"


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
    safe_details: dict[str, Any] = redact_value(details or {})
    row = AuditEvent(
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=safe_details,
        details_digest=details_digest(safe_details),
    )
    db.add(row)
    entry = {
        "actor": actor,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "details": safe_details,
        "details_digest": row.details_digest,
    }
    db.info.setdefault(_PENDING, []).append((row, entry))


@event.listens_for(Session, "after_flush")
def _capture_ids(session: Session, _context: object) -> None:
    for row, entry in session.info.get(_PENDING, []):
        if row.id is not None:
            entry["id"] = row.id


@event.listens_for(Session, "after_commit")
def _emit(session: Session) -> None:
    for _row, entry in session.info.pop(_PENDING, []):
        entry.setdefault("logged_at", datetime.now(UTC).isoformat(timespec="milliseconds"))
        audit_logger.info("audit %s by %s", entry["action"], entry["actor"], extra={"audit": entry})


@event.listens_for(Session, "after_rollback")
def _discard(session: Session) -> None:
    session.info.pop(_PENDING, None)
