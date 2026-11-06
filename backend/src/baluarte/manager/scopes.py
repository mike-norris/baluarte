"""Manager scopes: the GitLab groups an administrator assigns to a manager (docs/MANAGER_SPACE.md).

A manager sees time per person only for projects in their assigned groups, subgroups included.
Everything else in the manager space is read instance-wide, like other reads. Administrators
see everyone's time. Assignments are append-only: ending one keeps it as history, and a
database trigger refuses any other change.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth.roles import Role
from baluarte.db.models import AppUser, GitLabGroup, ManagerScope
from baluarte.hierarchy import GroupTree
from baluarte.policy.changes import Actor

MAX_GROUPS = 100


class ScopeRefused(Exception):
    """The assignment cannot be made; ``code`` and ``reasons`` explain why."""

    def __init__(self, code: str, message: str, reasons: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.reasons = list(reasons)


class UnknownScope(Exception):
    pass


def open_scopes(
    db: Session, instance_id: int | None, user_id: int | None = None
) -> list[ManagerScope]:
    statement = select(ManagerScope).where(
        ManagerScope.instance_id == (instance_id or 0), ManagerScope.ended_at.is_(None)
    )
    if user_id is not None:
        statement = statement.where(ManagerScope.user_id == user_id)
    return list(db.scalars(statement.order_by(ManagerScope.group_path, ManagerScope.id)))


def all_scopes(db: Session, instance_id: int | None) -> list[ManagerScope]:
    return list(
        db.scalars(
            select(ManagerScope)
            .where(ManagerScope.instance_id == (instance_id or 0))
            .order_by(ManagerScope.ended_at.is_not(None), ManagerScope.group_path, ManagerScope.id)
        )
    )


def assign(
    db: Session, instance_id: int, user_id: int, group_gitlab_ids: Sequence[int], by: Actor
) -> list[ManagerScope]:
    """Assign groups to a user; groups already assigned are kept as they are. The caller commits."""
    if db.get(AppUser, user_id) is None:
        raise ScopeRefused("unknown_user", "No such user has signed in", [str(user_id)])
    wanted = list(dict.fromkeys(group_gitlab_ids))
    if not wanted:
        raise ScopeRefused("no_groups", "Name at least one group")
    if len(wanted) > MAX_GROUPS:
        raise ScopeRefused("too_many_groups", f"At most {MAX_GROUPS} groups at once")
    paths = dict(
        db.execute(
            select(GitLabGroup.gitlab_id, GitLabGroup.full_path).where(
                GitLabGroup.instance_id == instance_id, GitLabGroup.gitlab_id.in_(wanted)
            )
        ).all()
    )
    unknown = [str(group) for group in wanted if group not in paths]
    if unknown:
        raise ScopeRefused("unknown_group", "Some groups are not in the inventory", unknown)
    existing = {row.group_gitlab_id: row for row in open_scopes(db, instance_id, user_id)}
    rows: list[ManagerScope] = []
    added: list[str] = []
    for group in wanted:
        row = existing.get(group)
        if row is None:
            row = ManagerScope(
                instance_id=instance_id,
                user_id=user_id,
                group_gitlab_id=group,
                group_path=paths[group],
                assigned_by=by.actor,
                assigned_by_user_id=by.user_id,
            )
            db.add(row)
            added.append(paths[group])
        rows.append(row)
    db.flush()
    if added:
        audit.record(
            db,
            actor=by.actor,
            action="manager.scope_assigned",
            resource_type="user",
            resource_id=str(user_id),
            details={"groups": added},
        )
    return rows


def end(db: Session, scope_id: int, by: Actor) -> ManagerScope:
    """End an open assignment. The caller commits."""
    row = db.get(ManagerScope, scope_id, with_for_update=True)
    if row is None:
        raise UnknownScope
    if row.ended_at is not None:
        raise ScopeRefused("already_ended", "This assignment has already ended")
    row.ended_at = datetime.now(UTC)
    row.ended_by = by.actor
    row.ended_by_user_id = by.user_id
    db.flush()
    audit.record(
        db,
        actor=by.actor,
        action="manager.scope_ended",
        resource_type="user",
        resource_id=str(row.user_id),
        details={"scope_id": row.id, "group": row.group_path},
    )
    return row


@dataclass(frozen=True)
class PeopleScope:
    """The projects whose time per person a reader may see: all of them, or ``project_ids``."""

    all: bool
    project_ids: frozenset[int]
    groups: tuple[int, ...] = ()

    def allows(self, project_id: int) -> bool:
        return self.all or project_id in self.project_ids


def people_scope(
    db: Session, instance_id: int | None, user_id: int, roles: frozenset[Role]
) -> PeopleScope:
    """Administrators see every project; anyone else only the projects of their open scopes."""
    if Role.ADMIN in roles:
        return PeopleScope(all=True, project_ids=frozenset())
    groups = tuple(row.group_gitlab_id for row in open_scopes(db, instance_id, user_id))
    if not groups:
        return PeopleScope(all=False, project_ids=frozenset())
    tree = GroupTree.load(db, instance_id)
    return PeopleScope(all=False, project_ids=frozenset(tree.projects_under(groups)), groups=groups)
