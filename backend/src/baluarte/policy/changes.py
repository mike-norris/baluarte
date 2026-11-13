"""Changes to the baselines in force, and their two-person review.

Every new version, assignment and unassignment is recorded as a ``policy_change``. While the
``governance`` setting requires review, the change is ``proposed`` and has no effect: ``policy``
and ``policy_assignment``, which evaluation reads, hold only approved changes. Someone with
``policy:write`` other than the proposer then approves it, which applies it, or rejects it.
While review is not required the change is applied at once and recorded ``approved`` with
``self_approved`` set, so the evidence shows that no one else reviewed it.

A database trigger and CHECK constraints enforce the lifecycle and the approver rule again.
"""

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.compliance import operations
from baluarte.configuration.store import read_setting
from baluarte.db.models import GitLabInstance, Policy, PolicyAssignment, PolicyChange
from baluarte.policy.assignments import Target, TargetType, assign, find_target
from baluarte.policy.loader import parse_policy
from baluarte.policy.store import latest_version, save_version
from baluarte.settings import own_decision_refused

GOVERNANCE = "governance"
MAX_NOTE = 2000
ChangeKind = Literal["version", "assign", "unassign"]


class ChangeConflict(Exception):
    """The change cannot be made or decided in the current state."""


class NotAllowed(Exception):
    """The caller may not decide this change (it is their own)."""


@dataclass(frozen=True)
class Actor:
    user_id: int
    actor: str


def review_required(db: Session) -> bool:
    value = read_setting(db, GOVERNANCE) or {}
    return bool(value.get("require_review", False))


def set_review_required(db: Session, required: bool, actor: str) -> None:
    """Store the setting and audit it. The caller commits.

    Raises BelowMinimum when the operating profile requires review and no compliance exception
    for ``change-review`` is in force.
    """
    violation = operations.review_violation(db, required)
    if violation is not None:
        raise operations.BelowMinimum([violation])
    operations.write_review(db, required, actor)


def _proposed(db: Session, **where: Any) -> PolicyChange | None:
    statement = select(PolicyChange).where(PolicyChange.state == "proposed")
    for column, value in where.items():
        statement = statement.where(getattr(PolicyChange, column) == value)
    return db.scalars(statement).first()


def _record(db: Session, change: PolicyChange, by: Actor, required: bool) -> PolicyChange:
    change.review_required = required
    change.proposed_by = by.actor
    change.proposed_by_user_id = by.user_id
    if required:
        change.state = "proposed"
    else:
        change.state = "approved"
        change.self_approved = True
        change.decided_by = by.actor
        change.decided_by_user_id = by.user_id
        change.decided_at = func.now()
    db.add(change)
    db.flush()
    return change


def _audit_proposed(db: Session, change: PolicyChange) -> None:
    audit.record(
        db,
        actor=change.proposed_by,
        action="policy.change_proposed",
        resource_type="policy_change",
        resource_id=str(change.id),
        details=change_details(change),
    )


def change_details(change: PolicyChange) -> dict[str, Any]:
    details: dict[str, Any] = {"kind": change.kind, "policy_name": change.policy_name}
    if change.kind == "version":
        details["yaml_digest"] = change.yaml_digest
        details["base_version"] = change.base_version
    else:
        details.update(
            policy_id=change.policy_id,
            target_type=change.target_type,
            target_id=change.target_id,
            assignment_id=change.assignment_id,
        )
    if change.development_decision:
        details["development_decision"] = True
    return details


@dataclass(frozen=True)
class VersionChange:
    """The outcome of saving a baseline."""

    change: PolicyChange | None
    # Set when the version is in force: newly saved, or identical to the latest.
    policy: Policy | None
    created: bool


def propose_version(db: Session, source: str, by: Actor) -> VersionChange:
    """Save ``source`` as the next version, or propose it. The caller commits.

    Raises PolicyInvalid when the document is not a valid baseline, and ChangeConflict when a
    different version of the policy is already waiting for review.
    """
    parsed = parse_policy(source)
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext("policy:" + parsed.name))))
    latest = latest_version(db, parsed.name)
    if latest is not None and latest.yaml_digest == parsed.digest:
        return VersionChange(change=None, policy=latest, created=False)
    pending = _proposed(db, kind="version", policy_name=parsed.name)
    if pending is not None:
        if pending.yaml_digest == parsed.digest:
            return VersionChange(change=pending, policy=None, created=False)
        raise ChangeConflict("Another version of this baseline is already waiting for review")
    required = review_required(db)
    # Applied first when no review is required, so the change is recorded with its result.
    saved = None if required else save_version(db, source)
    change = _record(
        db,
        PolicyChange(
            kind="version",
            policy_name=parsed.name,
            source_yaml=source,
            yaml_digest=parsed.digest,
            base_version=latest.version if latest else None,
            policy_id=saved.policy.id if saved else None,
        ),
        by,
        required,
    )
    if saved is None:
        _audit_proposed(db, change)
        return VersionChange(change=change, policy=None, created=True)
    return VersionChange(change=change, policy=saved.policy, created=True)


@dataclass(frozen=True)
class AssignmentChange:
    change: PolicyChange | None
    # Set when in force: the new assignment, or the existing one when nothing changed.
    assignment: PolicyAssignment | None
    replaced: PolicyAssignment | None
    replaced_version: int | None
    created: bool


def _current(db: Session, instance_id: int, target: Target, name: str) -> PolicyAssignment | None:
    return db.scalars(
        select(PolicyAssignment).where(
            PolicyAssignment.instance_id == instance_id,
            PolicyAssignment.target_type == target.target_type,
            PolicyAssignment.target_id == target.target_id,
            PolicyAssignment.policy_name == name,
        )
    ).first()


def _slot_lock(db: Session, instance_id: int, target_type: str, target_id: int, name: str) -> None:
    key = f"policy_assignment:{instance_id}:{target_type}:{target_id}:{name}"
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext(key))))


def _pending_for_slot(
    db: Session, instance_id: int, target_type: str, target_id: int, name: str
) -> PolicyChange | None:
    return _proposed(
        db,
        instance_id=instance_id,
        target_type=target_type,
        target_id=target_id,
        policy_name=name,
    )


def propose_assignment(
    db: Session, instance: GitLabInstance, policy: Policy, target: Target, by: Actor
) -> AssignmentChange:
    """Pin ``policy`` to ``target``, or propose it. The caller commits."""
    _slot_lock(db, instance.id, target.target_type, target.target_id, policy.name)
    existing = _current(db, instance.id, target, policy.name)
    if existing is not None and existing.policy_id == policy.id:
        return AssignmentChange(None, existing, None, None, created=False)
    pending = _pending_for_slot(db, instance.id, target.target_type, target.target_id, policy.name)
    if pending is not None:
        if pending.kind == "assign" and pending.policy_id == policy.id:
            return AssignmentChange(pending, None, None, None, created=False)
        raise ChangeConflict("A change to this baseline here is already waiting for review")
    required = review_required(db)
    assigned = None if required else assign(db, instance.id, policy, target)
    change = _record(
        db,
        PolicyChange(
            kind="assign",
            policy_name=policy.name,
            policy_id=policy.id,
            instance_id=instance.id,
            target_type=target.target_type,
            target_id=target.target_id,
            assignment_id=assigned.assignment.id if assigned else None,
        ),
        by,
        required,
    )
    if assigned is None:
        _audit_proposed(db, change)
        return AssignmentChange(change, None, None, None, created=True)
    return AssignmentChange(
        change, assigned.assignment, assigned.replaced, assigned.replaced_version, created=True
    )


def propose_unassignment(
    db: Session, assignment: PolicyAssignment, by: Actor
) -> tuple[PolicyChange, bool]:
    """Remove ``assignment``, or propose removing it. Returns the change and whether it applied.

    The caller commits.
    """
    _slot_lock(
        db,
        assignment.instance_id,
        assignment.target_type,
        assignment.target_id,
        assignment.policy_name,
    )
    pending = _pending_for_slot(
        db,
        assignment.instance_id,
        assignment.target_type,
        assignment.target_id,
        assignment.policy_name,
    )
    if pending is not None:
        if pending.kind == "unassign" and pending.assignment_id == assignment.id:
            return pending, False
        raise ChangeConflict("A change to this baseline here is already waiting for review")
    required = review_required(db)
    change = _record(
        db,
        PolicyChange(
            kind="unassign",
            policy_name=assignment.policy_name,
            policy_id=assignment.policy_id,
            instance_id=assignment.instance_id,
            target_type=assignment.target_type,
            target_id=assignment.target_id,
            assignment_id=assignment.id,
        ),
        by,
        required,
    )
    if required:
        _audit_proposed(db, change)
        return change, False
    db.delete(assignment)
    db.flush()
    return change, True


@dataclass(frozen=True)
class Applied:
    """What approving a change put in force, for the caller's audit events."""

    policy: Policy | None = None
    assignment: PolicyAssignment | None = None
    replaced: PolicyAssignment | None = None
    replaced_version: int | None = None
    removed: PolicyAssignment | None = None


def _check_decider(change: PolicyChange, by: Actor) -> None:
    if change.state != "proposed":
        raise ChangeConflict(f"The change is already {change.state}")
    if own_decision_refused(change.proposed_by_user_id, by.user_id):
        raise NotAllowed("A change must be approved or rejected by someone other than its proposer")


def _apply(db: Session, change: PolicyChange) -> Applied:
    if change.kind == "version":
        if change.source_yaml is None:
            raise ChangeConflict("The proposed version has no source")
        saved = save_version(db, change.source_yaml)
        change.policy_id = saved.policy.id
        return Applied(policy=saved.policy)
    instance = db.get(GitLabInstance, change.instance_id)
    target_id = change.target_id
    target_type: TargetType
    if change.target_type == "instance":
        target_type = "instance"
    elif change.target_type == "group":
        target_type = "group"
    else:
        target_type = "project"
    if instance is None or target_id is None:
        raise ChangeConflict("The change's GitLab target is no longer recorded")
    _slot_lock(db, instance.id, target_type, target_id, change.policy_name)
    if change.kind == "unassign":
        assignment = db.get(PolicyAssignment, change.assignment_id, with_for_update=True)
        if assignment is None:
            raise ChangeConflict("The assignment no longer exists; reject this change")
        db.delete(assignment)
        db.flush()
        return Applied(removed=assignment)
    policy = db.get(Policy, change.policy_id)
    target = find_target(db, instance, target_type, target_id)
    if policy is None or target is None or target.state not in (None, "active"):
        raise ChangeConflict(
            f"The {change.target_type} is no longer active in the inventory; reject this change"
        )
    assigned = assign(db, instance.id, policy, target)
    change.assignment_id = assigned.assignment.id
    return Applied(
        assignment=assigned.assignment,
        replaced=assigned.replaced,
        replaced_version=assigned.replaced_version,
    )


def approve(
    db: Session, change_id: int, by: Actor, note: str | None
) -> tuple[PolicyChange, Applied]:
    """Apply a proposed change. The caller audits what it applied and commits.

    Raises LookupError, NotAllowed (own change) or ChangeConflict (already decided, or it can no
    longer be applied).
    """
    change = db.get(PolicyChange, change_id, with_for_update=True)
    if change is None:
        raise LookupError(change_id)
    _check_decider(change, by)
    applied = _apply(db, change)
    change.state = "approved"
    change.decided_by = by.actor
    change.decided_by_user_id = by.user_id
    change.development_decision = change.decided_by_user_id == change.proposed_by_user_id
    change.decided_at = func.now()
    change.decision_note = note or None
    db.flush()
    audit.record(
        db,
        actor=by.actor,
        action="policy.change_approved",
        resource_type="policy_change",
        resource_id=str(change.id),
        details={**change_details(change), "proposed_by": change.proposed_by, "note": note or None},
    )
    return change, applied


def reject(db: Session, change_id: int, by: Actor, note: str) -> PolicyChange:
    """Reject a proposed change. The caller commits."""
    change = db.get(PolicyChange, change_id, with_for_update=True)
    if change is None:
        raise LookupError(change_id)
    _check_decider(change, by)
    change.state = "rejected"
    change.decided_by = by.actor
    change.decided_by_user_id = by.user_id
    change.development_decision = change.decided_by_user_id == change.proposed_by_user_id
    change.decided_at = func.now()
    change.decision_note = note
    db.flush()
    audit.record(
        db,
        actor=by.actor,
        action="policy.change_rejected",
        resource_type="policy_change",
        resource_id=str(change.id),
        details={**change_details(change), "proposed_by": change.proposed_by, "note": note},
    )
    return change
