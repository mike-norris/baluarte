"""Effective configuration for administrators. Secrets are reported only as set or not set."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from baluarte import __version__
from baluarte.auth.dependencies import Principal, auth_settings, require
from baluarte.auth.roles import Permission, Role
from baluarte.auth.settings import AuthSettings
from baluarte.config import database_url
from baluarte.integrations.gitlab.settings import load_gitlab_settings
from baluarte.settings import ConfigurationError, load_logging_settings

router = APIRouter(prefix="/settings", tags=["settings"])

SecretState = Literal["configured", "not_set"]


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
    url: str
    token: SecretState
    timeout_seconds: float


class SettingsInfo(BaseModel):
    version: str
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
            url=gitlab_settings.url,
            token=_state(gitlab_settings.token),
            timeout_seconds=gitlab_settings.timeout_seconds,
        )
        if gitlab_settings
        else None
    )
    return SettingsInfo(
        version=__version__,
        logging=logging_info,
        database=database,
        gitlab=gitlab,
        authentication=AuthenticationInfo(
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
