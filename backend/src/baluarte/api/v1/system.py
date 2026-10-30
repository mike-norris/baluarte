"""Effective configuration for administrators, and changing what the web UI may change.

Secrets are reported only as set or not set. The identity provider and GitLab sections can be
changed here unless the deployment's environment sets them (ADR-0008).
"""

from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from baluarte import __version__
from baluarte.api.v1.gitlab import gitlab_transport
from baluarte.api.v1.setup import http_errors, oidc_transport
from baluarte.auth.dependencies import Principal, auth_settings, database, require
from baluarte.auth.roles import Permission, Role
from baluarte.auth.settings import AuthSettings
from baluarte.config import database_url
from baluarte.configuration import service
from baluarte.configuration.schemas import (
    GitLabConfig,
    GitLabTest,
    GitLabTestResult,
    GitLabUpdate,
    OidcConfig,
    OidcTest,
    OidcTestResult,
    OidcUpdate,
    Source,
)
from baluarte.integrations.gitlab.settings import load_gitlab_settings
from baluarte.settings import ConfigurationError, load_logging_settings

router = APIRouter(prefix="/settings", tags=["settings"])

SecretState = Literal["configured", "not_set"]
Admin = Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))]
Database = Annotated[Session, Depends(database)]


class LoggingInfo(BaseModel):
    level: str
    format: str


class DatabaseInfo(BaseModel):
    host: str | None
    port: int | None
    name: str | None
    user: str | None
    ssl_mode: str | None
    password: SecretState


class AuthenticationInfo(BaseModel):
    source: Source
    public_url: str
    issuer: str
    client_id: str
    client_secret: SecretState
    scopes: str
    roles_claim: str
    group_roles: dict[Role, list[str]]
    session_idle_minutes: int
    session_lifetime_minutes: int


class GitLabInfo(BaseModel):
    source: Source
    url: str
    token: SecretState
    timeout_seconds: float
    sync_interval_minutes: int


class SettingsInfo(BaseModel):
    version: str
    # The key that seals secrets entered in the web UI (BALUARTE_SECRET_KEY_FILE).
    secret_key: SecretState
    logging: LoggingInfo | None
    database: DatabaseInfo | None
    gitlab: GitLabInfo | None
    authentication: AuthenticationInfo


def _state(value: object) -> SecretState:
    return "configured" if value else "not_set"


@router.get("", response_model=SettingsInfo, summary="Effective settings (no secret values)")
def read_settings(
    _: Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))],
    auth: Annotated[AuthSettings, Depends(auth_settings)],
) -> SettingsInfo:
    try:
        logging_settings = load_logging_settings()
        logging_info: LoggingInfo | None = LoggingInfo(
            level=logging_settings.level, format=logging_settings.format
        )
    except ConfigurationError:
        logging_info = None
    try:
        url = database_url()
        ssl_mode = url.query.get("sslmode")
        database: DatabaseInfo | None = DatabaseInfo(
            host=url.host,
            port=url.port,
            name=url.database,
            user=url.username,
            ssl_mode=ssl_mode if isinstance(ssl_mode, str) else None,
            password=_state(url.password),
        )
    except ConfigurationError:
        database = None
    try:
        gitlab_settings = load_gitlab_settings()
    except ConfigurationError:
        gitlab_settings = None
    gitlab = (
        GitLabInfo(
            source=gitlab_settings.source,
            url=gitlab_settings.url,
            token=_state(gitlab_settings.token),
            timeout_seconds=gitlab_settings.timeout_seconds,
            sync_interval_minutes=gitlab_settings.sync_interval_minutes,
        )
        if gitlab_settings
        else None
    )
    return SettingsInfo(
        version=__version__,
        secret_key="configured" if service.secret_key_configured() else "not_set",
        logging=logging_info,
        database=database,
        gitlab=gitlab,
        authentication=AuthenticationInfo(
            source=auth.source,
            public_url=auth.public_url,
            issuer=auth.issuer,
            client_id=auth.client_id,
            client_secret=_state(auth.client_secret),
            scopes=auth.scopes,
            roles_claim=auth.roles_claim,
            group_roles={role: sorted(groups) for role, groups in auth.group_roles.items()},
            session_idle_minutes=int(auth.session_idle.total_seconds() // 60),
            session_lifetime_minutes=int(auth.session_lifetime.total_seconds() // 60),
        ),
    )


@router.get("/oidc", response_model=OidcConfig, summary="Identity provider settings")
def read_oidc(_: Admin, db: Database) -> OidcConfig:
    with http_errors():
        return service.oidc_config(db)


@router.put(
    "/oidc",
    response_model=OidcConfig,
    summary="Change the identity provider settings",
    description="409 when the deployment sets them. Takes effect for new sign-ins; existing "
    "sessions keep their roles until they end.",
)
def write_oidc(body: OidcUpdate, principal: Admin, db: Database) -> OidcConfig:
    with http_errors():
        return service.update_oidc(db, body, principal.actor, "settings")


@router.post(
    "/oidc/test", response_model=OidcTestResult, summary="Check an issuer's discovery document"
)
def test_oidc(
    body: OidcTest,
    _: Admin,
    transport: Annotated[httpx.BaseTransport | None, Depends(oidc_transport)],
) -> OidcTestResult:
    with http_errors():
        return service.check_oidc_discovery(body.issuer, transport)


@router.get("/gitlab", response_model=GitLabConfig, summary="GitLab connection settings")
def read_gitlab(_: Admin, db: Database) -> GitLabConfig:
    with http_errors():
        return service.gitlab_config(db)


@router.put(
    "/gitlab",
    response_model=GitLabConfig,
    summary="Change the GitLab connection",
    description="409 when the deployment sets it. The token must be entered again when the "
    "URL changes.",
)
def write_gitlab(body: GitLabUpdate, principal: Admin, db: Database) -> GitLabConfig:
    with http_errors():
        return service.update_gitlab(db, body, principal.actor, "settings")


@router.delete(
    "/gitlab",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Disconnect GitLab",
    description="Removes the stored URL and token. Collected data is kept. 409 when the "
    "deployment sets the connection.",
)
def delete_gitlab(principal: Admin, db: Database) -> Response:
    with http_errors():
        service.disconnect_gitlab(db, principal.actor)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/gitlab/test",
    response_model=GitLabTestResult,
    summary="Test a GitLab URL and token without saving them",
)
def test_gitlab(
    body: GitLabTest,
    _: Admin,
    db: Database,
    transport: Annotated[httpx.BaseTransport | None, Depends(gitlab_transport)],
) -> GitLabTestResult:
    with http_errors():
        return service.check_gitlab_connection(db, body, transport)
