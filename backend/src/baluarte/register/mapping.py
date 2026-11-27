"""Issue-to-control mappings: which auditable issue supports which control, and with what.

A mapping names one framework control and one auditable issue, with an overall rationale, and
cites items of the issue's captured evidence (M12.2), each with its own rationale: a comment, a
history event, a merge request with its approvals and pipelines, a pipeline, a release, a
linked issue, an attachment or the description. It is pinned to the capture it cites, so a
later capture never silently changes what it rests on; a newer capture is shown as a gap to
review.

Review follows the ``governance`` setting exactly like control mapping changes
(``baluarte.controls.changes``): while it is required a mapping is ``proposed`` until someone
with ``policy:write`` other than the proposer approves it; otherwise it is ``active`` at once and
recorded ``self_approved``. A mapping is never edited: a revision supersedes it and the old one
is withdrawn when the revision takes effect. A trigger enforces the lifecycle again.

Audit periods are named date ranges. Each cited item is dated from the capture, so evidence
inside a period is kept apart from later remediation. Closing an issue is never taken as proof
that a control works: a mapping that cites only the closure says so.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal

from sqlalchemy import and_, func, not_, or_, select, tuple_
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from baluarte import audit
from baluarte.controls import versions
from baluarte.controls.catalogue import MAPPING_VERSION, control
from baluarte.db.models import (
    AuditPeriod,
    IssueControlEvidence,
    IssueControlMapping,
    IssueEvidence,
    Observation,
    RegisterIssue,
)
from baluarte.integrations.gitlab.issue_evidence import SOURCE
from baluarte.policy.changes import Actor, ChangeConflict, NotAllowed, review_required
from baluarte.register.selection import active_rules, excluded, included, selected
from baluarte.settings import own_decision_refused
from baluarte.workspaces import current_workspace_id

__all__ = ["Actor", "ChangeConflict", "NotAllowed"]

ItemKind = Literal[
    "description",
    "note",
    "event",
    "link",
    "merge_request",
    "pipeline",
    "release",
    "attachment",
]
Timing = Literal["before", "in_period", "after", "undated"]
OPEN_STATES = ("proposed", "active")
MAX_ITEMS = 50

Reason = Literal[
    "control_unknown",
    "issue_not_auditable",
    "not_captured",
    "item_unknown",
    "item_repeated",
    "no_items",
    "too_many_items",
    "duplicate",
    "supersedes_not_active",
    "supersedes_other_pair",
    "supersede_pending",
]

MESSAGES: dict[Reason, str] = {
    "control_unknown": "The control is not in the catalogue.",
    "issue_not_auditable": "The issue is not in the register, or it is excluded.",
    "not_captured": "The issue's evidence has not been captured yet.",
    "item_unknown": "An evidence item is not in the issue's latest capture.",
    "item_repeated": "An evidence item is cited twice.",
    "no_items": "Cite at least one evidence item.",
    "too_many_items": f"Cite at most {MAX_ITEMS} evidence items.",
    "duplicate": "The issue is already mapped to the control, or a mapping is waiting for "
    "review; revise it instead.",
    "supersedes_not_active": "The mapping to revise is not in force.",
    "supersedes_other_pair": "The mapping to revise is for another issue or control.",
    "supersede_pending": "A revision of it is already waiting for review.",
}


class MappingConflict(ChangeConflict):
    """The mapping cannot be made now, for every reason in ``reasons``."""

    def __init__(self, reasons: list[Reason]) -> None:
        super().__init__(" ".join(MESSAGES[reason] for reason in reasons))
        self.reasons = reasons


@dataclass(frozen=True)
class EvidenceItem:
    """One citable item of a capture, with the time it happened where the capture says."""

    key: str
    kind: ItemKind
    label: str
    at: datetime | None


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _short(text: Any, limit: int = 80) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _event_label(event: Mapping[str, Any]) -> str:
    who = event.get("user") or "someone"
    if event.get("kind") == "state":
        return f"{who} {event.get('state') or 'changed'} the issue"
    if event.get("kind") == "label":
        verb = "removed" if event.get("action") == "remove" else "added"
        return f"{who} {verb} label {event.get('label') or ''}".rstrip()
    verb = "removed" if event.get("action") == "remove" else "set"
    return f"{who} {verb} milestone {event.get('milestone') or ''}".rstrip()


def items(payload: Mapping[str, Any]) -> dict[str, EvidenceItem]:
    """Every citable item of a capture, by key. Keys are stable across captures of the issue."""
    found: dict[str, EvidenceItem] = {}

    def add(key: str, kind: ItemKind, label: str, at: Any) -> None:
        found.setdefault(key, EvidenceItem(key, kind, label, parse_time(at)))

    issue = payload.get("issue") or {}
    # GitHub captures (roadmap M13.5) name pull requests and workflow runs as GitHub does.
    github = payload.get("provider") == "github"
    change, mark, run = (
        ("Pull request", "#", "Workflow run") if github else ("Merge request", "!", "Pipeline")
    )
    notes = [n for n in payload.get("notes") or [] if isinstance(n, dict)]
    note_times = {f"note:{n.get('id')}": n.get("created_at") for n in notes}
    if issue.get("description"):
        add("description", "description", "The description", issue.get("created_at"))
    for note in notes:
        if note.get("id") is None:
            continue
        kind = "System note" if note.get("system") else "Comment"
        label = f"{kind} by {note.get('author') or 'unknown'}: {_short(note.get('body'))}"
        add(f"note:{note['id']}", "note", label, note.get("created_at"))
    for event in payload.get("history") or []:
        if isinstance(event, dict) and event.get("id") is not None:
            key = f"event:{event.get('kind')}:{event['id']}"
            add(key, "event", _event_label(event), event.get("created_at"))
    for link in payload.get("links") or []:
        if isinstance(link, dict) and link.get("iid") is not None:
            key = f"link:{link.get('project_id')}:{link['iid']}"
            label = f"Linked issue #{link['iid']}: {_short(link.get('title'))}"
            add(key, "link", label, None)
    for mr in payload.get("merge_requests") or []:
        if not isinstance(mr, dict) or mr.get("iid") is None:
            continue
        approvers = ", ".join((mr.get("approvals") or {}).get("approved_by") or []) or "nobody"
        label = f"{change} {mark}{mr['iid']} ({mr.get('state')}, approved by {approvers})"
        at = mr.get("merged_at") or mr.get("created_at")
        add(f"merge_request:{mr.get('project_id')}:{mr['iid']}", "merge_request", label, at)
        for pipeline in mr.get("pipelines") or []:
            if isinstance(pipeline, dict) and pipeline.get("id") is not None:
                label = f"{run} {pipeline['id']} of {mark}{mr['iid']}: {pipeline.get('status')}"
                add(f"pipeline:{pipeline['id']}", "pipeline", label, pipeline.get("created_at"))
    for release in payload.get("releases") or []:
        if isinstance(release, dict) and release.get("tag_name"):
            at = release.get("released_at") or release.get("created_at")
            add(f"release:{release['tag_name']}", "release", f"Release {release['tag_name']}", at)
    for attachment in payload.get("attachments") or []:
        if not isinstance(attachment, dict) or not attachment.get("sha256"):
            continue
        # A file is dated by the earliest text that references it.
        times = [
            parse_time(issue.get("created_at") if where == "description" else note_times.get(where))
            for where in attachment.get("referenced_in") or []
        ]
        dated = [t for t in times if t is not None]
        label = f"Attachment {attachment.get('filename')}"
        found.setdefault(
            f"attachment:{attachment['sha256']}",
            EvidenceItem(
                f"attachment:{attachment['sha256']}",
                "attachment",
                label,
                min(dated) if dated else None,
            ),
        )
    return found


def auditable(db: Session) -> ColumnElement[bool]:
    """Whether a ``register_issue`` row is auditable in the current workspace, in SQL."""
    workspace_id = current_workspace_id(db)
    rules = active_rules(db, workspace_id)
    return and_(or_(selected(rules), included(workspace_id)), not_(excluded(workspace_id)))


def auditable_pairs(db: Session, pairs: Iterable[tuple[int, int]]) -> set[tuple[int, int]]:
    """Which of the (project, issue) pairs are auditable in the current workspace."""
    wanted = list(set(pairs))
    if not wanted:
        return set()
    rows = db.execute(
        select(RegisterIssue.project_id, RegisterIssue.issue_iid).where(
            tuple_(RegisterIssue.project_id, RegisterIssue.issue_iid).in_(wanted),
            auditable(db),
        )
    )
    return {(p, i) for p, i in rows}


def closure_only(cited: Iterable[tuple[str, str]]) -> bool:
    """Whether every cited item is the issue being closed: never proof that a control works."""
    keys = [(key, label) for key, label in cited]
    return bool(keys) and all(
        key.startswith("event:state:") and label.endswith(" closed the issue")
        for key, label in keys
    )


@dataclass(frozen=True)
class Period:
    starts_on: date
    ends_on: date

    @property
    def start(self) -> datetime:
        return datetime.combine(self.starts_on, time.min, tzinfo=UTC)

    @property
    def end(self) -> datetime:
        """The first moment after the period (it includes all of ``ends_on``)."""
        return datetime.combine(self.ends_on + timedelta(days=1), time.min, tzinfo=UTC)


def timing(at: datetime | None, period: Period | None) -> Timing | None:
    if period is None:
        return None
    if at is None:
        return "undated"
    if at < period.start:
        return "before"
    if at >= period.end:
        return "after"
    return "in_period"


# Gaps on one mapping, as codes the client words.
Gap = Literal[
    "awaiting_review",
    "not_auditable",
    "capture_partial",
    "capture_failed",
    "recaptured",
    "closure_only",
    "nothing_in_period",
    "only_later_remediation",
    "undated_items",
]


GAP_MESSAGES: dict[Gap, str] = {
    "awaiting_review": "The mapping is waiting for a second person's review.",
    "not_auditable": "The issue is no longer in the register, or it is excluded.",
    "capture_partial": "Some of the issue's evidence could not be captured.",
    "capture_failed": "GitLab no longer serves the issue.",
    "recaptured": "The issue's evidence was captured again since the mapping; review whether "
    "it still holds.",
    "closure_only": "The mapping cites only the issue being closed, which is never proof that "
    "a control works.",
    "nothing_in_period": "None of the cited evidence is dated inside the audit period.",
    "only_later_remediation": "All the cited evidence is dated after the audit period: it is "
    "later remediation, not evidence for the period.",
    "undated_items": "Some cited evidence has no date, so it cannot be placed in the period.",
}


@dataclass
class MappingView:
    mapping: IssueControlMapping
    evidence: list[IssueControlEvidence]
    gaps: list[Gap] = field(default_factory=list)
    timings: dict[str, Timing | None] = field(default_factory=dict)


def _capture_partial(payload: Mapping[str, Any]) -> bool:
    sources = payload.get("sources") or {}
    return any(state not in ("ok", "not_applicable") for state in sources.values())


def assess(
    db: Session, mappings: Sequence[IssueControlMapping], period: Period | None
) -> list[MappingView]:
    """Each mapping with its cited evidence, how it falls against ``period``, and its gaps."""
    if not mappings:
        return []
    ids = [m.id for m in mappings]
    cited: dict[int, list[IssueControlEvidence]] = {i: [] for i in ids}
    for row in db.scalars(
        select(IssueControlEvidence)
        .where(IssueControlEvidence.mapping_id.in_(ids))
        .order_by(IssueControlEvidence.mapping_id, IssueControlEvidence.position)
    ):
        cited[row.mapping_id].append(row)
    pairs = {(m.project_id, m.issue_iid) for m in mappings}
    latest = {
        (row.project_id, row.issue_iid): row
        for row in db.scalars(
            select(IssueEvidence).where(
                tuple_(IssueEvidence.project_id, IssueEvidence.issue_iid).in_(list(pairs))
            )
        )
    }
    pinned = {
        o.id: o
        for o in db.scalars(
            select(Observation).where(Observation.id.in_({m.observation_id for m in mappings}))
        )
    }
    in_register = auditable_pairs(db, pairs)
    views = []
    for mapping in mappings:
        view = MappingView(mapping, cited[mapping.id])
        key = (mapping.project_id, mapping.issue_iid)
        if mapping.state == "proposed":
            view.gaps.append("awaiting_review")
        if key not in in_register:
            view.gaps.append("not_auditable")
        observation = pinned.get(mapping.observation_id)
        current = latest.get(key)
        if current is not None and current.status == "failed":
            view.gaps.append("capture_failed")
        elif observation is not None and _capture_partial(observation.payload or {}):
            view.gaps.append("capture_partial")
        if current is not None and current.observation_id != mapping.observation_id:
            view.gaps.append("recaptured")
        if closure_only((row.item_key, row.item_label) for row in view.evidence):
            view.gaps.append("closure_only")
        if period is not None:
            view.timings = {row.item_key: timing(row.occurred_at, period) for row in view.evidence}
            found = set(view.timings.values())
            if "in_period" not in found:
                view.gaps.append(
                    "only_later_remediation" if found == {"after"} else "nothing_in_period"
                )
            if "undated" in found:
                view.gaps.append("undated_items")
        views.append(view)
    return views


@dataclass(frozen=True)
class CitedItem:
    key: str
    rationale: str


@dataclass(frozen=True)
class Proposal:
    project_id: int
    issue_iid: int
    framework: str
    control_id: str
    rationale: str
    items: tuple[CitedItem, ...]
    supersedes_id: int | None = None


def _lock(db: Session, proposal: Proposal) -> None:
    key = (
        f"issue_control:{proposal.project_id}:{proposal.issue_iid}:"
        f"{proposal.framework}:{proposal.control_id}"
    )
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext(key))))


def _open(db: Session, proposal: Proposal, exclude: int | None) -> list[IssueControlMapping]:
    statement = select(IssueControlMapping).where(
        IssueControlMapping.project_id == proposal.project_id,
        IssueControlMapping.issue_iid == proposal.issue_iid,
        IssueControlMapping.framework == proposal.framework,
        IssueControlMapping.control_id == proposal.control_id,
        IssueControlMapping.state.in_(OPEN_STATES),
    )
    if exclude is not None:
        statement = statement.where(IssueControlMapping.id != exclude)
    return list(db.scalars(statement))


def conflicts(
    db: Session,
    proposal: Proposal,
    capture: Mapping[str, EvidenceItem] | None,
    exclude: int | None = None,
) -> list[Reason]:
    """Every reason ``proposal`` cannot be made now; empty when it can.

    ``capture`` is the citable items of the capture the mapping rests on (None: not captured).
    ``exclude`` is the mapping itself when it is being approved.
    """
    if control(proposal.framework, proposal.control_id) is None:
        return ["control_unknown"]
    reasons: list[Reason] = []
    if not auditable_pairs(db, [(proposal.project_id, proposal.issue_iid)]):
        reasons.append("issue_not_auditable")
    keys = [item.key for item in proposal.items]
    if not keys:
        reasons.append("no_items")
    if len(keys) > MAX_ITEMS:
        reasons.append("too_many_items")
    if len(set(keys)) != len(keys):
        reasons.append("item_repeated")
    if capture is None:
        reasons.append("not_captured")
    elif any(key not in capture for key in keys):
        reasons.append("item_unknown")
    others = _open(db, proposal, exclude)
    if proposal.supersedes_id is None:
        if any(o.supersedes_id is None or o.state == "active" for o in others):
            reasons.append("duplicate")
    else:
        superseded = db.get(IssueControlMapping, proposal.supersedes_id)
        if superseded is None or superseded.state != "active":
            reasons.append("supersedes_not_active")
        elif (
            superseded.project_id,
            superseded.issue_iid,
            superseded.framework,
            superseded.control_id,
        ) != (proposal.project_id, proposal.issue_iid, proposal.framework, proposal.control_id):
            reasons.append("supersedes_other_pair")
        if any(o.supersedes_id == proposal.supersedes_id for o in others):
            reasons.append("supersede_pending")
    return reasons


def latest_capture(db: Session, project_id: int, iid: int) -> Observation | None:
    row = db.get(IssueEvidence, (project_id, iid))
    if row is None or row.status == "failed":
        return None
    observation = db.get(Observation, row.observation_id)
    return observation if observation is not None and observation.source == SOURCE else None


def details(mapping: IssueControlMapping, cited: Sequence[IssueControlEvidence]) -> dict[str, Any]:
    return {
        "project_id": mapping.project_id,
        "issue_iid": mapping.issue_iid,
        "framework": mapping.framework,
        "control_id": mapping.control_id,
        "rationale": mapping.rationale,
        "observation_id": mapping.observation_id,
        "items": [{"key": row.item_key, "rationale": row.rationale} for row in cited],
        "supersedes_id": mapping.supersedes_id,
        "mapping_version": mapping.mapping_version,
        **({"development_decision": True} if mapping.development_decision else {}),
    }


def _cited(db: Session, mapping_id: int) -> list[IssueControlEvidence]:
    return list(
        db.scalars(
            select(IssueControlEvidence)
            .where(IssueControlEvidence.mapping_id == mapping_id)
            .order_by(IssueControlEvidence.position)
        )
    )


def _withdraw(db: Session, mapping: IssueControlMapping, actor: str, reason: str) -> None:
    mapping.state = "withdrawn"
    mapping.withdrawn_by = actor
    mapping.withdrawn_at = func.now()
    mapping.withdraw_reason = reason
    db.flush()
    audit.record(
        db,
        actor=actor,
        action="register.mapping_withdrawn",
        resource_type="issue_control_mapping",
        resource_id=str(mapping.id),
        details={**details(mapping, _cited(db, mapping.id)), "reason": reason},
    )


def _take_effect(db: Session, mapping: IssueControlMapping, actor: str) -> None:
    if mapping.supersedes_id is None:
        return
    superseded = db.get(IssueControlMapping, mapping.supersedes_id, with_for_update=True)
    if superseded is None or superseded.state != "active":
        raise MappingConflict(["supersedes_not_active"])
    _withdraw(db, superseded, actor, f"Revised by mapping {mapping.id}.")


def propose(db: Session, proposal: Proposal, by: Actor) -> IssueControlMapping:
    """Make the mapping, or propose it while review is required. The caller commits.

    It rests on the issue's latest capture. Raises MappingConflict with every reason it cannot
    be made.
    """
    _lock(db, proposal)
    observation = latest_capture(db, proposal.project_id, proposal.issue_iid)
    capture = items(observation.payload or {}) if observation is not None else None
    reasons = conflicts(db, proposal, capture)
    if reasons:
        raise MappingConflict(reasons)
    if observation is None or capture is None:  # conflicts reported not_captured
        raise MappingConflict(["not_captured"])
    required = review_required(db)
    mapping = IssueControlMapping(
        workspace_id=current_workspace_id(db),
        project_id=proposal.project_id,
        issue_iid=proposal.issue_iid,
        framework=proposal.framework,
        control_id=proposal.control_id,
        rationale=proposal.rationale,
        observation_id=observation.id,
        supersedes_id=proposal.supersedes_id,
        state="proposed" if required else "active",
        review_required=required,
        self_approved=not required,
        mapping_version=MAPPING_VERSION,
        framework_version_id=versions.current_version_id(db, proposal.framework),
        proposed_by=by.actor,
        proposed_by_user_id=by.user_id,
    )
    if not required:
        mapping.decided_by = by.actor
        mapping.decided_by_user_id = by.user_id
        mapping.decided_at = func.now()
    db.add(mapping)
    db.flush()
    cited = [
        IssueControlEvidence(
            mapping_id=mapping.id,
            position=position,
            item_key=item.key,
            item_kind=capture[item.key].kind,
            item_label=capture[item.key].label,
            occurred_at=capture[item.key].at,
            rationale=item.rationale,
        )
        for position, item in enumerate(proposal.items)
    ]
    db.add_all(cited)
    db.flush()
    audit.record(
        db,
        actor=by.actor,
        action="register.mapping_proposed",
        resource_type="issue_control_mapping",
        resource_id=str(mapping.id),
        details={**details(mapping, cited), "state": mapping.state, "self_approved": not required},
    )
    if not required:
        _take_effect(db, mapping, by.actor)
    return mapping


def _decidable(db: Session, mapping_id: int, by: Actor) -> IssueControlMapping:
    mapping = db.get(IssueControlMapping, mapping_id, with_for_update=True)
    if mapping is None:
        raise LookupError(mapping_id)
    if mapping.state != "proposed":
        raise ChangeConflict(f"The mapping is already {mapping.state}")
    if own_decision_refused(mapping.proposed_by_user_id, by.user_id):
        raise NotAllowed(
            "A mapping must be approved or rejected by someone other than its proposer"
        )
    return mapping


def _proposal(mapping: IssueControlMapping, cited: Sequence[IssueControlEvidence]) -> Proposal:
    return Proposal(
        project_id=mapping.project_id,
        issue_iid=mapping.issue_iid,
        framework=mapping.framework,
        control_id=mapping.control_id,
        rationale=mapping.rationale,
        items=tuple(CitedItem(row.item_key, row.rationale) for row in cited),
        supersedes_id=mapping.supersedes_id,
    )


def _decide(
    db: Session, mapping: IssueControlMapping, by: Actor, state: str, note: str | None
) -> None:
    mapping.state = state
    mapping.decided_by = by.actor
    mapping.decided_by_user_id = by.user_id
    mapping.development_decision = mapping.decided_by_user_id == mapping.proposed_by_user_id
    mapping.decided_at = func.now()
    mapping.decision_note = note or None
    db.flush()


def approve(db: Session, mapping_id: int, by: Actor, note: str | None) -> IssueControlMapping:
    """Put a proposed mapping in force. The caller commits.

    Raises LookupError, NotAllowed (own proposal), ChangeConflict (already decided) or
    MappingConflict (it can no longer be made, for example the issue was excluded).
    """
    mapping = _decidable(db, mapping_id, by)
    cited = _cited(db, mapping.id)
    proposal = _proposal(mapping, cited)
    _lock(db, proposal)
    pinned = db.get(Observation, mapping.observation_id)
    capture = items(pinned.payload or {}) if pinned is not None else None
    reasons = conflicts(db, proposal, capture, exclude=mapping.id)
    if reasons:
        raise MappingConflict(reasons)
    _decide(db, mapping, by, "active", note)
    audit.record(
        db,
        actor=by.actor,
        action="register.mapping_approved",
        resource_type="issue_control_mapping",
        resource_id=str(mapping.id),
        details={**details(mapping, cited), "proposed_by": mapping.proposed_by, "note": note},
    )
    _take_effect(db, mapping, by.actor)
    return mapping


def reject(db: Session, mapping_id: int, by: Actor, note: str) -> IssueControlMapping:
    """Reject a proposed mapping. The caller commits."""
    mapping = _decidable(db, mapping_id, by)
    _decide(db, mapping, by, "rejected", note)
    audit.record(
        db,
        actor=by.actor,
        action="register.mapping_rejected",
        resource_type="issue_control_mapping",
        resource_id=str(mapping.id),
        details={
            **details(mapping, _cited(db, mapping.id)),
            "proposed_by": mapping.proposed_by,
            "note": note,
        },
    )
    return mapping


def withdraw(db: Session, mapping_id: int, by: Actor, reason: str) -> IssueControlMapping:
    """End a mapping in force, with a reason. The caller commits.

    Raises LookupError, or ChangeConflict when it is not in force.
    """
    mapping = db.get(IssueControlMapping, mapping_id, with_for_update=True)
    if mapping is None:
        raise LookupError(mapping_id)
    if mapping.state != "active":
        raise ChangeConflict(f"The mapping is {mapping.state}, not in force")
    _withdraw(db, mapping, by.actor, reason)
    return mapping


def periods(db: Session) -> list[AuditPeriod]:
    return list(
        db.scalars(
            select(AuditPeriod).order_by(AuditPeriod.starts_on.desc(), AuditPeriod.id.desc())
        )
    )
