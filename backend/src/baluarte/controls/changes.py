"""An organization's changes to the control mapping, with justification and review.

The default mapping (``baluarte.controls.catalogue``) is a starting point. An organization adds a
rule type to a control (``map``), removes a default pair (``unmap``), scopes a control out
(``not_applicable``), records that it is met outside GitLab (``manual``), or ends one of those
(``withdraw``). Every change needs a justification; scoping out and manual controls also need a
``review_by`` date, after which they stop counting until renewed.

Review follows the ``governance`` setting exactly like baseline changes
(``baluarte.policy.changes``): while it is required a change is ``proposed`` until someone with
``policy:write`` other than the proposer approves it; otherwise it applies at once and is
recorded ``self_approved``. A database trigger enforces the lifecycle again.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.controls.catalogue import MAPPING_VERSION, control, is_default, known_rule_type
from baluarte.db.models import ControlMappingChange
from baluarte.policy.changes import Actor, ChangeConflict, NotAllowed, review_required

ChangeKind = Literal["map", "unmap", "not_applicable", "manual", "withdraw"]
ChangeState = Literal["proposed", "active", "rejected", "withdrawn"]
# Kinds that say how a control is met instead of which rule types support it.
STATUS_KINDS = ("not_applicable", "manual")
OPEN_STATES = ("proposed", "active")
# How far ahead a not-applicable or manual control may be set for review.
MAX_REVIEW_DAYS = 365

# Why a change cannot be made, as API reason codes.
Reason = Literal[
    "control_unknown",
    "rule_type_unknown",
    "duplicate",
    "not_default",
    "already_mapped",
    "status_exists",
    "supersedes_not_active",
    "supersedes_other_control",
    "supersedes_invalid_kind",
    "supersede_pending",
    "review_by_required",
    "review_by_past",
    "review_by_too_far",
]

MESSAGES: dict[Reason, str] = {
    "control_unknown": "The control is not in the catalogue.",
    "rule_type_unknown": "The rule type is not in the baseline schema.",
    "duplicate": "The same change is already in force or waiting for review.",
    "not_default": "Only a default pair can be unmapped.",
    "already_mapped": "The rule type is already mapped to the control.",
    "status_exists": "The control is already not applicable or manual, or a change to that is "
    "waiting for review; supersede it to renew it.",
    "supersedes_not_active": "The change to supersede is not in force.",
    "supersedes_other_control": "The change to supersede is for another control.",
    "supersedes_invalid_kind": "A withdraw cannot be superseded, and a not-applicable or manual "
    "change can only renew another one.",
    "supersede_pending": "Another change superseding it is already waiting for review.",
    "review_by_required": "A not-applicable or manual change needs a review_by date.",
    "review_by_past": "The review_by date must be after today.",
    "review_by_too_far": f"The review_by date must be at most {MAX_REVIEW_DAYS} days ahead.",
}

__all__ = ["Actor", "ChangeConflict", "MappingConflict", "NotAllowed"]


class MappingConflict(ChangeConflict):
    """The change conflicts with the catalogue or with changes already made."""

    def __init__(self, reasons: list[Reason]) -> None:
        super().__init__(" ".join(MESSAGES[reason] for reason in reasons))
        self.reasons = reasons


@dataclass(frozen=True)
class Proposal:
    framework: str
    control_id: str
    kind: ChangeKind
    justification: str
    rule_type: str | None = None
    evidence_ref: str | None = None
    review_by: date | None = None
    supersedes_id: int | None = None


def _lock(db: Session, framework: str, control_id: str) -> None:
    key = f"control_mapping:{framework}:{control_id}"
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext(key))))


def _open(db: Session, framework: str, control_id: str, exclude: int | None) -> list[Any]:
    statement = select(ControlMappingChange).where(
        ControlMappingChange.framework == framework,
        ControlMappingChange.control_id == control_id,
        ControlMappingChange.state.in_(OPEN_STATES),
    )
    if exclude is not None:
        statement = statement.where(ControlMappingChange.id != exclude)
    return list(db.scalars(statement))


def conflicts(db: Session, proposal: Proposal, exclude: int | None = None) -> list[Reason]:
    """Every reason ``proposal`` cannot be made now; empty when it can.

    ``exclude`` is the change itself when it is being approved.
    """
    if control(proposal.framework, proposal.control_id) is None:
        return ["control_unknown"]
    reasons: list[Reason] = []
    if proposal.rule_type is not None and not known_rule_type(proposal.rule_type):
        reasons.append("rule_type_unknown")
    others = _open(db, proposal.framework, proposal.control_id, exclude)
    kind = proposal.kind
    if kind in ("map", "unmap"):
        if any(o.kind == kind and o.rule_type == proposal.rule_type for o in others):
            reasons.append("duplicate")
        default = proposal.rule_type is not None and is_default(
            proposal.rule_type, proposal.framework, proposal.control_id
        )
        if kind == "unmap" and not default:
            reasons.append("not_default")
        if kind == "map" and default:
            reasons.append("already_mapped")
    if proposal.supersedes_id is not None:
        reasons.extend(_supersede_conflicts(db, proposal, exclude))
    if kind in STATUS_KINDS:
        current = [o for o in others if o.kind in STATUS_KINDS and o.id != proposal.supersedes_id]
        if current:
            reasons.append("status_exists")
        reasons.extend(_review_by_conflicts(db, proposal.review_by))
    return reasons


def _supersede_conflicts(db: Session, proposal: Proposal, exclude: int | None) -> list[Reason]:
    superseded = db.get(ControlMappingChange, proposal.supersedes_id)
    if superseded is None or superseded.state != "active":
        return ["supersedes_not_active"]
    reasons: list[Reason] = []
    if (superseded.framework, superseded.control_id) != (proposal.framework, proposal.control_id):
        reasons.append("supersedes_other_control")
    if superseded.kind == "withdraw" or (
        proposal.kind in STATUS_KINDS and superseded.kind not in STATUS_KINDS
    ):
        reasons.append("supersedes_invalid_kind")
    pending = select(ControlMappingChange.id).where(
        ControlMappingChange.supersedes_id == superseded.id,
        ControlMappingChange.state.in_(OPEN_STATES),
    )
    if exclude is not None:
        pending = pending.where(ControlMappingChange.id != exclude)
    if db.scalars(pending).first() is not None:
        reasons.append("supersede_pending")
    return reasons


def _review_by_conflicts(db: Session, review_by: date | None) -> list[Reason]:
    if review_by is None:
        return ["review_by_required"]
    today: date = db.execute(select(func.current_date())).scalar_one()
    if review_by <= today:
        return ["review_by_past"]
    if review_by > today + timedelta(days=MAX_REVIEW_DAYS):
        return ["review_by_too_far"]
    return []


def change_details(change: ControlMappingChange) -> dict[str, Any]:
    return {
        "framework": change.framework,
        "control_id": change.control_id,
        "kind": change.kind,
        "rule_type": change.rule_type,
        "justification": change.justification,
        "evidence_ref": change.evidence_ref,
        "review_by": change.review_by.isoformat() if change.review_by else None,
        "supersedes_id": change.supersedes_id,
        "mapping_version": change.mapping_version,
    }


def _withdraw_superseded(db: Session, change: ControlMappingChange, actor: str) -> None:
    if change.supersedes_id is None:
        return
    superseded = db.get(ControlMappingChange, change.supersedes_id, with_for_update=True)
    if superseded is None or superseded.state != "active":
        raise MappingConflict(["supersedes_not_active"])
    superseded.state = "withdrawn"
    db.flush()
    audit.record(
        db,
        actor=actor,
        action="control_mapping.withdrawn",
        resource_type="control_mapping_change",
        resource_id=str(superseded.id),
        details={**change_details(superseded), "by_change_id": change.id},
    )


def propose(db: Session, proposal: Proposal, by: Actor) -> ControlMappingChange:
    """Make the change, or propose it while review is required. The caller commits.

    Raises MappingConflict with every reason the change cannot be made.
    """
    _lock(db, proposal.framework, proposal.control_id)
    reasons = conflicts(db, proposal)
    if reasons:
        raise MappingConflict(reasons)
    required = review_required(db)
    change = ControlMappingChange(
        framework=proposal.framework,
        control_id=proposal.control_id,
        kind=proposal.kind,
        rule_type=proposal.rule_type,
        justification=proposal.justification,
        evidence_ref=proposal.evidence_ref,
        review_by=proposal.review_by,
        supersedes_id=proposal.supersedes_id,
        state="proposed" if required else "active",
        review_required=required,
        self_approved=not required,
        mapping_version=MAPPING_VERSION,
        proposed_by=by.actor,
        proposed_by_user_id=by.user_id,
    )
    if not required:
        change.decided_by = by.actor
        change.decided_by_user_id = by.user_id
        change.decided_at = func.now()
    db.add(change)
    db.flush()
    audit.record(
        db,
        actor=by.actor,
        action="control_mapping.proposed",
        resource_type="control_mapping_change",
        resource_id=str(change.id),
        details={**change_details(change), "state": change.state, "self_approved": not required},
    )
    if not required:
        _withdraw_superseded(db, change, by.actor)
    return change


def _proposal(change: ControlMappingChange) -> Proposal:
    return Proposal(
        framework=change.framework,
        control_id=change.control_id,
        kind=change.kind,  # type: ignore[arg-type]
        justification=change.justification,
        rule_type=change.rule_type,
        evidence_ref=change.evidence_ref,
        review_by=change.review_by,
        supersedes_id=change.supersedes_id,
    )


def _decidable(db: Session, change_id: int, by: Actor) -> ControlMappingChange:
    change = db.get(ControlMappingChange, change_id, with_for_update=True)
    if change is None:
        raise LookupError(change_id)
    if change.state != "proposed":
        raise ChangeConflict(f"The change is already {change.state}")
    if change.proposed_by_user_id == by.user_id:
        raise NotAllowed("A change must be approved or rejected by someone other than its proposer")
    return change


def approve(db: Session, change_id: int, by: Actor, note: str | None) -> ControlMappingChange:
    """Put a proposed change in force. The caller commits.

    Raises LookupError, NotAllowed (own change), ChangeConflict (already decided) or
    MappingConflict (it can no longer be made, for example its review date has passed).
    """
    change = _decidable(db, change_id, by)
    _lock(db, change.framework, change.control_id)
    reasons = conflicts(db, _proposal(change), exclude=change.id)
    if reasons:
        raise MappingConflict(reasons)
    change.state = "active"
    change.decided_by = by.actor
    change.decided_by_user_id = by.user_id
    change.decided_at = func.now()
    change.decision_note = note or None
    db.flush()
    audit.record(
        db,
        actor=by.actor,
        action="control_mapping.approved",
        resource_type="control_mapping_change",
        resource_id=str(change.id),
        details={**change_details(change), "proposed_by": change.proposed_by, "note": note or None},
    )
    _withdraw_superseded(db, change, by.actor)
    return change


def reject(db: Session, change_id: int, by: Actor, note: str) -> ControlMappingChange:
    """Reject a proposed change. The caller commits."""
    change = _decidable(db, change_id, by)
    change.state = "rejected"
    change.decided_by = by.actor
    change.decided_by_user_id = by.user_id
    change.decided_at = func.now()
    change.decision_note = note
    db.flush()
    audit.record(
        db,
        actor=by.actor,
        action="control_mapping.rejected",
        resource_type="control_mapping_change",
        resource_id=str(change.id),
        details={**change_details(change), "proposed_by": change.proposed_by, "note": note},
    )
    return change
