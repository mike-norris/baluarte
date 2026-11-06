"""The label taxonomy, label drift, and label plans (docs/LABELS.md, ADR-0009).

- The taxonomy is proposed with ``policy:write`` and, while baseline review is required,
  approved or rejected by someone else with ``policy:write``; reading it needs
  ``findings:read``.
- Drift and the labels of one group or project need ``findings:read``. Both take ``as_of``.
- Plans are created and submitted with ``remediation:plan``, approved or rejected by someone
  other than their creator with ``remediation:approve``, and applied with ``remediation:plan``
  once approved. Applying is the only path in Baluarte that writes to GitLab: it needs
  ``BALUARTE_LABEL_WRITES=true`` and a stored label write token, and never deletes a label.

Every change needs the ``X-CSRF-Token`` header and is audited. Refusals are
``{detail: {code, message, reasons}}``.
"""

from datetime import datetime
from typing import Annotated, Any, Literal, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from baluarte.api.v1.gitlab import gitlab_transport
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import (
    MAX_TAXONOMY_JUSTIFICATION,
    AppUser,
    GitLabInstance,
    LabelPlan,
    LabelTaxonomy,
)
from baluarte.evidence.as_of import RunRef, run_at, run_ref
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.integrations.gitlab.labels import Owner, collect
from baluarte.integrations.gitlab.settings import GitLabSettings, load_gitlab_settings
from baluarte.labels import drift, plans
from baluarte.labels import taxonomy as taxonomies
from baluarte.labels.drift import DRIFT_KINDS, DriftItem
from baluarte.labels.plans import NothingToPlan, PlanConflict, ScopeTooLarge
from baluarte.policy.changes import Actor, ChangeConflict, NotAllowed, review_required
from baluarte.policy.loader import PolicyInvalid
from baluarte.policy.schema import GROUP_PATTERN
from baluarte.settings import ConfigurationError

router = APIRouter(prefix="/labels", tags=["labels"])

Reader = Annotated[Principal, Depends(require(Permission.FINDINGS_READ))]
Database = Annotated[Session, Depends(database)]
Note = Annotated[str, Field(min_length=1, max_length=plans.MAX_NOTE)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GroupPath = Annotated[str, Field(pattern=GROUP_PATTERN, max_length=255)]
PlanState = Literal[
    "draft", "submitted", "approved", "rejected", "applying", "applied", "failed", "expired"
]
MAX_SOURCE = 256 * 1024


def _refusal(
    code: int, reason: str, message: str, reasons: list[str] | None = None
) -> HTTPException:
    return HTTPException(code, {"code": reason, "message": message, "reasons": reasons or [reason]})


def _settings() -> GitLabSettings | None:
    try:
        return load_gitlab_settings()
    except ConfigurationError:
        return None


def _instance(db: Session) -> GitLabInstance | None:
    settings = _settings()
    if settings is None:
        return None
    return db.scalars(select(GitLabInstance).where(GitLabInstance.base_url == settings.url)).first()


def _instance_id(db: Session) -> int | None:
    instance = _instance(db)
    return instance.id if instance is not None else None


class Person(BaseModel):
    actor: str
    user_id: int | None
    display_name: str | None


def _people(db: Session, user_ids: set[int | None]) -> dict[int, str | None]:
    ids = {user_id for user_id in user_ids if user_id is not None}
    if not ids:
        return {}
    return dict(
        db.execute(select(AppUser.id, AppUser.display_name).where(AppUser.id.in_(ids))).all()
    )


def _person(actor: str | None, user_id: int | None, names: dict[int, str | None]) -> Person | None:
    if actor is None:
        return None
    return Person(
        actor=actor, user_id=user_id, display_name=names.get(user_id) if user_id else None
    )


# The taxonomy.


class TaxonomyRef(BaseModel):
    id: int
    version: int
    name: str


class TaxonomyVersion(BaseModel):
    id: int
    version: int
    name: str
    # ``superseded``: approved, but a later version has been put in force since.
    state: Literal["proposed", "approved", "rejected", "superseded"]
    digest: str
    source_yaml: str
    spec: dict[str, Any]
    justification: str | None
    proposed_by: Person
    proposed_at: datetime
    review_required: bool
    self_approved: bool
    decided_by: Person | None
    decided_at: datetime | None
    decision_note: str | None
    # When it was put in force (its approval).
    in_force_from: datetime | None


class TaxonomyOut(BaseModel):
    current: TaxonomyVersion | None
    versions: list[TaxonomyVersion]
    review_required: bool
    as_of: datetime | None
    evaluation_run: RunRef | None


class TaxonomyIn(BaseModel):
    source_yaml: Annotated[str, Field(min_length=1, max_length=MAX_SOURCE)]
    justification: Annotated[str | None, Field(max_length=MAX_TAXONOMY_JUSTIFICATION)] = None


class ValidateIn(BaseModel):
    source_yaml: Annotated[str, Field(max_length=MAX_SOURCE)]


class ValidationErrorOut(BaseModel):
    path: str
    message: str


class ValidateOut(BaseModel):
    valid: bool
    errors: list[ValidationErrorOut]
    spec: dict[str, Any] | None
    name: str | None


class ApproveTaxonomyIn(BaseModel):
    note: Annotated[str | None, Field(max_length=plans.MAX_NOTE)] = None


class RejectIn(BaseModel):
    note: Note


def _versions(
    db: Session, rows: list[LabelTaxonomy], current: LabelTaxonomy | None, as_of: datetime | None
) -> list[TaxonomyVersion]:
    names = _people(db, {u for r in rows for u in (r.proposed_by_user_id, r.decided_by_user_id)})
    found = []
    for row in rows:
        state: Any = row.state
        decided = row.decided_at is not None and (as_of is None or row.decided_at <= as_of)
        if as_of is not None and not decided:
            state = "proposed"
        if state == "approved" and (current is None or current.id != row.id):
            state = "superseded"
        found.append(
            TaxonomyVersion(
                id=row.id,
                version=row.version,
                name=row.name,
                state=state,
                digest=row.yaml_digest,
                source_yaml=row.source_yaml,
                spec=taxonomies.spec_of(row).model_dump(mode="json"),
                justification=row.justification,
                proposed_by=Person(
                    actor=row.proposed_by,
                    user_id=row.proposed_by_user_id,
                    display_name=names.get(row.proposed_by_user_id),
                ),
                proposed_at=row.proposed_at,
                review_required=row.review_required,
                self_approved=row.self_approved,
                decided_by=_person(row.decided_by, row.decided_by_user_id, names)
                if decided
                else None,
                decided_at=row.decided_at if decided else None,
                decision_note=row.decision_note if decided else None,
                in_force_from=row.decided_at if decided and row.state == "approved" else None,
            )
        )
    return found


def _taxonomy_out(db: Session, as_of: datetime | None) -> TaxonomyOut:
    instance_id = _instance_id(db)
    rows = taxonomies.versions(db, instance_id)
    if as_of is not None:
        rows = [row for row in rows if row.proposed_at <= as_of]
    current = taxonomies.in_force(db, instance_id, as_of)
    listed = _versions(db, rows, current, as_of)
    return TaxonomyOut(
        current=next((v for v in listed if current is not None and v.id == current.id), None),
        versions=listed,
        review_required=review_required(db),
        as_of=as_of,
        evaluation_run=run_ref(run_at(db, instance_id, as_of)),
    )


@router.get(
    "/taxonomy",
    response_model=TaxonomyOut,
    summary="The label taxonomy in force, and every version",
    description="With `as_of`, the taxonomy and versions as they stood then.",
)
def read_taxonomy(_: Reader, db: Database, as_of: datetime | None = None) -> TaxonomyOut:
    return _taxonomy_out(db, as_of)


@router.post(
    "/taxonomy/validate",
    response_model=ValidateOut,
    summary="Validate a taxonomy document without storing it",
    description="Requires the `X-CSRF-Token` header. Stores nothing.",
)
def validate_taxonomy(
    body: ValidateIn,
    _: Annotated[Principal, Depends(require(Permission.POLICY_WRITE))],
) -> ValidateOut:
    try:
        parsed = taxonomies.parse_taxonomy(body.source_yaml)
    except PolicyInvalid as error:
        return ValidateOut(
            valid=False,
            errors=[ValidationErrorOut(path=e.path, message=e.message) for e in error.errors],
            spec=None,
            name=None,
        )
    return ValidateOut(
        valid=True,
        errors=[],
        spec=parsed.document.spec.model_dump(mode="json"),
        name=parsed.name,
    )


def _invalid(error: PolicyInvalid) -> HTTPException:
    errors = [{"path": e.path, "message": e.message} for e in error.errors]
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        {"code": "invalid_taxonomy", "message": str(error), "errors": errors},
    )


def _conflict(error: ChangeConflict) -> HTTPException:
    return _refusal(status.HTTP_409_CONFLICT, "not_proposed", str(error))


def _own_change(error: NotAllowed) -> HTTPException:
    return _refusal(status.HTTP_403_FORBIDDEN, "own_change", str(error))


def _version(db: Session, row: LabelTaxonomy) -> TaxonomyVersion:
    current = taxonomies.in_force(db, row.instance_id)
    [found] = _versions(db, [row], current, None)
    return found


@router.post(
    "/taxonomy",
    response_model=TaxonomyVersion,
    status_code=status.HTTP_201_CREATED,
    summary="Propose a new version of the label taxonomy",
    description="While baseline review is required it waits for someone else to approve it; "
    "otherwise it is in force at once and `self_approved`. The same document as the version "
    "in force or waiting returns that version. 422 `invalid_taxonomy` lists every error; 409 "
    "when another version is waiting. Requires the `X-CSRF-Token` header. Audited.",
    responses={409: {"description": "Another version is waiting for review"}},
)
def propose_taxonomy(
    body: TaxonomyIn,
    principal: Annotated[Principal, Depends(require(Permission.POLICY_WRITE))],
    db: Database,
) -> TaxonomyVersion:
    instance = _instance(db)
    if instance is None:
        raise _refusal(
            status.HTTP_409_CONFLICT, "gitlab_not_configured", "GitLab is not configured"
        )
    try:
        proposed = taxonomies.propose(
            db,
            instance.id,
            body.source_yaml,
            body.justification,
            Actor(principal.user_id, principal.actor),
        )
    except PolicyInvalid as error:
        raise _invalid(error) from None
    except ChangeConflict as error:
        raise _refusal(status.HTTP_409_CONFLICT, "taxonomy_pending", str(error)) from None
    db.commit()
    return _version(db, proposed.row)


def _decide(
    db: Session,
    taxonomy_id: int,
    principal: Principal,
    decision: Literal["approve", "reject"],
    note: str | None,
) -> TaxonomyVersion:
    instance_id = _instance_id(db)
    row = db.get(LabelTaxonomy, taxonomy_id)
    if row is None or row.instance_id != instance_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such taxonomy version")
    by = Actor(principal.user_id, principal.actor)
    try:
        if decision == "approve":
            row = taxonomies.approve(db, taxonomy_id, by, note)
        else:
            row = taxonomies.reject(db, taxonomy_id, by, note or "")
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such taxonomy version") from None
    except NotAllowed as error:
        raise _own_change(error) from None
    except ChangeConflict as error:
        raise _conflict(error) from None
    db.commit()
    return _version(db, row)


_DECISION_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"description": "Missing permission, CSRF failure, or deciding one's own version"},
    404: {"description": "No such taxonomy version"},
    409: {"description": "Already decided"},
}


@router.post(
    "/taxonomy/{taxonomy_id}/approve",
    response_model=TaxonomyVersion,
    summary="Approve a proposed taxonomy version, which puts it in force",
    description="Someone other than the proposer. Requires the `X-CSRF-Token` header.",
    responses=_DECISION_RESPONSES,
)
def approve_taxonomy(
    taxonomy_id: Annotated[int, Path(gt=0)],
    body: ApproveTaxonomyIn,
    principal: Annotated[Principal, Depends(require(Permission.POLICY_WRITE))],
    db: Database,
) -> TaxonomyVersion:
    return _decide(db, taxonomy_id, principal, "approve", body.note)


@router.post(
    "/taxonomy/{taxonomy_id}/reject",
    response_model=TaxonomyVersion,
    summary="Reject a proposed taxonomy version, with a note",
    description="Someone other than the proposer. Requires the `X-CSRF-Token` header.",
    responses=_DECISION_RESPONSES,
)
def reject_taxonomy(
    taxonomy_id: Annotated[int, Path(gt=0)],
    body: RejectIn,
    principal: Annotated[Principal, Depends(require(Permission.POLICY_WRITE))],
    db: Database,
) -> TaxonomyVersion:
    return _decide(db, taxonomy_id, principal, "reject", body.note)


# Drift.


class OwnerOut(BaseModel):
    type: Literal["group", "project"]
    gitlab_id: int
    path: str


class ObservationRef(BaseModel):
    id: int
    payload_digest: str | None
    collected_at: datetime


class DriftItemOut(BaseModel):
    id: str
    owner: OwnerOut
    top_level_group: str
    kind: str
    label: str | None
    observed: dict[str, Any] | None
    expected: dict[str, Any] | None
    reason: str
    observations: list[ObservationRef]


class GroupTotals(BaseModel):
    group: str
    total: int
    by_kind: dict[str, int]


class Totals(BaseModel):
    total: int
    by_kind: dict[str, int]
    by_group: list[GroupTotals]


class DriftPage(BaseModel):
    items: list[DriftItemOut]
    next_after: str | None
    # Of every item matching the filters, not only this page.
    totals: Totals
    taxonomy: TaxonomyRef | None
    collected_at: datetime | None
    as_of: datetime | None
    evaluation_run: RunRef | None


def _ref(row: LabelTaxonomy | None) -> TaxonomyRef | None:
    return TaxonomyRef(id=row.id, version=row.version, name=row.name) if row is not None else None


def _item(item: DriftItem) -> DriftItemOut:
    return DriftItemOut.model_validate(item.as_dict())


def _in_group(item: DriftItem, group: str) -> bool:
    return item.owner.path == group or item.owner.path.startswith(group + "/")


@router.get(
    "/drift",
    response_model=DriftPage,
    summary="Label drift from the taxonomy in force",
    description="Ordered by owner path, owner type, kind and label; follow `next_after`. "
    "`group` keeps drift on that group and below; `kind` and `label` filter exactly. With "
    "`as_of`, from the taxonomy and label observations as they stood then.",
)
def read_drift(
    _: Reader,
    db: Database,
    group: Annotated[str | None, Query(max_length=255)] = None,
    kind: Annotated[str | None, Query(max_length=32)] = None,
    label: Annotated[str | None, Query(max_length=255)] = None,
    after: Annotated[str | None, Query(max_length=600)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    as_of: datetime | None = None,
) -> DriftPage:
    if kind is not None and kind not in DRIFT_KINDS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"kind must be one of {DRIFT_KINDS}"
        )
    instance_id = _instance_id(db)
    found = drift.load(db, instance_id, as_of)
    items = [
        item
        for item in found.items
        if (group is None or _in_group(item, group))
        and (kind is None or item.kind == kind)
        and (label is None or item.label == label)
    ]
    start = 0
    if after is not None:
        start = next((i + 1 for i, item in enumerate(items) if item.id == after), len(items))
    page = items[start : start + limit]
    return DriftPage(
        items=[_item(item) for item in page],
        next_after=page[-1].id if start + limit < len(items) else None,
        totals=Totals.model_validate(drift.totals(items)),
        taxonomy=_ref(found.taxonomy),
        collected_at=found.collected_at,
        as_of=as_of,
        evaluation_run=run_ref(run_at(db, instance_id, as_of)),
    )


class OwnerLabel(BaseModel):
    id: int
    name: str
    color: str | None
    description: str | None
    priority: int | None
    # Where the label is defined: the owner itself, or a group above it.
    defined_at: OwnerOut


class OwnerDetail(BaseModel):
    owner: OwnerOut
    # ``unknown`` when the labels of the owner or a group above it could not be read.
    status: Literal["known", "unknown"]
    reason: str | None
    observation: ObservationRef | None
    labels: list[OwnerLabel]
    drift: list[DriftItemOut]
    as_of: datetime | None


@router.get(
    "/owners/{owner_type}/{gitlab_id}",
    response_model=OwnerDetail,
    summary="The labels a group or project has, its own and inherited, with the drift reaching it",
    responses={404: {"description": "No such group or project in the inventory"}},
)
def read_owner(
    owner_type: Literal["group", "project"],
    gitlab_id: Annotated[int, Path(gt=0)],
    _: Reader,
    db: Database,
    as_of: datetime | None = None,
) -> OwnerDetail:
    found = drift.load(db, _instance_id(db), as_of)
    inventory = found.inventory
    if owner_type == "project":
        project = next((p for p in inventory.projects if p.gitlab_id == gitlab_id), None)
        if project is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project in the inventory")
        chain = drift.chain(inventory, project)
        path = project.path
        rows = {("project", project.gitlab_id): project.row_id}
    else:
        group = inventory.groups.get(gitlab_id)
        if group is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such group in the inventory")
        chain = [("group", group.gitlab_id)]
        parent = inventory.groups.get(group.parent_gitlab_id) if group.parent_gitlab_id else None
        while parent is not None:
            chain.append(("group", parent.gitlab_id))
            parent = (
                inventory.groups.get(parent.parent_gitlab_id) if parent.parent_gitlab_id else None
            )
        path = group.path
        rows = {}
    for kind, gid in chain:
        if kind == "group" and gid in inventory.groups:
            rows[("group", gid)] = inventory.groups[gid].row_id
    labels: list[OwnerLabel] = []
    unread: list[str] = []
    own: ObservationRef | None = None
    for kind, gid in chain:
        row_id = rows.get((kind, gid))
        defined = OwnerOut(
            type=kind,  # type: ignore[arg-type]
            gitlab_id=gid,
            path=path if (kind, gid) == (owner_type, gitlab_id) else inventory.groups[gid].path,
        )
        observed = found.labels.get((kind, row_id)) if row_id is not None else None
        if (kind, gid) == (owner_type, gitlab_id) and observed is not None:
            own = ObservationRef(
                id=observed.observation_id,
                payload_digest=observed.payload_digest,
                collected_at=observed.collected_at,
            )
        if observed is None or not observed.readable:
            unread.append(defined.path)
            continue
        for entry in observed.labels:
            labels.append(
                OwnerLabel(
                    id=entry["id"],
                    name=entry["name"],
                    color=entry.get("color"),
                    description=entry.get("description"),
                    priority=entry.get("priority"),
                    defined_at=defined,
                )
            )
    owners = set(chain)
    reaching = [item for item in found.items if (item.owner.type, item.owner.gitlab_id) in owners]
    return OwnerDetail(
        owner=OwnerOut(type=owner_type, gitlab_id=gitlab_id, path=path),
        status="unknown" if unread else "known",
        reason=f"The labels of {', '.join(unread)} have not been read." if unread else None,
        observation=own,
        labels=sorted(labels, key=lambda label: (label.name, label.id)),
        drift=[_item(item) for item in reaching],
        as_of=as_of,
    )


# Plans.


class PlanScopeIn(BaseModel):
    groups: Annotated[list[GroupPath], Field(max_length=200)] = []


class PlanIn(BaseModel):
    scope: PlanScopeIn = PlanScopeIn()


class ApprovePlanIn(BaseModel):
    plan_digest: Annotated[
        Digest, Field(description="The digest of the plan the approver reviewed.")
    ]
    note: Annotated[str | None, Field(max_length=plans.MAX_NOTE)] = None


class StepOut(BaseModel):
    step: int
    owner: OwnerOut
    action: Literal["create", "rename", "update"]
    label: str
    prior: dict[str, Any] | None
    intended: dict[str, Any]
    idempotency_key: str
    status: Literal["pending", "skipped", "applied", "failed", "not_run"]
    request_status: int | None
    verified: bool | None
    message: str | None
    finished_at: datetime | None


class ManualItemOut(BaseModel):
    owner: OwnerOut
    kind: str
    label: str | None
    reason: str


class CanApply(BaseModel):
    allowed: bool
    reasons: list[str]


class LabelPlanSummary(BaseModel):
    id: int
    state: PlanState
    taxonomy: TaxonomyRef
    scope: dict[str, list[str]]
    steps: int
    manual: int
    plan_digest: str
    created_by: Person
    created_at: datetime
    expires_at: datetime
    decided_by: Person | None
    decided_at: datetime | None
    finished_at: datetime | None


class LabelPlanOut(BaseModel):
    id: int
    state: PlanState
    scope: dict[str, list[str]]
    taxonomy: TaxonomyRef
    steps: list[StepOut]
    manual: list[ManualItemOut]
    plan_digest: str
    # Recomputed from the stored plan on every read.
    digest_valid: bool
    created_by: Person
    created_at: datetime
    expires_at: datetime
    submitted_at: datetime | None
    decided_by: Person | None
    decided_at: datetime | None
    decision_note: str | None
    review_required: bool
    self_approved: bool
    apply_started_at: datetime | None
    finished_at: datetime | None
    failed_step: int | None
    failure: str | None
    can_apply: CanApply


class LabelPlanPage(BaseModel):
    items: list[LabelPlanSummary]
    next_after_id: int | None


def _taxonomy_ref(row: LabelPlan) -> TaxonomyRef:
    found = row.plan["taxonomy"]
    return TaxonomyRef(id=found["id"], version=found["version"], name=found["name"])


def _summary(row: LabelPlan, state: str, names: dict[int, str | None]) -> LabelPlanSummary:
    return LabelPlanSummary(
        id=row.id,
        state=state,  # type: ignore[arg-type]
        taxonomy=_taxonomy_ref(row),
        scope=row.plan["scope"],
        steps=row.steps,
        manual=row.manual,
        plan_digest=row.plan_digest,
        created_by=Person(
            actor=row.created_by,
            user_id=row.created_by_user_id,
            display_name=names.get(row.created_by_user_id),
        ),
        created_at=row.created_at,
        expires_at=row.expires_at,
        decided_by=_person(row.decided_by, row.decided_by_user_id, names),
        decided_at=row.decided_at,
        finished_at=row.finished_at,
    )


def _plan_out(db: Session, plan_id: int) -> LabelPlanOut:
    instance_id = _instance_id(db)
    found = db.execute(
        select(LabelPlan, plans.effective_state()).where(
            LabelPlan.id == plan_id, LabelPlan.instance_id == (instance_id or 0)
        )
    ).first()
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such label plan")
    row, state = found
    names = _people(db, {row.created_by_user_id, row.decided_by_user_id})
    progress = {entry["step"]: entry for entry in row.progress}
    steps = []
    for step in row.plan.get("steps", []):
        done = progress.get(step["step"], {})
        steps.append(
            StepOut(
                step=step["step"],
                owner=step["owner"],
                action=step["action"],
                label=step["label"],
                prior=step["prior"],
                intended=step["intended"],
                idempotency_key=step["idempotency_key"],
                status=done.get("status", "pending"),
                request_status=done.get("request_status"),
                verified=done.get("verified"),
                message=done.get("message"),
                finished_at=done.get("finished_at"),
            )
        )
    reasons = plans.apply_refusals(db, row)
    return LabelPlanOut(
        id=row.id,
        state=state,
        scope=row.plan["scope"],
        taxonomy=_taxonomy_ref(row),
        steps=steps,
        manual=[ManualItemOut.model_validate(item) for item in row.plan.get("manual", [])],
        plan_digest=row.plan_digest,
        digest_valid=plans.digest_valid(row),
        created_by=Person(
            actor=row.created_by,
            user_id=row.created_by_user_id,
            display_name=names.get(row.created_by_user_id),
        ),
        created_at=row.created_at,
        expires_at=row.expires_at,
        submitted_at=row.submitted_at,
        decided_by=_person(row.decided_by, row.decided_by_user_id, names),
        decided_at=row.decided_at,
        decision_note=row.decision_note,
        review_required=row.review_required,
        self_approved=row.self_approved,
        apply_started_at=row.apply_started_at,
        finished_at=row.finished_at,
        failed_step=row.failed_step,
        failure=row.failure,
        can_apply=CanApply(allowed=not reasons, reasons=reasons),
    )


def _plan_owners(
    found: drift.Inventory, spec: taxonomies.TaxonomySpec, groups: list[str]
) -> tuple[list[Owner], drift.Inventory]:
    """The groups and projects a plan reads: those in both scopes, and the groups above them
    within the taxonomy's scope (where the labels they lack would be created)."""
    projects = [
        p
        for p in found.projects
        if taxonomies.covers(spec.scope, p.path) and plans.in_plan_scope(groups, p.path)
    ]
    chosen = {
        gid
        for gid, g in found.groups.items()
        if taxonomies.covers(spec.scope, g.path) and plans.in_plan_scope(groups, g.path)
    }
    for gid in list(chosen) + [p.namespace_gitlab_id for p in projects]:
        current = found.groups.get(gid)
        while current is not None and taxonomies.covers(spec.scope, current.path):
            chosen.add(current.gitlab_id)
            current = (
                found.groups.get(current.parent_gitlab_id) if current.parent_gitlab_id else None
            )
    narrowed = drift.Inventory({gid: found.groups[gid] for gid in chosen}, projects)
    owners = [Owner("group", g.row_id, g.gitlab_id, g.path) for g in narrowed.groups.values()]
    owners += [Owner("project", p.row_id, p.gitlab_id, p.path) for p in projects]
    return sorted(owners, key=lambda o: (o.type, o.path)), narrowed


@router.post(
    "/plans",
    response_model=LabelPlanOut,
    status_code=status.HTTP_201_CREATED,
    summary="Build a label plan from a fresh read of the labels in scope",
    description="Reads the labels of every group and project in `scope.groups` (all of the "
    "taxonomy's scope when empty) with the read-only token, then plans the creates, renames "
    "and updates that remove drift on each hierarchy's top-level group. Everything else is "
    "listed under `manual`. Nothing is deleted, now or when applied. 409 `scope_too_large` "
    f"above {plans.MAX_OWNERS} groups and projects, `nothing_to_plan` when nothing drifts, "
    "`no_taxonomy` when none is in force. Requires the `X-CSRF-Token` header. Audited.",
)
def create_plan(
    body: PlanIn,
    principal: Annotated[Principal, Depends(require(Permission.REMEDIATION_PLAN))],
    db: Database,
    transport: Annotated[httpx.BaseTransport | None, Depends(gitlab_transport)],
) -> LabelPlanOut:
    settings = _settings()
    instance = _instance(db)
    if settings is None or instance is None:
        raise _refusal(
            status.HTTP_409_CONFLICT, "gitlab_not_configured", "GitLab is not configured"
        )
    current = taxonomies.in_force(db, instance.id)
    if current is None:
        raise _refusal(status.HTTP_409_CONFLICT, "no_taxonomy", "No label taxonomy is in force")
    spec = taxonomies.spec_of(current)
    owners, narrowed = _plan_owners(drift.inventory(db, instance.id), spec, body.scope.groups)
    if len(owners) > plans.MAX_OWNERS:
        raise _refusal(
            status.HTTP_409_CONFLICT,
            "scope_too_large",
            f"The scope holds {len(owners)} groups and projects; a plan reads at most "
            f"{plans.MAX_OWNERS}. Name fewer groups.",
        )
    if not owners:
        raise _refusal(
            status.HTTP_409_CONFLICT, "nothing_to_plan", "No group or project is in scope"
        )
    engine = cast(Engine, db.get_bind())
    db.commit()  # release the session's snapshot while GitLab is read
    with GitLabClient(settings, transport=transport) as client:
        collect(client, engine, instance.id, only=owners)
    labels = drift.observations(db, instance.id, only={(o.type, o.row_id) for o in owners})
    items = drift.compute(spec, narrowed, labels)
    try:
        row = plans.create(
            db,
            instance,
            current,
            items,
            body.scope.groups,
            Actor(principal.user_id, principal.actor),
            review_required(db),
        )
    except NothingToPlan as exc:
        raise _refusal(status.HTTP_409_CONFLICT, "nothing_to_plan", str(exc)) from None
    except ScopeTooLarge as exc:
        raise _refusal(status.HTTP_409_CONFLICT, "scope_too_large", str(exc)) from None
    db.commit()
    return _plan_out(db, row.id)


@router.get(
    "/plans",
    response_model=LabelPlanPage,
    summary="Label plans",
    description="Filter by `state` (with `expired` counting any open plan past `expires_at`). "
    "Newest first; follow `next_after_id`.",
)
def list_plans(
    _: Reader,
    db: Database,
    state: PlanState | None = None,
    after_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> LabelPlanPage:
    statement = select(LabelPlan, plans.effective_state()).where(
        LabelPlan.instance_id == (_instance_id(db) or 0)
    )
    if state is not None:
        statement = statement.where(plans.effective_state() == state)
    if after_id is not None:
        statement = statement.where(LabelPlan.id < after_id)
    rows = list(db.execute(statement.order_by(LabelPlan.id.desc()).limit(limit + 1)).all())
    names = _people(db, {u for r, _ in rows for u in (r.created_by_user_id, r.decided_by_user_id)})
    items = [_summary(row, row_state, names) for row, row_state in rows[:limit]]
    return LabelPlanPage(items=items, next_after_id=items[-1].id if len(rows) > limit else None)


@router.get(
    "/plans/{plan_id}",
    response_model=LabelPlanOut,
    summary="One label plan, with its steps, manual items and apply progress",
    responses={404: {"description": "No such label plan"}},
)
def read_plan(plan_id: Annotated[int, Path(gt=0)], _: Reader, db: Database) -> LabelPlanOut:
    return _plan_out(db, plan_id)


def _transition(
    db: Session,
    plan_id: int,
    principal: Principal,
    action: Literal["submit", "approved", "rejected", "apply"],
    note: str | None = None,
    digest: str | None = None,
) -> LabelPlanOut:
    _plan_out(db, plan_id)  # 404 unless it belongs to the configured instance
    row = plans.locked(db, plan_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such label plan")
    by = Actor(principal.user_id, principal.actor)
    try:
        if action == "submit":
            plans.submit(db, row, by)
        elif action == "apply":
            plans.start_apply(db, row, by)
        else:
            plans.decide(db, row, action, by, note, digest)
    except PlanConflict as exc:
        reasons = plans.apply_refusals(db, row) if action == "apply" else [exc.code]
        raise _refusal(status.HTTP_409_CONFLICT, exc.code, str(exc), reasons) from None
    except PermissionError as exc:
        raise _refusal(status.HTTP_403_FORBIDDEN, "own_change", str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    db.commit()
    return _plan_out(db, plan_id)


_TRANSITION_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"description": "Missing permission, CSRF failure, or not allowed for this user"},
    404: {"description": "No such label plan"},
    409: {"description": "Not allowed now; `reasons` lists every reason code"},
}


@router.post(
    "/plans/{plan_id}/submit",
    response_model=LabelPlanOut,
    summary="Submit a draft plan for approval",
    description="Only the plan's creator. While review is not required the plan is approved at "
    "once and `self_approved`. Requires the `X-CSRF-Token` header.",
    responses=_TRANSITION_RESPONSES,
)
def submit_plan(
    plan_id: Annotated[int, Path(gt=0)],
    principal: Annotated[Principal, Depends(require(Permission.REMEDIATION_PLAN))],
    db: Database,
) -> LabelPlanOut:
    return _transition(db, plan_id, principal, "submit")


@router.post(
    "/plans/{plan_id}/approve",
    response_model=LabelPlanOut,
    summary="Approve a submitted plan",
    description="Only someone other than its creator, before `expires_at`, naming the "
    "`plan_digest` they reviewed (409 `digest_mismatch` otherwise), while the same taxonomy is "
    "in force (409 `taxonomy_changed`). Requires the `X-CSRF-Token` header.",
    responses=_TRANSITION_RESPONSES,
)
def approve_plan(
    plan_id: Annotated[int, Path(gt=0)],
    body: ApprovePlanIn,
    principal: Annotated[Principal, Depends(require(Permission.REMEDIATION_APPROVE))],
    db: Database,
) -> LabelPlanOut:
    return _transition(db, plan_id, principal, "approved", body.note, body.plan_digest)


@router.post(
    "/plans/{plan_id}/reject",
    response_model=LabelPlanOut,
    summary="Reject a submitted plan",
    description="Only someone other than its creator; `note` says why. Requires the "
    "`X-CSRF-Token` header.",
    responses=_TRANSITION_RESPONSES,
)
def reject_plan(
    plan_id: Annotated[int, Path(gt=0)],
    body: RejectIn,
    principal: Annotated[Principal, Depends(require(Permission.REMEDIATION_APPROVE))],
    db: Database,
) -> LabelPlanOut:
    return _transition(db, plan_id, principal, "rejected", body.note)


@router.post(
    "/plans/{plan_id}/apply",
    response_model=LabelPlanOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Apply an approved plan to GitLab",
    description="Queues the applier and returns the plan `applying`; follow its steps' "
    "progress. Refused (409, every reason listed) unless `BALUARTE_LABEL_WRITES` is true, a "
    "label write token is stored, the plan is approved and unexpired, and its taxonomy is "
    "still in force. Each write is sent once, read back with the read-only token, and the plan "
    "stops at the first unexpected result. Requires the `X-CSRF-Token` header. Audited.",
    responses=_TRANSITION_RESPONSES,
)
def apply_plan(
    plan_id: Annotated[int, Path(gt=0)],
    principal: Annotated[Principal, Depends(require(Permission.REMEDIATION_PLAN))],
    db: Database,
) -> LabelPlanOut:
    return _transition(db, plan_id, principal, "apply")
