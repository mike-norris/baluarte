"""Collection freshness: is each kind of GitLab data current, and did its last run succeed?

The status of a kind is decided by its runs:

- ``failed``: the latest finished run failed, whatever succeeded before it. A failure is never
  hidden behind an older success.
- ``never``: no run has finished.
- ``stale``: the latest finished run succeeded, but longer ago than ``stale_after``.
- ``partial``: the latest run finished but some resources could not be confirmed.
- ``fresh``: the latest run succeeded within ``stale_after``.

The overall status is the worst of the three kinds, in the order above.

The report also carries:

- ``alert``: collection is unhealthy, so operators and auditors are told rather than having to
  notice a status. ``failing`` when a kind's last ``FAILURE_ALERT_AFTER`` finished runs (job
  attempts) all failed; ``overdue`` when collection is scheduled and a kind's last success is
  older than ``stale_after`` (two intervals), e.g. because no worker is running. Failing wins.
- ``population``: which groups and projects the inventory can cover, from the token's user in
  the latest capability snapshot. Only an administrator's token sees the whole instance.
"""

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte.db.models import CollectionRun, GitLabGroup, GitLabInstance, GitLabProject
from baluarte.integrations.gitlab.runs import INVENTORY_KINDS, RunKind
from baluarte.integrations.gitlab.settings import GitLabSettings

Freshness = Literal["failed", "never", "stale", "partial", "fresh"]
SEVERITY: tuple[Freshness, ...] = ("failed", "never", "stale", "partial", "fresh")
UNSCHEDULED_STALE_AFTER = timedelta(hours=24)
# Failed runs in a row, per kind, before collection counts as failing. Each attempt is a run, so
# one scheduled collection whose retries all fail (three or four attempts) reaches it, while a
# single failure that the next attempt recovers from does not.
FAILURE_ALERT_AFTER = 3


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
    # Finished runs since the last success that failed, and when the first of them started.
    consecutive_failures: int = 0
    failing_since: datetime | None = None


class CollectionAlert(BaseModel):
    reason: Literal["failing", "overdue"]
    kinds: list[str]
    consecutive_failures: int
    failing_since: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    message: str


PopulationBasis = Literal["instance", "token_visibility", "unknown"]


class Population(BaseModel):
    """What the inventory can cover: everything the token's user can see, and no more."""

    basis: PopulationBasis
    token_username: str | None
    is_admin: bool | None
    # Whether the token could use administrator access; an administrator without it sees only
    # what the account sees as a member.
    admin_access: bool | None = None
    statement: str


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
    alert: CollectionAlert | None = None
    population: Population | None = None


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
    failures = scope & (CollectionRun.status == "failed")
    if last_success_at is not None:
        failures &= CollectionRun.finished_at > last_success_at
    consecutive, failing_since = db.execute(
        select(func.count(), func.min(CollectionRun.started_at)).where(failures)
    ).one()
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
        consecutive_failures=consecutive,
        failing_since=failing_since,
    )


def kind_freshness(
    db: Session, instance_id: int | None, kind: RunKind, settings: GitLabSettings | None
) -> KindFreshness:
    """Freshness of one collection kind on its own, e.g. security policy discovery."""
    if instance_id is None or settings is None:
        return KindFreshness(status="never", running=False, last_run=None, last_success_at=None)
    now: datetime = db.execute(select(func.now())).scalar_one()
    return _kind(db, instance_id, kind, now, stale_after(settings))


def population(snapshot: dict[str, Any] | None) -> Population:
    """The population statement for the token in an instance's capability ``snapshot``."""
    user = (snapshot or {}).get("token_user")
    if not isinstance(user, dict):
        user = {}
    username = user.get("username") if isinstance(user.get("username"), str) else None
    is_admin = user.get("is_admin") is True
    admin_access = user.get("admin_access")
    if is_admin and admin_access is True:
        return Population(
            basis="instance",
            token_username=username,
            is_admin=True,
            admin_access=True,
            statement="Inventory covers every group and project on the instance.",
        )
    if username is not None:
        if is_admin:
            # Only a successful admin-only read proves the whole instance is visible.
            why = (
                f"{username} is a GitLab administrator, but this token could not use "
                "administrator access (with Admin Mode on, the token needs the admin_mode scope)"
            )
        else:
            why = f"{username} is not a GitLab administrator"
        return Population(
            basis="token_visibility",
            token_username=username,
            is_admin=is_admin,
            admin_access=admin_access if isinstance(admin_access, bool) else None,
            statement=(
                f"Inventory covers only the groups and projects visible to {username}. {why}, "
                "so projects this account cannot see are not included and are not checked."
            ),
        )
    return Population(
        basis="unknown",
        token_username=None,
        is_admin=None,
        statement=(
            "Baluarte has not confirmed which GitLab account the token belongs to, so it cannot "
            "say whether the inventory covers the whole instance. Projects the token cannot see "
            "are not included and are not checked."
        ),
    )


def alert(
    collections: dict[str, KindFreshness], scheduled: bool, limit: timedelta, now: datetime
) -> CollectionAlert | None:
    """Whether collection is unhealthy; see the module docstring."""
    failing = {
        kind: c for kind, c in collections.items() if c.consecutive_failures >= FAILURE_ALERT_AFTER
    }
    overdue = (
        {
            kind: c
            for kind, c in collections.items()
            if c.last_success_at is not None and now - c.last_success_at > limit
        }
        if scheduled
        else {}
    )
    affected = failing or overdue
    if not affected:
        return None
    reason: Literal["failing", "overdue"] = "failing" if failing else "overdue"
    successes = [c.last_success_at for c in affected.values() if c.last_success_at is not None]
    last_success_at = min(successes) if len(successes) == len(affected) else None
    since = [c.failing_since for c in affected.values() if c.failing_since is not None]
    failing_since = min(since) if since else None
    latest = max(
        (c.last_run for c in affected.values() if c.last_run is not None),
        key=lambda run: (run.finished_at or run.started_at, run.id),
        default=None,
    )
    last_error = latest.error if latest is not None and latest.status == "failed" else None
    count = max(c.consecutive_failures for c in affected.values())
    seen = (
        f"last succeeded at {last_success_at.isoformat()}"
        if last_success_at is not None
        else "has not succeeded"
    )
    if reason == "failing":
        message = (
            f"GitLab collection has failed {count} times in a row"
            + (f" since {failing_since.isoformat()}" if failing_since else "")
            + f" and {seen}. Check that Baluarte can reach GitLab and that the token is valid."
        )
    else:
        message = (
            f"GitLab collection {seen}, more than "
            f"{int(limit.total_seconds() // 60)} minutes ago. Check that the worker is running."
        )
    return CollectionAlert(
        reason=reason,
        kinds=sorted(affected),
        consecutive_failures=count,
        failing_since=failing_since,
        last_success_at=last_success_at,
        last_error=last_error,
        message=message,
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
    kinds: tuple[str, ...] = INVENTORY_KINDS
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
    instance = db.get(GitLabInstance, instance_id) if instance_id is not None else None
    return FreshnessReport(
        status=min((c.status for c in collections.values()), key=SEVERITY.index),
        checked_at=now,
        stale_after_minutes=int(limit.total_seconds() // 60),
        sync_interval_minutes=settings.sync_interval_minutes,
        collections=collections,
        resources=resources,
        alert=alert(collections, bool(settings.sync_interval_minutes), limit, now),
        population=population(instance.capability_snapshot if instance is not None else None),
    )
