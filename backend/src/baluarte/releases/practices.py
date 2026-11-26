"""Release practices: how projects are meant to reach production (RG.2, ADR-0017).

A practice is a preset with parameters, or a custom one built from the same blocks:

- ``merge_request``: production deploys the default branch; changes reach it through reviewed
  merge requests.
- ``release_tag``: production deploys a tag matching one of ``ref_patterns``, such as
  ``*-release``, approved through GitLab's deployment approvals.
- ``release_branch``: production deploys a branch matching one of ``ref_patterns``, such as
  ``release/*``.
- ``custom``: any ``production_from`` with any parameters.

A practice is assigned to a connection, a group or a project, and the nearest assignment on a
project's path decides (the project, its groups up to the top level, then the connection). Each
project's recorded production deployments in the last ``WINDOW_DAYS`` are compared with its
practice: the refs it deployed, whether they are what the practice expects, and so whether the
project follows it. The release checks (RG.3) judge approvals and people.
"""

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.db.models import (
    ChangeRecord,
    GitLabGroup,
    GitLabInstance,
    GitLabProject,
    Observation,
    ReleasePractice,
    ReleasePracticeAssignment,
)
from baluarte.integrations.gitlab.branches import matches
from baluarte.policy.assignments import Target
from baluarte.scope.exclusions import coverage
from baluarte.workspaces import current_workspace_id

Preset = Literal["merge_request", "release_tag", "release_branch", "custom"]
ProductionFrom = Literal["default_branch", "tag", "branch"]
Status = Literal["follows", "differs", "unknown", "no_releases", "unassigned"]
RefKind = Literal["default_branch", "tag", "branch", "unknown"]

WINDOW_DAYS = 90
# Refs listed per project when it differs from its practice.
MAX_REFS = 5

Pattern = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
Environment = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class PracticeSettings(BaseModel):
    """A practice's parameters."""

    model_config = ConfigDict(extra="forbid")

    production_from: ProductionFrom
    # Tag or branch patterns production deploys, GitLab wildcards (``*``); none for the
    # default branch.
    ref_patterns: Annotated[list[Pattern], Field(max_length=20)] = []
    environments: Annotated[list[Environment], Field(min_length=1, max_length=10)] = ["production"]
    # Deployment approvals each production release needs (GitLab Premium or Ultimate).
    required_approvals: Annotated[int, Field(ge=0, le=10)] = 0
    # Whether whoever started the release's pipeline (for a tag, its creator) may approve it.
    creator_may_approve: bool = False
    # Whether whoever GitLab records as the deployer may also approve.
    deployer_may_approve: bool = False
    # Whether each change into the default branch must also be reviewed by someone other than
    # its author.
    require_merge_request_review: bool = True

    @model_validator(mode="after")
    def _patterns(self) -> "PracticeSettings":
        if self.production_from == "default_branch" and self.ref_patterns:
            raise ValueError("ref_patterns are for tags or branches, not the default branch")
        if self.production_from != "default_branch" and not self.ref_patterns:
            raise ValueError(f"a {self.production_from} practice needs at least one ref pattern")
        self.ref_patterns = sorted(set(self.ref_patterns))
        self.environments = sorted(set(self.environments))
        return self


PRESETS: dict[str, PracticeSettings] = {
    "merge_request": PracticeSettings(production_from="default_branch"),
    "release_tag": PracticeSettings(
        production_from="tag", ref_patterns=["*-release"], required_approvals=1
    ),
    "release_branch": PracticeSettings(production_from="branch", ref_patterns=["release/*"]),
}

PRESET_LABELS: dict[str, str] = {
    "merge_request": "Merge request to the default branch",
    "release_tag": "Protected release tag with deployment approval",
    "release_branch": "Release branch",
    "custom": "Custom",
}


class PracticeInvalid(ValueError):
    pass


def validated(preset: str, settings: Mapping[str, Any] | None) -> PracticeSettings:
    """The practice's settings: the preset's defaults with ``settings`` applied. A preset keeps
    its ``production_from``; only a custom practice chooses it."""
    if preset not in PRESET_LABELS:
        raise PracticeInvalid(f"unknown preset {preset}")
    given = dict(settings or {})
    if preset == "custom":
        if "production_from" not in given:
            raise PracticeInvalid("a custom practice needs production_from")
        base: dict[str, Any] = {}
    else:
        base = PRESETS[preset].model_dump()
        if given.get("production_from", base["production_from"]) != base["production_from"]:
            raise PracticeInvalid(
                f"the {PRESET_LABELS[preset]} preset deploys from {base['production_from']}; "
                "use a custom practice for another"
            )
    try:
        return PracticeSettings.model_validate(base | given)
    except ValueError as error:
        raise PracticeInvalid(str(error)) from None


# Practices ---------------------------------------------------------------------------------------


def find(db: Session, name: str) -> ReleasePractice | None:
    return db.scalars(select(ReleasePractice).where(ReleasePractice.name == name)).first()


def create(
    db: Session,
    name: str,
    preset: str,
    settings: Mapping[str, Any] | None,
    description: str | None,
    actor: str,
) -> ReleasePractice:
    """A new practice. The caller commits."""
    if find(db, name) is not None:
        raise PracticeInvalid(f"a practice named {name} already exists")
    values = validated(preset, settings).model_dump()
    practice = ReleasePractice(
        workspace_id=current_workspace_id(db),
        name=name,
        preset=preset,
        description=description,
        settings=values,
        created_by=actor,
        updated_by=actor,
    )
    db.add(practice)
    db.flush()
    audit.record(
        db,
        actor=actor,
        action="release_practice.created",
        resource_type="release_practice",
        resource_id=str(practice.id),
        details={"name": name, "preset": preset, "description": description, "settings": values},
    )
    return practice


def update(
    db: Session,
    practice: ReleasePractice,
    settings: Mapping[str, Any] | None,
    description: str | None,
    actor: str,
) -> bool:
    """Change a practice's settings and description; False when nothing changed."""
    values = validated(practice.preset, settings).model_dump()
    if values == practice.settings and description == practice.description:
        return False
    audit.record(
        db,
        actor=actor,
        action="release_practice.updated",
        resource_type="release_practice",
        resource_id=str(practice.id),
        details={
            "name": practice.name,
            "before": {"settings": practice.settings, "description": practice.description},
            "after": {"settings": values, "description": description},
        },
    )
    practice.settings = values
    practice.description = description
    practice.updated_by = actor
    practice.updated_at = func.now()
    return True


def assignments_of(db: Session, practice: ReleasePractice) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(ReleasePracticeAssignment)
            .where(ReleasePracticeAssignment.practice_id == practice.id)
        )
        or 0
    )


def delete(db: Session, practice: ReleasePractice, actor: str) -> None:
    """Remove a practice that applies nowhere. The caller commits."""
    if assignments_of(db, practice):
        raise PracticeInvalid("the practice is still assigned; remove its assignments first")
    audit.record(
        db,
        actor=actor,
        action="release_practice.deleted",
        resource_type="release_practice",
        resource_id=str(practice.id),
        details={"name": practice.name, "preset": practice.preset, "settings": practice.settings},
    )
    db.delete(practice)


def assign(
    db: Session,
    instance: GitLabInstance,
    practice: ReleasePractice,
    target: Target,
    actor: str,
) -> ReleasePracticeAssignment:
    """Make ``practice`` the target's, replacing its practice. The caller commits."""
    replaced = db.scalars(
        select(ReleasePracticeAssignment).where(
            ReleasePracticeAssignment.instance_id == instance.id,
            ReleasePracticeAssignment.target_type == target.target_type,
            ReleasePracticeAssignment.target_id == target.target_id,
        )
    ).first()
    replaced_name = (
        db.get_one(ReleasePractice, replaced.practice_id).name if replaced is not None else None
    )
    if replaced is not None:
        db.delete(replaced)
        db.flush()
    assignment = ReleasePracticeAssignment(
        workspace_id=instance.workspace_id,
        instance_id=instance.id,
        practice_id=practice.id,
        target_type=target.target_type,
        target_id=target.target_id,
        assigned_by=actor,
    )
    db.add(assignment)
    db.flush()
    audit.record(
        db,
        actor=actor,
        action="release_practice.assigned",
        resource_type="release_practice_assignment",
        resource_id=str(assignment.id),
        details={
            "connection_id": instance.id,
            "name": practice.name,
            "target_type": target.target_type,
            "target_id": target.target_id,
            "target_path": target.path,
            "replaced": (
                {"assignment_id": replaced.id, "name": replaced_name}
                if replaced is not None
                else None
            ),
        },
    )
    return assignment


def unassign(db: Session, assignment: ReleasePracticeAssignment, actor: str) -> None:
    practice = db.get_one(ReleasePractice, assignment.practice_id)
    audit.record(
        db,
        actor=actor,
        action="release_practice.unassigned",
        resource_type="release_practice_assignment",
        resource_id=str(assignment.id),
        details={
            "connection_id": assignment.instance_id,
            "name": practice.name,
            "target_type": assignment.target_type,
            "target_id": assignment.target_id,
        },
    )
    db.delete(assignment)


# Where practices apply, and what releases show ---------------------------------------------------


class Observed(BaseModel):
    """A project's production deployments in the window, by what they deployed."""

    deployments: int
    by_kind: dict[str, int]
    # Deployments of a ref the practice expects, of another, and whose ref kind is not known.
    expected: int
    other: int
    unknown: int
    # Refs deployed that the practice does not expect, newest first.
    other_refs: list[str]
    last_deployed_at: datetime | None


class ProjectPractice(BaseModel):
    project_id: int
    gitlab_id: int
    path: str
    default_branch: str | None
    practice: str | None
    preset: str | None
    decided_by: Target | None
    status: Status
    observed: Observed


@dataclass(frozen=True)
class Deployment:
    environment: str | None
    ref: str | None
    tag: bool | None
    finished_at: datetime


def ref_kind(deployment: Deployment, default_branch: str | None) -> RefKind:
    if deployment.tag is True:
        return "tag"
    if deployment.tag is None or deployment.ref is None or default_branch is None:
        return "unknown"
    return "default_branch" if deployment.ref == default_branch else "branch"


def expected(settings: PracticeSettings, kind: RefKind, ref: str | None) -> bool | None:
    """Whether the practice expects a deployment of this ref; None when its kind is unknown."""
    if kind == "unknown":
        return None
    if settings.production_from == "default_branch":
        return kind == "default_branch"
    wanted: RefKind = "tag" if settings.production_from == "tag" else "branch"
    if kind == "default_branch" and wanted == "branch":
        kind = "branch"  # a branch pattern may name the default branch
    return (
        kind == wanted
        and ref is not None
        and any(matches(pattern, ref) for pattern in settings.ref_patterns)
    )


def observe(
    settings: PracticeSettings | None,
    deployments: Iterable[Deployment],
    default_branch: str | None,
) -> tuple[Status, Observed]:
    """What a project's deployments show against its practice."""
    rows = sorted(deployments, key=lambda d: d.finished_at, reverse=True)
    if settings is not None:
        rows = [d for d in rows if d.environment in settings.environments]
    kinds = Counter(ref_kind(d, default_branch) for d in rows)
    judged = [
        (d, expected(settings, ref_kind(d, default_branch), d.ref)) if settings else (d, None)
        for d in rows
    ]
    other_refs: list[str] = []
    for d, verdict in judged:
        if verdict is False and d.ref is not None and d.ref not in other_refs:
            other_refs.append(d.ref)
    observed = Observed(
        deployments=len(rows),
        by_kind=dict(sorted(kinds.items())),
        expected=sum(1 for _, v in judged if v is True),
        other=sum(1 for _, v in judged if v is False),
        unknown=sum(1 for _, v in judged if v is None) if settings else 0,
        other_refs=other_refs[:MAX_REFS],
        last_deployed_at=rows[0].finished_at if rows else None,
    )
    if settings is None:
        return "unassigned", observed
    if not rows:
        return "no_releases", observed
    if observed.other:
        return "differs", observed
    if observed.unknown:
        return "unknown", observed
    return "follows", observed


def _deployments(db: Session, instance_id: int, since: datetime) -> dict[int, list[Deployment]]:
    rows = db.execute(
        select(ChangeRecord.project_id, ChangeRecord.changed_at, Observation.payload)
        .join(Observation, Observation.id == ChangeRecord.observation_id)
        .where(
            ChangeRecord.instance_id == instance_id,
            ChangeRecord.kind == "deployment",
            ChangeRecord.changed_at >= since,
        )
    ).all()
    found: dict[int, list[Deployment]] = {}
    for project_id, changed_at, payload in rows:
        body = payload if isinstance(payload, dict) else {}
        tag = body.get("tag")
        found.setdefault(project_id, []).append(
            Deployment(
                environment=body.get("environment"),
                ref=body.get("ref"),
                tag=tag if isinstance(tag, bool) else None,
                finished_at=changed_at,
            )
        )
    return found


def overview(db: Session, instance: GitLabInstance) -> list[ProjectPractice]:
    """Every active project in monitoring scope with its practice, who decided it, and what its
    production deployments in the last ``WINDOW_DAYS`` show."""
    practices = {p.id: p for p in db.scalars(select(ReleasePractice))}
    assigned = {
        (a.target_type, a.target_id): a
        for a in db.scalars(
            select(ReleasePracticeAssignment).where(
                ReleasePracticeAssignment.instance_id == instance.id
            )
        )
    }
    groups = {
        g.gitlab_id: g
        for g in db.scalars(select(GitLabGroup).where(GitLabGroup.instance_id == instance.id))
    }
    scope = coverage(db, instance.id)
    projects = db.scalars(
        select(GitLabProject)
        .where(
            GitLabProject.instance_id == instance.id,
            GitLabProject.state == "active",
            scope.project_included(),
        )
        .order_by(GitLabProject.path_with_namespace)
    ).all()
    now: datetime = db.execute(select(func.now())).scalar_one()
    deployed = _deployments(db, instance.id, now - timedelta(days=WINDOW_DAYS))
    root = Target(target_type="instance", target_id=instance.id, path=instance.base_url, state=None)
    out = []
    for project in projects:
        path = [
            Target(
                target_type="project",
                target_id=project.gitlab_id,
                path=project.path_with_namespace,
                state=project.state,
            )
        ]
        gitlab_id = project.namespace_gitlab_id if project.namespace_kind != "user" else None
        seen: set[int] = set()
        while gitlab_id is not None and gitlab_id not in seen:
            seen.add(gitlab_id)
            group = groups.get(gitlab_id)
            if group is None:
                break
            path.append(
                Target(
                    target_type="group",
                    target_id=group.gitlab_id,
                    path=group.full_path,
                    state=group.state,
                )
            )
            gitlab_id = group.parent_gitlab_id
        path.append(root)
        decided = next(
            (
                (level, assigned[(level.target_type, level.target_id)])
                for level in path
                if (level.target_type, level.target_id) in assigned
            ),
            None,
        )
        practice = practices.get(decided[1].practice_id) if decided else None
        settings = PracticeSettings.model_validate(practice.settings) if practice else None
        status, observed = observe(settings, deployed.get(project.id, []), project.default_branch)
        out.append(
            ProjectPractice(
                project_id=project.id,
                gitlab_id=project.gitlab_id,
                path=project.path_with_namespace,
                default_branch=project.default_branch,
                practice=practice.name if practice else None,
                preset=practice.preset if practice else None,
                decided_by=decided[0] if decided else None,
                status=status,
                observed=observed,
            )
        )
    return out
