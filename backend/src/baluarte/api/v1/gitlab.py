"""GitLab connection diagnostics. Runs on demand; nothing is written to GitLab."""

import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.integrations.gitlab.diagnostics import (
    DiagnosticsReport,
    not_configured,
    run_diagnostics,
)
from baluarte.integrations.gitlab.settings import load_gitlab_settings
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
