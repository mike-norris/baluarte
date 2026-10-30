"""Request and response bodies shared by the setup wizard and the admin Settings page."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

Source = Literal["deployment", "database"]
SecretState = Literal["configured", "not_set"]
SetupStateName = Literal["needs_setup", "awaiting_admin_sign_in", "complete"]

GroupName = Annotated[str, Field(max_length=255)]


class SectionStatus(BaseModel):
    source: Source | None
    configured: bool


class SetupStatus(BaseModel):
    state: SetupStateName
    # Whether the caller holds a valid setup session.
    setup_session: bool
    # Null once setup is complete, so the public endpoint discloses nothing more.
    secret_key_configured: bool | None
    oidc: SectionStatus | None
    gitlab: SectionStatus | None


class SetupCode(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class SetupSessionOut(BaseModel):
    csrf_token: str


class OidcUpdate(BaseModel):
    public_url: str = Field(max_length=2048)
    issuer: str = Field(max_length=2048)
    client_id: str = Field(min_length=1, max_length=512)
    # Null keeps the stored secret; it must be entered again when the issuer changes.
    client_secret: str | None = Field(default=None, min_length=1, max_length=4096)
    clear_client_secret: bool = False
    scopes: str = Field(default="openid profile email", max_length=1024)
    roles_claim: str = Field(default="groups", max_length=255)
    admin_groups: list[GroupName] = Field(min_length=1, max_length=100)
    analyst_groups: list[GroupName] = Field(default=[], max_length=100)
    auditor_groups: list[GroupName] = Field(default=[], max_length=100)


class OidcConfig(BaseModel):
    source: Source
    public_url: str
    issuer: str
    client_id: str
    client_secret: SecretState
    scopes: str
    roles_claim: str
    admin_groups: list[str]
    analyst_groups: list[str]
    auditor_groups: list[str]
    # Register this with the identity provider.
    redirect_uri: str


class OidcTest(BaseModel):
    issuer: str = Field(max_length=2048)


class OidcTestResult(BaseModel):
    ok: bool
    issuer: str | None
    authorization_endpoint: str | None
    scopes_supported: list[str]
    error: str | None


class GitLabUpdate(BaseModel):
    url: str = Field(max_length=2048)
    # Null keeps the stored token; it must be entered again when the URL changes.
    token: str | None = Field(default=None, min_length=1, max_length=1024)
    timeout_seconds: float = 15
    sync_interval_minutes: int = 360


class GitLabConfig(BaseModel):
    source: Source
    url: str
    token: SecretState
    timeout_seconds: float
    sync_interval_minutes: int


class GitLabTest(BaseModel):
    url: str = Field(max_length=2048)
    # Null tests the stored token, which is only sent to the stored URL.
    token: str | None = Field(default=None, min_length=1, max_length=1024)


class GitLabUser(BaseModel):
    username: str
    is_admin: bool


class CheckResult(BaseModel):
    name: str
    status: Literal["pass", "warn", "fail"]
    message: str


class GitLabTestResult(BaseModel):
    ok: bool
    version: str | None
    user: GitLabUser | None
    token_scopes: list[str] | None
    token_expires_at: str | None
    checks: list[CheckResult]
    error: str | None
