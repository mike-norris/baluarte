"""Collection freshness: is each kind of GitLab data current, and did its last run succeed?

The status of a kind is decided by its runs:

- ``failed``: the latest finished run failed, whatever succeeded before it. A failure is never
  hidden behind an older success.
- ``never``: no run has finished.
- ``stale``: the latest finished run succeeded, but longer ago than ``stale_after``.
- ``partial``: the latest run finished but some resources could not be confirmed.
- ``fresh``: the latest run succeeded within ``stale_after``.

The overall status is the worst of the three kinds, in the order above.
"""

from datetime import datetime, timedelta
from typing import Any, Literal, get_args

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte.db.models import CollectionRun, GitLabGroup, GitLabProject
from baluarte.integrations.gitlab.runs import RunKind
from baluarte.integrations.gitlab.settings import GitLabSettings

Freshness = Literal["failed", "never", "stale", "partial", "fresh"]
SEVERITY: tuple[Freshness, ...] = ("failed", "never", "stale", "partial", "fresh")
UNSCHEDULED_STALE_AFTER = timedelta(hours=24)


def stale_after(settings: GitLabSettings) -> timedelta:
    """Two missed intervals when scheduled, so one slow run does not flag everything."""
    if settings.sync_interval_minutes:
        return timedelta(minutes=2 * settings.sync_interval_minutes)
    return UNSCHEDULED_STALE_AFTER


class RunOut(BaseModel):
    id: int
    kind: str
    status: str
    collection_job_id: int | None
    attempt: int | None
    started_at: datetime
    finished_at: datetime | None
    seen: int
    counts: dict[str, Any]
    error: str | None


class KindFreshness(BaseModel):
    status: Freshness
    running: bool
    last_run: RunOut | None
    last_success_at: datetime | None


class ResourceCounts(BaseModel):
    active: int = 0
    missing: int = 0
    inaccessible: int = 0
    with_errors: int = 0


class FreshnessReport(BaseModel):
    status: Freshness | Literal["not_configured"]
    checked_at: datetime
    stale_after_minutes: int
    sync_interval_minutes: int | None
    collections: dict[str, KindFreshness]
    resources: dict[str, ResourceCounts]


def _kind(
    db: Session, instance_id: int, kind: str, now: datetime, limit: timedelta
) -> KindFreshness:
    scope = (CollectionRun.instance_id == instance_id) & (CollectionRun.kind == kind)
    latest = db.scalars(
        select(CollectionRun)
        .where(scope, CollectionRun.status != "running")
        .order_by(CollectionRun.finished_at.desc(), CollectionRun.id.desc())
        .limit(1)
    ).first()
    last_success_at = db.execute(
        select(func.max(CollectionRun.finished_at)).where(
            scope, CollectionRun.status.in_(("succeeded", "partial"))
        )
    ).scalar_one()
    running = db.execute(
        select(func.count()).where(scope, CollectionRun.status == "running")
    ).scalar_one()
    status: Freshness
    if latest is None:
        status = "never"
    elif latest.status == "failed":
        status = "failed"
    elif last_success_at is None or now - last_success_at > limit:
        status = "stale"
    elif latest.status == "partial":
        status = "partial"
    else:
        status = "fresh"
    return KindFreshness(
        status=status,
        running=running > 0,
        last_run=RunOut.model_validate(latest, from_attributes=True) if latest else None,
        last_success_at=last_success_at,
    )


def _resources(
    db: Session, model: type[GitLabGroup] | type[GitLabProject], instance_id: int
) -> ResourceCounts:
    counts = ResourceCounts()
    for state, count in db.execute(
        select(model.state, func.count())
        .where(model.instance_id == instance_id)
        .group_by(model.state)
    ):
        setattr(counts, state, count)
    counts.with_errors = db.execute(
        select(func.count()).where(model.instance_id == instance_id, model.last_error.is_not(None))
    ).scalar_one()
    return counts


def report(
    db: Session, instance_id: int | None, settings: GitLabSettings | None
) -> FreshnessReport:
    now: datetime = db.execute(select(func.now())).scalar_one()
    if settings is None:
        return FreshnessReport(
            status="not_configured",
            checked_at=now,
            stale_after_minutes=int(UNSCHEDULED_STALE_AFTER.total_seconds() // 60),
            sync_interval_minutes=None,
            collections={},
            resources={},
        )
    limit = stale_after(settings)
    kinds: tuple[str, ...] = get_args(RunKind)
    collections = {
        kind: (
            _kind(db, instance_id, kind, now, limit)
            if instance_id is not None
            else KindFreshness(status="never", running=False, last_run=None, last_success_at=None)
        )
        for kind in kinds
    }
    resources = (
        {
            "groups": _resources(db, GitLabGroup, instance_id),
            "projects": _resources(db, GitLabProject, instance_id),
        }
        if instance_id is not None
        else {"groups": ResourceCounts(), "projects": ResourceCounts()}
    )
    return FreshnessReport(
        status=min((c.status for c in collections.values()), key=SEVERITY.index),
        checked_at=now,
        stale_after_minutes=int(limit.total_seconds() // 60),
        sync_interval_minutes=settings.sync_interval_minutes,
        collections=collections,
        resources=resources,
    )
