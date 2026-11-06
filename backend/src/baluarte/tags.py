"""Tags kept in Baluarte to group projects across the GitLab hierarchy (docs/TAGS.md).

A tag is a ``key=value`` pair such as ``team=payments``. Assigned to a group it applies to the
group's subgroups and projects; assigned to a project directly it wins over a value of the same
key inherited from a group, and the nearest group wins over one further up. A project therefore
has at most one value per key. Two values of one key may not be assigned directly to the same
group or project (``key_conflict``).

Tags never change GitLab and are not evidence of compliance: they only group results. Every
change is audited as ``tags.*``; assignments are ended rather than deleted, so the history stays.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.db.models import GitLabGroup, GitLabProject, Tag, TagAssignment
from baluarte.hierarchy import GroupTree
from baluarte.policy.changes import Actor

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
MAX_TARGETS = 500

TargetType = Literal["group", "project"]
Via = Literal["project", "group"]


class TagRefused(Exception):
    """A tag change cannot be made; ``code`` and ``reasons`` explain why."""

    def __init__(self, code: str, message: str, reasons: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.reasons = list(reasons)


class UnknownTag(Exception):
    pass


def valid_name(text: str) -> bool:
    return NAME.fullmatch(text) is not None


def parse_filter(text: str) -> tuple[str, str] | None:
    """``key:value`` as used by list filters; None when it is not one."""
    key, sep, value = text.partition(":")
    if not sep or not valid_name(key) or not valid_name(value):
        return None
    return key, value


def _color(color: str | None) -> str | None:
    if color is None or not color.strip():
        return None
    if COLOR.fullmatch(color.strip()) is None:
        raise TagRefused("invalid_color", "A colour is #RRGGBB")
    return color.strip().upper()


def _description(description: str | None) -> str | None:
    text = (description or "").strip()
    if len(text) > 500:
        raise TagRefused("invalid_description", "A description has at most 500 characters")
    return text or None


def create(
    db: Session,
    instance_id: int,
    key: str,
    value: str,
    description: str | None,
    color: str | None,
    by: Actor,
) -> Tag:
    """Create a tag. The caller commits."""
    if not valid_name(key) or not valid_name(value):
        raise TagRefused("invalid_name", "Keys and values are lowercase: a-z, 0-9, '.', '_', '-'")
    if db.scalars(
        select(Tag.id).where(Tag.instance_id == instance_id, Tag.key == key, Tag.value == value)
    ).first():
        raise TagRefused("tag_exists", f"The tag {key}={value} already exists", [f"{key}={value}"])
    row = Tag(
        instance_id=instance_id,
        key=key,
        value=value,
        description=_description(description),
        color=_color(color),
        created_by=by.actor,
        created_by_user_id=by.user_id,
    )
    db.add(row)
    db.flush()
    audit.record(
        db,
        actor=by.actor,
        action="tags.created",
        resource_type="tag",
        resource_id=str(row.id),
        details={"key": key, "value": value},
    )
    return row


def update(
    db: Session,
    tag_id: int,
    by: Actor,
    *,
    description: str | None = None,
    color: str | None = None,
    set_description: bool = False,
    set_color: bool = False,
) -> Tag:
    """Change a tag's description or colour. The caller commits."""
    row = db.get(Tag, tag_id)
    if row is None:
        raise UnknownTag
    if set_description:
        row.description = _description(description)
    if set_color:
        row.color = _color(color)
    row.updated_at = datetime.now(UTC)
    db.flush()
    audit.record(
        db,
        actor=by.actor,
        action="tags.updated",
        resource_type="tag",
        resource_id=str(row.id),
        details={"description": row.description, "color": row.color},
    )
    return row


@dataclass(frozen=True)
class Target:
    type: TargetType
    gitlab_id: int


def _paths(db: Session, instance_id: int, targets: Sequence[Target]) -> dict[Target, str]:
    groups = [t.gitlab_id for t in targets if t.type == "group"]
    projects = [t.gitlab_id for t in targets if t.type == "project"]
    found: dict[Target, str] = {}
    if groups:
        for gitlab_id, path in db.execute(
            select(GitLabGroup.gitlab_id, GitLabGroup.full_path).where(
                GitLabGroup.instance_id == instance_id, GitLabGroup.gitlab_id.in_(groups)
            )
        ):
            found[Target("group", gitlab_id)] = path
    if projects:
        for gitlab_id, path in db.execute(
            select(GitLabProject.gitlab_id, GitLabProject.path_with_namespace).where(
                GitLabProject.instance_id == instance_id, GitLabProject.gitlab_id.in_(projects)
            )
        ):
            found[Target("project", gitlab_id)] = path
    return found


def open_assignments(db: Session, tag_ids: Iterable[int] | None = None) -> list[TagAssignment]:
    statement = select(TagAssignment).where(TagAssignment.ended_at.is_(None))
    if tag_ids is not None:
        statement = statement.where(TagAssignment.tag_id.in_(list(tag_ids)))
    return list(db.scalars(statement.order_by(TagAssignment.target_path, TagAssignment.id)))


def assign(db: Session, tag_id: int, targets: Sequence[Target], by: Actor) -> list[TagAssignment]:
    """Assign a tag to groups and projects; targets that already have it are left as they are.

    Refused with ``key_conflict`` when a target has another value of the same key assigned
    directly. The caller commits.
    """
    tag = db.get(Tag, tag_id)
    if tag is None:
        raise UnknownTag
    wanted = list(dict.fromkeys(targets))
    if not wanted:
        raise TagRefused("no_targets", "Name at least one group or project")
    if len(wanted) > MAX_TARGETS:
        raise TagRefused("too_many_targets", f"At most {MAX_TARGETS} targets at once")
    paths = _paths(db, tag.instance_id, wanted)
    unknown = [f"{t.type}:{t.gitlab_id}" for t in wanted if t not in paths]
    if unknown:
        raise TagRefused("unknown_target", "Some targets are not in the inventory", unknown)
    siblings = select(Tag.id).where(
        Tag.instance_id == tag.instance_id, Tag.key == tag.key, Tag.id != tag.id
    )
    conflicts = sorted(
        row.target_path
        for row in db.scalars(
            select(TagAssignment).where(
                TagAssignment.ended_at.is_(None), TagAssignment.tag_id.in_(siblings)
            )
        )
        if Target(row.target_type, row.target_gitlab_id) in paths  # type: ignore[arg-type]
    )
    if conflicts:
        raise TagRefused(
            "key_conflict",
            f"These already have another value of {tag.key} assigned directly",
            conflicts,
        )
    existing = {
        Target(row.target_type, row.target_gitlab_id): row  # type: ignore[arg-type]
        for row in open_assignments(db, [tag.id])
    }
    rows: list[TagAssignment] = []
    added: list[str] = []
    for target in wanted:
        row = existing.get(target)
        if row is None:
            row = TagAssignment(
                tag_id=tag.id,
                target_type=target.type,
                target_gitlab_id=target.gitlab_id,
                target_path=paths[target],
                assigned_by=by.actor,
                assigned_by_user_id=by.user_id,
            )
            db.add(row)
            added.append(f"{target.type}:{paths[target]}")
        rows.append(row)
    db.flush()
    if added:
        audit.record(
            db,
            actor=by.actor,
            action="tags.assigned",
            resource_type="tag",
            resource_id=str(tag.id),
            details={"tag": f"{tag.key}={tag.value}", "targets": added},
        )
    return rows


def unassign(db: Session, tag_id: int, targets: Sequence[Target], by: Actor) -> int:
    """End the tag's assignments to ``targets``; targets without it are ignored. The caller
    commits. Returns how many ended."""
    tag = db.get(Tag, tag_id)
    if tag is None:
        raise UnknownTag
    wanted = set(targets)
    now = datetime.now(UTC)
    ended: list[str] = []
    for row in open_assignments(db, [tag.id]):
        if Target(row.target_type, row.target_gitlab_id) in wanted:  # type: ignore[arg-type]
            row.ended_at, row.ended_by, row.ended_by_user_id = now, by.actor, by.user_id
            ended.append(f"{row.target_type}:{row.target_path}")
    db.flush()
    if ended:
        audit.record(
            db,
            actor=by.actor,
            action="tags.unassigned",
            resource_type="tag",
            resource_id=str(tag.id),
            details={"tag": f"{tag.key}={tag.value}", "targets": ended},
        )
    return len(ended)


@dataclass(frozen=True)
class Applied:
    """One tag as it applies to a project or group, and where it comes from."""

    tag_id: int
    key: str
    value: str
    via: Via
    from_path: str


@dataclass
class TagIndex:
    """Every project's and group's tags, resolved for one instance."""

    tags: dict[int, Tag] = field(default_factory=dict)
    by_project: dict[int, dict[str, Applied]] = field(default_factory=dict)
    by_group: dict[int, dict[str, Applied]] = field(default_factory=dict)

    def of_project(self, row_id: int) -> list[Applied]:
        return sorted(self.by_project.get(row_id, {}).values(), key=lambda a: (a.key, a.value))

    def of_group(self, gitlab_id: int) -> list[Applied]:
        return sorted(self.by_group.get(gitlab_id, {}).values(), key=lambda a: (a.key, a.value))

    def projects_with(self, filters: Sequence[tuple[str, str]]) -> set[int]:
        """Row IDs of projects that have every ``(key, value)``."""
        return {
            row_id
            for row_id, applied in self.by_project.items()
            if all(key in applied and applied[key].value == value for key, value in filters)
        }

    def groups_with(self, filters: Sequence[tuple[str, str]]) -> set[int]:
        return {
            gitlab_id
            for gitlab_id, applied in self.by_group.items()
            if all(key in applied and applied[key].value == value for key, value in filters)
        }


def index(db: Session, instance_id: int | None, tree: GroupTree | None = None) -> TagIndex:
    """Resolve the open assignments of ``instance_id`` against today's group tree."""
    found = TagIndex()
    if instance_id is None:
        return found
    found.tags = {
        row.id: row for row in db.scalars(select(Tag).where(Tag.instance_id == instance_id))
    }
    if not found.tags:
        return found
    tree = tree or GroupTree.load(db, instance_id)
    direct_groups: dict[int, dict[str, Applied]] = {}
    direct_projects: dict[int, dict[str, Applied]] = {}
    for row in open_assignments(db, found.tags):
        tag = found.tags[row.tag_id]
        target = direct_groups if row.target_type == "group" else direct_projects
        applied = Applied(tag.id, tag.key, tag.value, row.target_type, row.target_path)  # type: ignore[arg-type]
        target.setdefault(row.target_gitlab_id, {}).setdefault(tag.key, applied)
    for group in tree.parents:
        resolved: dict[str, Applied] = {}
        # Nearest first, so the nearest group's value wins.
        for ancestor in tree.ancestors(group):
            for key, applied in direct_groups.get(ancestor, {}).items():
                resolved.setdefault(key, applied)
        if resolved:
            found.by_group[group] = resolved
    for node in tree.projects.values():
        resolved = dict(direct_projects.get(node.gitlab_id, {}))
        if node.group_gitlab_id is not None:
            for key, applied in found.by_group.get(node.group_gitlab_id, {}).items():
                resolved.setdefault(
                    key, Applied(applied.tag_id, key, applied.value, "group", applied.from_path)
                )
        if resolved:
            found.by_project[node.row_id] = resolved
    return found
