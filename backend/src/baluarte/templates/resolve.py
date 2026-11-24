"""A project's effective template settings (roadmap M14.1, docs/CONFIGURATION_TEMPLATES.md).

The project's path is walked nearest first, as for baselines: the project, its groups up to the
top level, then the connection. For each setting key the nearest assignment that sets it decides,
through its overrides first, then its template, then the versions that template extends. Every
other value found on the way is listed as replaced, so precedence can be read back.
"""

from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from baluarte.db.models import (
    ConfigurationTemplate,
    GitLabInstance,
    GitLabProject,
    TemplateAssignment,
)
from baluarte.policy.assignments import Gap, Target, group_ancestry
from baluarte.templates.schema import RULE_TYPE, SETTING_KEYS, SUPPORT, Support, flatten
from baluarte.templates.store import chain, parsed

Origin = Literal["override", "template", "extends"]


class Source(BaseModel):
    value: Any
    origin: Origin
    # The template version the value came from; for an override, the assigned one.
    template: str
    version: int
    assignment_id: int
    assigned_to: Target


class EffectiveSetting(BaseModel):
    key: str
    value: Any
    rule_type: str
    support: Support
    support_note: str | None
    decided_by: Source
    # Values farther away, or lower in the same assignment, that this one replaced.
    replaced: list[Source]


class AssignmentOnPath(BaseModel):
    assignment_id: int
    template: str
    version: int
    assigned_to: Target
    override_reason: str | None
    overrides: dict[str, Any] | None


class Effective(BaseModel):
    target: Target
    provider: str
    ancestry: Literal["complete", "incomplete"]
    gaps: list[Gap]
    path: list[Target]
    assignments: list[AssignmentOnPath]
    settings: list[EffectiveSetting]


def _sources(
    db: Session, assignment: TemplateAssignment, template: ConfigurationTemplate, at: Target
) -> list[tuple[str, Source]]:
    """What one assignment says, in its own precedence: overrides, the template, its parents."""
    found: list[tuple[str, Source]] = []
    for key, value in flatten(assignment.overrides).items():
        found.append(
            (
                key,
                Source(
                    value=value,
                    origin="override",
                    template=template.name,
                    version=template.version,
                    assignment_id=assignment.id,
                    assigned_to=at,
                ),
            )
        )
    for depth, member in enumerate(chain(db, template)):
        for key, value in flatten(parsed(member).document.spec.settings).items():
            found.append(
                (
                    key,
                    Source(
                        value=value,
                        origin="template" if depth == 0 else "extends",
                        template=member.name,
                        version=member.version,
                        assignment_id=assignment.id,
                        assigned_to=at,
                    ),
                )
            )
    return found


def effective_settings(db: Session, instance: GitLabInstance, project: GitLabProject) -> Effective:
    target = Target(
        target_type="project",
        target_id=project.gitlab_id,
        path=project.path_with_namespace,
        state=project.state,
    )
    start = None if project.namespace_kind == "user" else project.namespace_gitlab_id
    groups, gaps = group_ancestry(db, instance.id, start)
    root = Target(target_type="instance", target_id=instance.id, path=instance.base_url, state=None)
    path = [target, *groups, root]
    group_ids = [level.target_id for level in groups]
    rows = db.execute(
        select(TemplateAssignment, ConfigurationTemplate)
        .join(ConfigurationTemplate, ConfigurationTemplate.id == TemplateAssignment.template_id)
        .where(
            TemplateAssignment.instance_id == instance.id,
            or_(
                (TemplateAssignment.target_type == "project")
                & (TemplateAssignment.target_id == project.gitlab_id),
                (TemplateAssignment.target_type == "group")
                & TemplateAssignment.target_id.in_(group_ids),
                (TemplateAssignment.target_type == "instance")
                & (TemplateAssignment.target_id == instance.id),
            ),
        )
    ).all()
    at_level = {(row[0].target_type, row[0].target_id): (row[0], row[1]) for row in rows}
    by_key: dict[str, list[Source]] = {}
    on_path: list[AssignmentOnPath] = []
    for level in path:
        found = at_level.get((level.target_type, level.target_id))
        if found is None:
            continue
        assignment, template = found
        on_path.append(
            AssignmentOnPath(
                assignment_id=assignment.id,
                template=template.name,
                version=template.version,
                assigned_to=level,
                override_reason=assignment.override_reason,
                overrides=assignment.overrides,
            )
        )
        for key, source in _sources(db, assignment, template, level):
            by_key.setdefault(key, []).append(source)
    provider = instance.provider
    settings = []
    for key in SETTING_KEYS:
        sources = by_key.get(key)
        if not sources:
            continue
        support, note = SUPPORT[key].get(provider, ("unsupported", "Not judged on this provider."))
        settings.append(
            EffectiveSetting(
                key=key,
                value=sources[0].value,
                rule_type=RULE_TYPE[key],
                support=support,
                support_note=note,
                decided_by=sources[0],
                replaced=sources[1:],
            )
        )
    return Effective(
        target=target,
        provider=provider,
        ancestry="incomplete" if gaps else "complete",
        gaps=gaps,
        path=path,
        assignments=on_path,
        settings=settings,
    )
