"""Evaluation freshness: are the stored results current, and did the last evaluation succeed?

Same statuses and order as collection freshness:

- ``failed``: the latest finished run failed, whatever succeeded before it.
- ``never``: no run has finished.
- ``stale``: the latest successful run started longer ago than ``stale_after``.
- ``fresh``: otherwise.

``behind_collection`` is true when a project discovery finished after the latest successful
evaluation started: the stored results predate the inventory until the queued evaluation runs.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte.db.models import CollectionJob, CollectionRun, EvaluationRun
from baluarte.integrations.gitlab.freshness import UNSCHEDULED_STALE_AFTER, stale_after
from baluarte.integrations.gitlab.settings import GitLabSettings
from baluarte.policy.evaluation.runner import EVALUATE

EvaluationFreshnessStatus = Literal["not_configured", "failed", "never", "stale", "fresh"]


class EvaluationRunOut(BaseModel):
    id: int
    status: str
    collection_job_id: int | None
    attempt: int | None
    started_at: datetime
    finished_at: datetime | None
    projects: int
    counts: dict[str, Any]
    error: str | None


class EvaluationFreshness(BaseModel):
    status: EvaluationFreshnessStatus
    checked_at: datetime
    stale_after_minutes: int
    running: bool
    queued: bool
    behind_collection: bool
    last_run: EvaluationRunOut | None
    last_success_at: datetime | None


def report(
    db: Session, instance_id: int | None, settings: GitLabSettings | None
) -> EvaluationFreshness:
    now: datetime = db.execute(select(func.now())).scalar_one()
    limit = stale_after(settings) if settings is not None else UNSCHEDULED_STALE_AFTER
    queued = (
        db.scalars(
            select(CollectionJob.id)
            .where(CollectionJob.kind == EVALUATE, CollectionJob.state == "queued")
            .limit(1)
        ).first()
        is not None
    )
    empty = EvaluationFreshness(
        status="not_configured" if settings is None else "never",
        checked_at=now,
        stale_after_minutes=int(limit.total_seconds() // 60),
        running=False,
        queued=queued,
        behind_collection=False,
        last_run=None,
        last_success_at=None,
    )
    if settings is None or instance_id is None:
        return empty
    scope = EvaluationRun.instance_id == instance_id
    latest = db.scalars(
        select(EvaluationRun)
        .where(scope, EvaluationRun.status != "running")
        .order_by(EvaluationRun.finished_at.desc(), EvaluationRun.id.desc())
        .limit(1)
    ).first()
    success = db.scalars(
        select(EvaluationRun)
        .where(scope, EvaluationRun.status == "succeeded")
        .order_by(EvaluationRun.finished_at.desc(), EvaluationRun.id.desc())
        .limit(1)
    ).first()
    running = db.execute(
        select(func.count()).where(scope, EvaluationRun.status == "running")
    ).scalar_one()
    last_collection: datetime | None = db.execute(
        select(func.max(CollectionRun.finished_at)).where(
            CollectionRun.instance_id == instance_id,
            CollectionRun.kind == "projects",
            CollectionRun.status.in_(("succeeded", "partial")),
        )
    ).scalar_one()
    status: EvaluationFreshnessStatus
    if latest is None:
        status = "never"
    elif latest.status == "failed":
        status = "failed"
    elif success is None or now - success.started_at > limit:
        status = "stale"
    else:
        status = "fresh"
    return EvaluationFreshness(
        status=status,
        checked_at=now,
        stale_after_minutes=int(limit.total_seconds() // 60),
        running=running > 0,
        queued=queued,
        behind_collection=(
            last_collection is not None
            and (success is None or last_collection > success.started_at)
        ),
        last_run=EvaluationRunOut.model_validate(latest, from_attributes=True) if latest else None,
        last_success_at=success.finished_at if success else None,
    )
