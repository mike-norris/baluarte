"""Background jobs for the GitLab integration, and the schedule that queues them.

Every attempt opens a ``collection_run`` before calling GitLab and closes it with the outcome,
so failures are recorded even when the job will be retried.
"""

import logging
import time
from collections.abc import Callable

import httpx
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from baluarte.evidence.observations import Provenance
from baluarte.integrations.gitlab.capabilities import detect, observe_snapshot, record_snapshot
from baluarte.integrations.gitlab.client import GitLabClient, GitLabError
from baluarte.integrations.gitlab.discovery import Discovery
from baluarte.integrations.gitlab.groups import discover_groups
from baluarte.integrations.gitlab.projects import discover_projects
from baluarte.integrations.gitlab.runs import RunKind, RunOutcome, finish_run, start_run
from baluarte.integrations.gitlab.settings import GitLabSettings, load_gitlab_settings
from baluarte.jobs import queue
from baluarte.jobs.queue import ClaimedJob, JobError, PermanentJobError
from baluarte.settings import ConfigurationError

logger = logging.getLogger(__name__)

DETECT_CAPABILITIES = "gitlab.detect_capabilities"
DISCOVER_GROUPS = "gitlab.discover_groups"
DISCOVER_PROJECTS = "gitlab.discover_projects"

# Scheduled each interval, in this order.
SCHEDULED_KINDS = (DETECT_CAPABILITIES, DISCOVER_GROUPS, DISCOVER_PROJECTS)

SNAPSHOT_OUTCOMES: dict[str, RunOutcome] = {
    "ok": "succeeded",
    "partial": "partial",
    "failed": "failed",
}

GitLabHandler = Callable[[ClaimedJob, Engine], None]
DiscoverFn = Callable[[GitLabClient, Engine, str, Provenance], Discovery]


def _settings() -> GitLabSettings:
    try:
        settings = load_gitlab_settings()
    except ConfigurationError as exc:
        raise PermanentJobError(str(exc)) from exc
    if settings is None:
        raise PermanentJobError("GitLab is not configured (BALUARTE_GITLAB_URL is not set)")
    return settings


def _unexpected(engine: Engine, run_id: int, exc: Exception) -> None:
    finish_run(engine, run_id, "failed", error=f"unexpected error: {type(exc).__name__}")


def make_detect_capabilities(transport: httpx.BaseTransport | None = None) -> GitLabHandler:
    """Build the handler; tests pass a transport that stands in for GitLab."""

    def detect_capabilities(job: ClaimedJob, engine: Engine) -> None:
        settings = _settings()
        run_id = start_run(engine, settings.url, "capabilities", job)
        try:
            # No database transaction is open while GitLab is called.
            with GitLabClient(settings, transport=transport) as client:
                snapshot = detect(client)
            with Session(engine) as db:
                instance = record_snapshot(db, settings.url, snapshot)
                db.flush()
                observe_snapshot(db, instance.id, snapshot, Provenance(run_id, job.id))
                db.commit()
        except Exception as exc:
            _unexpected(engine, run_id, exc)
            raise
        outcome = SNAPSHOT_OUTCOMES[snapshot.status]
        unsupported = sorted(
            name for name, probe in snapshot.probes.items() if probe.status != "supported"
        )
        finish_run(
            engine,
            run_id,
            outcome,
            seen=len(snapshot.probes),
            counts={"not_supported": unsupported},
            error=snapshot.error,
        )
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
    kind: RunKind, run: DiscoverFn, transport: httpx.BaseTransport | None
) -> GitLabHandler:
    singular = kind.removesuffix("s")

    def handler(job: ClaimedJob, engine: Engine) -> None:
        settings = _settings()
        run_id = start_run(engine, settings.url, kind, job)
        try:
            with GitLabClient(settings, transport=transport) as client:
                result = run(client, engine, settings.url, Provenance(run_id, job.id))
        except GitLabError as error:
            # Pages already stored stay; nothing is marked missing after an incomplete listing.
            message = f"{singular} discovery failed: {error}"
            finish_run(engine, run_id, "failed", error=message)
            raise JobError(message) from error
        except Exception as exc:
            _unexpected(engine, run_id, exc)
            raise
        unconfirmed = result.reconciled["unchanged"]
        finish_run(
            engine,
            run_id,
            "partial" if unconfirmed else "succeeded",
            seen=result.seen,
            counts=result.reconciled,
            error=f"{unconfirmed} {kind} could not be confirmed" if unconfirmed else None,
        )
        logger.info(
            "gitlab %s: %s listed, reconciled %s",
            kind,
            result.seen,
            result.reconciled,
            extra={"job_id": job.id},
        )

    return handler


def make_discover_groups(transport: httpx.BaseTransport | None = None) -> GitLabHandler:
    return _make_discovery("groups", discover_groups, transport)


def make_discover_projects(transport: httpx.BaseTransport | None = None) -> GitLabHandler:
    return _make_discovery("projects", discover_projects, transport)


class Schedule:
    """Queues every GitLab collection once per interval.

    The interval is cut into buckets on the database clock and each job's idempotency key names
    its bucket, so any number of workers queue each collection exactly once per interval.
    """

    def __init__(
        self,
        engine: Engine,
        interval_minutes: int,
        check_every: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine = engine
        self.interval = interval_minutes * 60
        self.check_every = check_every
        self.clock = clock
        self._next_check = 0.0
        self._last_bucket: int | None = None

    def __call__(self) -> None:
        if self.clock() < self._next_check:
            return
        self._next_check = self.clock() + self.check_every
        with Session(self.engine) as db:
            # Database clock, so workers on hosts with skewed clocks agree on the bucket.
            now = float(db.execute(select(func.extract("epoch", func.now()))).scalar_one())
            bucket = int(now // self.interval)
            if bucket == self._last_bucket:
                return
            queued = [
                queue.enqueue(db, kind, f"{kind}:scheduled:{bucket}", max_attempts=3)
                for kind in SCHEDULED_KINDS
            ]
            db.commit()
        self._last_bucket = bucket
        logger.info("gitlab: scheduled collection %s (jobs %s)", bucket, queued)


def scheduler(engine: Engine) -> Schedule | None:
    """The schedule for this deployment, or None when GitLab or scheduling is off."""
    try:
        settings = load_gitlab_settings()
    except ConfigurationError as exc:
        logger.error("gitlab: collection is not scheduled: %s", exc)
        return None
    if settings is None or settings.sync_interval_minutes == 0:
        return None
    return Schedule(engine, settings.sync_interval_minutes)
