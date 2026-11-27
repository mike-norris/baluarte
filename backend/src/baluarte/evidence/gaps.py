"""Evidence gaps (roadmap M15.4, docs/EVIDENCE_MANAGEMENT.md#evidence-gaps-m154).

For a framework version and a period, each control in it is reported with the gaps in its
evidence. Nothing is stored; the report is computed from the records as they stand.

- ``missing``: no accepted evidence item for the period and no collected evidence: the
  evaluation run in force at the end of the period has no result for the control.
- ``stale``: every accepted item for the period stopped being fresh (``valid_until``) before the
  period ended, or the collected evidence is older than its freshness window (two collection
  intervals, or a day when collection is not scheduled) at the end of the period, or now if
  that is earlier.
- ``inaccessible``: an assessment in force for the period cites a link whose last check failed,
  or a file retention removed.
- ``incomplete``: no approved assessment covers the period, or one in force cites nothing.

An item or assessment is "for the period" when its own period overlaps it; an item without a
period counts for any. An assessment is in force unless rejected or superseded by an approved
one. Controls the organization decided are not applicable are listed without gaps.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.orm import Session

from baluarte.controls import versions
from baluarte.controls.catalogue import control as catalogue_control
from baluarte.controls.posture import ControlStatus, posture
from baluarte.db.models import (
    ControlAssessment,
    EvaluationRun,
    EvidenceItem,
    EvidenceItemControl,
    FrameworkControl,
    FrameworkVersion,
    GitLabInstance,
)
from baluarte.evidence import assessments
from baluarte.evidence.items import EvidenceRefused
from baluarte.workspaces import current_connection_id

Gap = Literal["missing", "stale", "inaccessible", "incomplete"]
GAPS: tuple[Gap, ...] = ("missing", "stale", "inaccessible", "incomplete")
UNSCHEDULED_WINDOW = timedelta(hours=24)
MAX_PERIOD_DAYS = 3 * 366


@dataclass
class ControlGaps:
    control_id: str
    title: str
    area: str
    status: ControlStatus | None
    gaps: list[Gap] = field(default_factory=list)
    accepted_items: int = 0
    fresh_items: int = 0
    collected_results: int = 0
    assessment_id: int | None = None
    conclusion: str | None = None
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GapReport:
    framework: str
    version: str
    period_start: date
    period_end: date
    evaluation_run_id: int | None
    collected_at: datetime | None
    controls: list[ControlGaps]


def _overlaps(start: Any, end: Any, lo: date, hi: date) -> ColumnElement[bool]:
    """Whether a period (either end open when null) overlaps ``lo`` to ``hi``."""
    return and_(or_(start.is_(None), start <= hi), or_(end.is_(None), end >= lo))


def _window(db: Session, instance_id: int | None) -> timedelta:
    minutes = (
        db.scalar(
            select(GitLabInstance.sync_interval_minutes).where(GitLabInstance.id == instance_id)
        )
        if instance_id is not None
        else None
    )
    return timedelta(minutes=2 * minutes) if minutes else UNSCHEDULED_WINDOW


def report(
    db: Session, framework: str, version: str | None, period_start: date, period_end: date
) -> GapReport:
    if period_end < period_start:
        raise EvidenceRefused("period", "The period ends before it starts", invalid=True)
    if (period_end - period_start).days > MAX_PERIOD_DAYS:
        raise EvidenceRefused("period", "A period is at most three years", invalid=True)
    owner: FrameworkVersion | None = (
        versions.version(db, framework, version)
        if version is not None
        else versions.current(db, framework)
    )
    if owner is None:
        raise EvidenceRefused("version", "No such framework version", invalid=True)
    rows = versions.controls(db, owner.id)
    ids = [row.id for row in rows]

    # Collected evidence: the evaluation run in force at the end of the period.
    end_of_period = datetime.combine(period_end, time.max, tzinfo=UTC)
    reference = min(end_of_period, datetime.now(UTC))
    instance_id = current_connection_id(db)
    catalogued = [c for c in (catalogue_control(framework, r.control_id) for r in rows) if c]
    found = posture(db, instance_id, controls=catalogued, as_of=end_of_period)
    by_id = {state.control.id: state for state in found.controls}
    run = db.get(EvaluationRun, found.evaluation_run_id) if found.evaluation_run_id else None
    collected_at = run.finished_at if run is not None else None
    collected_stale = collected_at is None or reference - collected_at > _window(db, instance_id)

    # Accepted, unsuperseded items for the period, per control row (by control ID, any version).
    same_control = (
        select(FrameworkControl.id, FrameworkControl.control_id)
        .join(FrameworkVersion, FrameworkVersion.id == FrameworkControl.framework_version_id)
        .where(FrameworkVersion.framework == framework)
        .subquery()
    )
    superseding = select(EvidenceItem.supersedes_id).where(
        EvidenceItem.supersedes_id.is_not(None), EvidenceItem.state == "accepted"
    )
    item_rows = db.execute(
        select(same_control.c.control_id, EvidenceItem.id, EvidenceItem.valid_until)
        .join(EvidenceItemControl, EvidenceItemControl.evidence_item_id == EvidenceItem.id)
        .join(same_control, same_control.c.id == EvidenceItemControl.framework_control_id)
        .where(
            EvidenceItem.state == "accepted",
            EvidenceItem.id.not_in(superseding),
            _overlaps(EvidenceItem.period_start, EvidenceItem.period_end, period_start, period_end),
        )
    ).all()
    items: dict[str, dict[int, date | None]] = {}
    for control_id, item_id, valid_until in item_rows:
        items.setdefault(control_id, {})[item_id] = valid_until

    # Assessments in force for the period, of this version's controls.
    found_assessments = list(
        db.scalars(
            select(ControlAssessment)
            .where(
                ControlAssessment.framework_control_id.in_(ids),
                ControlAssessment.state != "rejected",
                _overlaps(
                    ControlAssessment.period_start,
                    ControlAssessment.period_end,
                    period_start,
                    period_end,
                ),
            )
            .order_by(ControlAssessment.period_end.desc(), ControlAssessment.id.desc())
        )
    )
    replaced = assessments.superseded_by(db, [a.id for a in found_assessments])
    in_force = [a for a in found_assessments if a.id not in replaced]
    cited = assessments.citations(db, [a.id for a in in_force])
    cited_item_ids = {
        c.evidence_item_id for group in cited.values() for c in group if c.evidence_item_id
    }
    cited_items = {
        item.id: item
        for item in (
            db.scalars(select(EvidenceItem).where(EvidenceItem.id.in_(cited_item_ids)))
            if cited_item_ids
            else []
        )
    }
    per_control: dict[int, list[ControlAssessment]] = {}
    for a in in_force:
        per_control.setdefault(a.framework_control_id, []).append(a)

    out: list[ControlGaps] = []
    for row in rows:
        state = by_id.get(row.control_id)
        status = state.status if state is not None else None
        entry = ControlGaps(
            control_id=row.control_id, title=row.title, area=row.area, status=status
        )
        out.append(entry)
        if status == "not_applicable":
            continue
        held = items.get(row.control_id, {})
        entry.accepted_items = len(held)
        entry.fresh_items = sum(1 for v in held.values() if v is None or v >= period_end)
        entry.collected_results = state.counts.results if state is not None else 0
        if not held and entry.collected_results == 0:
            entry.gaps.append("missing")
            entry.reasons.append("No accepted evidence item and no collected result")
        stale_items = bool(held) and entry.fresh_items == 0
        stale_collected = entry.collected_results > 0 and collected_stale
        if stale_items or stale_collected:
            entry.gaps.append("stale")
            if stale_items:
                entry.reasons.append("Every accepted item stopped being fresh before the end")
            if stale_collected:
                entry.reasons.append("Collected evidence is older than its freshness window")
        mine = per_control.get(row.id, [])
        broken = [
            item
            for a in mine
            for c in cited[a.id]
            if c.evidence_item_id is not None
            for item in [cited_items.get(c.evidence_item_id)]
            if item is not None
            and (item.file_removed_at is not None or item.check_status == "unreachable")
        ]
        if broken:
            entry.gaps.append("inaccessible")
            entry.reasons.append("A cited link failed its last check or a cited file was removed")
        approved = [a for a in mine if a.state == "approved"]
        if approved:
            entry.assessment_id = approved[0].id
            entry.conclusion = approved[0].conclusion
        if not approved or any(not cited[a.id] for a in mine):
            entry.gaps.append("incomplete")
            entry.reasons.append(
                "No approved assessment covers the period"
                if not approved
                else "An assessment in force cites nothing"
            )
    return GapReport(
        framework=framework,
        version=owner.version,
        period_start=period_start,
        period_end=period_end,
        evaluation_run_id=found.evaluation_run_id,
        collected_at=collected_at,
        controls=out,
    )


def summary(controls: Sequence[ControlGaps]) -> dict[str, int]:
    return {gap: sum(1 for c in controls if gap in c.gaps) for gap in GAPS}
