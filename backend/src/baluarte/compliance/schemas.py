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
    backup_interval_hours: int


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


# Audit shipping --------------------------------------------------------------------------------

SecretState = Literal["configured", "not_set"]
ShippingType = Literal["syslog_tls", "elasticsearch"]
Pem = Annotated[str, Field(max_length=65536)]


class SyslogIn(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=6514, ge=1, le=65535)
    server_name: str | None = Field(default=None, max_length=253)
    ca_pem: Pem | None = None


class ElasticsearchIn(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    index: str = Field(default="baluarte-audit", min_length=1, max_length=255)
    auth: Literal["api_key", "basic"] = "api_key"
    username: str | None = Field(default=None, max_length=255)
    ca_pem: Pem | None = None


class AuditShippingUpdate(BaseModel):
    enabled: bool
    type: ShippingType
    syslog: SyslogIn | None = None
    elasticsearch: ElasticsearchIn | None = None
    # Null keeps the stored value; it must be entered again when the URL changes.
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)
    password: str | None = Field(default=None, min_length=1, max_length=4096)


class SyslogOut(BaseModel):
    host: str
    port: int
    server_name: str | None
    ca_pem: str | None


class ElasticsearchOut(BaseModel):
    url: str
    index: str
    auth: Literal["api_key", "basic"]
    username: str | None
    ca_pem: str | None


class AuditShippingConfig(BaseModel):
    """Always complete: before anything is saved, shipping is off with the defaults."""

    configured: bool
    enabled: bool
    type: ShippingType
    syslog: SyslogOut
    elasticsearch: ElasticsearchOut
    api_key: SecretState
    password: SecretState
    updated_by: str | None
    updated_at: datetime | None


class AuditShippingTestResult(BaseModel):
    ok: bool
    detail: str


class AuditShippingStatus(BaseModel):
    enabled: bool
    type: ShippingType | None
    destination: str | None
    latest_event_id: int
    last_shipped_id: int
    lag_events: int
    oldest_unshipped_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    consecutive_failures: int


# Backups and services --------------------------------------------------------------------------


class BackupRunOut(BaseModel):
    id: int
    started_at: datetime
    finished_at: datetime
    status: Literal["succeeded", "failed"]
    file_name: str | None
    size_bytes: int | None
    sha256: str | None
    encrypted: bool
    recipient: str | None
    error: str | None
    host: str | None
    reported_by: str


class BackupsOut(BaseModel):
    interval_hours: int
    last_success: BackupRunOut | None
    runs: list[BackupRunOut]


class ServiceHowTo(BaseModel):
    compose: str
    helm: str


class ServiceFile(BaseModel):
    file_name: str
    content: str


class ServiceConfig(BaseModel):
    compose: ServiceFile
    helm: ServiceFile


class ServiceOut(BaseModel):
    id: Literal["backup", "audit-shipping"]
    name: str
    purpose: str
    required: bool
    detected: bool
    healthy: bool
    detail: str
    how_to: ServiceHowTo
    config: ServiceConfig | None
