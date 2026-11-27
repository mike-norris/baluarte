"""Evidence gaps per control and period (roadmap M15.4,
docs/EVIDENCE_MANAGEMENT.md#evidence-gaps-m154)."""

from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.compliance.catalogue import FrameworkId
from baluarte.evidence import gaps as service
from baluarte.evidence.items import EvidenceRefused

router = APIRouter(tags=["evidence"])

Gap = Literal["missing", "stale", "inaccessible", "incomplete"]


class ControlGapsOut(BaseModel):
    control_id: str
    title: str
    area: str
    # The automated status at the end of the period; null when the control is not catalogued.
    status: str | None
    gaps: list[Gap]
    # Why each gap applies, in the order of ``gaps``.
    reasons: list[str]
    accepted_items: int
    fresh_items: int
    collected_results: int
    # The latest approved assessment in force for the period, if any.
    assessment_id: int | None
    conclusion: str | None


class GapReportOut(BaseModel):
    framework: str
    version: str
    period_start: date
    period_end: date
    evaluation_run_id: int | None
    collected_at: datetime | None
    # Controls with each gap.
    summary: dict[Gap, int]
    controls: list[ControlGapsOut]


@router.get(
    "/evidence-gaps",
    response_model=GapReportOut,
    summary="Evidence gaps per control for a framework version and period",
    description="Each control of the framework version (the current one unless `version` is "
    "given) with its gaps for the period: `missing`, `stale`, `inaccessible` and `incomplete`. "
    "Computed from the records as they stand; nothing is stored. A period is at most three "
    "years.",
    responses={422: {"description": "A bad period or an unknown version"}},
)
def read_gaps(
    _: Annotated[Principal, Depends(require(Permission.EVIDENCE_READ))],
    db: Annotated[Session, Depends(database)],
    framework: FrameworkId,
    period_start: date,
    period_end: date,
    version: Annotated[str | None, Query(min_length=1, max_length=40)] = None,
) -> GapReportOut:
    try:
        found = service.report(db, framework, version, period_start, period_end)
    except EvidenceRefused as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": error.code, "message": str(error)},
        ) from None
    return GapReportOut(
        framework=found.framework,
        version=found.version,
        period_start=found.period_start,
        period_end=found.period_end,
        evaluation_run_id=found.evaluation_run_id,
        collected_at=found.collected_at,
        summary=service.summary(found.controls),  # type: ignore[arg-type]
        controls=[
            ControlGapsOut(
                control_id=c.control_id,
                title=c.title,
                area=c.area,
                status=c.status,
                gaps=c.gaps,
                reasons=c.reasons,
                accepted_items=c.accepted_items,
                fresh_items=c.fresh_items,
                collected_results=c.collected_results,
                assessment_id=c.assessment_id,
                conclusion=c.conclusion,
            )
            for c in found.controls
        ],
    )
