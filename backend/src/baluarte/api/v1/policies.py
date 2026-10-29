"""Policy baselines: validate, save immutable versions, and read them back.

Reading needs ``findings:read`` (every role): a baseline explains the findings it produces, and
auditors must be able to see exactly which version was in force. Validating and saving need
``policy:write`` (admin, analyst).
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import Policy
from baluarte.policy.loader import MAX_SOURCE_BYTES, PolicyInvalid, parse_policy
from baluarte.policy.schema import NAME_PATTERN
from baluarte.policy.store import save_version

router = APIRouter(prefix="/policies", tags=["policies"])

Name = Annotated[str, Path(pattern=NAME_PATTERN, max_length=63)]


class PolicySource(BaseModel):
    source_yaml: Annotated[
        str,
        Field(
            min_length=1,
            max_length=MAX_SOURCE_BYTES,
            description="The baseline as YAML text. Stored and hashed exactly as sent.",
        ),
    ]


class ErrorOut(BaseModel):
    path: str
    message: str


class ValidationResult(BaseModel):
    valid: bool
    errors: list[ErrorOut]
    name: str | None = None
    yaml_digest: str | None = None
    rule_ids: list[str] = []


class VersionSummary(BaseModel):
    id: int
    name: str
    version: int
    yaml_digest: str
    effective_at: datetime
    created_at: datetime


class VersionOut(VersionSummary):
    source_yaml: str
    document: dict[str, Any]


class PolicySummary(BaseModel):
    name: str
    latest_version: int
    versions: int
    latest_effective_at: datetime


def _errors(invalid: PolicyInvalid) -> list[ErrorOut]:
    return [ErrorOut(path=e.path, message=e.message) for e in invalid.errors]


@router.post(
    "/validate",
    response_model=ValidationResult,
    summary="Validate a baseline without saving it",
    description="Always 200 when the request is well formed; read `valid` and `errors`. "
    "Requires the `X-CSRF-Token` header.",
)
def validate(
    body: PolicySource,
    _: Annotated[Principal, Depends(require(Permission.POLICY_WRITE))],
) -> ValidationResult:
    try:
        parsed = parse_policy(body.source_yaml)
    except PolicyInvalid as invalid:
        return ValidationResult(valid=False, errors=_errors(invalid))
    return ValidationResult(
        valid=True,
        errors=[],
        name=parsed.name,
        yaml_digest=parsed.digest,
        rule_ids=[rule.id for rule in parsed.document.spec.rules],
    )


def _version_out(policy: Policy) -> VersionOut:
    document = parse_policy(policy.source_yaml).document
    return VersionOut(
        **VersionSummary.model_validate(policy, from_attributes=True).model_dump(),
        source_yaml=policy.source_yaml,
        document=document.model_dump(mode="json"),
    )


@router.post(
    "",
    response_model=VersionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Save a baseline as the next version of its policy",
    description="The policy is named by `metadata.name`. 201 with a new version; 200 with the "
    "existing version when the source is identical to the latest one; 422 with `errors` when "
    "invalid. Requires the `X-CSRF-Token` header.",
    responses={
        200: {"model": VersionOut, "description": "Identical to the latest version"},
        422: {"description": "Invalid baseline (`detail.errors`) or malformed request"},
    },
)
def create_version(
    body: PolicySource,
    principal: Annotated[Principal, Depends(require(Permission.POLICY_WRITE))],
    db: Annotated[Session, Depends(database)],
) -> Any:
    try:
        saved = save_version(db, body.source_yaml)
    except PolicyInvalid as invalid:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"errors": [e.model_dump() for e in _errors(invalid)]},
        ) from None
    if saved.created:
        audit.record(
            db,
            actor=principal.actor,
            action="policy.version_created",
            resource_type="policy",
            resource_id=f"{saved.policy.name}@{saved.policy.version}",
            details={
                "name": saved.policy.name,
                "version": saved.policy.version,
                "yaml_digest": saved.policy.yaml_digest,
            },
        )
    db.commit()
    out = _version_out(saved.policy)
    if not saved.created:
        return JSONResponse(out.model_dump(mode="json"), status_code=status.HTTP_200_OK)
    return out


@router.get("", response_model=list[PolicySummary], summary="Policies and their latest version")
def list_policies(
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
) -> list[PolicySummary]:
    rows = db.execute(
        select(
            Policy.name,
            func.max(Policy.version),
            func.count(),
            func.max(Policy.effective_at),
        )
        .group_by(Policy.name)
        .order_by(Policy.name)
    ).all()
    return [
        PolicySummary(name=name, latest_version=latest, versions=count, latest_effective_at=at)
        for name, latest, count, at in rows
    ]


@router.get(
    "/{name}/versions",
    response_model=list[VersionSummary],
    summary="Versions of a policy, newest first",
    responses={404: {"description": "No policy with this name"}},
)
def list_versions(
    name: Name,
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[VersionSummary]:
    rows = db.scalars(
        select(Policy).where(Policy.name == name).order_by(Policy.version.desc()).limit(limit)
    ).all()
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No policy with this name")
    return [VersionSummary.model_validate(row, from_attributes=True) for row in rows]


@router.get(
    "/{name}/versions/{version}",
    response_model=VersionOut,
    summary="One policy version: exact source, digest and parsed document",
    responses={404: {"description": "No such version"}},
)
def read_version(
    name: Name,
    version: Annotated[int, Path(ge=1)],
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
) -> VersionOut:
    policy = db.scalars(
        select(Policy).where(Policy.name == name, Policy.version == version)
    ).first()
    if policy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such version")
    return _version_out(policy)
