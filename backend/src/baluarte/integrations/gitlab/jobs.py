"""Background jobs for the GitLab integration."""

import logging
from collections.abc import Callable

import httpx
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from baluarte.integrations.gitlab.capabilities import detect, record_snapshot
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.integrations.gitlab.settings import load_gitlab_settings
from baluarte.jobs.queue import ClaimedJob, JobError, PermanentJobError
from baluarte.settings import ConfigurationError

logger = logging.getLogger(__name__)

DETECT_CAPABILITIES = "gitlab.detect_capabilities"


def make_detect_capabilities(
    transport: httpx.BaseTransport | None = None,
) -> Callable[[ClaimedJob, Engine], None]:
    """Build the handler; tests pass a transport that stands in for GitLab."""

    def detect_capabilities(job: ClaimedJob, engine: Engine) -> None:
        try:
            settings = load_gitlab_settings()
        except ConfigurationError as exc:
            raise PermanentJobError(str(exc)) from exc
        if settings is None:
            raise PermanentJobError("GitLab is not configured (BALUARTE_GITLAB_URL is not set)")
        # No database transaction is open while GitLab is called.
        with GitLabClient(settings, transport=transport) as client:
            snapshot = detect(client)
        with Session(engine) as db:
            record_snapshot(db, settings.url, snapshot)
            db.commit()
        logger.info(
            "gitlab capabilities: %s (version %s, edition %s)",
            snapshot.status,
            snapshot.version,
            snapshot.edition,
            extra={"job_id": job.id},
        )
        if snapshot.status == "failed":
            # Recorded above, so the failure is visible; retry in case it was transient.
            raise JobError(f"capability detection failed: {snapshot.error}")

    return detect_capabilities
