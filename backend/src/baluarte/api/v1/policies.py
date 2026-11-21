"""Policy baselines: validate, save immutable versions, and read them back.

Reading needs ``findings:read`` (every role): a baseline explains the findings it produces, and
auditors must be able to see exactly which version was in force. Validating and saving need
``policy:write`` (admin, analyst). While review is required (``baluarte.policy.changes``) saving
proposes the version instead, and it becomes a version once someone else approves it.
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.api.v1.policy_changes import PolicyChangeDetail, Review, change_detail, reviews
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import GitLabInstance, Policy
from baluarte.policy import coverage as rule_coverage
from baluarte.policy.changes import Actor, ChangeConflict, propose_version
from baluarte.policy.loader import MAX_SOURCE_BYTES, PolicyInvalid, parse_policy
from baluarte.policy.schema import NAME_PATTERN
from baluarte.workspaces import current_connection

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
    # The change that put this version in force; null only if none is recorded.
    review: Review | None = None


class VersionOut(VersionSummary):
    source_yaml: str
    document: dict[str, Any]


class PolicySummary(BaseModel):
    name: str
    latest_version: int
    versions: int
    latest_effective_at: datetime


class BaselineRef(BaseModel):
    name: str
    version: int


class RuleTypeCoverage(BaseModel):
    rule_type: str
    # An applied baseline version has a rule of this type.
    in_force: bool
    baselines: list[BaselineRef]
    # From detected capabilities: null when not known (never detected, or not provable).
    supported: bool | None
    capability: str | None


class UncoveredControl(BaseModel):
    framework: str
    control_id: str
    title: str
    area: str
    rule_types: list[str]
    status_change: Literal["not_applicable", "manual"] | None


class BaselineRules(BaseModel):
    name: str
    latest_version: int
    applied: bool
    rule_types: list[str]


class CoverageOut(BaseModel):
    rule_types: list[RuleTypeCoverage]
    controls: list[UncoveredControl]
    baselines: list[BaselineRules]
    label_taxonomy_version: int | None
    capabilities_checked_at: datetime | None


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


def _summary(policy: Policy, found: dict[int, Review]) -> VersionSummary:
    summary = VersionSummary.model_validate(policy, from_attributes=True)
    summary.review = found.get(policy.id)
    return summary


def _version_out(db: Session, policy: Policy) -> VersionOut:
    document = parse_policy(policy.source_yaml).document
    return VersionOut(
        **_summary(policy, reviews(db, "version", [policy.id])).model_dump(),
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
    "invalid. While review is required, 202 with the proposed change (`PolicyChangeDetail`): "
    "it becomes a version when someone else approves it; 409 when another version of the "
    "policy is already waiting. Requires the `X-CSRF-Token` header.",
    responses={
        200: {"model": VersionOut, "description": "Identical to the latest version"},
        202: {"model": PolicyChangeDetail, "description": "Proposed; waiting for review"},
        409: {"description": "Another version of this policy is waiting for review"},
        422: {"description": "Invalid baseline (`detail.errors`) or malformed request"},
    },
)
def create_version(
    body: PolicySource,
    principal: Annotated[Principal, Depends(require(Permission.POLICY_WRITE))],
    db: Annotated[Session, Depends(database)],
) -> Any:
    try:
        saved = propose_version(db, body.source_yaml, Actor(principal.user_id, principal.actor))
    except PolicyInvalid as invalid:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"errors": [e.model_dump() for e in _errors(invalid)]},
        ) from None
    except ChangeConflict as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
    if saved.policy is None and saved.change is not None:
        db.commit()
        change = change_detail(db, saved.change)
        return JSONResponse(change.model_dump(mode="json"), status_code=status.HTTP_202_ACCEPTED)
    if saved.created and saved.policy is not None and saved.change is not None:
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
                "change_id": saved.change.id,
                "self_approved": True,
            },
        )
    db.commit()
    if saved.policy is None:
        raise RuntimeError("a saved baseline has a version")
    out = _version_out(db, saved.policy)
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


def _instance(db: Session) -> GitLabInstance | None:
    return current_connection(db)


@router.get(
    "/coverage",
    response_model=CoverageOut,
    summary="Rule types in force, and the controls left without automated evidence",
    description="In force means used by a baseline version applied anywhere (proposed versions "
    "and assignments, and baselines not applied, do not count). `rule_types` lists every rule "
    "type with the applied baselines that use it and whether the instance supports it "
    "(`supported` is null when capabilities cannot tell). `controls` lists every control the "
    "mapping in force maps rule types to, none of which is in force: Baluarte collects no "
    "automated evidence for it. `baselines` gives each baseline's latest version and its rule "
    "types.",
)
def read_coverage(
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
) -> CoverageOut:
    found = rule_coverage.coverage(db, _instance(db))
    return CoverageOut.model_validate(found, from_attributes=True)


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
    found = reviews(db, "version", [row.id for row in rows])
    return [_summary(row, found) for row in rows]


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
    return _version_out(db, policy)
