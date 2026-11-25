"""Template versions and assignments (roadmap M14.1, docs/CONFIGURATION_TEMPLATES.md).

Versions are immutable, as for baselines: saving changed text under a name adds the next version,
identical text returns the latest. ``extends`` must name a saved version; the chain is at most
``MAX_CHAIN`` templates long and, counting what it inherits, sets at least one setting.

An assignment pins a version to a connection, group or project that is active in the inventory.
A target holds one template; assigning another replaces it. Overrides need a reason.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.db.models import ConfigurationTemplate, GitLabInstance, TemplateAssignment
from baluarte.policy.assignments import Target
from baluarte.templates.loader import (
    ParsedTemplate,
    TemplateError,
    TemplateInvalid,
    parse_settings,
    parse_template,
)
from baluarte.templates.schema import MAX_CHAIN, Settings, flatten, split_extends
from baluarte.workspaces import current_workspace_id


@dataclass(frozen=True)
class Saved:
    template: ConfigurationTemplate
    created: bool


def latest_version(db: Session, name: str) -> ConfigurationTemplate | None:
    return db.scalars(
        select(ConfigurationTemplate)
        .where(ConfigurationTemplate.name == name)
        .order_by(ConfigurationTemplate.version.desc())
        .limit(1)
    ).first()


def find_version(db: Session, name: str, version: int) -> ConfigurationTemplate | None:
    return db.scalars(
        select(ConfigurationTemplate).where(
            ConfigurationTemplate.name == name, ConfigurationTemplate.version == version
        )
    ).first()


def parsed(template: ConfigurationTemplate) -> ParsedTemplate:
    return parse_template(template.source_yaml)


def chain(db: Session, template: ConfigurationTemplate) -> list[ConfigurationTemplate]:
    """The template, then the versions it extends, nearest first."""
    found = [template]
    current = template
    while current.extends_id is not None and len(found) <= MAX_CHAIN:
        parent = db.get(ConfigurationTemplate, current.extends_id)
        if parent is None:
            break
        found.append(parent)
        current = parent
    return found


def chain_settings(db: Session, template: ConfigurationTemplate) -> dict[str, Any]:
    """Every setting the template sets or inherits, the template's own over its parents'."""
    merged: dict[str, Any] = {}
    for member in chain(db, template):
        for key, value in flatten(parsed(member).document.spec.settings).items():
            merged.setdefault(key, value)
    return merged


def save_version(db: Session, source: str, actor: str) -> Saved:
    """Validate ``source`` and store it as the next version of its template. The caller commits.

    Raises TemplateInvalid when the document, or what it extends, is not valid.
    """
    parsed_source = parse_template(source)
    name = parsed_source.name
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext("template:" + name))))
    latest = latest_version(db, name)
    if latest is not None and latest.yaml_digest == parsed_source.digest:
        return Saved(template=latest, created=False)
    parent: ConfigurationTemplate | None = None
    extends = parsed_source.document.spec.extends
    if extends is not None:
        parent_name, parent_version = split_extends(extends)
        if parent_name == name:
            raise TemplateInvalid(
                [TemplateError("spec.extends", "a template cannot extend itself")]
            )
        parent = find_version(db, parent_name, parent_version)
        if parent is None:
            raise TemplateInvalid(
                [TemplateError("spec.extends", "no saved template has that name and version")]
            )
        if len(chain(db, parent)) >= MAX_CHAIN:
            raise TemplateInvalid(
                [TemplateError("spec.extends", f"templates extend at most {MAX_CHAIN} deep")]
            )
    own = flatten(parsed_source.document.spec.settings)
    inherited = chain_settings(db, parent) if parent is not None else {}
    if not own and not inherited:
        raise TemplateInvalid(
            [TemplateError("spec.settings", "set at least one setting, here or by extends")]
        )
    template = ConfigurationTemplate(
        workspace_id=current_workspace_id(db),
        name=name,
        version=(latest.version if latest else 0) + 1,
        source_yaml=source,
        yaml_digest=parsed_source.digest,
        extends_id=parent.id if parent is not None else None,
        created_by=actor,
    )
    db.add(template)
    db.flush()
    audit.record(
        db,
        actor=actor,
        action="template.version_created",
        resource_type="configuration_template",
        resource_id=str(template.id),
        details={
            "name": name,
            "version": template.version,
            "yaml_digest": template.yaml_digest,
            "extends": extends,
        },
    )
    return Saved(template=template, created=True)


@dataclass(frozen=True)
class Assigned:
    assignment: TemplateAssignment
    replaced: TemplateAssignment | None


def validated_overrides(data: dict[str, Any] | None, reason: str | None) -> dict[str, Any] | None:
    """Overrides in the template's ``settings`` format; None when there are none."""
    if not data:
        if reason:
            raise TemplateInvalid(
                [TemplateError("override_reason", "give a reason only with overrides")]
            )
        return None
    settings: Settings = parse_settings(data)
    if not flatten(settings):
        raise TemplateInvalid([TemplateError("overrides", "set at least one setting")])
    if reason is None or not 20 <= len(reason.strip()) <= 2000:
        raise TemplateInvalid(
            [TemplateError("override_reason", "overrides need a reason of 20 to 2,000 characters")]
        )
    return settings.model_dump(exclude_none=True)


def assign(
    db: Session,
    instance: GitLabInstance,
    template: ConfigurationTemplate,
    target: Target,
    overrides: dict[str, Any] | None,
    reason: str | None,
    actor: str,
    *,
    auto_remediate: bool = False,
) -> Assigned:
    """Pin ``template`` to ``target``, replacing the target's template. The caller commits."""
    cleaned = validated_overrides(overrides, reason)
    replaced = db.scalars(
        select(TemplateAssignment).where(
            TemplateAssignment.instance_id == instance.id,
            TemplateAssignment.target_type == target.target_type,
            TemplateAssignment.target_id == target.target_id,
        )
    ).first()
    replaced_template = (
        db.get(ConfigurationTemplate, replaced.template_id) if replaced is not None else None
    )
    if replaced is not None:
        db.delete(replaced)
        db.flush()
    assignment = TemplateAssignment(
        workspace_id=instance.workspace_id,
        instance_id=instance.id,
        template_id=template.id,
        target_type=target.target_type,
        target_id=target.target_id,
        overrides=cleaned,
        override_reason=reason.strip() if cleaned is not None and reason else None,
        auto_remediate=auto_remediate,
        assigned_by=actor,
    )
    db.add(assignment)
    db.flush()
    audit.record(
        db,
        actor=actor,
        action="template.assigned",
        resource_type="template_assignment",
        resource_id=str(assignment.id),
        details={
            "connection_id": instance.id,
            "name": template.name,
            "version": template.version,
            "target_type": target.target_type,
            "target_id": target.target_id,
            "target_path": target.path,
            "overrides": cleaned,
            "override_reason": assignment.override_reason,
            "auto_remediate": auto_remediate,
            "replaced": (
                {
                    "assignment_id": replaced.id,
                    "name": replaced_template.name,
                    "version": replaced_template.version,
                }
                if replaced is not None and replaced_template is not None
                else None
            ),
        },
    )
    return Assigned(assignment=assignment, replaced=replaced)


def unassign(db: Session, assignment: TemplateAssignment, actor: str) -> None:
    template = db.get_one(ConfigurationTemplate, assignment.template_id)
    audit.record(
        db,
        actor=actor,
        action="template.unassigned",
        resource_type="template_assignment",
        resource_id=str(assignment.id),
        details={
            "connection_id": assignment.instance_id,
            "name": template.name,
            "version": template.version,
            "target_type": assignment.target_type,
            "target_id": assignment.target_id,
        },
    )
    db.delete(assignment)
