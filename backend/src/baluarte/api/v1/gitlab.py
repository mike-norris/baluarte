"""GitLab connection diagnostics and capability detection. Nothing is ever written to GitLab."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import GitLabGroup, GitLabInstance
from baluarte.integrations.gitlab.capabilities import CapabilitySnapshot
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.integrations.gitlab.diagnostics import (
    DiagnosticsReport,
    not_configured,
    run_diagnostics,
)
from baluarte.integrations.gitlab.jobs import DETECT_CAPABILITIES, DISCOVER_GROUPS
from baluarte.integrations.gitlab.settings import load_gitlab_settings
from baluarte.jobs import queue
from baluarte.settings import ConfigurationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/gitlab", tags=["gitlab"])


def gitlab_transport() -> httpx.BaseTransport | None:
    """The HTTP transport for GitLab calls; None uses the network. Overridden in tests."""
    return None


@router.post(
    "/diagnostics",
    response_model=DiagnosticsReport,
    summary="Test the GitLab connection and the token's permissions",
    description="Calls GitLab with the configured token and reports each check as ok, warning, "
    "failed or unknown. Always 200 when the checks ran; read `status`. Requires the "
    "`X-CSRF-Token` header.",
)
def diagnose(
    principal: Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))],
    db: Annotated[Session, Depends(database)],
    transport: Annotated[httpx.BaseTransport | None, Depends(gitlab_transport)],
) -> DiagnosticsReport:
    try:
        settings = load_gitlab_settings()
    except ConfigurationError as exc:
        report = not_configured(str(exc))
    else:
        if settings is None:
            report = not_configured("BALUARTE_GITLAB_URL is not set")
        else:
            with GitLabClient(settings, transport=transport) as client:
                report = run_diagnostics(client, settings.url)
    audit.record(
        db,
        actor=principal.actor,
        action="gitlab.diagnostics_run",
        resource_type="gitlab_instance",
        resource_id=report.gitlab_url,
        details={
            "status": report.status,
            "version": report.version,
            "checks": {check.id: check.status for check in report.checks},
        },
    )
    db.commit()
    logger.info("gitlab diagnostics: %s", report.status)
    return report


# A snapshot older than this is reported as stale.
STALE_AFTER = timedelta(hours=24)


class InstanceOut(BaseModel):
    configured: bool
    url: str | None
    version: str | None
    edition: str | None
    last_checked_at: datetime | None
    stale: bool
    capabilities: CapabilitySnapshot | None


def _configured_url() -> str | None:
    try:
        settings = load_gitlab_settings()
    except ConfigurationError:
        return None
    return settings.url if settings else None


@router.get(
    "/instance",
    response_model=InstanceOut,
    summary="The GitLab instance: version, edition and detected capabilities",
    description="`capabilities` is null until detection has run. `stale` is true when the last "
    "detection is older than 24 hours or never ran.",
)
def read_instance(
    _: Annotated[Principal, Depends(require(Permission.INVENTORY_READ))],
    db: Annotated[Session, Depends(database)],
) -> InstanceOut:
    url = _configured_url()
    instance = (
        db.scalars(select(GitLabInstance).where(GitLabInstance.base_url == url)).first()
        if url
        else None
    )
    if instance is None:
        return InstanceOut(
            configured=url is not None,
            url=url,
            version=None,
            edition=None,
            last_checked_at=None,
            stale=True,
            capabilities=None,
        )
    checked = instance.last_checked_at
    return InstanceOut(
        configured=True,
        url=url,
        version=instance.version,
        edition=instance.edition,
        last_checked_at=checked,
        stale=checked is None or datetime.now(UTC) - checked > STALE_AFTER,
        capabilities=(
            CapabilitySnapshot.model_validate(instance.capability_snapshot)
            if instance.capability_snapshot
            else None
        ),
    )


class JobQueued(BaseModel):
    job_id: int


def _queue(db: Session, principal: Principal, kind: str, action: str, attempts: int) -> JobQueued:
    """Queue a GitLab job once per minute and audit the request."""
    url = _configured_url()
    if url is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "GitLab is not configured")
    minute = datetime.now(UTC).strftime("%Y%m%dT%H%M")
    job_id = queue.enqueue(db, kind, f"{kind}:{minute}", max_attempts=attempts)
    audit.record(
        db,
        actor=principal.actor,
        action=action,
        resource_type="gitlab_instance",
        resource_id=url,
        details={"job_id": job_id},
    )
    db.commit()
    return JobQueued(job_id=job_id)


_QUEUED_DOC = (
    "Runs in the worker. Requests within the same minute share one job. Requires the "
    "`X-CSRF-Token` header."
)


@router.post(
    "/capabilities/refresh",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobQueued,
    summary="Queue capability detection",
    description=_QUEUED_DOC,
    responses={409: {"description": "GitLab is not configured"}},
)
def refresh_capabilities(
    principal: Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))],
    db: Annotated[Session, Depends(database)],
) -> JobQueued:
    return _queue(db, principal, DETECT_CAPABILITIES, "gitlab.capabilities_refresh_requested", 3)


@router.post(
    "/groups/discover",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobQueued,
    summary="Queue group and subgroup discovery",
    description=_QUEUED_DOC,
    responses={409: {"description": "GitLab is not configured"}},
)
def discover_groups(
    principal: Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))],
    db: Annotated[Session, Depends(database)],
) -> JobQueued:
    return _queue(db, principal, DISCOVER_GROUPS, "gitlab.group_discovery_requested", 5)


class GroupOut(BaseModel):
    id: int
    gitlab_id: int
    parent_gitlab_id: int | None
    full_path: str
    name: str
    visibility: str | None
    state: str
    first_seen_at: datetime
    last_seen_at: datetime


class GroupPage(BaseModel):
    items: list[GroupOut]
    next_after_id: int | None


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@router.get(
    "/groups",
    response_model=GroupPage,
    summary="Discovered groups and subgroups",
    description="Groups of the configured instance in ID order. `state` is `active`, "
    "`inaccessible` (exists, but the token may not read it) or `missing` (deleted or hidden "
    "from the token). `last_seen_at` is when discovery last listed the group.",
)
def list_groups(
    _: Annotated[Principal, Depends(require(Permission.INVENTORY_READ))],
    db: Annotated[Session, Depends(database)],
    state: Annotated[Literal["active", "missing", "inaccessible"] | None, Query()] = None,
    search: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    parent_gitlab_id: Annotated[int | None, Query(gt=0)] = None,
    after_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> GroupPage:
    url = _configured_url()
    if url is None:
        return GroupPage(items=[], next_after_id=None)
    statement = (
        select(GitLabGroup)
        .join(GitLabInstance, GitLabInstance.id == GitLabGroup.instance_id)
        .where(GitLabInstance.base_url == url)
        .order_by(GitLabGroup.id)
        .limit(limit + 1)
    )
    if state is not None:
        statement = statement.where(GitLabGroup.state == state)
    if search is not None:
        statement = statement.where(GitLabGroup.full_path.ilike(_like(search), escape="\\"))
    if parent_gitlab_id is not None:
        statement = statement.where(GitLabGroup.parent_gitlab_id == parent_gitlab_id)
    if after_id is not None:
        statement = statement.where(GitLabGroup.id > after_id)
    rows = db.scalars(statement).all()
    items = [GroupOut.model_validate(row, from_attributes=True) for row in rows[:limit]]
    return GroupPage(items=items, next_after_id=items[-1].id if len(rows) > limit else None)
