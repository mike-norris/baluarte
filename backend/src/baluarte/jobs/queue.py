"""Enqueue, claim, renew and finish jobs.

All timestamps come from the database clock (``now()``), so workers on hosts with skewed clocks
still agree on lease expiry. Every write after a claim is fenced on the lease owner and attempt
number: a worker whose lease expired cannot overwrite the outcome of a later attempt.
"""

import logging
import random
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from baluarte.db.models import CollectionJob

logger = logging.getLogger(__name__)

QUEUED, RUNNING, SUCCEEDED, FAILED = "queued", "running", "succeeded", "failed"
LEASE_EXPIRED = "lease expired before the job finished"
MAX_ERROR_LENGTH = 1000
MAX_KEY_LENGTH = 200

BACKOFF_BASE = timedelta(seconds=30)
BACKOFF_CAP = timedelta(hours=1)
BACKOFF_JITTER = 0.2
# Payload key holding a job's own retry delays, in seconds; see ``retry_delay``.
RETRY_DELAYS = "retry_delays_seconds"


class JobError(Exception):
    """A failure worth retrying. The message is stored on the job, so it must hold no secrets."""


class PermanentJobError(JobError):
    """A failure that retrying cannot fix. The job fails without further attempts."""


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    kind: str
    idempotency_key: str
    payload: dict[str, Any]
    attempt: int
    max_attempts: int
    lease_owner: str


def backoff(attempt: int, rng: Callable[[], float] = random.random) -> timedelta:
    """Delay before retrying after ``attempt`` failed: 30s, 60s, 120s ... capped at one hour,
    with +/-20% jitter so jobs that failed together do not retry together."""
    delay: timedelta = min(BACKOFF_BASE * (2 ** max(attempt - 1, 0)), BACKOFF_CAP)
    return delay * (1 + BACKOFF_JITTER * (2 * rng() - 1))


def retry_delay(
    attempt: int, payload: Mapping[str, Any] | None, rng: Callable[[], float] = random.random
) -> timedelta:
    """Delay before retrying after ``attempt`` failed.

    A job enqueued with ``retry_delays`` waits the delay listed for that attempt (the last one
    for any later attempt), with the same jitter; any other job uses ``backoff``.
    """
    delays = (payload or {}).get(RETRY_DELAYS)
    if (
        isinstance(delays, list)
        and delays
        and all(isinstance(d, int | float) and not isinstance(d, bool) and d > 0 for d in delays)
    ):
        delay = timedelta(seconds=delays[min(max(attempt, 1), len(delays)) - 1])
        return delay * (1 + BACKOFF_JITTER * (2 * rng() - 1))
    return backoff(attempt, rng)


def enqueue(
    db: Session,
    kind: str,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
    *,
    max_attempts: int = 5,
    delay: timedelta = timedelta(0),
    retry_delays: Sequence[float] | None = None,
) -> int:
    """Add a job unless one with the same idempotency key exists; return the job's ID either way.

    The key identifies the work, not the attempt: enqueueing the same key twice is a no-op even
    after the first job finished. ``retry_delays`` (seconds) replaces the default backoff for this
    job and is kept in its payload under ``RETRY_DELAYS``. The caller commits.
    """
    if not kind or not idempotency_key or len(idempotency_key) > MAX_KEY_LENGTH:
        raise ValueError("kind and an idempotency key of at most 200 characters are required")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if retry_delays is not None:
        if not retry_delays or any(d <= 0 for d in retry_delays):
            raise ValueError("retry_delays must be positive numbers of seconds")
        payload = {**(payload or {}), RETRY_DELAYS: list(retry_delays)}
    inserted = db.execute(
        pg_insert(CollectionJob)
        .values(
            kind=kind,
            idempotency_key=idempotency_key,
            payload=payload or {},
            max_attempts=max_attempts,
            run_after=func.now() + delay,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(CollectionJob.id)
    ).scalar_one_or_none()
    if inserted is not None:
        return inserted
    return db.execute(
        select(CollectionJob.id).where(CollectionJob.idempotency_key == idempotency_key)
    ).scalar_one()


def expire_leases(db: Session, rng: Callable[[], float] = random.random) -> int:
    """Return jobs whose worker stopped renewing its lease to the queue, or fail them when they
    have no attempts left. Returns how many jobs were affected. The caller commits."""
    expired = db.execute(
        select(CollectionJob)
        .where(CollectionJob.state == RUNNING, CollectionJob.lease_until < func.now())
        .with_for_update(skip_locked=True)
    ).scalars()
    count = 0
    for job in expired:
        count += 1
        logger.warning("job %s: lease held by %s expired", job.id, job.lease_owner)
        job.lease_owner = None
        job.lease_until = None
        job.error = LEASE_EXPIRED
        if job.attempt >= job.max_attempts:
            job.state = FAILED
            job.finished_at = func.now()
        else:
            job.state = QUEUED
            job.run_after = func.now() + retry_delay(job.attempt, job.payload, rng)
    return count


def claim(db: Session, owner: str, lease: timedelta, kinds: Collection[str]) -> ClaimedJob | None:
    """Lease the next due job of one of ``kinds``, or return None. Commits the claim.

    ``FOR UPDATE SKIP LOCKED`` lets concurrent workers claim different jobs without blocking.
    """
    if not kinds:
        return None
    expire_leases(db)
    job = db.execute(
        select(CollectionJob)
        .where(
            CollectionJob.state == QUEUED,
            CollectionJob.run_after <= func.now(),
            CollectionJob.kind.in_(list(kinds)),
        )
        .order_by(CollectionJob.run_after, CollectionJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if job is None:
        db.commit()
        return None
    job.state = RUNNING
    job.attempt += 1
    job.lease_owner = owner
    job.lease_until = func.now() + lease
    job.started_at = func.coalesce(CollectionJob.started_at, func.now())
    claimed = ClaimedJob(
        id=job.id,
        kind=job.kind,
        idempotency_key=job.idempotency_key,
        payload=dict(job.payload),
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        lease_owner=owner,
    )
    db.commit()
    return claimed


def _held(job: ClaimedJob) -> Any:
    return and_(
        CollectionJob.id == job.id,
        CollectionJob.state == RUNNING,
        CollectionJob.lease_owner == job.lease_owner,
        CollectionJob.attempt == job.attempt,
    )


def _fenced_update(db: Session, job: ClaimedJob, **values: Any) -> bool:
    result = db.execute(update(CollectionJob).where(_held(job)).values(**values))
    db.commit()
    held = bool(result.rowcount)  # type: ignore[attr-defined]
    if not held:
        logger.warning("job %s: lease for attempt %s was lost", job.id, job.attempt)
    return held


def renew(db: Session, job: ClaimedJob, lease: timedelta) -> bool:
    """Extend the lease. False means it was lost and another worker may now own the job."""
    return _fenced_update(db, job, lease_until=func.now() + lease)


def complete(db: Session, job: ClaimedJob) -> bool:
    return _fenced_update(
        db,
        job,
        state=SUCCEEDED,
        lease_owner=None,
        lease_until=None,
        error=None,
        finished_at=func.now(),
    )


def fail(
    db: Session,
    job: ClaimedJob,
    error: str,
    *,
    retry: bool,
    rng: Callable[[], float] = random.random,
) -> bool:
    """Record a failed attempt: requeue with backoff, or fail for good when out of attempts."""
    message = error[:MAX_ERROR_LENGTH]
    if retry and job.attempt < job.max_attempts:
        return _fenced_update(
            db,
            job,
            state=QUEUED,
            lease_owner=None,
            lease_until=None,
            error=message,
            run_after=func.now() + retry_delay(job.attempt, job.payload, rng),
        )
    return _fenced_update(
        db,
        job,
        state=FAILED,
        lease_owner=None,
        lease_until=None,
        error=message,
        finished_at=func.now(),
    )
