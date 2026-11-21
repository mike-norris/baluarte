"""Connections: the GitLab deployments a workspace collects from (ADR-0013, roadmap M11.3).

Reading the list needs inventory access; adding, changing, testing, collecting and removing need
settings management. Tokens are write-only: the API says whether one is stored, never what it is.
Nothing is ever written to GitLab.
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy.orm import Session

from baluarte import audit, connections
from baluarte.api.v1.gitlab import gitlab_transport
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.configuration.crypto import SecretUnavailable, load_secret_box
from baluarte.configuration.store import secret_stored
from baluarte.connections import ConnectionConflict, ConnectionRefused
from baluarte.db.models import GitLabInstance
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.integrations.gitlab.diagnostics import DiagnosticsReport, run_diagnostics
from baluarte.integrations.gitlab.freshness import (
    Freshness,
    KindFreshness,
    connection_freshness,
)
from baluarte.integrations.gitlab.jobs import GITHUB_COLLECT, SCHEDULED_KINDS
from baluarte.integrations.gitlab.settings import GitLabSettings
from baluarte.jobs import queue
from baluarte.settings import ConfigurationError
from baluarte.workspaces import configured_settings

router = APIRouter(prefix="/connections", tags=["connections"])

ConnectionId = Annotated[int, Path(ge=1)]


class ConnectionOut(BaseModel):
    id: int
    provider: str
    name: str | None
    url: str
    # The primary connection is set in the deployment or on the Settings page.
    primary: bool
    source: Literal["deployment", "settings_page", "connections_page"]
    paused: bool
    token: Literal["configured", "not_set"]
    timeout_seconds: float
    sync_interval_minutes: int
    allow_private_network: bool
    # How the connection authenticates: ``token``, or for GitHub ``app`` (an App installation).
    auth_kind: Literal["token", "app"] = "token"
    app_id: int | None = None
    installation_id: int | None = None
    version: str | None
    edition: str | None
    created_at: AwareDatetime
    health: Freshness
    collections: dict[str, KindFreshness]


class ConnectionList(BaseModel):
    connections: list[ConnectionOut]


def _out(db: Session, row: GitLabInstance, primary: GitLabSettings | None) -> ConnectionOut:
    is_primary = connections.is_primary(row, primary)
    if is_primary and primary is not None:
        source: Literal["deployment", "settings_page", "connections_page"] = (
            "deployment" if primary.source == "deployment" else "settings_page"
        )
        timeout, interval, token = primary.timeout_seconds, primary.sync_interval_minutes, True
    else:
        source = "connections_page"
        timeout, interval = row.timeout_seconds, row.sync_interval_minutes
        token = secret_stored(db, connections.token_name(row.id))
    health, kinds = connection_freshness(db, row.id, interval)
    return ConnectionOut(
        id=row.id,
        provider=row.provider,
        name=row.name,
        url=row.base_url,
        primary=is_primary,
        source=source,
        paused=row.paused,
        token="configured" if token else "not_set",
        timeout_seconds=timeout,
        sync_interval_minutes=interval,
        allow_private_network=row.allow_private_network,
        auth_kind=row.auth_kind,  # type: ignore[arg-type]
        app_id=row.app_id,
        installation_id=row.installation_id,
        version=row.version,
        edition=row.edition,
        created_at=row.created_at,
        health=health,
        collections=kinds,
    )


@router.get(
    "",
    response_model=ConnectionList,
    summary="List connections and their health",
    description="Connections not removed, oldest first. `health` is the worst freshness of the "
    "connection's inventory collections.",
)
def list_connections(
    _: Annotated[Principal, Depends(require(Permission.INVENTORY_READ))],
    db: Annotated[Session, Depends(database)],
) -> ConnectionList:
    primary = configured_settings()
    return ConnectionList(connections=[_out(db, row, primary) for row in connections.active(db)])


class ConnectionCreate(BaseModel):
    name: str = Field(default="", max_length=200)
    url: str = Field(max_length=2000)
    token: str = Field(min_length=1, max_length=1000, repr=False)
    timeout_seconds: float = 15
    sync_interval_minutes: int = 360
    allow_private_network: bool = False


class GitHubConnectionCreate(BaseModel):
    """A GitHub.com organization, read with a fine-grained token or a GitHub App installation."""

    name: str = Field(default="", max_length=200)
    organization: str = Field(min_length=1, max_length=39)
    auth_kind: Literal["token", "app"] = "token"
    # The fine-grained token, or the App's private key (PEM).
    secret: str = Field(min_length=1, max_length=10000, repr=False)
    app_id: int | None = Field(default=None, ge=1)
    installation_id: int | None = Field(default=None, ge=1)
    timeout_seconds: float = 15
    sync_interval_minutes: int = 360


class ConnectionUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    token: str | None = Field(default=None, min_length=1, max_length=1000, repr=False)
    timeout_seconds: float | None = None
    sync_interval_minutes: int | None = None
    allow_private_network: bool | None = None
    paused: bool | None = None


_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "No such connection, or it was removed"},
    409: {"description": "Not allowed for this connection, or no key to seal its token"},
    422: {"description": "Invalid settings, or the URL resolves to a refused address"},
}


def _refused(exc: Exception) -> HTTPException:
    if isinstance(exc, ConnectionConflict):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status.HTTP_404_NOT_FOUND, "No such connection")
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ConnectionOut,
    summary="Add a GitLab connection",
    description="The URL must be https and its host must resolve only to public addresses, "
    "unless `allow_private_network` is set; loopback, link-local and metadata addresses are "
    "always refused. The token is sealed and never returned. Adding the URL of a removed "
    "connection restores it with its data. Requires the `X-CSRF-Token` header.",
    responses=_ERRORS,
)
def add_connection(
    body: ConnectionCreate,
    principal: Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))],
    db: Annotated[Session, Depends(database)],
) -> ConnectionOut:
    try:
        row = connections.add(
            db,
            name=body.name,
            url=body.url,
            token=body.token,
            timeout_seconds=body.timeout_seconds,
            sync_interval_minutes=body.sync_interval_minutes,
            allow_private_network=body.allow_private_network,
            actor=principal.actor,
        )
    except (ConnectionRefused, ConnectionConflict) as exc:
        raise _refused(exc) from exc
    db.commit()
    return _out(db, row, configured_settings())


@router.post(
    "/github",
    status_code=status.HTTP_201_CREATED,
    response_model=ConnectionOut,
    summary="Add a GitHub.com organization",
    description="Its URL is `https://github.com/<organization>`. `auth_kind` `token` takes a "
    "fine-grained personal access token as `secret`; `app` takes the GitHub App's private key "
    "with `app_id` and `installation_id`. The credential is sealed and never returned. Only "
    "read permissions are needed (docs/GITHUB_INTEGRATION.md). Requires the `X-CSRF-Token` "
    "header.",
    responses=_ERRORS,
)
def add_github_connection(
    body: GitHubConnectionCreate,
    principal: Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))],
    db: Annotated[Session, Depends(database)],
) -> ConnectionOut:
    try:
        row = connections.add_github(
            db,
            name=body.name,
            organization=body.organization,
            auth_kind=body.auth_kind,
            secret=body.secret,
            app_id=body.app_id,
            installation_id=body.installation_id,
            timeout_seconds=body.timeout_seconds,
            sync_interval_minutes=body.sync_interval_minutes,
            actor=principal.actor,
        )
    except (ConnectionRefused, ConnectionConflict) as exc:
        raise _refused(exc) from exc
    db.commit()
    return _out(db, row, configured_settings())


@router.patch(
    "/{connection_id}",
    response_model=ConnectionOut,
    summary="Change a connection",
    description="Fields left out stay as they are. The primary connection's token and timing "
    "are changed on the Settings page; here only its name and pause. Requires the "
    "`X-CSRF-Token` header.",
    responses=_ERRORS,
)
def change_connection(
    connection_id: ConnectionId,
    body: ConnectionUpdate,
    principal: Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))],
    db: Annotated[Session, Depends(database)],
) -> ConnectionOut:
    try:
        row = connections.change(
            db,
            connection_id,
            actor=principal.actor,
            name=body.name,
            token=body.token,
            timeout_seconds=body.timeout_seconds,
            sync_interval_minutes=body.sync_interval_minutes,
            allow_private_network=body.allow_private_network,
            paused=body.paused,
        )
    except (ConnectionRefused, ConnectionConflict, LookupError) as exc:
        raise _refused(exc) from exc
    db.commit()
    return _out(db, row, configured_settings())


@router.delete(
    "/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a connection",
    description="Stops collecting it and forgets its token. Its inventory, findings and evidence "
    "stay. Requires the `X-CSRF-Token` header.",
    responses=_ERRORS,
)
def remove_connection(
    connection_id: ConnectionId,
    principal: Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))],
    db: Annotated[Session, Depends(database)],
) -> None:
    try:
        connections.remove(db, connection_id, principal.actor)
    except (ConnectionConflict, LookupError) as exc:
        raise _refused(exc) from exc
    db.commit()


@router.post(
    "/{connection_id}/diagnostics",
    response_model=DiagnosticsReport,
    summary="Test a connection and its token's permissions",
    description="The same checks as the Settings page's test. Always 200 when the checks ran; "
    "read `status`. Requires the `X-CSRF-Token` header.",
    responses=_ERRORS,
)
def diagnose_connection(
    connection_id: ConnectionId,
    principal: Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))],
    db: Annotated[Session, Depends(database)],
    transport: Annotated[httpx.BaseTransport | None, Depends(gitlab_transport)],
) -> DiagnosticsReport:
    try:
        settings = connections.settings_for(db, connection_id, load_secret_box()).settings
    except (ConnectionRefused, ConfigurationError, SecretUnavailable) as exc:
        raise _refused(exc) from exc
    db.rollback()  # no transaction stays open while GitLab is called
    with GitLabClient(settings, transport=transport) as client:
        report = run_diagnostics(client, settings.url)
    audit.record(
        db,
        actor=principal.actor,
        action="connection.diagnostics_run",
        resource_type="gitlab_instance",
        resource_id=str(connection_id),
        details={
            "url": report.gitlab_url,
            "status": report.status,
            "checks": {check.id: check.status for check in report.checks},
        },
    )
    db.commit()
    return report


class CollectionQueued(BaseModel):
    job_ids: list[int]


@router.post(
    "/{connection_id}/collect",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CollectionQueued,
    summary="Collect a connection now",
    description="Queues capability detection and group and project discovery for this "
    "connection; evaluation and the other collections follow. Requests within the same minute "
    "share the jobs. Requires the `X-CSRF-Token` header.",
    responses=_ERRORS,
)
def collect_connection(
    connection_id: ConnectionId,
    principal: Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))],
    db: Annotated[Session, Depends(database)],
) -> CollectionQueued:
    row = db.get(GitLabInstance, connection_id)
    if row is None or row.removed_at is not None:
        raise _refused(LookupError(connection_id))
    if row.paused:
        raise _refused(ConnectionConflict("The connection is paused"))
    minute = datetime.now(UTC).strftime("%Y%m%dT%H%M")
    kinds = SCHEDULED_KINDS if row.provider == "gitlab" else (GITHUB_COLLECT,)
    job_ids = [
        queue.enqueue(
            db,
            kind,
            f"{kind}:connection-{row.id}:{minute}",
            connections.payload_for(row.id),
            max_attempts=3,
        )
        for kind in kinds
    ]
    audit.record(
        db,
        actor=principal.actor,
        action="connection.collect_requested",
        resource_type="gitlab_instance",
        resource_id=str(row.id),
        details={"url": row.base_url, "job_ids": job_ids},
    )
    db.commit()
    return CollectionQueued(job_ids=job_ids)
