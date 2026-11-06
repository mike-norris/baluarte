"""Label plans: the label changes that would remove drift, reviewed, then applied (ADR-0009).

A plan is built from a fresh read of the labels in its scope (``build`` is given the drift
computed right after that read). Only three kinds of step are planned, all on the hierarchy's
root group, where the taxonomy places every label:

- ``create``: a missing label, ``POST /groups/:id/labels``;
- ``rename``: a misnamed label renamed in place to the canonical name, with the taxonomy's
  colour and description, ``PUT /groups/:id/labels/:label_id`` with ``new_name``. Issues and
  merge requests keep it. Only when the canonical name is not already defined there;
- ``update``: the colour and description of a label set to the taxonomy's.

Nothing is ever deleted. Everything else is listed as a manual item with the reason: duplicates,
misplaced labels, misnamed labels below the root (renaming them there would define the label
below the root), labels the taxonomy does not name, and owners whose labels could not be read.

Lifecycle (enforced again by a trigger): ``draft`` -> ``submitted`` by its creator ->
``approved`` or ``rejected`` by someone else with ``remediation:approve``, bound to the digest
they reviewed. While review is not required, submitting approves it at once, ``self_approved``.
``approved`` -> ``applying`` when someone applies it -> ``applied`` or ``failed``. A plan not
applied within ``LABEL_PLAN_HOURS`` expires.

Applying needs ``BALUARTE_LABEL_WRITES=true``, a stored write token and the same taxonomy still
in force. The ``gitlab.apply_label_plan`` job applies the steps in order. For each it reads the
owner's labels with the read token; a step already as intended is skipped; a label that changed
since the plan stops the plan. Otherwise it sends the write once (never retried), reads the
label back with the read token, and stops at the first write GitLab refused or that does not read
back as intended. Every write is audited as ``labels.write``; afterwards a label collection is
queued so drift shows the result.
"""

import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

import httpx
from sqlalchemy import Engine, and_, case, func, select, update
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.audit import details_digest
from baluarte.configuration.crypto import load_secret_box
from baluarte.db.models import (
    LABEL_PLAN_HOURS,
    MAX_LABEL_PLAN_STEPS,
    GitLabInstance,
    LabelPlan,
    LabelTaxonomy,
)
from baluarte.integrations.gitlab.client import GitLabClient, GitLabError
from baluarte.integrations.gitlab.label_writer import LabelWriter, WriteResult
from baluarte.integrations.gitlab.labels import Owner, read_labels
from baluarte.integrations.gitlab.settings import load_gitlab_settings
from baluarte.jobs import queue
from baluarte.jobs.queue import ClaimedJob
from baluarte.labels import write_token
from baluarte.labels.drift import DriftItem
from baluarte.labels.taxonomy import TaxonomySpec, covers, in_force, normalized, spec_of
from baluarte.policy.changes import Actor
from baluarte.settings import ConfigurationError

logger = logging.getLogger(__name__)

FORMAT = "baluarte.label-plan/v1"
APPLY_LABEL_PLAN = "gitlab.apply_label_plan"
SYSTEM_ACTOR = "system:label-plans"
TTL = timedelta(hours=LABEL_PLAN_HOURS)
MAX_OWNERS = 500
MAX_NOTE = 2000
Action = Literal["create", "rename", "update"]
OPEN_STATES = ("draft", "submitted", "approved")

MANUAL_REASONS = {
    "duplicate": "Defined again below the top-level group, shadowing it. Merge it into the "
    "top-level label in GitLab, then delete this one; Baluarte never deletes labels.",
    "misplaced": "Defined below the top-level group. Once the top-level label exists, merge this "
    "one into it in GitLab; Baluarte never deletes labels.",
    "misnamed": "Renaming it here would define the label below the top-level group. Merge it into "
    "the top-level label in GitLab.",
    "unknown_label": "Not in the taxonomy. Add it to the taxonomy, or remove it in GitLab.",
    "unknown": "Its labels could not be read, so no change is planned for it.",
    "rename_blocked": "The canonical name is already defined here, so this label cannot be renamed "
    "to it. Merge the two in GitLab.",
}


class PlanConflict(Exception):
    """The plan cannot make this transition; ``code`` names why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class NothingToPlan(Exception):
    pass


class ScopeTooLarge(Exception):
    pass


def plan_digest(plan: dict[str, Any]) -> str:
    return details_digest(plan)


def digest_valid(row: LabelPlan) -> bool:
    return row.plan_digest == plan_digest(row.plan)


def effective_state() -> Any:
    """SQL: the state, counting an open plan past its expiry as expired."""
    return case(
        (
            and_(LabelPlan.state.in_(OPEN_STATES), LabelPlan.expires_at <= func.now()),
            "expired",
        ),
        else_=LabelPlan.state,
    )


def current_state(db: Session, row: LabelPlan) -> str:
    if row.state in OPEN_STATES and db.execute(select(func.now() >= row.expires_at)).scalar_one():
        return "expired"
    return row.state


def in_plan_scope(groups: Sequence[str], path: str) -> bool:
    return not groups or any(path == g or path.startswith(g + "/") for g in groups)


def owners_in_scope(
    spec: TaxonomySpec, groups: Sequence[str], owners: Sequence[Owner]
) -> list[Owner]:
    """The groups and projects a plan for ``groups`` reads: in both scopes."""
    return [
        owner
        for owner in owners
        if covers(spec.scope, owner.path) and in_plan_scope(groups, owner.path)
    ]


def _key(instance_id: int, owner: dict[str, Any], action: str, label: str, intended: Any) -> str:
    raw = json.dumps(
        [instance_id, owner["type"], owner["gitlab_id"], action, label, intended],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _label_of(item: DriftItem) -> dict[str, Any] | None:
    for observation in item.observations:
        for label in observation.labels:
            if label.get("id") == item.label_id:
                return dict(label)
    return None


@dataclass
class Built:
    steps: list[dict[str, Any]] = field(default_factory=list)
    manual: list[dict[str, Any]] = field(default_factory=list)


def build(
    instance_id: int, spec: TaxonomySpec, items: Sequence[DriftItem], groups: Sequence[str]
) -> Built:
    """The steps and manual items that address ``items`` within the plan's ``groups``."""
    wanted = {label.name: label for label in spec.labels}
    built = Built()
    creates: list[dict[str, Any]] = []
    renames: list[dict[str, Any]] = []
    updates: dict[tuple[str, int], dict[str, Any]] = {}
    renamed: set[tuple[str, str]] = set()

    def manual(item: DriftItem, reason_code: str) -> None:
        built.manual.append(
            {
                "owner": item.owner.as_dict(),
                "kind": item.kind,
                "label": item.label,
                "observed_name": (item.observed or {}).get("name"),
                "reason": MANUAL_REASONS[reason_code],
            }
        )

    def step(
        item: DriftItem, action: Action, prior: dict[str, Any] | None, intended: dict[str, Any]
    ) -> dict[str, Any]:
        owner = item.owner.as_dict()
        label = item.label or ""
        if action == "create":
            request = {"method": "POST", "path": f"/groups/{item.owner.gitlab_id}/labels"}
        else:
            request = {
                "method": "PUT",
                "path": f"/{item.owner.type}s/{item.owner.gitlab_id}/labels/{item.label_id}",
            }
        observation = item.observations[0]
        return {
            "owner": owner,
            "action": action,
            "label": label,
            "label_id": item.label_id,
            "prior": prior,
            "intended": intended,
            "request": request,
            "observation": {
                "id": observation.observation_id,
                "payload_digest": observation.payload_digest,
            },
            "idempotency_key": _key(instance_id, owner, action, label, intended),
        }

    for item in items:
        if not in_plan_scope(groups, item.owner.path):
            continue
        at_root = item.owner == item.root
        target = wanted.get(item.label or "")
        if item.kind == "missing" and target is not None:
            creates.append(
                step(
                    item,
                    "create",
                    None,
                    {"name": target.name, "color": target.color, "description": target.description},
                )
            )
        elif item.kind == "misnamed" and at_root and target is not None and item.label_id:
            current = _label_of(item)
            names_here = {str(label.get("name")) for o in item.observations for label in o.labels}
            if (
                current is None
                or target.name in names_here
                or (item.owner.path, target.name) in renamed
            ):
                manual(item, "rename_blocked")
                continue
            renamed.add((item.owner.path, target.name))
            renames.append(
                step(
                    item,
                    "rename",
                    {k: current.get(k) for k in ("name", "color", "description")},
                    {"name": target.name, "color": target.color, "description": target.description},
                )
            )
        elif item.kind in ("color", "description") and at_root and target and item.label_id:
            current = _label_of(item)
            if current is None:
                manual(item, "unknown")
                continue
            key = (item.owner.path, item.label_id)
            if key not in updates:
                updates[key] = step(
                    item,
                    "update",
                    {k: current.get(k) for k in ("name", "color", "description")},
                    {"name": target.name, "color": target.color, "description": target.description},
                )
        elif item.kind == "misnamed":
            manual(item, "misnamed")
        else:
            manual(item, item.kind if item.kind in MANUAL_REASONS else "unknown")
    ordered = (
        sorted(creates, key=lambda s: (s["owner"]["path"], s["label"]))
        + sorted(renames, key=lambda s: (s["owner"]["path"], s["label"]))
        + sorted(updates.values(), key=lambda s: (s["owner"]["path"], s["label"]))
    )
    built.steps = [{"step": number, **entry} for number, entry in enumerate(ordered, start=1)]
    return built


def expire_due(db: Session) -> list[int]:
    """Mark open plans past ``expires_at`` expired and audit it. The caller commits."""
    expired = list(
        db.scalars(
            update(LabelPlan)
            .where(LabelPlan.state.in_(OPEN_STATES), LabelPlan.expires_at <= func.now())
            .values(state="expired", finished_at=func.now())
            .returning(LabelPlan.id)
        )
    )
    for plan_id in expired:
        audit.record(
            db,
            actor=SYSTEM_ACTOR,
            action="labels.plan_expired",
            resource_type="label_plan",
            resource_id=str(plan_id),
        )
    return expired


def _details(row: LabelPlan, **extra: Any) -> dict[str, Any]:
    return {
        "plan_digest": row.plan_digest,
        "steps": row.steps,
        "manual": row.manual,
        "taxonomy_id": row.taxonomy_id,
        **extra,
    }


def create(
    db: Session,
    instance: GitLabInstance,
    taxonomy: LabelTaxonomy,
    items: Sequence[DriftItem],
    groups: Sequence[str],
    by: Actor,
    review_required: bool,
) -> LabelPlan:
    """Store a draft plan for ``items`` (drift computed from a fresh read). The caller commits.

    Raises NothingToPlan when nothing in scope drifts, and ScopeTooLarge for too many steps.
    """
    expire_due(db)
    built = build(instance.id, spec_of(taxonomy), items, groups)
    if not built.steps and not built.manual:
        raise NothingToPlan("Nothing in the plan's scope drifts from the taxonomy")
    if len(built.steps) > MAX_LABEL_PLAN_STEPS:
        raise ScopeTooLarge(f"The plan would have more than {MAX_LABEL_PLAN_STEPS} steps")
    now: datetime = db.execute(select(func.now())).scalar_one()
    expires_at = now + TTL
    plan = {
        "format": FORMAT,
        "dry_run": True,
        "instance": instance.base_url,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "created_by": by.actor,
        "taxonomy": {
            "id": taxonomy.id,
            "name": taxonomy.name,
            "version": taxonomy.version,
            "yaml_digest": taxonomy.yaml_digest,
        },
        "scope": {"groups": list(groups)},
        "steps": built.steps,
        "manual": built.manual,
    }
    row = LabelPlan(
        instance_id=instance.id,
        taxonomy_id=taxonomy.id,
        created_by=by.actor,
        created_by_user_id=by.user_id,
        created_at=now,
        expires_at=expires_at,
        plan=plan,
        plan_digest=plan_digest(plan),
        steps=len(built.steps),
        manual=len(built.manual),
        review_required=review_required,
    )
    db.add(row)
    db.flush()
    db.refresh(row)
    audit.record(
        db,
        actor=by.actor,
        action="labels.plan_created",
        resource_type="label_plan",
        resource_id=str(row.id),
        details=_details(row, scope=list(groups), expires_at=expires_at.isoformat()),
    )
    return row


def locked(db: Session, plan_id: int) -> LabelPlan | None:
    return db.get(LabelPlan, plan_id, with_for_update=True, populate_existing=True)


def _taxonomy_current(db: Session, row: LabelPlan) -> bool:
    current = in_force(db, row.instance_id)
    return current is not None and current.id == row.taxonomy_id


def submit(db: Session, row: LabelPlan, by: Actor) -> LabelPlan:
    """Send a draft for approval; without review, approve it at once. Only its creator may."""
    state = current_state(db, row)
    if state != "draft":
        raise PlanConflict(
            "plan_expired" if state == "expired" else "not_draft",
            f"Only a draft plan can be submitted; this one is {state}",
        )
    if by.user_id != row.created_by_user_id:
        raise PermissionError("Only the plan's creator can submit it")
    now = db.execute(select(func.now())).scalar_one()
    row.submitted_at = now
    if row.review_required:
        row.state = "submitted"
        action = "labels.plan_submitted"
    else:
        row.state = "approved"
        row.self_approved = True
        row.decided_by = by.actor
        row.decided_by_user_id = by.user_id
        row.decided_at = now
        row.approved_digest = row.plan_digest
        action = "labels.plan_approved"
    db.flush()
    audit.record(
        db,
        actor=by.actor,
        action=action,
        resource_type="label_plan",
        resource_id=str(row.id),
        details=_details(row, self_approved=row.self_approved),
    )
    return row


def decide(
    db: Session,
    row: LabelPlan,
    decision: Literal["approved", "rejected"],
    by: Actor,
    note: str | None,
    reviewed_digest: str | None = None,
) -> LabelPlan:
    """Approve or reject a submitted plan; never one's own."""
    state = current_state(db, row)
    if state != "submitted":
        raise PlanConflict(
            "plan_expired" if state == "expired" else "not_submitted",
            f"Only a submitted plan can be decided; this one is {state}",
        )
    if by.user_id == row.created_by_user_id:
        raise PermissionError(
            "A plan must be approved or rejected by someone other than its creator"
        )
    if decision == "approved":
        if reviewed_digest != row.plan_digest or not digest_valid(row):
            raise PlanConflict("digest_mismatch", "The reviewed digest does not match the plan's")
        if not _taxonomy_current(db, row):
            raise PlanConflict(
                "taxonomy_changed",
                "The taxonomy in force is no longer the one the plan was built for",
            )
    elif not (note or "").strip():
        raise ValueError("A rejection needs a note")
    now = db.execute(select(func.now())).scalar_one()
    row.state = decision
    row.decided_by = by.actor
    row.decided_by_user_id = by.user_id
    row.decided_at = now
    row.decision_note = (note or "").strip() or None
    row.approved_digest = row.plan_digest if decision == "approved" else None
    db.flush()
    audit.record(
        db,
        actor=by.actor,
        action=f"labels.plan_{decision}",
        resource_type="label_plan",
        resource_id=str(row.id),
        details=_details(row, created_by=row.created_by, note=row.decision_note),
    )
    return row


def apply_refusals(db: Session, row: LabelPlan) -> list[str]:
    """Why the plan cannot be applied now (codes); empty when it can."""
    reasons: list[str] = []
    try:
        enabled = write_token.writes_enabled()
    except ConfigurationError:
        enabled = False
    if not enabled:
        reasons.append("label_writes_disabled")
    if not write_token.status(db).set:
        reasons.append("write_token_missing")
    state = current_state(db, row)
    if state == "expired":
        reasons.append("expired")
    elif state != "approved":
        reasons.append("not_approved")
    if state in ("approved", "submitted", "draft") and not _taxonomy_current(db, row):
        reasons.append("taxonomy_changed")
    if row.steps == 0 and "not_approved" not in reasons:
        reasons.append("nothing_to_apply")
    return reasons


REFUSAL_MESSAGES = {
    "label_writes_disabled": "Label writes are off for this deployment (BALUARTE_LABEL_WRITES).",
    "write_token_missing": "No label write token is stored.",
    "expired": "The plan has expired.",
    "not_approved": "Only an approved plan can be applied.",
    "taxonomy_changed": "The taxonomy in force is no longer the one the plan was built for.",
    "nothing_to_apply": "The plan has no steps, only manual items.",
}


def start_apply(db: Session, row: LabelPlan, by: Actor) -> LabelPlan:
    """Mark an approved plan ``applying`` and queue the applier. The caller commits."""
    reasons = apply_refusals(db, row)
    if reasons:
        code = "plan_expired" if reasons[0] == "expired" else reasons[0]
        raise PlanConflict(code, " ".join(REFUSAL_MESSAGES[r] for r in reasons))
    now = db.execute(select(func.now())).scalar_one()
    row.state = "applying"
    row.applied_by = by.actor
    row.apply_started_at = now
    row.progress = [
        {"step": step["step"], "status": "pending"} for step in row.plan.get("steps", [])
    ]
    db.flush()
    # Applied at most once: a failed attempt is recorded on the plan, never retried.
    queue.enqueue(
        db, APPLY_LABEL_PLAN, f"{APPLY_LABEL_PLAN}:{row.id}", {"plan_id": row.id}, max_attempts=1
    )
    audit.record(
        db,
        actor=by.actor,
        action="labels.plan_applying",
        resource_type="label_plan",
        resource_id=str(row.id),
        details=_details(row),
    )
    return row


# Applying.


def _same(label: dict[str, Any], intended: dict[str, Any]) -> bool:
    color = label.get("color")
    return (
        label.get("name") == intended["name"]
        and isinstance(color, str)
        and color.lower() == str(intended["color"]).lower()
        and (label.get("description") or None) == (intended.get("description") or None)
    )


def _prior_holds(label: dict[str, Any], prior: dict[str, Any]) -> bool:
    return (
        label.get("name") == prior.get("name")
        and str(label.get("color") or "").lower() == str(prior.get("color") or "").lower()
        and (label.get("description") or None) == (prior.get("description") or None)
    )


def _find(
    labels: list[dict[str, Any]], step: dict[str, Any], label_id: int | None
) -> dict[str, Any] | None:
    if label_id is not None:
        return next((label for label in labels if label.get("id") == label_id), None)
    name = step["intended"]["name"]
    return next((label for label in labels if label.get("name") == name), None)


@dataclass(frozen=True)
class StepOutcome:
    status: Literal["skipped", "applied", "failed"]
    message: str
    request_status: int | None = None
    verified: bool | None = None
    label_id: int | None = None


def apply_step(
    client: GitLabClient,
    writer: LabelWriter,
    step: dict[str, Any],
    audit_write: Callable[[WriteResult], None],
) -> StepOutcome:
    """Apply one step: read, skip or write once, then read back with the read token."""
    owner_ref = step["owner"]
    owner = Owner(owner_ref["type"], 0, owner_ref["gitlab_id"], owner_ref["path"])
    intended = step["intended"]
    try:
        labels = read_labels(client, owner)
    except GitLabError as error:
        return StepOutcome("failed", f"The labels of {owner.path} could not be read: {error}")
    current = _find(labels, step, step.get("label_id"))
    if current is not None and _same(current, intended):
        return StepOutcome(
            "skipped", "Already as intended.", verified=True, label_id=current.get("id")
        )
    if step["action"] == "create":
        if current is not None:
            return StepOutcome(
                "failed", f"{owner.path} now defines {intended['name']!r} differently than planned."
            )
        if any(
            normalized(str(label.get("name"))) == normalized(intended["name"]) for label in labels
        ):
            return StepOutcome(
                "failed", f"{owner.path} now defines a variant of {intended['name']!r}."
            )
        result = writer.create_group_label(
            owner.gitlab_id, intended["name"], intended["color"], intended.get("description")
        )
    else:
        prior = step["prior"] or {}
        if current is None or not _prior_holds(current, prior):
            return StepOutcome(
                "failed", f"The label on {owner.path} changed since the plan was made."
            )
        if step["action"] == "rename" and any(
            label.get("name") == intended["name"] for label in labels
        ):
            return StepOutcome("failed", f"{owner.path} now already defines {intended['name']!r}.")
        result = writer.update_label(
            owner.type,
            owner.gitlab_id,
            int(step["label_id"]),
            color=intended["color"],
            new_name=intended["name"] if step["action"] == "rename" else None,
            # An empty description clears one the taxonomy does not have.
            description=intended.get("description") or "",
        )
    audit_write(result)
    if not result.ok:
        reason = result.error or f"GitLab answered {result.status}"
        return StepOutcome(
            "failed", f"{result.method} {result.path} failed: {reason}.", result.status
        )
    body = result.body if isinstance(result.body, dict) else {}
    label_id = body.get("id") if type(body.get("id")) is int else step.get("label_id")
    try:
        after = read_labels(client, owner)
    except GitLabError as error:
        return StepOutcome(
            "failed",
            f"The write was sent but could not be read back: {error}",
            result.status,
            False,
        )
    found = next((label for label in after if label.get("id") == label_id), None)
    if found is None or not _same(found, intended):
        return StepOutcome(
            "failed", "The label does not read back as intended.", result.status, False, label_id
        )
    return StepOutcome(
        "applied", "Written and read back as intended.", result.status, True, label_id
    )


def _record(db: Session, row: LabelPlan, number: int, outcome: StepOutcome) -> None:
    now = db.execute(select(func.now())).scalar_one()
    progress = [dict(entry) for entry in row.progress]
    for entry in progress:
        if entry["step"] == number:
            entry.update(
                status=outcome.status,
                request_status=outcome.request_status,
                verified=outcome.verified,
                message=outcome.message,
                label_id=outcome.label_id,
                finished_at=now.isoformat().replace("+00:00", "Z"),
            )
    row.progress = progress


def _finish(db: Session, row: LabelPlan, failure: tuple[int, str] | None) -> None:
    now = db.execute(select(func.now())).scalar_one()
    if failure is None:
        row.state = "applied"
    else:
        row.state = "failed"
        row.failed_step, row.failure = failure
        row.progress = [
            {**entry, "status": "not_run"} if entry["status"] == "pending" else entry
            for entry in row.progress
        ]
    row.finished_at = now
    counts: dict[str, int] = {}
    for entry in row.progress:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    audit.record(
        db,
        actor=SYSTEM_ACTOR,
        action=f"labels.plan_{row.state}",
        resource_type="label_plan",
        resource_id=str(row.id),
        details=_details(
            row, steps_by_status=counts, failed_step=row.failed_step, failure=row.failure
        ),
    )
    # Collect again so drift shows what changed; imported here, the jobs module imports this one.
    from baluarte.integrations.gitlab.jobs import queue_labels

    queue_labels(db, f"after-plan:{row.id}")


def make_apply_label_plan(
    transport: httpx.BaseTransport | None = None,
) -> Callable[[ClaimedJob, Engine], None]:
    def apply(job: ClaimedJob, engine: Engine) -> None:
        plan_id = int(job.payload.get("plan_id", 0))
        with Session(engine) as db:
            row = locked(db, plan_id)
            if row is None or row.state != "applying":
                return
            steps = list(row.plan.get("steps", []))
            done = {e["step"] for e in row.progress if e.get("status") in ("applied", "skipped")}
            problem: str | None = None
            token: str | None = None
            settings = None
            try:
                settings = load_gitlab_settings()
                if not write_token.writes_enabled():
                    problem = REFUSAL_MESSAGES["label_writes_disabled"]
                token = write_token.load(db, load_secret_box())
            except ConfigurationError as exc:
                problem = str(exc)
            if problem is None and (settings is None or token is None):
                problem = "GitLab or the label write token is no longer configured."
            if not digest_valid(row):
                problem = "The stored plan no longer matches its digest."
            if problem is not None or settings is None or token is None:
                _finish(db, row, (steps[0]["step"] if steps else 0, problem or "not configured"))
                db.commit()
                return
            db.commit()

        def audit_write(result: WriteResult) -> None:
            with Session(engine) as db:
                audit.record(
                    db,
                    actor=f"label-plan:{plan_id}",
                    action="labels.write",
                    resource_type="label_plan",
                    resource_id=str(plan_id),
                    details={"method": result.method, "path": result.path, "status": result.status},
                )
                db.commit()

        failure: tuple[int, str] | None = None
        with (
            GitLabClient(settings, transport=transport) as client,
            LabelWriter(settings, token, transport=transport) as writer,
        ):
            for step in steps:
                if step["step"] in done:
                    continue
                outcome = apply_step(client, writer, step, audit_write)
                with Session(engine) as db:
                    row = locked(db, plan_id)
                    if row is None:
                        return
                    _record(db, row, step["step"], outcome)
                    db.commit()
                if outcome.status == "failed":
                    failure = (step["step"], outcome.message)
                    break
        with Session(engine) as db:
            row = locked(db, plan_id)
            if row is not None and row.state == "applying":
                _finish(db, row, failure)
                db.commit()
        logger.info(
            "label plan %s: %s",
            plan_id,
            "applied" if failure is None else f"failed at step {failure[0]}",
            extra={"job_id": job.id},
        )

    return apply
