"""Configuration templates, their assignments and each project's effective settings (M14.1).

Reading needs ``findings:read``, like baselines; saving, assigning and unassigning need
``policy:write`` and the CSRF header, and are audited (docs/CONFIGURATION_TEMPLATES.md#api).
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import (
    ConfigurationTemplate,
    GitLabInstance,
    GitLabProject,
    TemplateAssignment,
)
from baluarte.policy.assignments import Target, TargetType, find_target
from baluarte.policy.loader import MAX_SOURCE_BYTES
from baluarte.policy.schema import NAME_PATTERN
from baluarte.templates.loader import TemplateInvalid
from baluarte.templates.resolve import Effective, effective_settings
from baluarte.templates.schema import SETTING_KEYS, SUPPORT, flatten
from baluarte.templates.store import (
    assign,
    chain,
    chain_settings,
    find_version,
    parsed,
    save_version,
    unassign,
)
from baluarte.workspaces import current_connection

router = APIRouter(tags=["templates"])

Name = Annotated[str, Path(pattern=NAME_PATTERN, max_length=63)]
Reader = Annotated[Principal, Depends(require(Permission.FINDINGS_READ))]
Writer = Annotated[Principal, Depends(require(Permission.POLICY_WRITE))]


class TemplateSource(BaseModel):
    source_yaml: Annotated[str, Field(min_length=1, max_length=MAX_SOURCE_BYTES)]


class VersionSummary(BaseModel):
    name: str
    version: int
    yaml_digest: str
    extends: str | None
    created_by: str
    created_at: datetime


class TemplateOut(BaseModel):
    name: str
    description: str | None
    versions: list[VersionSummary]


class TemplateList(BaseModel):
    templates: list[TemplateOut]


class VersionOut(VersionSummary):
    description: str | None
    source_yaml: str
    # The template's own settings, then everything it sets or inherits, one value per key.
    settings: dict[str, Any]
    resolved_settings: dict[str, Any]
    # The versions it extends, nearest first.
    chain: list[str]


class SupportOut(BaseModel):
    key: str
    gitlab: str
    gitlab_note: str | None
    github: str
    github_note: str | None


def _summary(db: Session, template: ConfigurationTemplate) -> VersionSummary:
    parent = db.get(ConfigurationTemplate, template.extends_id) if template.extends_id else None
    return VersionSummary(
        name=template.name,
        version=template.version,
        yaml_digest=template.yaml_digest,
        extends=f"{parent.name}@{parent.version}" if parent else None,
        created_by=template.created_by,
        created_at=template.created_at,
    )


def _version_out(db: Session, template: ConfigurationTemplate) -> VersionOut:
    document = parsed(template).document
    return VersionOut(
        **_summary(db, template).model_dump(),
        description=document.metadata.description,
        source_yaml=template.source_yaml,
        settings=flatten(document.spec.settings),
        resolved_settings=chain_settings(db, template),
        chain=[f"{member.name}@{member.version}" for member in chain(db, template)[1:]],
    )


def _invalid(invalid: TemplateInvalid) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        {
            "message": "; ".join(f"{e.path}: {e.message}" for e in invalid.errors),
            "errors": [{"path": e.path, "message": e.message} for e in invalid.errors],
        },
    )


@router.get(
    "/templates",
    response_model=TemplateList,
    summary="Configuration templates and their versions",
    description="Every template of the workspace, its versions newest first.",
)
def list_templates(_: Reader, db: Annotated[Session, Depends(database)]) -> TemplateList:
    rows = db.scalars(
        select(ConfigurationTemplate).order_by(
            ConfigurationTemplate.name, ConfigurationTemplate.version.desc()
        )
    ).all()
    grouped: dict[str, list[ConfigurationTemplate]] = {}
    for row in rows:
        grouped.setdefault(row.name, []).append(row)
    return TemplateList(
        templates=[
            TemplateOut(
                name=name,
                description=parsed(versions[0]).document.metadata.description,
                versions=[_summary(db, version) for version in versions],
            )
            for name, versions in grouped.items()
        ]
    )


@router.get(
    "/templates/settings",
    response_model=list[SupportOut],
    summary="The settings a template can hold, and where",
    description="Each setting with its support on GitLab and GitHub: `supported`, "
    "`unsupported` (no equivalent) or `plan` (depends on the plan), with why.",
)
def template_settings(_: Reader) -> list[SupportOut]:
    return [
        SupportOut(
            key=key,
            gitlab=SUPPORT[key]["gitlab"][0],
            gitlab_note=SUPPORT[key]["gitlab"][1],
            github=SUPPORT[key]["github"][0],
            github_note=SUPPORT[key]["github"][1],
        )
        for key in SETTING_KEYS
    ]


@router.get(
    "/templates/{name}/versions/{version}",
    response_model=VersionOut,
    summary="One template version",
    responses={404: {"description": "No such template version"}},
)
def read_version(
    name: Name,
    version: Annotated[int, Path(ge=1)],
    _: Reader,
    db: Annotated[Session, Depends(database)],
) -> VersionOut:
    template = find_version(db, name, version)
    if template is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such template version")
    return _version_out(db, template)


@router.post(
    "/templates",
    response_model=VersionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Save a template as the next version of its name",
    description="Named by `metadata.name`. 201 with a new version; 200 with the existing one "
    "when identical to the latest; 422 with `detail.errors` when invalid, or when `extends` "
    "names no saved version. Requires the `X-CSRF-Token` header. Audited.",
    responses={
        200: {"model": VersionOut, "description": "Identical to the latest version"},
        422: {"description": "Invalid template (`detail.errors`)"},
    },
)
def create_version(
    body: TemplateSource, principal: Writer, db: Annotated[Session, Depends(database)]
) -> Any:
    try:
        saved = save_version(db, body.source_yaml, principal.actor)
    except TemplateInvalid as invalid:
        raise _invalid(invalid) from None
    db.commit()
    out = _version_out(db, saved.template)
    if not saved.created:
        return JSONResponse(out.model_dump(mode="json"), status_code=status.HTTP_200_OK)
    return out


class AssignmentIn(BaseModel):
    connection_id: Annotated[
        int | None, Field(gt=0, description="Omitted for the connection the request works on")
    ] = None
    name: Annotated[str, Field(pattern=NAME_PATTERN, max_length=63)]
    version: Annotated[int, Field(ge=1)]
    target_type: TargetType
    target_id: Annotated[
        int | None,
        Field(gt=0, description="Provider ID of the group or project; omitted for the connection"),
    ] = None
    overrides: dict[str, Any] | None = None
    override_reason: Annotated[str, Field(max_length=2000)] | None = None

    @model_validator(mode="after")
    def _target(self) -> "AssignmentIn":
        if (self.target_type == "instance") != (self.target_id is None):
            raise ValueError("target_id is required for a group or project, and omitted otherwise")
        return self


class AssignmentOut(BaseModel):
    id: int
    connection_id: int
    name: str
    version: int
    target: Target
    overrides: dict[str, Any] | None
    override_reason: str | None
    assigned_by: str
    created_at: datetime


class AssignmentList(BaseModel):
    assignments: list[AssignmentOut]


def _connection(db: Session, connection_id: int | None) -> GitLabInstance:
    if connection_id is None:
        current = current_connection(db)
        if current is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No connection is configured")
        return current
    found = db.scalars(
        select(GitLabInstance).where(
            GitLabInstance.id == connection_id, GitLabInstance.removed_at.is_(None)
        )
    ).first()
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such connection")
    return found


def _assignment_out(db: Session, row: TemplateAssignment) -> AssignmentOut:
    instance = db.get_one(GitLabInstance, row.instance_id)
    template = db.get_one(ConfigurationTemplate, row.template_id)
    target = find_target(db, instance, row.target_type, row.target_id)  # type: ignore[arg-type]
    return AssignmentOut(
        id=row.id,
        connection_id=row.instance_id,
        name=template.name,
        version=template.version,
        target=target
        or Target(
            target_type=row.target_type,  # type: ignore[arg-type]
            target_id=row.target_id,
            path="(not in the inventory)",
            state=None,
        ),
        overrides=row.overrides,
        override_reason=row.override_reason,
        assigned_by=row.assigned_by,
        created_at=row.created_at,
    )


@router.get(
    "/template-assignments",
    response_model=AssignmentList,
    summary="Template assignments",
    description="Every template assignment of the workspace, with its target's inventory state.",
)
def list_assignments(_: Reader, db: Annotated[Session, Depends(database)]) -> AssignmentList:
    rows = db.scalars(select(TemplateAssignment).order_by(TemplateAssignment.id)).all()
    return AssignmentList(assignments=[_assignment_out(db, row) for row in rows])


@router.post(
    "/template-assignments",
    response_model=AssignmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Assign a template version to a connection, group or project",
    description="Replaces the target's template. Overrides use the template's `settings` "
    "format and need `override_reason` (20 to 2,000 characters). Requires the `X-CSRF-Token` "
    "header. Audited.",
    responses={
        404: {"description": "No such connection, template version or target"},
        409: {"description": "The target is not active in the inventory"},
        422: {"description": "Invalid overrides or reason (`detail.errors`)"},
    },
)
def create_assignment(
    body: AssignmentIn, principal: Writer, db: Annotated[Session, Depends(database)]
) -> AssignmentOut:
    instance = _connection(db, body.connection_id)
    template = find_version(db, body.name, body.version)
    if template is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such template version")
    target_id = instance.id if body.target_type == "instance" else body.target_id
    target = find_target(db, instance, body.target_type, int(target_id or 0))
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The target is not in the inventory")
    if target.state not in (None, "active"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"The target is {target.state}")
    try:
        assigned = assign(
            db, instance, template, target, body.overrides, body.override_reason, principal.actor
        )
    except TemplateInvalid as invalid:
        raise _invalid(invalid) from None
    db.commit()
    return _assignment_out(db, assigned.assignment)


@router.delete(
    "/template-assignments/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a template assignment",
    description="Requires the `X-CSRF-Token` header. Audited.",
    responses={404: {"description": "No such assignment"}},
)
def remove_assignment(
    assignment_id: Annotated[int, Path(ge=1)],
    principal: Writer,
    db: Annotated[Session, Depends(database)],
) -> Response:
    row = db.scalars(
        select(TemplateAssignment).where(TemplateAssignment.id == assignment_id)
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such assignment")
    unassign(db, row, principal.actor)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/templates/effective/{project_id}",
    response_model=Effective,
    summary="A project's effective template settings",
    description="For each setting, the value in force, which template, version and assignment "
    "decided it, what it replaced, and whether the project's provider can hold it. "
    "`project_id` is Baluarte's project ID.",
    responses={404: {"description": "No such project in this workspace"}},
)
def project_settings(
    project_id: Annotated[int, Path(ge=1)], _: Reader, db: Annotated[Session, Depends(database)]
) -> Effective:
    project = db.scalars(select(GitLabProject).where(GitLabProject.id == project_id)).first()
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project")
    instance = db.get_one(GitLabInstance, project.instance_id)
    return effective_settings(db, instance, project)
