"""Automatic drift remediation, proposing only (roadmap M14.5,
docs/CONFIGURATION_TEMPLATES.md#automatic-proposals).

A template assignment with ``auto_remediate`` asks Baluarte to propose, after each evaluation, a
change set fixing the drift of the settings that assignment decides. The set is built exactly as
a person's would be (each project's effective settings, from stored evidence), is created by
Baluarte itself (``created_by_system``), and waits for the usual approval: one person, or two
for a sensitive set. Nothing is approved or applied automatically.

To avoid proposing the same thing over and over:

- nothing is proposed while an earlier automatic set of the connection is still open (proposed,
  approved, applying, paused or rolling back);
- a set identical to one people rejected (same targets, values and skips) is not proposed
  again; a different drift is.
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.db.models import GitLabInstance, TemplateAssignment, TemplateChangeSet
from baluarte.templates import change_sets
from baluarte.templates.change_sets import (
    DEFAULT_HOURS,
    MAX_TARGETS,
    ChangeSetRefused,
    NothingToChange,
    Source,
    build,
    effective_state,
    fingerprint,
)
from baluarte.templates.selection import Selection

logger = logging.getLogger(__name__)

SYSTEM_ACTOR = "system:template-auto-remediation"
TITLE = "Fix template drift (proposed automatically)"
OPEN = ("proposed", "approved", "applying", "paused", "rolling_back")


def _open_automatic(db: Session, instance_id: int) -> int | None:
    return db.scalars(
        select(TemplateChangeSet.id).where(
            TemplateChangeSet.instance_id == instance_id,
            TemplateChangeSet.created_by_system.is_(True),
            effective_state().in_(OPEN),
        )
    ).first()


def _rejected_before(db: Session, instance_id: int, shape: str) -> int | None:
    rows = db.scalars(
        select(TemplateChangeSet).where(
            TemplateChangeSet.instance_id == instance_id,
            TemplateChangeSet.created_by_system.is_(True),
            TemplateChangeSet.state == "rejected",
        )
    )
    for row in rows:
        if fingerprint(row.document) == shape:
            return row.id
    return None


def propose(db: Session, instance: GitLabInstance) -> TemplateChangeSet | None:
    """Propose a fix for the drift of auto-remediated settings, or None. The caller commits."""
    assignments = tuple(
        sorted(
            db.scalars(
                select(TemplateAssignment.id).where(
                    TemplateAssignment.instance_id == instance.id,
                    TemplateAssignment.auto_remediate.is_(True),
                )
            )
        )
    )
    if not assignments or _open_automatic(db, instance.id) is not None:
        return None
    source = Source({}, None, None, effective=True, assignments=assignments)
    selection = Selection(drift=["drift"])
    try:
        document = build(db, instance, selection, source)
    except NothingToChange:
        return None
    except ChangeSetRefused as exc:
        # More drifting projects than one set holds: say so rather than propose part of it.
        logger.warning("automatic remediation of connection %s not proposed: %s", instance.id, exc)
        audit.record(
            db,
            actor=SYSTEM_ACTOR,
            action="template_change_set.auto_skipped",
            resource_type="gitlab_instance",
            resource_id=str(instance.id),
            details={"connection_id": instance.id, "reason": str(exc), "limit": MAX_TARGETS},
        )
        return None
    if change_sets.totals(document).changes == 0:
        return None
    if _rejected_before(db, instance.id, fingerprint(document)) is not None:
        return None
    return change_sets.create(
        db, instance, TITLE, selection, source, DEFAULT_HOURS, None, SYSTEM_ACTOR
    )
