"""Retention: delete what the operating profile no longer requires Baluarte to keep.

The ``retention.expire`` job runs once a day (``RetentionSchedule``) and, in one transaction:

* evaluations older than the evidence period, except a finding's latest;
* observations older than it, except the newest of each target and any an evaluation still
  refers to;
* evaluation and collection runs older than it that nothing still refers to;
* finished jobs and exports older than the job period that nothing still refers to;
* sessions, sign-ins and setup sessions that ended more than ``SESSION_DAYS`` ago;
* audit events older than the audit period, through ``baluarte_expire_audit`` (migration 0016),
  which the application's database role may run although it cannot delete audit events itself;
  while audit shipping is on, never one the destination has not received
  (``baluarte.compliance.shipping``);
* backup runs older than the evidence period, except the latest successful one.

Legal hold deletes nothing. Every run is audited as ``retention.expired`` with the cutoffs, the
counts and, for audit events, the first and last IDs and a SHA-256 over their digests.
"""

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, Engine, func, select, text
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.compliance import operations
from baluarte.compliance.shipping import oldest_unshipped
from baluarte.jobs import queue
from baluarte.jobs.queue import ClaimedJob

logger = logging.getLogger(__name__)

RETENTION_EXPIRE = "retention.expire"
ACTOR = "system:retention"
SESSION_DAYS = 30
DAY_SECONDS = 86400

# Each statement deletes rows older than :cutoff and returns how many.
_DELETE: list[tuple[str, str, str]] = [
    (
        "evaluation",
        "evidence",
        """
        DELETE FROM evaluation e
         WHERE e.evaluated_at < :cutoff
           AND NOT EXISTS (SELECT 1 FROM finding f WHERE f.latest_evaluation_id = e.id)
        """,
    ),
    (
        "observation",
        "evidence",
        """
        DELETE FROM observation o
         WHERE o.collected_at < :cutoff
           AND NOT EXISTS (SELECT 1 FROM evaluation e WHERE e.observation_id = o.id)
           AND EXISTS (
               SELECT 1 FROM observation n
                WHERE n.instance_id = o.instance_id
                  AND n.project_id IS NOT DISTINCT FROM o.project_id
                  AND n.group_id IS NOT DISTINCT FROM o.group_id
                  AND n.source = o.source
                  AND (n.collected_at, n.id) > (o.collected_at, o.id))
        """,
    ),
    (
        "evaluation_run",
        "evidence",
        """
        DELETE FROM evaluation_run r
         WHERE r.started_at < :cutoff AND r.status <> 'running'
           AND NOT EXISTS (SELECT 1 FROM evaluation e WHERE e.evaluation_run_id = r.id)
        """,
    ),
    (
        "collection_run",
        "evidence",
        """
        DELETE FROM collection_run r
         WHERE r.started_at < :cutoff AND r.status <> 'running'
           AND NOT EXISTS (SELECT 1 FROM observation o WHERE o.collection_run_id = r.id)
           AND NOT EXISTS (
               SELECT 1 FROM gitlab_security_policy p WHERE p.last_collection_run_id = r.id)
        """,
    ),
    (
        "collection_job",
        "jobs",
        """
        DELETE FROM collection_job j
         WHERE j.state IN ('succeeded', 'failed', 'cancelled')
           AND coalesce(j.finished_at, j.created_at) < :cutoff
           AND NOT EXISTS (SELECT 1 FROM collection_run r WHERE r.collection_job_id = j.id)
           AND NOT EXISTS (SELECT 1 FROM evaluation_run r WHERE r.collection_job_id = j.id)
           AND NOT EXISTS (SELECT 1 FROM observation o WHERE o.collection_job_id = j.id)
        """,
    ),
    (
        "export_job",
        "jobs",
        """
        DELETE FROM export_job x
         WHERE x.status IN ('succeeded', 'failed') AND x.created_at < :cutoff
        """,
    ),
    (
        "user_session",
        "sessions",
        """
        DELETE FROM user_session s
         WHERE s.expires_at < :cutoff OR s.revoked_at < :cutoff
        """,
    ),
    (
        "backup_run",
        "evidence",
        """
        DELETE FROM backup_run b
         WHERE b.finished_at < :cutoff
           AND b.id IS DISTINCT FROM (
               SELECT l.id FROM backup_run l
                WHERE l.status = 'succeeded' AND l.encrypted
                ORDER BY l.finished_at DESC, l.id DESC LIMIT 1)
        """,
    ),
    ("oidc_login", "sessions", "DELETE FROM oidc_login l WHERE l.expires_at < :cutoff"),
    ("setup_session", "sessions", "DELETE FROM setup_session s WHERE s.expires_at < :cutoff"),
]


def expire(db: Session) -> dict[str, Any]:
    """Delete what has expired and audit it; return the audit details. The caller commits."""
    profile = operations.load(db)
    settings = profile.settings
    now: datetime = db.execute(select(func.now())).scalar_one()
    cutoffs = {
        "audit": now - timedelta(days=int(settings["audit_retention_days"])),
        "evidence": now - timedelta(days=int(settings["evidence_retention_days"])),
        "jobs": now - timedelta(days=int(settings["job_retention_days"])),
        "sessions": now - timedelta(days=SESSION_DAYS),
    }
    unshipped = oldest_unshipped(db)
    if unshipped is not None and unshipped < cutoffs["audit"]:
        cutoffs["audit"] = unshipped
    details: dict[str, Any] = {
        "legal_hold": bool(settings["legal_hold"]),
        "audit_held_for_shipping": unshipped is not None and cutoffs["audit"] == unshipped,
        "cutoffs": {name: cutoff.isoformat() for name, cutoff in cutoffs.items()},
        "deleted": {},
        "audit_events": None,
    }
    if not settings["legal_hold"]:
        for table, period, statement in _DELETE:
            result = db.execute(text(statement), {"cutoff": cutoffs[period]})
            details["deleted"][table] = cast(CursorResult[Any], result).rowcount
        row = db.execute(
            text("SELECT * FROM baluarte_expire_audit(:cutoff)"), {"cutoff": cutoffs["audit"]}
        ).one()
        details["deleted"]["audit_event"] = row.deleted
        details["audit_events"] = {
            "deleted": row.deleted,
            "first_id": row.first_id,
            "last_id": row.last_id,
            # SHA-256 over the deleted events' details_digest values, in ID order.
            "digest": row.digest,
        }
    audit.record(
        db, actor=ACTOR, action="retention.expired", resource_type="retention", details=details
    )
    return details


def handle_expire(_job: ClaimedJob, engine: Engine) -> None:
    with Session(engine) as db:
        details = expire(db)
        db.commit()
    logger.info(
        "retention: %s",
        "legal hold is on; nothing deleted" if details["legal_hold"] else "expired old data",
        extra={"event": "retention.expired", "deleted": details["deleted"]},
    )


class RetentionSchedule:
    """Queues ``retention.expire`` once per UTC day, keyed by the day on the database clock so
    any number of workers queue it exactly once."""

    def __init__(
        self,
        engine: Engine,
        check_every: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine = engine
        self.check_every = check_every
        self.clock = clock
        self._next_check = 0.0
        self._last_day: int | None = None

    def __call__(self) -> None:
        if self.clock() < self._next_check:
            return
        self._next_check = self.clock() + self.check_every
        with Session(self.engine) as db:
            now = float(db.execute(select(func.extract("epoch", func.now()))).scalar_one())
            day = int(now // DAY_SECONDS)
            if day == self._last_day:
                return
            job_id = queue.enqueue(
                db, RETENTION_EXPIRE, f"{RETENTION_EXPIRE}:{day}", max_attempts=3
            )
            db.commit()
        self._last_day = day
        logger.info("retention: scheduled for day %s (job %s)", day, job_id)
