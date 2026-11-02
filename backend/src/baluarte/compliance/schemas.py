"""Request and response bodies of the operating profile, shared by setup and the admin API."""

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from baluarte.compliance.catalogue import ControlKind, FrameworkId, Op
from baluarte.compliance.operations import SettingSource
from baluarte.compliance.posture import Status
from baluarte.db.models import MAX_STATEMENT, MIN_STATEMENT

Statement = Annotated[str, Field(min_length=MIN_STATEMENT, max_length=MAX_STATEMENT)]
Note = Annotated[str, Field(max_length=MAX_STATEMENT)]


class FrameworkOut(BaseModel):
    id: FrameworkId
    name: str


class RequirementOut(BaseModel):
    op: Op
    value: int | bool


class ControlOut(BaseModel):
    id: str
    title: str
    area: str
    kind: ControlKind
    description: str
    how_to: str
    mappings: dict[FrameworkId, list[str]]
    setting: str | None
    requirements: dict[FrameworkId, RequirementOut] | None
    attestation_days: int | None


class Catalogue(BaseModel):
    frameworks: list[FrameworkOut]
    controls: list[ControlOut]


class OperatingSettings(BaseModel):
    audit_retention_days: int
    evidence_retention_days: int
    job_retention_days: int
    session_idle_minutes: int
    session_lifetime_minutes: int
    require_review: bool
    legal_hold: bool


class MinimumOut(BaseModel):
    op: Op
    value: int | bool
    # The selected frameworks that set this, the strictest, value.
    frameworks: list[FrameworkId]


class ProfileOut(BaseModel):
    frameworks: list[FrameworkId]
    # False until a profile has been saved; the defaults are shown meanwhile.
    configured: bool
    settings: OperatingSettings
    sources: dict[str, SettingSource]
    minimums: dict[str, MinimumOut]
    updated_by: str | None
    updated_at: datetime | None


class ProfileUpdate(BaseModel):
    frameworks: list[FrameworkId] = Field(max_length=3)
    # Checked by the profile, so a bound or a minimum is reported with its reason.
    settings: dict[str, Any]


class ExceptionRef(BaseModel):
    id: int
    expires_on: date


class AttestationRef(BaseModel):
    id: int
    attested_by: str
    attested_at: datetime
    expires_at: datetime


class ControlPostureOut(BaseModel):
    id: str
    title: str
    status: Status
    detail: str
    evidence: dict[str, Any]
    exception: ExceptionRef | None
    attestation: AttestationRef | None


class PostureSummary(BaseModel):
    met: int
    not_met: int
    needs_attestation: int
    excepted: int
    not_applicable: int


class PostureOut(BaseModel):
    evaluated_at: datetime
    frameworks: list[FrameworkId]
    configured: bool
    summary: PostureSummary
    controls: list[ControlPostureOut]


class ComplianceExceptionOut(BaseModel):
    id: int
    control_id: str
    justification: str
    # The first UTC day it is no longer in force.
    expires_on: date
    state: Literal["proposed", "approved", "rejected", "revoked"]
    in_force: bool
    requested_by: str
    requested_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    self_approved: bool
    note: str | None


class ComplianceExceptionIn(BaseModel):
    control_id: str = Field(max_length=64)
    justification: Statement
    expires_on: date


class DecisionIn(BaseModel):
    note: Note | None = None


class AttestationOut(BaseModel):
    id: int
    control_id: str
    statement: str
    attested_by: str
    attested_at: datetime
    expires_at: datetime


class AttestationIn(BaseModel):
    control_id: str = Field(max_length=64)
    statement: Statement
