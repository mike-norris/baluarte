"""GitLab connection diagnostics and capability detection. Nothing is ever written to GitLab."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import GitLabInstance
from baluarte.integrations.gitlab.capabilities import CapabilitySnapshot
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.integrations.gitlab.diagnostics import (
    DiagnosticsReport,
    not_configured,
    run_diagnostics,
)
from baluarte.integrations.gitlab.jobs import DETECT_CAPABILITIES
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


class RefreshQueued(BaseModel):
    job_id: int


@router.post(
    "/capabilities/refresh",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RefreshQueued,
    summary="Queue capability detection",
    description="Runs in the worker. Requests within the same minute share one job. Requires the "
    "`X-CSRF-Token` header.",
    responses={409: {"description": "GitLab is not configured"}},
)
def refresh_capabilities(
    principal: Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))],
    db: Annotated[Session, Depends(database)],
) -> RefreshQueued:
    url = _configured_url()
    if url is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "GitLab is not configured")
    minute = datetime.now(UTC).strftime("%Y%m%dT%H%M")
    job_id = queue.enqueue(
        db, DETECT_CAPABILITIES, f"{DETECT_CAPABILITIES}:{minute}", max_attempts=3
    )
    audit.record(
        db,
        actor=principal.actor,
        action="gitlab.capabilities_refresh_requested",
        resource_type="gitlab_instance",
        resource_id=url,
        details={"job_id": job_id},
    )
    db.commit()
    return RefreshQueued(job_id=job_id)
