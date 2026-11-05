"""Projects, groups and personal namespaces taken out of monitoring, and brought back.

An organization excludes projects it does not want judged (R&D, personal experiments) with a
justification. Excluded projects stay in the inventory, so it still shows everything discovery
found, but they are not evaluated, not read in detail and not counted.

A project is excluded while an ``active`` exclusion names it, one of its namespace's groups
(a group exclusion covers its subgroups and their projects), or, for a project in a personal
namespace, ``personal_namespaces``. :func:`coverage` answers that for the whole instance at once,
and names the covering exclusion: the project's own, else its nearest group's, else the
personal-namespace one.

Review follows the ``governance`` setting exactly like baseline and control-mapping changes:
while it is required a batch is ``proposed`` until someone with ``policy:write`` other than the
proposer approves or rejects it; otherwise it is ``active`` at once and recorded
``self_approved``. Ending an exclusion widens monitoring again, so it needs a justification but
no second person. A database trigger enforces the lifecycle again.
"""

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import ColumnElement, and_, false, func, or_, select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.db.models import GitLabGroup, GitLabProject, ScopeExclusion
from baluarte.policy.changes import Actor, ChangeConflict, NotAllowed, review_required

TargetType = Literal["project", "group", "personal_namespaces"]
ExclusionState = Literal["proposed", "active", "rejected", "ended"]
OPEN_STATES = ("proposed", "active")
MAX_TARGETS = 500

Reason = Literal[
    "no_targets",
    "too_many_targets",
    "duplicate_target",
    "unknown_target",
    "already_excluded",
    "not_active",
    "exclusion_unknown",
]

MESSAGES: dict[Reason, str] = {
    "no_targets": "Choose at least one project or group, or personal namespaces.",
    "too_many_targets": f"A batch holds at most {MAX_TARGETS} targets.",
    "duplicate_target": "A target is listed more than once.",
    "unknown_target": "Not in the inventory:",
    "already_excluded": "Already excluded, or waiting for review:",
    "not_active": "Not in force, so it cannot be ended:",
    "exclusion_unknown": "No such exclusion:",
}

__all__ = ["Actor", "ChangeConflict", "NotAllowed", "ScopeConflict"]


@dataclass(frozen=True)
class ReasonDetail:
    code: Reason
    message: str


class ScopeConflict(ChangeConflict):
    """The exclusions cannot be made or ended as asked."""

    def __init__(self, reasons: list[ReasonDetail]) -> None:
        super().__init__(" ".join(reason.message for reason in reasons))
        self.reasons = reasons


def _reason(code: Reason, items: Sequence[str] = ()) -> ReasonDetail:
    if not items:
        return ReasonDetail(code, MESSAGES[code])
    shown = ", ".join(items[:10])
    more = f" and {len(items) - 10} more" if len(items) > 10 else ""
    return ReasonDetail(code, f"{MESSAGES[code]} {shown}{more}.")


@dataclass(frozen=True)
class Target:
    type: Literal["project", "group"]
    gitlab_id: int


@dataclass(frozen=True)
class Covering:
    """The active exclusion that takes a project or group out of monitoring."""

    exclusion_id: int
    via: TargetType
    # The excluded project or group's path as proposed; None for personal namespaces.
    path: str | None


@dataclass
class Coverage:
    """Every active exclusion of one instance, resolved against the group tree."""

    projects: dict[int, Covering] = field(default_factory=dict)
    # Every excluded group and subgroup (GitLab ID) and the group exclusion that covers it.
    groups: dict[int, Covering] = field(default_factory=dict)
    personal: Covering | None = None

    def for_group(self, gitlab_id: int) -> Covering | None:
        return self.groups.get(gitlab_id)

    def for_project(
        self, gitlab_id: int, namespace_gitlab_id: int, namespace_kind: str | None
    ) -> Covering | None:
        direct = self.projects.get(gitlab_id)
        if direct is not None:
            return direct
        if namespace_kind == "user":
            return self.personal
        return self.groups.get(namespace_gitlab_id)

    def of(self, project: GitLabProject) -> Covering | None:
        return self.for_project(
            project.gitlab_id, project.namespace_gitlab_id, project.namespace_kind
        )

    @property
    def empty(self) -> bool:
        return not self.projects and not self.groups and self.personal is None

    def project_excluded(self, model: Any = GitLabProject) -> ColumnElement[bool]:
        """SQL: the project (``model``, GitLabProject or an alias) is excluded."""
        conditions: list[ColumnElement[bool]] = []
        if self.projects:
            conditions.append(model.gitlab_id.in_(sorted(self.projects)))
        if self.groups:
            conditions.append(
                and_(
                    model.namespace_kind.is_distinct_from("user"),
                    model.namespace_gitlab_id.in_(sorted(self.groups)),
                )
            )
        if self.personal is not None:
            conditions.append(model.namespace_kind == "user")
        return or_(*conditions) if conditions else false()

    def project_included(self, model: Any = GitLabProject) -> ColumnElement[bool]:
        """SQL: the project (``model``) is monitored."""
        if self.empty:
            return ~false()
        return ~self.project_excluded(model)

    def group_excluded(self) -> ColumnElement[bool]:
        if not self.groups:
            return false()
        return GitLabGroup.gitlab_id.in_(sorted(self.groups))


def _covering(row: ScopeExclusion) -> Covering:
    return Covering(
        exclusion_id=row.id,
        via=row.target_type,  # type: ignore[arg-type]
        path=row.target_path,
    )


def coverage(db: Session, instance_id: int | None, as_of: datetime | None = None) -> Coverage:
    """The active exclusions of ``instance_id``, resolved for lookups and SQL filters.

    With ``as_of``, the exclusions in force then: approved (or applied) by then and not yet
    ended. Groups are resolved against today's group tree, which keeps no history.
    """
    result = Coverage()
    if instance_id is None:
        return result
    statement = select(ScopeExclusion).where(ScopeExclusion.instance_id == instance_id)
    if as_of is None:
        statement = statement.where(ScopeExclusion.state == "active")
    else:
        statement = statement.where(
            ScopeExclusion.state.in_(("active", "ended")),
            ScopeExclusion.decided_at <= as_of,
            or_(ScopeExclusion.ended_at.is_(None), ScopeExclusion.ended_at > as_of),
        )
    rows = db.scalars(statement.order_by(ScopeExclusion.id)).all()
    excluded_groups: dict[int, Covering] = {}
    for row in rows:
        if row.target_type == "project" and row.target_id is not None:
            result.projects.setdefault(row.target_id, _covering(row))
        elif row.target_type == "group" and row.target_id is not None:
            excluded_groups.setdefault(row.target_id, _covering(row))
        elif row.target_type == "personal_namespaces" and result.personal is None:
            result.personal = _covering(row)
    if excluded_groups:
        parents: dict[int, int | None] = {
            gitlab_id: parent
            for gitlab_id, parent in db.execute(
                select(GitLabGroup.gitlab_id, GitLabGroup.parent_gitlab_id).where(
                    GitLabGroup.instance_id == instance_id
                )
            )
        }
        for group_id in set(parents) | set(excluded_groups):
            # The nearest excluded group on the way up, guarding against a cycle in stale data.
            current: int | None = group_id
            seen: set[int] = set()
            while current is not None and current not in seen:
                seen.add(current)
                if current in excluded_groups:
                    result.groups[group_id] = excluded_groups[current]
                    break
                current = parents.get(current)
    return result


def exclusion_for(db: Session, project: GitLabProject) -> Covering | None:
    """The active exclusion that covers ``project``, or None when it is monitored."""
    return coverage(db, project.instance_id).of(project)


def excluded_projects(db: Session, instance_id: int | None) -> dict[int, Covering]:
    """Every excluded inventory project of the instance, by row ID, with what covers it."""
    found = coverage(db, instance_id)
    if found.empty or instance_id is None:
        return {}
    rows = db.execute(
        select(
            GitLabProject.id,
            GitLabProject.gitlab_id,
            GitLabProject.namespace_gitlab_id,
            GitLabProject.namespace_kind,
        ).where(GitLabProject.instance_id == instance_id, found.project_excluded())
    ).all()
    result: dict[int, Covering] = {}
    for row_id, gitlab_id, namespace_id, kind in rows:
        covering = found.for_project(gitlab_id, namespace_id, kind)
        if covering is not None:
            result[row_id] = covering
    return result


# Changes ------------------------------------------------------------------------------------


def justification_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _target_detail(row: ScopeExclusion) -> dict[str, Any]:
    return {
        "id": row.id,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "target_path": row.target_path,
    }


def _details(rows: Sequence[ScopeExclusion], **extra: Any) -> dict[str, Any]:
    first = rows[0]
    return {
        "batch_id": first.batch_id,
        "targets": [_target_detail(row) for row in rows],
        "justification_sha256": justification_sha256(first.justification),
        **extra,
    }


def _lock(db: Session, instance_id: int) -> None:
    key = f"scope_exclusion:{instance_id}"
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext(key))))


def _label(target: Target) -> str:
    return f"{target.type} {target.gitlab_id}"


def propose(
    db: Session,
    instance_id: int,
    targets: Sequence[Target],
    personal_namespaces: bool,
    justification: str,
    by: Actor,
) -> list[ScopeExclusion]:
    """Exclude the targets, or propose it while review is required. The caller commits.

    Raises ScopeConflict with every reason the batch cannot be made.
    """
    count = len(targets) + (1 if personal_namespaces else 0)
    if count == 0:
        raise ScopeConflict([_reason("no_targets")])
    if count > MAX_TARGETS:
        raise ScopeConflict([_reason("too_many_targets")])
    if len(set(targets)) != len(targets):
        raise ScopeConflict([_reason("duplicate_target")])
    _lock(db, instance_id)
    project_ids = [target.gitlab_id for target in targets if target.type == "project"]
    group_ids = [target.gitlab_id for target in targets if target.type == "group"]
    paths: dict[tuple[str, int], str] = {}
    if project_ids:
        for gitlab_id, path in db.execute(
            select(GitLabProject.gitlab_id, GitLabProject.path_with_namespace).where(
                GitLabProject.instance_id == instance_id, GitLabProject.gitlab_id.in_(project_ids)
            )
        ):
            paths[("project", gitlab_id)] = path
    if group_ids:
        for gitlab_id, path in db.execute(
            select(GitLabGroup.gitlab_id, GitLabGroup.full_path).where(
                GitLabGroup.instance_id == instance_id, GitLabGroup.gitlab_id.in_(group_ids)
            )
        ):
            paths[("group", gitlab_id)] = path
    reasons: list[ReasonDetail] = []
    unknown = [_label(t) for t in targets if (t.type, t.gitlab_id) not in paths]
    if unknown:
        reasons.append(_reason("unknown_target", unknown))
    open_rows = db.execute(
        select(ScopeExclusion.target_type, ScopeExclusion.target_id).where(
            ScopeExclusion.instance_id == instance_id, ScopeExclusion.state.in_(OPEN_STATES)
        )
    ).all()
    taken = {(kind, target_id) for kind, target_id in open_rows}
    already = [_label(t) for t in targets if (t.type, t.gitlab_id) in taken]
    if personal_namespaces and ("personal_namespaces", None) in taken:
        already.append("personal namespaces")
    if already:
        reasons.append(_reason("already_excluded", already))
    if reasons:
        raise ScopeConflict(reasons)

    required = review_required(db)
    batch_id = str(uuid.uuid4())
    entries: list[tuple[str, int | None, str | None]] = [
        (t.type, t.gitlab_id, paths[(t.type, t.gitlab_id)]) for t in targets
    ]
    if personal_namespaces:
        entries.append(("personal_namespaces", None, None))
    rows: list[ScopeExclusion] = []
    for target_type, target_id, target_path in entries:
        row = ScopeExclusion(
            instance_id=instance_id,
            target_type=target_type,
            target_id=target_id,
            target_path=target_path,
            batch_id=batch_id,
            justification=justification,
            state="proposed" if required else "active",
            review_required=required,
            self_approved=not required,
            proposed_by=by.actor,
            proposed_by_user_id=by.user_id,
        )
        if not required:
            row.decided_by = by.actor
            row.decided_by_user_id = by.user_id
            row.decided_at = func.now()
        db.add(row)
        rows.append(row)
    db.flush()
    for row in rows:
        db.refresh(row)
    audit.record(
        db,
        actor=by.actor,
        action="scope.exclusion_proposed",
        resource_type="scope_exclusion_batch",
        resource_id=batch_id,
        details=_details(rows, state=rows[0].state, self_approved=not required),
    )
    return rows


def batch(db: Session, batch_id: str, lock: bool = False) -> list[ScopeExclusion]:
    statement = (
        select(ScopeExclusion)
        .where(ScopeExclusion.batch_id == batch_id)
        .order_by(ScopeExclusion.id)
    )
    if lock:
        statement = statement.with_for_update()
    return list(db.scalars(statement))


def _decidable(db: Session, batch_id: str, by: Actor) -> list[ScopeExclusion]:
    rows = batch(db, batch_id, lock=True)
    if not rows:
        raise LookupError(batch_id)
    states = {row.state for row in rows}
    if states != {"proposed"}:
        raise ChangeConflict(f"The batch is already {', '.join(sorted(states))}")
    if rows[0].proposed_by_user_id == by.user_id:
        raise NotAllowed("A batch must be approved or rejected by someone other than its proposer")
    return rows


def _decide(
    db: Session, batch_id: str, by: Actor, state: ExclusionState, note: str | None
) -> list[ScopeExclusion]:
    rows = _decidable(db, batch_id, by)
    for row in rows:
        row.state = state
        row.decided_by = by.actor
        row.decided_by_user_id = by.user_id
        row.decided_at = func.now()
        row.decision_note = note or None
    db.flush()
    for row in rows:
        db.refresh(row)
    audit.record(
        db,
        actor=by.actor,
        action="scope.exclusion_approved" if state == "active" else "scope.exclusion_rejected",
        resource_type="scope_exclusion_batch",
        resource_id=batch_id,
        details=_details(rows, proposed_by=rows[0].proposed_by, note=note or None),
    )
    return rows


def approve(db: Session, batch_id: str, by: Actor, note: str | None) -> list[ScopeExclusion]:
    """Put a proposed batch in force. The caller commits.

    Raises LookupError, NotAllowed (own batch) or ChangeConflict (already decided).
    """
    return _decide(db, batch_id, by, "active", note)


def reject(db: Session, batch_id: str, by: Actor, note: str) -> list[ScopeExclusion]:
    """Reject a proposed batch; ``note`` says why. The caller commits."""
    return _decide(db, batch_id, by, "rejected", note)


def end(
    db: Session, instance_id: int, ids: Sequence[int], justification: str, by: Actor
) -> list[ScopeExclusion]:
    """End active exclusions, so their projects are monitored again. The caller commits.

    Needs no second person: it widens monitoring. Raises ScopeConflict.
    """
    if not ids:
        raise ScopeConflict([_reason("no_targets")])
    if len(ids) > MAX_TARGETS:
        raise ScopeConflict([_reason("too_many_targets")])
    if len(set(ids)) != len(ids):
        raise ScopeConflict([_reason("duplicate_target")])
    _lock(db, instance_id)
    rows = list(
        db.scalars(
            select(ScopeExclusion)
            .where(ScopeExclusion.instance_id == instance_id, ScopeExclusion.id.in_(ids))
            .order_by(ScopeExclusion.id)
            .with_for_update()
        )
    )
    found = {row.id for row in rows}
    reasons: list[ReasonDetail] = []
    unknown = [str(row_id) for row_id in ids if row_id not in found]
    if unknown:
        reasons.append(_reason("exclusion_unknown", unknown))
    inactive = [f"{row.id} ({row.state})" for row in rows if row.state != "active"]
    if inactive:
        reasons.append(_reason("not_active", inactive))
    if reasons:
        raise ScopeConflict(reasons)
    for row in rows:
        row.state = "ended"
        row.ended_by = by.actor
        row.ended_by_user_id = by.user_id
        row.ended_at = func.now()
        row.end_justification = justification
    db.flush()
    for row in rows:
        db.refresh(row)
    audit.record(
        db,
        actor=by.actor,
        action="scope.exclusion_ended",
        resource_type="scope_exclusion",
        resource_id=",".join(str(row.id) for row in rows),
        details={
            "batch_ids": sorted({row.batch_id for row in rows}),
            "targets": [_target_detail(row) for row in rows],
            "justification_sha256": justification_sha256(justification),
        },
    )
    return rows
