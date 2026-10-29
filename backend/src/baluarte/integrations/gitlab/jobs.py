"""Background jobs for the GitLab integration."""

import logging
from collections.abc import Callable

import httpx
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from baluarte.integrations.gitlab.capabilities import detect, record_snapshot
from baluarte.integrations.gitlab.client import GitLabClient, GitLabError
from baluarte.integrations.gitlab.discovery import Discovery
from baluarte.integrations.gitlab.groups import discover_groups
from baluarte.integrations.gitlab.projects import discover_projects
from baluarte.integrations.gitlab.settings import GitLabSettings, load_gitlab_settings
from baluarte.jobs.queue import ClaimedJob, JobError, PermanentJobError
from baluarte.settings import ConfigurationError

logger = logging.getLogger(__name__)

DETECT_CAPABILITIES = "gitlab.detect_capabilities"
DISCOVER_GROUPS = "gitlab.discover_groups"
DISCOVER_PROJECTS = "gitlab.discover_projects"

GitLabHandler = Callable[[ClaimedJob, Engine], None]
DiscoverFn = Callable[[GitLabClient, Engine, str], Discovery]


def _settings() -> GitLabSettings:
    try:
        settings = load_gitlab_settings()
    except ConfigurationError as exc:
        raise PermanentJobError(str(exc)) from exc
    if settings is None:
        raise PermanentJobError("GitLab is not configured (BALUARTE_GITLAB_URL is not set)")
    return settings


def make_detect_capabilities(transport: httpx.BaseTransport | None = None) -> GitLabHandler:
    """Build the handler; tests pass a transport that stands in for GitLab."""

    def detect_capabilities(job: ClaimedJob, engine: Engine) -> None:
        settings = _settings()
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


def _make_discovery(
    kind: str, run: DiscoverFn, transport: httpx.BaseTransport | None
) -> GitLabHandler:
    def handler(job: ClaimedJob, engine: Engine) -> None:
        settings = _settings()
        with GitLabClient(settings, transport=transport) as client:
            try:
                result = run(client, engine, settings.url)
            except GitLabError as error:
                # Pages already stored stay; nothing is marked missing after an incomplete listing.
                raise JobError(f"{kind} discovery failed: {error}") from error
        logger.info(
            "gitlab %ss: %s listed, reconciled %s",
            kind,
            result.seen,
            result.reconciled,
            extra={"job_id": job.id},
        )

    return handler


def make_discover_groups(transport: httpx.BaseTransport | None = None) -> GitLabHandler:
    return _make_discovery("group", discover_groups, transport)


def make_discover_projects(transport: httpx.BaseTransport | None = None) -> GitLabHandler:
    return _make_discovery("project", discover_projects, transport)
