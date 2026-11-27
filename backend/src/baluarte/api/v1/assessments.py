"""Control assessments: a person's conclusion about a control for a period, citing evidence and
approved by a reviewer other than the assessor (roadmap M15.3,
docs/EVIDENCE_MANAGEMENT.md#control-assessments-m153)."""

import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.compliance.catalogue import FrameworkId
from baluarte.db.models import (
    ASSESSMENT_NARRATIVE,
    MAX_CITATION_RATIONALE,
    AppUser,
    AssessmentCitation,
    ControlAssessment,
)
from baluarte.evidence import assessments as service
from baluarte.evidence.items import Actor, ControlRef, EvidenceRefused, NotAllowed

router = APIRouter(tags=["evidence"])

AssessmentId = Annotated[int, Path(ge=1)]
CONTROL = re.compile(r"^(?P<framework>[a-z0-9_]+):(?P<control>[A-Za-z0-9.()-]{1,20})$")
Conclusion = Literal["effective", "partially_effective", "not_effective", "not_tested"]
State = Literal["draft", "submitted", "approved", "rejected"]
CitationKind = Literal[
    "evidence_item", "observation", "change_record", "change_set", "control_status"
]


class UserRef(BaseModel):
    user_id: int
    display_name: str | None


class ControlLink(BaseModel):
    framework: str
    version: str
    control_id: str
    title: str


class DraftIn(BaseModel):
    period_start: date
    period_end: date
    conclusion: Conclusion
    narrative: Annotated[
        str, Field(min_length=ASSESSMENT_NARRATIVE[0], max_length=ASSESSMENT_NARRATIVE[1])
    ]


class NewAssessmentIn(DraftIn):
    framework: FrameworkId
    control_id: Annotated[str, Field(min_length=1, max_length=20)]
    # The framework version; the current one when omitted.
    version: Annotated[str | None, Field(min_length=1, max_length=40)] = None
    # An approved assessment of the same control this one supersedes.
    supersedes_id: int | None = None


class CitationIn(BaseModel):
    kind: CitationKind
    # The evidence item, observation, change or change set; none for the automated status.
    target_id: Annotated[int | None, Field(ge=1)] = None
    rationale: Annotated[str, Field(min_length=1, max_length=MAX_CITATION_RATIONALE)]

    @model_validator(mode="after")
    def _target(self) -> "CitationIn":
        if (self.kind == "control_status") != (self.target_id is None):
            raise ValueError("target_id names the record, except for control_status")
        return self


class DecisionIn(BaseModel):
    note: Annotated[str | None, Field(max_length=2000)] = None


class CitationOut(BaseModel):
    id: int
    kind: CitationKind
    target_id: int | None
    title: str
    snapshot: dict[str, Any] | None
    digest: str | None
    rationale: str
    cited_at: datetime


class AssessmentOut(BaseModel):
    id: int
    control: ControlLink
    period_start: date
    period_end: date
    conclusion: Conclusion
    narrative: str
    state: State
    supersedes_id: int | None
    superseded_by_id: int | None
    assessor: UserRef
    created_at: datetime
    submitted_at: datetime | None
    reviewed_by: UserRef | None
    reviewed_at: datetime | None
    review_note: str | None
    development_review: bool
    citations: list[CitationOut]


class AssessmentsPage(BaseModel):
    items: list[AssessmentOut]


def _actor(principal: Principal) -> Actor:
    return Actor(principal.user_id, principal.actor)


def _refused(error: EvidenceRefused) -> HTTPException:
    code = status.HTTP_422_UNPROCESSABLE_CONTENT if error.invalid else status.HTTP_409_CONFLICT
    return HTTPException(code, detail={"code": error.code, "message": str(error)})


def _forbidden(error: NotAllowed) -> HTTPException:
    return HTTPException(
        status.HTTP_403_FORBIDDEN, detail={"code": "not_allowed", "message": str(error)}
    )


def _citation_out(row: AssessmentCitation, shown: dict[str, Any]) -> CitationOut:
    return CitationOut(
        id=row.id,
        kind=row.kind,  # type: ignore[arg-type]
        target_id=row.evidence_item_id
        or row.observation_id
        or row.change_record_id
        or row.change_set_id,
        title=shown.get("title", ""),
        snapshot=row.snapshot,
        digest=row.digest,
        rationale=row.rationale,
        cited_at=row.cited_at,
    )


def outs(db: Session, rows: Sequence[ControlAssessment]) -> list[AssessmentOut]:
    ids = [row.id for row in rows]
    cited = service.citations(db, ids)
    shown = service.cited_records(db, [c for group in cited.values() for c in group])
    newer = service.superseded_by(db, ids)
    controls = service.labels(db, [row.framework_control_id for row in rows])
    people = {row.assessor_user_id for row in rows} | {
        row.reviewed_by_user_id for row in rows if row.reviewed_by_user_id is not None
    }
    names = dict(
        db.execute(select(AppUser.id, AppUser.display_name).where(AppUser.id.in_(people))).all()
    )

    def ref(user_id: int) -> UserRef:
        return UserRef(user_id=user_id, display_name=names.get(user_id))

    out: list[AssessmentOut] = []
    for row in rows:
        label = controls[row.framework_control_id]
        out.append(
            AssessmentOut(
                id=row.id,
                control=ControlLink(
                    framework=label.framework,
                    version=label.version,
                    control_id=label.control_id,
                    title=label.title,
                ),
                period_start=row.period_start,
                period_end=row.period_end,
                conclusion=row.conclusion,  # type: ignore[arg-type]
                narrative=row.narrative,
                state=row.state,  # type: ignore[arg-type]
                supersedes_id=row.supersedes_id,
                superseded_by_id=newer.get(row.id),
                assessor=ref(row.assessor_user_id),
                created_at=row.created_at,
                submitted_at=row.submitted_at,
                reviewed_by=ref(row.reviewed_by_user_id)
                if row.reviewed_by_user_id is not None
                else None,
                reviewed_at=row.reviewed_at,
                review_note=row.review_note,
                development_review=row.development_review,
                citations=[_citation_out(c, shown.get(c.id, {})) for c in cited[row.id]],
            )
        )
    return out


def _out(db: Session, row: ControlAssessment) -> AssessmentOut:
    db.refresh(row)
    [out] = outs(db, [row])
    return out


def _found(db: Session, assessment_id: int) -> ControlAssessment:
    row = db.get(ControlAssessment, assessment_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such assessment")
    return row


def _draft(body: DraftIn) -> service.Draft:
    return service.Draft(body.period_start, body.period_end, body.conclusion, body.narrative)


@router.get(
    "/assessments",
    response_model=AssessmentsPage,
    summary="Control assessments",
    description="Newest first (at most 500), with their citations. Filter by `state` and by "
    "`control` (`framework:control_id`, any version).",
)
def list_assessments(
    _: Annotated[Principal, Depends(require(Permission.EVIDENCE_READ))],
    db: Annotated[Session, Depends(database)],
    state: State | None = None,
    control: Annotated[str | None, Query(max_length=60)] = None,
) -> AssessmentsPage:
    ref = None
    if control is not None:
        match = CONTROL.match(control)
        if match is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "control is framework:id")
        ref = ControlRef(match["framework"], match["control"])
    return AssessmentsPage(items=outs(db, service.assessments(db, state=state, control=ref)))


@router.get(
    "/assessments/{assessment_id}",
    response_model=AssessmentOut,
    summary="One control assessment",
)
def read_assessment(
    assessment_id: AssessmentId,
    _: Annotated[Principal, Depends(require(Permission.EVIDENCE_READ))],
    db: Annotated[Session, Depends(database)],
) -> AssessmentOut:
    return _out(db, _found(db, assessment_id))


@router.post(
    "/assessments",
    response_model=AssessmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Start an assessment of a control as a draft",
    description="The control in the current framework version unless `version` is given, the "
    "period, the conclusion and a narrative of what was tested and how. `supersedes_id` names "
    "an approved assessment of the same control this one replaces. Requires the `X-CSRF-Token` "
    "header. Audited.",
)
def add_assessment(
    body: NewAssessmentIn,
    principal: Annotated[Principal, Depends(require(Permission.ASSESSMENTS_WRITE))],
    db: Annotated[Session, Depends(database)],
) -> AssessmentOut:
    try:
        row = service.create(
            db,
            ControlRef(body.framework, body.control_id, body.version),
            _draft(body),
            _actor(principal),
            body.supersedes_id,
        )
    except EvidenceRefused as error:
        raise _refused(error) from None
    db.commit()
    return _out(db, row)


@router.put(
    "/assessments/{assessment_id}",
    response_model=AssessmentOut,
    summary="Edit a draft assessment",
    description="Only its assessor, and only while it is a draft. Requires the `X-CSRF-Token` "
    "header. Audited.",
)
def edit_assessment(
    assessment_id: AssessmentId,
    body: DraftIn,
    principal: Annotated[Principal, Depends(require(Permission.ASSESSMENTS_WRITE))],
    db: Annotated[Session, Depends(database)],
) -> AssessmentOut:
    row = _found(db, assessment_id)
    try:
        service.update(db, row, _draft(body), _actor(principal))
    except EvidenceRefused as error:
        raise _refused(error) from None
    except NotAllowed as error:
        raise _forbidden(error) from None
    db.commit()
    return _out(db, row)


@router.delete(
    "/assessments/{assessment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a draft assessment",
    description="Only its assessor, and only while it is a draft. Requires the `X-CSRF-Token` "
    "header. Audited.",
)
def delete_assessment(
    assessment_id: AssessmentId,
    principal: Annotated[Principal, Depends(require(Permission.ASSESSMENTS_WRITE))],
    db: Annotated[Session, Depends(database)],
) -> Response:
    row = _found(db, assessment_id)
    try:
        service.delete(db, row, _actor(principal))
    except EvidenceRefused as error:
        raise _refused(error) from None
    except NotAllowed as error:
        raise _forbidden(error) from None
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/assessments/{assessment_id}/citations",
    response_model=AssessmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Cite evidence in a draft assessment",
    description="An accepted `evidence_item`, an `observation`, a `change_record`, a "
    "`change_set` (each by `target_id`), or the control's automated status now "
    "(`control_status`), with a rationale. The digest of what was cited is recorded. Only the "
    "assessor, only while it is a draft. Requires the `X-CSRF-Token` header. Audited.",
)
def cite(
    assessment_id: AssessmentId,
    body: CitationIn,
    principal: Annotated[Principal, Depends(require(Permission.ASSESSMENTS_WRITE))],
    db: Annotated[Session, Depends(database)],
) -> AssessmentOut:
    row = _found(db, assessment_id)
    try:
        service.cite(
            db,
            row,
            body.kind,
            body.target_id,
            body.rationale,
            _actor(principal),
            reviewer=Permission.EVIDENCE_REVIEW in principal.permissions,
        )
    except EvidenceRefused as error:
        raise _refused(error) from None
    except NotAllowed as error:
        raise _forbidden(error) from None
    db.commit()
    return _out(db, row)


@router.delete(
    "/assessments/{assessment_id}/citations/{citation_id}",
    response_model=AssessmentOut,
    summary="Remove a citation from a draft assessment",
    description="Only the assessor, only while it is a draft. Requires the `X-CSRF-Token` "
    "header. Audited.",
)
def uncite(
    assessment_id: AssessmentId,
    citation_id: Annotated[int, Path(ge=1)],
    principal: Annotated[Principal, Depends(require(Permission.ASSESSMENTS_WRITE))],
    db: Annotated[Session, Depends(database)],
) -> AssessmentOut:
    row = _found(db, assessment_id)
    try:
        service.uncite(db, row, citation_id, _actor(principal))
    except EvidenceRefused as error:
        raise _refused(error) from None
    except NotAllowed as error:
        raise _forbidden(error) from None
    db.commit()
    return _out(db, row)


@router.post(
    "/assessments/{assessment_id}/submit",
    response_model=AssessmentOut,
    summary="Submit a draft assessment for review",
    description="Only the assessor; it needs at least one citation. Requires the "
    "`X-CSRF-Token` header. Audited.",
)
def submit(
    assessment_id: AssessmentId,
    principal: Annotated[Principal, Depends(require(Permission.ASSESSMENTS_WRITE))],
    db: Annotated[Session, Depends(database)],
) -> AssessmentOut:
    row = _found(db, assessment_id)
    try:
        service.submit(db, row, _actor(principal))
    except EvidenceRefused as error:
        raise _refused(error) from None
    except NotAllowed as error:
        raise _forbidden(error) from None
    db.commit()
    return _out(db, row)


def _decide(
    assessment_id: int, approve: bool, body: DecisionIn, principal: Principal, db: Session
) -> AssessmentOut:
    row = _found(db, assessment_id)
    try:
        service.decide(db, row, approve, body.note, _actor(principal))
    except EvidenceRefused as error:
        raise _refused(error) from None
    except NotAllowed as error:
        raise _forbidden(error) from None
    db.commit()
    return _out(db, row)


@router.post(
    "/assessments/{assessment_id}/approve",
    response_model=AssessmentOut,
    summary="Approve a submitted assessment",
    description="Never one's own, outside development mode. Requires the `X-CSRF-Token` "
    "header. Audited.",
)
def approve(
    assessment_id: AssessmentId,
    body: DecisionIn,
    principal: Annotated[Principal, Depends(require(Permission.ASSESSMENTS_REVIEW))],
    db: Annotated[Session, Depends(database)],
) -> AssessmentOut:
    return _decide(assessment_id, True, body, principal, db)


@router.post(
    "/assessments/{assessment_id}/reject",
    response_model=AssessmentOut,
    summary="Reject a submitted assessment",
    description="With a note saying why; never one's own, outside development mode. Requires "
    "the `X-CSRF-Token` header. Audited.",
)
def reject(
    assessment_id: AssessmentId,
    body: DecisionIn,
    principal: Annotated[Principal, Depends(require(Permission.ASSESSMENTS_REVIEW))],
    db: Annotated[Session, Depends(database)],
) -> AssessmentOut:
    return _decide(assessment_id, False, body, principal, db)
