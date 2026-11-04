"""Framework controls, their status, and the organization's changes to the control mapping.

Reading needs ``findings:read`` (every role): auditors must see the mapping in force and who
changed it, with the justification. Proposing, approving and rejecting a change need
``policy:write``; a change is approved or rejected by someone other than its proposer.
"""

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.compliance.catalogue import FRAMEWORKS, FrameworkId
from baluarte.controls import changes
from baluarte.controls.catalogue import MAPPING_VERSION, control
from baluarte.controls.changes import (
    Actor,
    ChangeConflict,
    ChangeKind,
    ChangeState,
    MappingConflict,
    NotAllowed,
    Proposal,
)
from baluarte.controls.posture import ControlPosture, ControlStatus, Counts, MappedRule, posture
from baluarte.db.models import (
    MAX_EVIDENCE_REF,
    MAX_JUSTIFICATION,
    MIN_JUSTIFICATION,
    AppUser,
    ControlMappingChange,
    GitLabInstance,
)
from baluarte.integrations.gitlab.settings import GitLabSettings, load_gitlab_settings
from baluarte.policy.changes import MAX_NOTE, review_required
from baluarte.settings import ConfigurationError

router = APIRouter(tags=["controls"])

ControlId = Annotated[str, Path(min_length=1, max_length=20)]


class FrameworkOut(BaseModel):
    id: FrameworkId
    name: str


class MappedOut(BaseModel):
    rule_type: str
    source: Literal["default", "organization"]
    # The change that mapped it; null for a default pair.
    change_id: int | None
    # A default pair's rationale, or the organization's justification.
    rationale: str | None
    justification: str | None


class UnmappedOut(BaseModel):
    """A default pair the organization removed; withdrawing ``change_id`` restores it."""

    rule_type: str
    change_id: int
    justification: str


class StatusChangeOut(BaseModel):
    """The active change that scopes the control out or records it as met outside GitLab."""

    id: int
    kind: Literal["not_applicable", "manual"]
    justification: str
    evidence_ref: str | None
    review_by: date


class CountsOut(BaseModel):
    # Results (one per project and rule) by outcome; unknown includes unsupported.
    pass_: int = Field(alias="pass")
    fail: int
    unknown: int
    excepted: int
    # Projects with at least one result.
    projects: int

    model_config = ConfigDict(populate_by_name=True)


class ControlOut(BaseModel):
    framework: FrameworkId
    id: str
    title: str
    area: str
    status: ControlStatus
    mapped: list[MappedOut]
    unmapped: list[UnmappedOut]
    change: StatusChangeOut | None
    counts: CountsOut


class ControlsPage(BaseModel):
    mapping_version: str
    # The evaluation run the counts come from: the latest successful one, or null.
    evaluation_run_id: int | None
    frameworks: list[FrameworkOut]
    controls: list[ControlOut]


class PolicyRef(BaseModel):
    name: str
    version: int


class RuleOut(BaseModel):
    policy: PolicyRef
    rule_id: str
    rule_type: str
    counts: CountsOut


class Person(BaseModel):
    actor: str
    user_id: int | None
    display_name: str | None


class ChangeOut(BaseModel):
    id: int
    framework: FrameworkId
    control_id: str
    kind: ChangeKind
    state: ChangeState
    rule_type: str | None
    justification: str
    evidence_ref: str | None
    review_by: date | None
    supersedes_id: int | None
    review_required: bool
    self_approved: bool
    mapping_version: str
    proposed_by: Person
    proposed_at: datetime
    decided_by: Person | None
    decided_at: datetime | None
    decision_note: str | None


class ControlDetail(ControlOut):
    # Results per baseline rule of a mapped type, from the same evaluation run.
    rules: list[RuleOut]
    # Every change made to this control, newest first.
    changes: list[ChangeOut]


class ChangePage(BaseModel):
    # Whether new changes currently need a second person (the ``governance`` setting).
    review_required: bool
    items: list[ChangeOut]
    next_before_id: int | None


class ChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    framework: FrameworkId
    control_id: Annotated[str, Field(min_length=1, max_length=20)]
    kind: ChangeKind
    rule_type: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    justification: Annotated[str, Field(min_length=MIN_JUSTIFICATION, max_length=MAX_JUSTIFICATION)]
    evidence_ref: Annotated[str, Field(min_length=1, max_length=MAX_EVIDENCE_REF)] | None = None
    review_by: date | None = None
    supersedes_id: Annotated[int, Field(gt=0)] | None = None

    @model_validator(mode="after")
    def _shape(self) -> "ChangeIn":
        pairs = self.kind in ("map", "unmap")
        if pairs != (self.rule_type is not None):
            raise ValueError("rule_type is required for map and unmap, and only for them")
        statuses = self.kind in changes.STATUS_KINDS
        if statuses != (self.review_by is not None):
            raise ValueError("review_by is required for not_applicable and manual, and only them")
        if self.kind == "manual" and self.evidence_ref is None:
            raise ValueError("evidence_ref is required for manual: where the evidence is kept")
        if self.kind == "withdraw" and self.supersedes_id is None:
            raise ValueError("supersedes_id is required for withdraw: the change it ends")
        if self.supersedes_id is not None and not (statuses or self.kind == "withdraw"):
            raise ValueError("supersedes_id is only for withdraw, not_applicable and manual")
        return self


class ApproveIn(BaseModel):
    note: Annotated[str, Field(max_length=MAX_NOTE)] | None = None


class RejectIn(BaseModel):
    note: Annotated[str, Field(min_length=1, max_length=MAX_NOTE)]


def _instance_id(db: Session) -> int | None:
    settings: GitLabSettings | None
    try:
        settings = load_gitlab_settings()
    except ConfigurationError:
        settings = None
    if settings is None:
        return None
    return db.scalars(
        select(GitLabInstance.id).where(GitLabInstance.base_url == settings.url)
    ).first()


def counts_out(counts: Counts) -> CountsOut:
    return CountsOut.model_validate(
        {
            "pass": counts.passed,
            "fail": counts.fail,
            "unknown": counts.unknown,
            "excepted": counts.excepted,
            "projects": len(counts.projects),
        }
    )


def _mapped_out(rule: MappedRule) -> MappedOut:
    default = rule.source == "default"
    return MappedOut(
        rule_type=rule.rule_type,
        source=rule.source,
        change_id=rule.change_id,
        rationale=rule.text if default else None,
        justification=None if default else rule.text,
    )


def control_out(item: ControlPosture) -> ControlOut:
    change = item.change
    return ControlOut(
        framework=item.control.framework,
        id=item.control.id,
        title=item.control.title,
        area=item.control.area,
        status=item.status,
        mapped=[_mapped_out(rule) for rule in item.mapped],
        unmapped=[
            UnmappedOut(rule_type=c.rule_type or "", change_id=c.id, justification=c.justification)
            for c in item.unmapped
        ],
        change=(
            StatusChangeOut(
                id=change.id,
                kind=change.kind,  # type: ignore[arg-type]
                justification=change.justification,
                evidence_ref=change.evidence_ref,
                review_by=change.review_by,  # type: ignore[arg-type]
            )
            if change is not None
            else None
        ),
        counts=counts_out(item.counts),
    )


def _user_id(actor: str | None) -> int | None:
    if actor and actor.startswith("user:") and actor[5:].isdigit():
        return int(actor[5:])
    return None


def _names(db: Session, rows: Iterable[ControlMappingChange]) -> dict[int, str | None]:
    ids = {
        user_id
        for row in rows
        for user_id in (_user_id(row.proposed_by), _user_id(row.decided_by))
        if user_id is not None
    }
    if not ids:
        return {}
    return dict(
        db.execute(select(AppUser.id, AppUser.display_name).where(AppUser.id.in_(ids))).all()
    )


def _person(actor: str, names: dict[int, str | None]) -> Person:
    user_id = _user_id(actor)
    return Person(
        actor=actor, user_id=user_id, display_name=names.get(user_id) if user_id else None
    )


def change_out(db: Session, rows: Sequence[ControlMappingChange]) -> list[ChangeOut]:
    names = _names(db, rows)
    return [
        ChangeOut(
            id=row.id,
            framework=row.framework,  # type: ignore[arg-type]
            control_id=row.control_id,
            kind=row.kind,  # type: ignore[arg-type]
            state=row.state,  # type: ignore[arg-type]
            rule_type=row.rule_type,
            justification=row.justification,
            evidence_ref=row.evidence_ref,
            review_by=row.review_by,
            supersedes_id=row.supersedes_id,
            review_required=row.review_required,
            self_approved=row.self_approved,
            mapping_version=row.mapping_version,
            proposed_by=_person(row.proposed_by, names),
            proposed_at=row.proposed_at,
            decided_by=_person(row.decided_by, names) if row.decided_by else None,
            decided_at=row.decided_at,
            decision_note=row.decision_note,
        )
        for row in rows
    ]


def _conflict(error: ChangeConflict) -> HTTPException:
    """409 with ``{code, message, reasons: [{code, message}]}``; ``code`` is the first reason."""
    if isinstance(error, MappingConflict):
        reasons = [{"code": r, "message": changes.MESSAGES[r]} for r in error.reasons]
    else:
        reasons = [{"code": "not_proposed", "message": str(error)}]
    detail = {"code": reasons[0]["code"], "message": str(error), "reasons": reasons}
    return HTTPException(status.HTTP_409_CONFLICT, detail=detail)


def _own_change(error: NotAllowed) -> HTTPException:
    return HTTPException(
        status.HTTP_403_FORBIDDEN, detail={"code": "own_change", "message": str(error)}
    )


@router.get(
    "/controls",
    response_model=ControlsPage,
    summary="Framework controls with their mapped rules and status",
    description="The built-in catalogue of SOC 2, ISO/IEC 27001:2022 and NIST SP 800-53 Rev. 5 "
    "controls that GitLab governance supports, with the effective mapping (the default plus the "
    "organization's active changes) and each control's status from the latest successful "
    "evaluation run. Filter with `framework`. Guidance for the organization and its auditor, "
    "not a certification.",
)
def list_controls(
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
    framework: FrameworkId | None = None,
) -> ControlsPage:
    found = posture(db, _instance_id(db), framework)
    return ControlsPage(
        mapping_version=MAPPING_VERSION,
        evaluation_run_id=found.evaluation_run_id,
        frameworks=[FrameworkOut(id=f.id, name=f.name) for f in FRAMEWORKS],
        controls=[control_out(item) for item in found.controls],
    )


@router.get(
    "/controls/{framework}/{control_id}",
    response_model=ControlDetail,
    summary="One control: its status, the results behind it and its change history",
    responses={404: {"description": "No such control in the catalogue"}},
)
def read_control(
    framework: FrameworkId,
    control_id: ControlId,
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
) -> ControlDetail:
    item = control(framework, control_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such control")
    [found] = posture(db, _instance_id(db), controls=[item]).controls
    history = db.scalars(
        select(ControlMappingChange)
        .where(
            ControlMappingChange.framework == framework,
            ControlMappingChange.control_id == control_id,
        )
        .order_by(ControlMappingChange.id.desc())
    ).all()
    return ControlDetail(
        **control_out(found).model_dump(by_alias=True),
        rules=[
            RuleOut(
                policy=PolicyRef(name=rule.policy, version=rule.version),
                rule_id=rule.rule_id,
                rule_type=rule.rule_type,
                counts=counts_out(rule.counts),
            )
            for rule in found.rules
        ],
        changes=change_out(db, history),
    )


@router.get(
    "/control-mapping/changes",
    response_model=ChangePage,
    summary="Changes to the control mapping, newest first",
    description="Every change the organization made or proposed, with its justification, who "
    "proposed it and who approved or rejected it. Filter with `state`, `framework` and "
    "`control_id`; follow `next_before_id`.",
)
def list_changes(
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
    state: ChangeState | None = None,
    framework: FrameworkId | None = None,
    control_id: Annotated[str | None, Query(min_length=1, max_length=20)] = None,
    before_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ChangePage:
    statement = (
        select(ControlMappingChange).order_by(ControlMappingChange.id.desc()).limit(limit + 1)
    )
    if state is not None:
        statement = statement.where(ControlMappingChange.state == state)
    if framework is not None:
        statement = statement.where(ControlMappingChange.framework == framework)
    if control_id is not None:
        statement = statement.where(ControlMappingChange.control_id == control_id)
    if before_id is not None:
        statement = statement.where(ControlMappingChange.id < before_id)
    rows = db.scalars(statement).all()
    items = change_out(db, rows[:limit])
    return ChangePage(
        review_required=review_required(db),
        items=items,
        next_before_id=items[-1].id if len(rows) > limit else None,
    )


@router.post(
    "/control-mapping/changes",
    response_model=ChangeOut,
    status_code=status.HTTP_201_CREATED,
    summary="Change the control mapping, with a justification",
    description="`map` adds a rule type to a control, `unmap` removes a default pair, "
    "`not_applicable` scopes a control out and `manual` records that it is met outside GitLab "
    "(both until `review_by`, at most 365 days ahead; `manual` needs `evidence_ref`), and "
    "`withdraw` ends the active change `supersedes_id`. A `not_applicable` or `manual` change "
    "with `supersedes_id` renews the control's current one. While review is required the change "
    "is `proposed` until someone else approves it; otherwise it is `active` and "
    "`self_approved`. Requires the `X-CSRF-Token` header. Audited.",
    responses={
        409: {"description": "The change conflicts; `reasons` lists every reason code"},
        422: {"description": "The body does not fit the kind of change"},
    },
)
def propose_change(
    body: ChangeIn,
    principal: Annotated[Principal, Depends(require(Permission.POLICY_WRITE))],
    db: Annotated[Session, Depends(database)],
) -> ChangeOut:
    proposal = Proposal(
        framework=body.framework,
        control_id=body.control_id,
        kind=body.kind,
        justification=body.justification,
        rule_type=body.rule_type,
        evidence_ref=body.evidence_ref,
        review_by=body.review_by,
        supersedes_id=body.supersedes_id,
    )
    try:
        row = changes.propose(db, proposal, Actor(principal.user_id, principal.actor))
    except ChangeConflict as error:
        raise _conflict(error) from None
    db.commit()
    [out] = change_out(db, [row])
    return out


@router.post(
    "/control-mapping/changes/{change_id}/approve",
    response_model=ChangeOut,
    summary="Approve a proposed change, which puts it in force",
    description="Someone other than the proposer, with `policy:write`. Requires the "
    "`X-CSRF-Token` header.",
    responses={
        403: {"description": "Missing permission, CSRF failure, or approving one's own change"},
        404: {"description": "No such change"},
        409: {"description": "Already decided, or it can no longer be made"},
    },
)
def approve_change(
    change_id: Annotated[int, Path(gt=0)],
    body: ApproveIn,
    principal: Annotated[Principal, Depends(require(Permission.POLICY_WRITE))],
    db: Annotated[Session, Depends(database)],
) -> ChangeOut:
    try:
        row = changes.approve(db, change_id, Actor(principal.user_id, principal.actor), body.note)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such change") from None
    except NotAllowed as error:
        raise _own_change(error) from None
    except ChangeConflict as error:
        raise _conflict(error) from None
    db.commit()
    [out] = change_out(db, [row])
    return out


@router.post(
    "/control-mapping/changes/{change_id}/reject",
    response_model=ChangeOut,
    summary="Reject a proposed change, with a note",
    description="Someone other than the proposer, with `policy:write`. Nothing changes in force. "
    "Requires the `X-CSRF-Token` header.",
    responses={
        403: {"description": "Missing permission, CSRF failure, or rejecting one's own change"},
        404: {"description": "No such change"},
        409: {"description": "Already decided"},
    },
)
def reject_change(
    change_id: Annotated[int, Path(gt=0)],
    body: RejectIn,
    principal: Annotated[Principal, Depends(require(Permission.POLICY_WRITE))],
    db: Annotated[Session, Depends(database)],
) -> ChangeOut:
    try:
        row = changes.reject(db, change_id, Actor(principal.user_id, principal.actor), body.note)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such change") from None
    except NotAllowed as error:
        raise _own_change(error) from None
    except ChangeConflict as error:
        raise _conflict(error) from None
    db.commit()
    [out] = change_out(db, [row])
    return out
