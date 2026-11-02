"""Assigning policy versions to GitLab targets, and resolving the policies in force (BAL-018).

An assignment pins one immutable version to the instance, a group or a project. Resolution for
a project walks project, namespace group, its ancestors, then the instance:

- Every policy name assigned anywhere on that path applies once; different names add up.
- For one name, the assignment nearest the target wins and the others are ``overridden``.
- The winning version's ``spec.scope.groups`` then decides whether it applies: when the target
  is outside the scope the name is ``out_of_scope`` and does not fall back to a farther
  assignment, so a version that a nearer owner replaced is never applied by accident.
- A personal project's namespace is a user, so its path goes straight to the instance and is
  complete. When the namespace kind is unknown (not yet rediscovered), the namespace is looked
  up as a group like any other.
- A group on the path that is not in the inventory, or is missing or inaccessible, makes the
  ancestry ``incomplete`` and is listed in ``gaps``. An incomplete resolution may lack policies
  (or nearer overrides) assigned to the groups that could not be followed.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from baluarte.db.models import GitLabGroup, GitLabInstance, GitLabProject, Policy, PolicyAssignment
from baluarte.policy.loader import parse_policy

TargetType = Literal["instance", "group", "project"]
GapReason = Literal["not_in_inventory", "missing", "inaccessible", "cycle", "too_deep"]
Outcome = Literal["effective", "overridden", "out_of_scope"]

# GitLab allows 20 levels of subgroups; anything deeper means the inventory is inconsistent.
MAX_GROUP_DEPTH = 20


class Target(BaseModel):
    target_type: TargetType
    target_id: int
    path: str
    # Inventory state of a group or project; None for the instance.
    state: str | None


class Gap(BaseModel):
    gitlab_id: int
    reason: GapReason


class Resolved(BaseModel):
    assignment_id: int
    policy_id: int
    name: str
    version: int
    yaml_digest: str
    assigned_to: Target
    outcome: Outcome


class Resolution(BaseModel):
    target: Target
    ancestry: Literal["complete", "incomplete"]
    gaps: list[Gap]
    # Nearest first: the target, its groups up to the top level, then the instance.
    path: list[Target]
    policies: list[Resolved]
    considered: list[Resolved]
    # Set on a project that evaluation skips: "archived". Its policies still resolve,
    # but nothing is judged against them. Always null for groups and the instance.
    not_evaluated: Literal["archived"] | None = None


def in_scope(groups: Sequence[str], namespace_path: str) -> bool:
    """Whether a scope covers a namespace: ``"*"``, the group itself or one of its ancestors.

    GitLab paths are unique regardless of case, so they are compared case-insensitively.
    """
    path = namespace_path.casefold()
    for entry in groups:
        if entry == "*":
            return True
        prefix = entry.casefold()
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def find_target(
    db: Session, instance: GitLabInstance, target_type: TargetType, target_id: int
) -> Target | None:
    """The target as recorded in the inventory, or None when it is not there."""
    if target_type == "instance":
        if target_id != instance.id:
            return None
        return Target(
            target_type="instance", target_id=instance.id, path=instance.base_url, state=None
        )
    if target_type == "group":
        group = db.scalars(
            select(GitLabGroup).where(
                GitLabGroup.instance_id == instance.id, GitLabGroup.gitlab_id == target_id
            )
        ).first()
        if group is None:
            return None
        return Target(
            target_type="group", target_id=target_id, path=group.full_path, state=group.state
        )
    project = db.scalars(
        select(GitLabProject).where(
            GitLabProject.instance_id == instance.id, GitLabProject.gitlab_id == target_id
        )
    ).first()
    if project is None:
        return None
    return Target(
        target_type="project",
        target_id=target_id,
        path=project.path_with_namespace,
        state=project.state,
    )


@dataclass(frozen=True)
class Assigned:
    assignment: PolicyAssignment
    created: bool
    # The assignment of another version of the same policy that this one replaced.
    replaced: PolicyAssignment | None
    replaced_version: int | None


def assign(db: Session, instance_id: int, policy: Policy, target: Target) -> Assigned:
    """Pin ``policy`` to ``target``, replacing another version of it there. The caller commits."""
    # Serialises changes to one (target, policy name), so concurrent requests cannot both insert.
    key = f"policy_assignment:{instance_id}:{target.target_type}:{target.target_id}:{policy.name}"
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext(key))))
    existing = db.scalars(
        select(PolicyAssignment).where(
            PolicyAssignment.instance_id == instance_id,
            PolicyAssignment.target_type == target.target_type,
            PolicyAssignment.target_id == target.target_id,
            PolicyAssignment.policy_name == policy.name,
        )
    ).first()
    if existing is not None and existing.policy_id == policy.id:
        return Assigned(assignment=existing, created=False, replaced=None, replaced_version=None)
    replaced_version = None
    if existing is not None:
        replaced_version = db.scalars(
            select(Policy.version).where(Policy.id == existing.policy_id)
        ).one()
        db.delete(existing)
        db.flush()
    assignment = PolicyAssignment(
        instance_id=instance_id,
        policy_id=policy.id,
        policy_name=policy.name,
        target_type=target.target_type,
        target_id=target.target_id,
    )
    db.add(assignment)
    db.flush()
    return Assigned(
        assignment=assignment,
        created=True,
        replaced=existing,
        replaced_version=replaced_version,
    )


GroupLookup = Callable[[int], GitLabGroup | None]


def _walk(
    lookup: GroupLookup, group_id: int | None, seen: set[int]
) -> tuple[list[Target], list[Gap]]:
    """Groups from ``group_id`` up to its top-level group, and where the walk was uncertain."""
    path: list[Target] = []
    gaps: list[Gap] = []
    current = group_id
    while current is not None:
        if current in seen:
            gaps.append(Gap(gitlab_id=current, reason="cycle"))
            break
        if len(path) > MAX_GROUP_DEPTH:
            gaps.append(Gap(gitlab_id=current, reason="too_deep"))
            break
        seen.add(current)
        group = lookup(current)
        if group is None:
            # Its parent is unknown, so nothing above it can be followed.
            gaps.append(Gap(gitlab_id=current, reason="not_in_inventory"))
            break
        if group.state in ("missing", "inaccessible"):
            # Still followed using the last known parent, but no longer confirmed.
            reason: GapReason = "missing" if group.state == "missing" else "inaccessible"
            gaps.append(Gap(gitlab_id=current, reason=reason))
        path.append(
            Target(target_type="group", target_id=current, path=group.full_path, state=group.state)
        )
        current = group.parent_gitlab_id
    return path, gaps


def _ancestry(
    db: Session, instance_id: int, group_id: int | None, seen: set[int]
) -> tuple[list[Target], list[Gap]]:
    def lookup(gitlab_id: int) -> GitLabGroup | None:
        return db.scalars(
            select(GitLabGroup).where(
                GitLabGroup.instance_id == instance_id, GitLabGroup.gitlab_id == gitlab_id
            )
        ).first()

    return _walk(lookup, group_id, seen)


def group_ancestry(
    db: Session, instance_id: int, group_id: int | None, seen: set[int] | None = None
) -> tuple[list[Target], list[Gap]]:
    """Groups from ``group_id`` to the top level as the inventory records them, nearest first."""
    return _ancestry(db, instance_id, group_id, set() if seen is None else seen)


ScopeOf = Callable[[Policy], Sequence[str]]


def _policy_scope(policy: Policy) -> Sequence[str]:
    return parse_policy(policy.source_yaml).document.spec.scope.groups


def _decide(
    path: list[Target],
    rows: Sequence[tuple[PolicyAssignment, Policy]],
    scope_path: str,
    scope_of: ScopeOf = _policy_scope,
) -> list[Resolved]:
    """Every assignment on ``path`` with its outcome: nearest wins per name, then its scope."""
    rank: dict[tuple[str, int], int] = {
        (level.target_type, level.target_id): index for index, level in enumerate(path)
    }
    # Nearest level first; at one level a name has a single assignment (unique constraint).
    ordered = sorted(
        rows,
        key=lambda row: (row[0].policy_name, rank[(row[0].target_type, row[0].target_id)]),
    )
    considered: list[Resolved] = []
    decided: set[str] = set()
    for assignment, policy in ordered:
        outcome: Outcome
        if assignment.policy_name in decided:
            outcome = "overridden"
        else:
            decided.add(assignment.policy_name)
            outcome = "effective" if in_scope(scope_of(policy), scope_path) else "out_of_scope"
        considered.append(
            Resolved(
                assignment_id=assignment.id,
                policy_id=policy.id,
                name=policy.name,
                version=policy.version,
                yaml_digest=policy.yaml_digest,
                assigned_to=path[rank[(assignment.target_type, assignment.target_id)]],
                outcome=outcome,
            )
        )
    return considered


def _resolve(
    db: Session, instance: GitLabInstance, target: Target, start_group: int | None, scope_path: str
) -> Resolution:
    seen = {target.target_id} if target.target_type == "group" else set()
    groups, gaps = _ancestry(db, instance.id, start_group, seen)
    root = Target(target_type="instance", target_id=instance.id, path=instance.base_url, state=None)
    path = [target, *groups, root]
    group_ids = [level.target_id for level in path if level.target_type == "group"]
    conditions = [
        (PolicyAssignment.target_type == "instance") & (PolicyAssignment.target_id == instance.id),
        (PolicyAssignment.target_type == "group") & PolicyAssignment.target_id.in_(group_ids),
    ]
    if target.target_type == "project":
        conditions.append(
            (PolicyAssignment.target_type == "project")
            & (PolicyAssignment.target_id == target.target_id)
        )
    rows = db.execute(
        select(PolicyAssignment, Policy)
        .join(Policy, Policy.id == PolicyAssignment.policy_id)
        .where(PolicyAssignment.instance_id == instance.id, or_(*conditions))
    ).all()
    considered = _decide(path, [(row[0], row[1]) for row in rows], scope_path)
    return _resolution(target, gaps, path, considered)


def _resolution(
    target: Target, gaps: list[Gap], path: list[Target], considered: list[Resolved]
) -> Resolution:
    return Resolution(
        target=target,
        ancestry="incomplete" if gaps else "complete",
        gaps=gaps,
        path=path,
        policies=[entry for entry in considered if entry.outcome == "effective"],
        considered=considered,
    )


def resolve_project(db: Session, instance: GitLabInstance, project: GitLabProject) -> Resolution:
    """Policies in force for a project.

    A personal project has no groups above it, so only project and instance assignments apply;
    in practice only ``"*"`` scopes cover it, since no group shares a username's path. A project
    whose namespace kind is unknown is not assumed to be personal: its namespace is looked up as
    a group and reported as ``not_in_inventory`` when absent.
    """
    target = Target(
        target_type="project",
        target_id=project.gitlab_id,
        path=project.path_with_namespace,
        state=project.state,
    )
    namespace_path = project.path_with_namespace.rsplit("/", 1)[0]
    start = None if project.namespace_kind == "user" else project.namespace_gitlab_id
    return _resolve(db, instance, target, start, namespace_path)


def resolve_group(db: Session, instance: GitLabInstance, group: GitLabGroup) -> Resolution:
    """Policies in force for a group: its own assignments, its ancestors' and the instance's.

    Like a project's own state, the group's state is reported in ``target`` and is not a gap.
    """
    target = Target(
        target_type="group", target_id=group.gitlab_id, path=group.full_path, state=group.state
    )
    return _resolve(db, instance, target, group.parent_gitlab_id, group.full_path)


# Where policies apply for every group at once ("Where baselines apply" in the web UI).
EffectStatus = Literal["set_here", "inherited", "out_of_scope"]


class TreeAssignment(BaseModel):
    assignment_id: int
    policy_id: int
    name: str
    version: int
    yaml_digest: str


class TreeEffect(BaseModel):
    """The deciding assignment of one policy name at a group or project."""

    name: str
    assignment_id: int
    policy_id: int
    version: int
    yaml_digest: str
    # ``set_here`` and ``inherited`` apply. ``out_of_scope``: the deciding version's scope
    # excludes this target, which then does not fall back to a farther assignment.
    status: EffectStatus
    assigned_to: Target
    # Farther assignments of the same name that this one replaces, nearest first.
    replaces: list[Resolved]


class TreeNode(BaseModel):
    target: Target
    # The parent group on the path: a group's parent, or a project's namespace group (null for
    # a personal project).
    parent_gitlab_id: int | None
    ancestry: Literal["complete", "incomplete"]
    gaps: list[Gap]
    # Assignments made to this target itself.
    assignments: list[TreeAssignment]
    in_effect: list[TreeEffect]


class TreeProject(TreeNode):
    namespace_gitlab_id: int
    namespace_kind: str | None


class AssignmentTree(BaseModel):
    instance: Target
    instance_assignments: list[TreeAssignment]
    # Every group of the instance, ordered by path; ``parent_gitlab_id`` builds the hierarchy.
    groups: list[TreeNode]
    # Only projects with an assignment of their own. Every other project in a group has what
    # its namespace group has in effect, with the scope judged on the same namespace path.
    projects: list[TreeProject]


Row = tuple[PolicyAssignment, Policy]


def _tree_assignment(assignment: PolicyAssignment, policy: Policy) -> TreeAssignment:
    return TreeAssignment(
        assignment_id=assignment.id,
        policy_id=policy.id,
        name=policy.name,
        version=policy.version,
        yaml_digest=policy.yaml_digest,
    )


def _effects(target: Target, considered: list[Resolved]) -> list[TreeEffect]:
    effects: list[TreeEffect] = []
    here = (target.target_type, target.target_id)
    for entry in considered:
        if entry.outcome == "overridden":
            continue
        status: EffectStatus
        if entry.outcome == "out_of_scope":
            status = "out_of_scope"
        elif (entry.assigned_to.target_type, entry.assigned_to.target_id) == here:
            status = "set_here"
        else:
            status = "inherited"
        effects.append(
            TreeEffect(
                name=entry.name,
                assignment_id=entry.assignment_id,
                policy_id=entry.policy_id,
                version=entry.version,
                yaml_digest=entry.yaml_digest,
                status=status,
                assigned_to=entry.assigned_to,
                replaces=[
                    other
                    for other in considered
                    if other.name == entry.name and other.outcome == "overridden"
                ],
            )
        )
    return effects


def resolve_tree(db: Session, instance: GitLabInstance) -> AssignmentTree:
    """What each group, and each project with its own assignment, has in effect, in one read.

    Applies exactly the rules of :func:`resolve_group` and :func:`resolve_project` to data read
    once, so the entry for any one target matches its effective-policies read.
    """
    groups = {
        group.gitlab_id: group
        for group in db.scalars(select(GitLabGroup).where(GitLabGroup.instance_id == instance.id))
    }
    rows: list[Row] = [
        (row[0], row[1])
        for row in db.execute(
            select(PolicyAssignment, Policy)
            .join(Policy, Policy.id == PolicyAssignment.policy_id)
            .where(PolicyAssignment.instance_id == instance.id)
            .order_by(PolicyAssignment.id)
        )
    ]
    at: dict[tuple[str, int], list[Row]] = {}
    for row in rows:
        at.setdefault((row[0].target_type, row[0].target_id), []).append(row)
    scopes: dict[int, Sequence[str]] = {}

    def scope_of(policy: Policy) -> Sequence[str]:
        if policy.id not in scopes:
            scopes[policy.id] = _policy_scope(policy)
        return scopes[policy.id]

    root = Target(target_type="instance", target_id=instance.id, path=instance.base_url, state=None)

    def node(target: Target, start: int | None, seen: set[int], scope_path: str) -> TreeNode:
        ancestors, gaps = _walk(groups.get, start, seen)
        path = [target, *ancestors, root]
        on_path = [
            row for level in path for row in at.get((level.target_type, level.target_id), [])
        ]
        considered = _decide(path, on_path, scope_path, scope_of)
        return TreeNode(
            target=target,
            parent_gitlab_id=start,
            ancestry="incomplete" if gaps else "complete",
            gaps=gaps,
            assignments=[
                _tree_assignment(*row) for row in at.get((target.target_type, target.target_id), [])
            ],
            in_effect=_effects(target, considered),
        )

    group_nodes = [
        node(
            Target(
                target_type="group",
                target_id=group.gitlab_id,
                path=group.full_path,
                state=group.state,
            ),
            group.parent_gitlab_id,
            {group.gitlab_id},
            group.full_path,
        )
        for group in sorted(groups.values(), key=lambda item: item.full_path.casefold())
    ]

    project_ids = sorted({row[0].target_id for row in rows if row[0].target_type == "project"})
    projects = (
        db.scalars(
            select(GitLabProject).where(
                GitLabProject.instance_id == instance.id, GitLabProject.gitlab_id.in_(project_ids)
            )
        ).all()
        if project_ids
        else []
    )
    project_nodes: list[TreeProject] = []
    for project in sorted(projects, key=lambda item: item.path_with_namespace.casefold()):
        target = Target(
            target_type="project",
            target_id=project.gitlab_id,
            path=project.path_with_namespace,
            state=project.state,
        )
        start = None if project.namespace_kind == "user" else project.namespace_gitlab_id
        namespace_path = project.path_with_namespace.rsplit("/", 1)[0]
        resolved = node(target, start, set(), namespace_path)
        project_nodes.append(
            TreeProject(
                **resolved.model_dump(),
                namespace_gitlab_id=project.namespace_gitlab_id,
                namespace_kind=project.namespace_kind,
            )
        )
    return AssignmentTree(
        instance=root,
        instance_assignments=[
            _tree_assignment(*row) for row in at.get(("instance", instance.id), [])
        ],
        groups=group_nodes,
        projects=project_nodes,
    )
