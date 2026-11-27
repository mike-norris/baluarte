"""Control assessments (roadmap M15.3, docs/EVIDENCE_MANAGEMENT.md#control-assessments-m153).

A person's conclusion about a control in a framework version for a period, kept apart from the
automated status, which it may cite and may disagree with.

- **Lifecycle.** ``draft`` (editable and deletable by its assessor) -> ``submitted`` ->
  ``approved`` or ``rejected`` by someone with ``assessments:review`` other than the assessor;
  development mode lets the assessor review their own, recorded as ``development_review``. Only
  an approved assessment counts, and it never changes; a later one of the same control
  ``supersedes`` it, and both stay. Triggers enforce the same (migration 0052).
- **Citations.** Each cites one piece of evidence with a rationale, and is added or removed only
  while the assessment is a draft: an accepted evidence item, a collected observation, a change
  on a default branch, a template change set, or the control's automated status as it stands
  (a snapshot). ``digest`` fixes what was cited so an export can show it is unchanged.
- **Submitting** needs at least one citation.
"""

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte.audit import record
from baluarte.controls import versions
from baluarte.controls.catalogue import control as catalogue_control
from baluarte.controls.posture import posture
from baluarte.db.models import (
    ASSESSMENT_CONCLUSIONS,
    ASSESSMENT_NARRATIVE,
    CITATION_KINDS,
    MAX_CITATION_RATIONALE,
    AssessmentCitation,
    ChangeRecord,
    ControlAssessment,
    EvidenceItem,
    FrameworkControl,
    FrameworkVersion,
    Observation,
    TemplateChangeSet,
)
from baluarte.evidence.items import Actor, ControlRef, EvidenceRefused, NotAllowed, visible
from baluarte.settings import development_mode
from baluarte.workspaces import current_connection_id, current_workspace_id

__all__ = [
    "Actor",
    "ControlRef",
    "EvidenceRefused",
    "NotAllowed",
]


@dataclass(frozen=True)
class Draft:
    period_start: date
    period_end: date
    conclusion: str
    narrative: str


@dataclass(frozen=True)
class ControlLabel:
    framework: str
    version: str
    control_id: str
    title: str


def _audit(
    db: Session, by: Actor, action: str, assessment: ControlAssessment, **details: object
) -> None:
    record(
        db,
        actor=by.actor,
        action=f"control_assessment.{action}",
        resource_type="control_assessment",
        resource_id=str(assessment.id),
        details={
            "framework_control_id": assessment.framework_control_id,
            "conclusion": assessment.conclusion,
            "state": assessment.state,
            **details,
        },
    )


# --- reading ---------------------------------------------------------------------------------


def labels(db: Session, framework_control_ids: Iterable[int]) -> dict[int, ControlLabel]:
    ids = list(set(framework_control_ids))
    if not ids:
        return {}
    rows = db.execute(
        select(
            FrameworkControl.id,
            FrameworkVersion.framework,
            FrameworkVersion.version,
            FrameworkControl.control_id,
            FrameworkControl.title,
        )
        .join(FrameworkVersion, FrameworkVersion.id == FrameworkControl.framework_version_id)
        .where(FrameworkControl.id.in_(ids))
    ).all()
    return {row[0]: ControlLabel(row[1], row[2], row[3], row[4]) for row in rows}


def _framework_control_ids(db: Session, ref: ControlRef) -> list[int]:
    """The rows for a control in every version of its framework, or the one version named."""
    return list(
        db.scalars(
            select(FrameworkControl.id)
            .join(FrameworkVersion, FrameworkVersion.id == FrameworkControl.framework_version_id)
            .where(
                FrameworkVersion.framework == ref.framework,
                FrameworkControl.control_id == ref.control_id,
                *([FrameworkVersion.version == ref.version] if ref.version is not None else []),
            )
        )
    )


def assessments(
    db: Session, *, state: str | None = None, control: ControlRef | None = None
) -> list[ControlAssessment]:
    query = select(ControlAssessment).order_by(ControlAssessment.id.desc()).limit(500)
    if state is not None:
        query = query.where(ControlAssessment.state == state)
    if control is not None:
        query = query.where(
            ControlAssessment.framework_control_id.in_(_framework_control_ids(db, control))
        )
    return list(db.scalars(query))


def citations(db: Session, assessment_ids: Iterable[int]) -> dict[int, list[AssessmentCitation]]:
    ids = list(assessment_ids)
    out: dict[int, list[AssessmentCitation]] = {i: [] for i in ids}
    if ids:
        for row in db.scalars(
            select(AssessmentCitation)
            .where(AssessmentCitation.assessment_id.in_(ids))
            .order_by(AssessmentCitation.id)
        ):
            out[row.assessment_id].append(row)
    return out


def superseded_by(db: Session, assessment_ids: Iterable[int]) -> dict[int, int]:
    """The approved assessment that supersedes each, where one does."""
    ids = list(assessment_ids)
    if not ids:
        return {}
    rows = db.execute(
        select(ControlAssessment.supersedes_id, ControlAssessment.id).where(
            ControlAssessment.supersedes_id.in_(ids), ControlAssessment.state == "approved"
        )
    ).all()
    return {old: new for old, new in rows if old is not None}


def latest_approved(db: Session, ref: ControlRef) -> ControlAssessment | None:
    """The latest approved assessment of a control (any version, unless one is named) that no
    approved assessment supersedes, by the end of its period and then by ID."""
    found = db.scalars(
        select(ControlAssessment)
        .where(
            ControlAssessment.framework_control_id.in_(_framework_control_ids(db, ref)),
            ControlAssessment.state == "approved",
        )
        .order_by(ControlAssessment.period_end.desc(), ControlAssessment.id.desc())
    ).all()
    replaced = superseded_by(db, [a.id for a in found])
    return next((a for a in found if a.id not in replaced), None)


# --- writing ---------------------------------------------------------------------------------


def _clean(draft: Draft) -> Draft:
    if draft.conclusion not in ASSESSMENT_CONCLUSIONS:
        raise EvidenceRefused("conclusion", "Choose a conclusion", invalid=True)
    narrative = draft.narrative.strip()
    low, high = ASSESSMENT_NARRATIVE
    if not low <= len(narrative) <= high:
        raise EvidenceRefused(
            "narrative",
            f"Say what was tested and how, in {low} to {high} characters",
            invalid=True,
        )
    if draft.period_end < draft.period_start:
        raise EvidenceRefused("period", "The period ends before it starts", invalid=True)
    return Draft(draft.period_start, draft.period_end, draft.conclusion, narrative)


def _control_id(db: Session, ref: ControlRef) -> int:
    owner = (
        versions.version(db, ref.framework, ref.version)
        if ref.version is not None
        else versions.current(db, ref.framework)
    )
    row = versions.control_row(db, owner.id, ref.control_id) if owner is not None else None
    if row is None:
        raise EvidenceRefused(
            "unknown_control",
            f"{ref.framework} {ref.control_id} is not in that framework version",
            invalid=True,
        )
    return row.id


def create(
    db: Session, ref: ControlRef, draft: Draft, by: Actor, supersedes_id: int | None = None
) -> ControlAssessment:
    cleaned = _clean(draft)
    framework_control_id = _control_id(db, ref)
    if supersedes_id is not None:
        earlier = db.get(ControlAssessment, supersedes_id)
        if earlier is None or earlier.state != "approved":
            raise EvidenceRefused("supersedes", "Only an approved assessment can be superseded")
        if earlier.framework_control_id != framework_control_id:
            raise EvidenceRefused(
                "supersedes", "A later assessment supersedes one of the same control and version"
            )
        if supersedes_id in superseded_by(db, [supersedes_id]):
            raise EvidenceRefused("supersedes", "That assessment is already superseded")
    assessment = ControlAssessment(
        workspace_id=current_workspace_id(db),
        framework_control_id=framework_control_id,
        period_start=cleaned.period_start,
        period_end=cleaned.period_end,
        conclusion=cleaned.conclusion,
        narrative=cleaned.narrative,
        supersedes_id=supersedes_id,
        assessor=by.actor,
        assessor_user_id=by.user_id,
    )
    db.add(assessment)
    db.flush()
    _audit(db, by, "created", assessment, supersedes_id=supersedes_id)
    return assessment


def _own_draft(assessment: ControlAssessment, by: Actor) -> None:
    if assessment.assessor_user_id != by.user_id:
        raise NotAllowed("Only the assessor changes their draft")
    if assessment.state != "draft":
        raise EvidenceRefused("not_draft", "Only a draft assessment can change")


def update(db: Session, assessment: ControlAssessment, draft: Draft, by: Actor) -> None:
    _own_draft(assessment, by)
    cleaned = _clean(draft)
    assessment.period_start = cleaned.period_start
    assessment.period_end = cleaned.period_end
    assessment.conclusion = cleaned.conclusion
    assessment.narrative = cleaned.narrative
    db.flush()
    _audit(db, by, "edited", assessment)


def delete(db: Session, assessment: ControlAssessment, by: Actor) -> None:
    _own_draft(assessment, by)
    _audit(db, by, "deleted", assessment)
    db.delete(assessment)
    db.flush()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def status_snapshot(db: Session, framework_control_id: int) -> dict[str, Any]:
    """The control's automated status now, from the latest evaluation run of the connection in
    use, as the citation records it."""
    label = labels(db, [framework_control_id])[framework_control_id]
    item = catalogue_control(label.framework, label.control_id)
    if item is None:
        raise EvidenceRefused(
            "unknown_control", "That control has no automated status", invalid=True
        )
    found = posture(db, current_connection_id(db), controls=[item])
    [state] = found.controls
    return {
        "framework": label.framework,
        "control_id": label.control_id,
        "status": state.status,
        "evaluation_run_id": found.evaluation_run_id,
        "counts": {
            "pass": state.counts.passed,
            "fail": state.counts.fail,
            "unknown": state.counts.unknown,
            "excepted": state.counts.excepted,
            "projects": len(state.counts.projects),
        },
        "taken_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def cite(
    db: Session,
    assessment: ControlAssessment,
    kind: str,
    target_id: int | None,
    rationale: str,
    by: Actor,
    *,
    reviewer: bool = False,
) -> AssessmentCitation:
    """Cite one piece of evidence. ``target_id`` names the evidence item, observation, change or
    change set; the automated status takes none. ``reviewer`` says whether the caller may read
    restricted evidence items."""
    _own_draft(assessment, by)
    why = rationale.strip()
    if not 1 <= len(why) <= MAX_CITATION_RATIONALE:
        raise EvidenceRefused(
            "rationale",
            f"Say why this evidence supports the conclusion, in up to {MAX_CITATION_RATIONALE} "
            "characters",
            invalid=True,
        )
    if kind not in CITATION_KINDS:
        raise EvidenceRefused("kind", "Choose what to cite", invalid=True)
    citation = AssessmentCitation(assessment_id=assessment.id, kind=kind, rationale=why)
    if kind == "control_status":
        snapshot = status_snapshot(db, assessment.framework_control_id)
        citation.snapshot = snapshot
        citation.digest = _digest(snapshot)
    elif target_id is None:
        raise EvidenceRefused("target", "Say which record to cite", invalid=True)
    elif kind == "evidence_item":
        item = db.get(EvidenceItem, target_id)
        if item is None or not visible(item, by.user_id, reviewer):
            raise EvidenceRefused("target", "No such evidence item", invalid=True)
        if item.state != "accepted":
            raise EvidenceRefused("not_accepted", "Only an accepted evidence item can be cited")
        citation.evidence_item_id = item.id
        citation.digest = item.file_sha256 or item.removed_sha256 or item.check_sha256
    elif kind == "observation":
        observation = db.get(Observation, target_id)
        if observation is None:
            raise EvidenceRefused("target", "No such observation", invalid=True)
        citation.observation_id = observation.id
        citation.digest = observation.payload_digest
    elif kind == "change_record":
        change = db.get(ChangeRecord, target_id)
        if change is None:
            raise EvidenceRefused("target", "No such change", invalid=True)
        citation.change_record_id = change.id
        citation.digest = db.scalar(
            select(Observation.payload_digest).where(Observation.id == change.observation_id)
        )
    else:
        change_set = db.get(TemplateChangeSet, target_id)
        if change_set is None:
            raise EvidenceRefused("target", "No such change set", invalid=True)
        citation.change_set_id = change_set.id
        citation.digest = change_set.digest
    db.add(citation)
    db.flush()
    _audit(db, by, "cited", assessment, citation_kind=kind, citation_id=citation.id)
    return citation


def uncite(db: Session, assessment: ControlAssessment, citation_id: int, by: Actor) -> None:
    _own_draft(assessment, by)
    citation = db.get(AssessmentCitation, citation_id)
    if citation is None or citation.assessment_id != assessment.id:
        raise EvidenceRefused("target", "No such citation", invalid=True)
    db.delete(citation)
    db.flush()
    _audit(db, by, "uncited", assessment, citation_kind=citation.kind, citation_id=citation_id)


def submit(db: Session, assessment: ControlAssessment, by: Actor) -> None:
    _own_draft(assessment, by)
    if not citations(db, [assessment.id])[assessment.id]:
        raise EvidenceRefused("no_evidence", "Cite at least one piece of evidence")
    assessment.state = "submitted"
    assessment.submitted_at = func.now()
    db.flush()
    _audit(db, by, "submitted", assessment)


def decide(
    db: Session, assessment: ControlAssessment, approve: bool, note: str | None, by: Actor
) -> None:
    if assessment.state != "submitted":
        raise EvidenceRefused("not_submitted", "Only a submitted assessment can be reviewed")
    own = assessment.assessor_user_id == by.user_id
    if own and not development_mode():
        raise NotAllowed("Someone other than the assessor reviews an assessment")
    cleaned = (note or "").strip() or None
    if not approve and cleaned is None:
        raise EvidenceRefused("note", "Say why the assessment is rejected", invalid=True)
    if approve and assessment.supersedes_id is not None:
        if assessment.supersedes_id in superseded_by(db, [assessment.supersedes_id]):
            raise EvidenceRefused(
                "supersedes", "The assessment it supersedes was superseded meanwhile"
            )
    assessment.state = "approved" if approve else "rejected"
    assessment.reviewed_by = by.actor
    assessment.reviewed_by_user_id = by.user_id
    assessment.reviewed_at = func.now()
    assessment.review_note = cleaned
    assessment.development_review = own
    db.flush()
    _audit(db, by, "approved" if approve else "rejected", assessment, development_review=own)


def cited_records(db: Session, rows: Sequence[AssessmentCitation]) -> dict[int, dict[str, Any]]:
    """What each citation points at, for display: a title and, for an evidence item, its state."""
    out: dict[int, dict[str, Any]] = {}
    item_ids = [r.evidence_item_id for r in rows if r.evidence_item_id is not None]
    titles = (
        dict(
            db.execute(
                select(EvidenceItem.id, EvidenceItem.title).where(EvidenceItem.id.in_(item_ids))
            ).all()
        )
        if item_ids
        else {}
    )
    for row in rows:
        if row.evidence_item_id is not None:
            out[row.id] = {"title": titles.get(row.evidence_item_id, "")}
        elif row.snapshot is not None:
            out[row.id] = {"title": f"Automated status: {row.snapshot.get('status')}"}
        else:
            target = row.observation_id or row.change_record_id or row.change_set_id
            out[row.id] = {"title": f"{row.kind.replace('_', ' ')} {target}"}
    return out
