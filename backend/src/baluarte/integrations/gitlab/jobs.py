"""Background jobs for the GitLab integration, and the schedule that queues them.

Every attempt opens a ``collection_run`` before calling GitLab and closes it with the outcome,
so failures are recorded even when the job will be retried.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from baluarte.changes import settings as change_settings
from baluarte.db.models import GitLabInstance
from baluarte.evidence.observations import Provenance
from baluarte.integrations.gitlab import changes, labels, security_policies, timelogs
from baluarte.integrations.gitlab.capabilities import detect, observe_snapshot, record_snapshot
from baluarte.integrations.gitlab.client import GitLabClient, GitLabError
from baluarte.integrations.gitlab.discovery import Discovery
from baluarte.integrations.gitlab.freshness import report as freshness_report
from baluarte.integrations.gitlab.groups import discover_groups
from baluarte.integrations.gitlab.instances import instance_for, instance_id_for
from baluarte.integrations.gitlab.projects import discover_projects
from baluarte.integrations.gitlab.runs import RunKind, RunOutcome, finish_run, start_run
from baluarte.integrations.gitlab.settings import GitLabSettings, load_gitlab_settings
from baluarte.jobs import queue
from baluarte.jobs.queue import ClaimedJob, JobError, PermanentJobError
from baluarte.policy.evaluation.runner import queue_after_collection
from baluarte.settings import ConfigurationError
from baluarte.timetracking import settings as time_settings

logger = logging.getLogger(__name__)

DETECT_CAPABILITIES = "gitlab.detect_capabilities"
DISCOVER_GROUPS = "gitlab.discover_groups"
DISCOVER_PROJECTS = "gitlab.discover_projects"
DISCOVER_SECURITY_POLICIES = "gitlab.discover_security_policies"
COLLECT_CHANGES = "gitlab.collect_changes"
COLLECT_LABELS = "gitlab.collect_labels"
COLLECT_TIME = "gitlab.collect_time"

# Scheduled each interval, in this order. Security policy discovery and change, label and time
# collection are queued by each finished project discovery instead, so they always read the
# inventory that discovery just confirmed.
SCHEDULED_KINDS = (DETECT_CAPABILITIES, DISCOVER_GROUPS, DISCOVER_PROJECTS)

# Retries of a scheduled collection, in seconds after each failed attempt: long enough to ride
# out a network or GitLab outage of some minutes. Manual collection keeps the queue's short
# default backoff, so "Collect now" reports quickly.
SCHEDULED_RETRY_DELAYS = (60, 300, 900)


def scheduled_retry_delays(interval_minutes: int) -> tuple[int, ...]:
    """The retry delays that fit in half the interval, so one collection's retries finish well
    before the next one is queued: (60, 300, 900) from a 42-minute interval, (60, 300) below."""
    delays: list[int] = []
    for delay in SCHEDULED_RETRY_DELAYS:
        if sum(delays) + delay > interval_minutes * 60 / 2:
            break
        delays.append(delay)
    return tuple(delays)


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
        counts: dict[str, Any] = dict(result.reconciled)
        counts["listed"] = dict(result.listed)
        problems = [f"{unconfirmed} {kind} could not be confirmed"] if unconfirmed else []
        if result.protection is not None:
            counts["protection"] = result.protection
            unread = result.protection.get("unread", 0)
            if unread:
                # Their protection evaluates as unknown, so the run is not a full success.
                problems.append(f"default-branch protection of {unread} projects was not read")
        for reader, statuses in (result.reads or {}).items():
            counts.setdefault("reads", {})[reader] = statuses
            # Forbidden and not found are answers (a role, a feature the edition lacks), and
            # evaluate as unknown; only a read that failed makes the run partial.
            if statuses.get("failed"):
                problems.append(
                    f"{reader.replace('_', ' ')} of {statuses['failed']} projects could not be read"
                )
        finish_run(
            engine,
            run_id,
            "partial" if problems else "succeeded",
            seen=result.seen,
            counts=counts,
            error="; ".join(problems) or None,
        )
        if kind == "projects":
            # Re-evaluate against the new inventory. A failed discovery queues none (its attempt
            # is retried); the collection freshness already reports the failure.
            with Session(engine) as db:
                queue_after_collection(db, run_id)
                queue_security_policies(db, settings.url, run_id)
                queue_changes(db, run_id)
                queue_labels(db, f"after-run:{run_id}")
                queue_time(db, run_id)
                db.commit()
        logger.info(
            "gitlab %s: %s listed (%s new, %s known, %s restored); %s not listed: %s still "
            "active, %s inaccessible, %s missing, %s could not be confirmed",
            kind,
            result.seen,
            result.listed["new"],
            result.listed["known"],
            result.listed["restored"],
            sum(result.reconciled.values()),
            result.reconciled["active"],
            result.reconciled["inaccessible"],
            result.reconciled["missing"],
            result.reconciled["unchanged"],
            extra={"job_id": job.id},
        )

    return handler


def make_discover_groups(transport: httpx.BaseTransport | None = None) -> GitLabHandler:
    return _make_discovery("groups", discover_groups, transport)


def make_discover_projects(transport: httpx.BaseTransport | None = None) -> GitLabHandler:
    return _make_discovery("projects", discover_projects, transport)


def _snapshot(db: Session, base_url: str) -> dict[str, Any] | None:
    instance = db.scalars(select(GitLabInstance).where(GitLabInstance.base_url == base_url)).first()
    return instance.capability_snapshot if instance is not None else None


def queue_security_policies(db: Session, base_url: str, collection_run_id: int) -> int | None:
    """Queue security policy discovery after a project discovery, once per run.

    Only when the latest capability snapshot says links and at least one policy type are
    readable; otherwise nothing is queued and the Coverage view shows discovery as never run.
    """
    if not security_policies.readable(_snapshot(db, base_url)):
        return None
    return queue.enqueue(
        db,
        DISCOVER_SECURITY_POLICIES,
        f"{DISCOVER_SECURITY_POLICIES}:after-run:{collection_run_id}",
        max_attempts=3,
    )


def make_discover_security_policies(
    transport: httpx.BaseTransport | None = None,
) -> GitLabHandler:
    def discover(job: ClaimedJob, engine: Engine) -> None:
        settings = _settings()
        run_id = start_run(engine, settings.url, "security_policies", job)
        try:
            with Session(engine) as db:
                instance_id = instance_for(db, settings.url).id
                snapshot = _snapshot(db, settings.url)
                db.commit()
            if not security_policies.readable(snapshot):
                message = (
                    "security policies are not readable with this token, or capability "
                    "detection has not confirmed it; run capability detection"
                )
                finish_run(engine, run_id, "failed", error=message)
                raise PermanentJobError(message)
            availability = security_policies.type_availability(snapshot)
            with GitLabClient(settings, transport=transport) as client:
                result = security_policies.collect(
                    client, engine, instance_id, availability, Provenance(run_id, job.id)
                )
        except PermanentJobError:
            raise
        except Exception as exc:
            _unexpected(engine, run_id, exc)
            raise
        problems = []
        if result.unread:
            problems.append(f"security policies of {result.unread} groups or projects not read")
        if result.incomplete:
            problems.append(
                f"{result.incomplete} groups or projects have policy types that are unknown, "
                "unsupported or cut short"
            )
        unavailable = sorted(t for t, status in availability.items() if status != "ok")
        if result.seen == 0 and result.unread:
            outcome: RunOutcome = "failed"
        else:
            outcome = "partial" if problems else "succeeded"
        finish_run(
            engine,
            run_id,
            outcome,
            seen=result.seen,
            counts={
                "groups": result.groups.as_counts(),
                "projects": result.projects.as_counts(),
                "policies": result.policies,
                "by_type": result.by_type,
                "types_unavailable": unavailable,
            },
            error="; ".join(problems) or None,
        )
        logger.info(
            "gitlab security policies: %s groups and %s projects read, %s not read; "
            "%s policies (%s new, %s now missing)",
            result.groups.read,
            result.projects.read,
            result.unread,
            result.policies.get("active", 0),
            result.policies.get("new", 0),
            result.policies.get("missing", 0),
            extra={"job_id": job.id},
        )
        if outcome == "failed":
            raise JobError("security policy discovery read no group or project")
        # The security policy rules judge these observations: re-evaluate, once per run.
        with Session(engine) as db:
            queue_after_collection(db, run_id)
            db.commit()

    return discover


def queue_changes(db: Session, collection_run_id: int) -> int:
    """Queue change collection after a project discovery, once per run."""
    return queue.enqueue(
        db, COLLECT_CHANGES, f"{COLLECT_CHANGES}:after-run:{collection_run_id}", max_attempts=3
    )


def make_collect_changes(transport: httpx.BaseTransport | None = None) -> GitLabHandler:
    def collect(job: ClaimedJob, engine: Engine) -> None:
        settings = _settings()
        run_id = start_run(engine, settings.url, "changes", job)
        try:
            with Session(engine) as db:
                instance_id = instance_for(db, settings.url).id
                options = change_settings.load(db)
                db.commit()
            with GitLabClient(settings, transport=transport) as client:
                result = changes.collect(
                    client, engine, instance_id, options, Provenance(run_id, job.id)
                )
        except Exception as exc:
            _unexpected(engine, run_id, exc)
            raise
        problems = []
        if result.failed_projects:
            problems.append(f"changes of {result.failed_projects} projects could not be listed")
        failed_parts = sum(count for key, count in result.parts.items() if key.endswith(":failed"))
        if failed_parts:
            problems.append(f"{failed_parts} parts of changes could not be read")
        if result.rejected or (result.projects and result.failed_projects == result.projects):
            outcome: RunOutcome = "failed"
        else:
            outcome = "partial" if problems else "succeeded"
        finish_run(
            engine,
            run_id,
            outcome,
            seen=result.projects,
            counts=result.as_counts(),
            error="; ".join(problems) or None,
        )
        logger.info(
            "gitlab changes: %s projects read, %s failed: %s",
            result.projects,
            result.failed_projects,
            dict(result.changes),
            extra={"job_id": job.id},
        )
        if outcome == "failed":
            raise JobError("change collection read no project")
        # The change rules judge these observations: re-evaluate, once per run.
        with Session(engine) as db:
            queue_after_collection(db, run_id)
            db.commit()

    return collect


def queue_labels(db: Session, key: str) -> int:
    """Queue a label collection; ``key`` makes it once per discovery run or per plan applied."""
    return queue.enqueue(db, COLLECT_LABELS, f"{COLLECT_LABELS}:{key}", max_attempts=3)


def make_collect_labels(transport: httpx.BaseTransport | None = None) -> GitLabHandler:
    def collect(job: ClaimedJob, engine: Engine) -> None:
        settings = _settings()
        run_id = start_run(engine, settings.url, "labels", job)
        try:
            with Session(engine) as db:
                instance_id = instance_for(db, settings.url).id
                db.commit()
            with GitLabClient(settings, transport=transport) as client:
                result = labels.collect(client, engine, instance_id, Provenance(run_id, job.id))
        except Exception as exc:
            _unexpected(engine, run_id, exc)
            raise
        problems = []
        if result.failed:
            problems.append(f"labels of {result.failed} groups or projects could not be read")
        if result.rejected or (result.read and result.failed == result.read):
            outcome: RunOutcome = "failed"
        else:
            outcome = "partial" if problems else "succeeded"
        finish_run(
            engine,
            run_id,
            outcome,
            seen=result.read,
            counts=result.as_counts(),
            error="; ".join(problems) or None,
        )
        logger.info(
            "gitlab labels: %s groups and projects read, %s not read, %s labels",
            result.read,
            result.failed,
            result.labels,
            extra={"job_id": job.id},
        )
        if outcome == "failed":
            raise JobError("label collection read no group or project")
        # The label rule judges these observations: re-evaluate, once per run.
        with Session(engine) as db:
            queue_after_collection(db, run_id)
            db.commit()

    return collect


def queue_time(db: Session, collection_run_id: int) -> int:
    """Queue time collection after a project discovery, once per run."""
    return queue.enqueue(
        db, COLLECT_TIME, f"{COLLECT_TIME}:after-run:{collection_run_id}", max_attempts=3
    )


def make_collect_time(transport: httpx.BaseTransport | None = None) -> GitLabHandler:
    def collect(job: ClaimedJob, engine: Engine) -> None:
        settings = _settings()
        run_id = start_run(engine, settings.url, "time", job)
        try:
            with Session(engine) as db:
                instance_id = instance_for(db, settings.url).id
                options = time_settings.load(db)
                db.commit()
            with GitLabClient(settings, transport=transport) as client:
                result = timelogs.collect(
                    client, engine, instance_id, options, Provenance(run_id, job.id)
                )
        except Exception as exc:
            _unexpected(engine, run_id, exc)
            raise
        problems = []
        for what, counts in (("time logs", result.timelogs), ("estimates", result.estimates)):
            unread = sum(count for status, count in counts.items() if status != "ok")
            if unread:
                problems.append(f"{what} of {unread} projects could not be read")
        if result.rejected or (result.projects and result.timelogs.get("ok", 0) == 0):
            outcome: RunOutcome = "failed"
        else:
            outcome = "partial" if problems else "succeeded"
        finish_run(
            engine,
            run_id,
            outcome,
            seen=result.projects,
            counts=result.as_counts(),
            error="; ".join(problems) or None,
        )
        logger.info(
            "gitlab time: %s projects read: %s",
            result.projects,
            dict(result.timelogs),
            extra={"job_id": job.id},
        )
        if outcome == "failed":
            raise JobError("time collection read no project's time logs")

    return collect


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
            delays = scheduled_retry_delays(self.interval // 60)
            queued = [
                queue.enqueue(
                    db,
                    kind,
                    f"{kind}:scheduled:{bucket}",
                    max_attempts=len(delays) + 1,
                    retry_delays=delays or None,
                )
                for kind in SCHEDULED_KINDS
            ]
            db.commit()
        self._last_bucket = bucket
        logger.info("gitlab: scheduled collection %s (jobs %s)", bucket, queued)


class ConfiguredSchedule:
    """Follows the GitLab settings in force, rechecked every ``check_every`` seconds.

    GitLab connected, changed or disconnected in the web UI (ADR-0008) is scheduled, rescheduled
    or unscheduled without restarting the worker.
    """

    def __init__(
        self,
        engine: Engine,
        check_every: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine = engine
        self.check_every = check_every
        self.clock = clock
        self._next_check = 0.0
        self._schedule: Schedule | None = None
        self._problem: str | None = None

    @property
    def interval_minutes(self) -> int | None:
        """The interval being scheduled, or None when collection is not scheduled."""
        return self._schedule.interval // 60 if self._schedule is not None else None

    def _interval(self) -> int | None:
        try:
            settings = load_gitlab_settings()
        except ConfigurationError as exc:
            if str(exc) != self._problem:
                logger.error("gitlab: collection is not scheduled: %s", exc)
            self._problem = str(exc)
            return None
        self._problem = None
        if settings is None or settings.sync_interval_minutes == 0:
            return None
        return settings.sync_interval_minutes

    def __call__(self) -> None:
        if self.clock() >= self._next_check:
            self._next_check = self.clock() + self.check_every
            interval = self._interval()
            if interval is None:
                self._schedule = None
            elif self._schedule is None or self._schedule.interval != interval * 60:
                logger.info("gitlab: collecting every %s minutes", interval)
                self._schedule = Schedule(
                    self.engine, interval, check_every=self.check_every, clock=self.clock
                )
        if self._schedule is not None:
            self._schedule()


class CollectionHealth:
    """Logs one WARNING when collection becomes unhealthy and one when it recovers.

    Rechecked every ``check_every`` seconds from the freshness report's ``alert``, so operators
    can alert on the ``event`` field (``gitlab.collection_unhealthy`` and
    ``gitlab.collection_recovered``) instead of on every failed run. The state is held per worker
    process: a worker that starts while collection is unhealthy logs it once, and each worker
    process logs its own transitions.
    """

    UNHEALTHY = "gitlab.collection_unhealthy"
    RECOVERED = "gitlab.collection_recovered"

    def __init__(
        self,
        engine: Engine,
        check_every: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine = engine
        self.check_every = check_every
        self.clock = clock
        self._next_check = 0.0
        self.unhealthy = False

    def __call__(self) -> None:
        if self.clock() < self._next_check:
            return
        self._next_check = self.clock() + self.check_every
        try:
            settings = load_gitlab_settings()
        except ConfigurationError:
            return
        with Session(self.engine) as db:
            instance_id = instance_id_for(db, settings.url) if settings is not None else None
            alert = freshness_report(db, instance_id, settings).alert
        if alert is not None and not self.unhealthy:
            self.unhealthy = True
            logger.warning(
                "gitlab: collection unhealthy: %s",
                alert.message,
                extra={
                    "event": self.UNHEALTHY,
                    "reason": alert.reason,
                    "kinds": alert.kinds,
                    "consecutive_failures": alert.consecutive_failures,
                    "failing_since": alert.failing_since,
                    "last_success_at": alert.last_success_at,
                    "last_error": alert.last_error,
                },
            )
        elif alert is None and self.unhealthy:
            self.unhealthy = False
            logger.warning("gitlab: collection recovered", extra={"event": self.RECOVERED})


def ticks(*callables: Callable[[], None]) -> Callable[[], None]:
    """One worker tick that runs each callable in turn."""

    def tick() -> None:
        for call in callables:
            call()

    return tick
