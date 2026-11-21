"""What produces a notification, each at most once.

Every notification has a ``dedup_key`` naming its event, and ``notification.dedup_key`` is
unique: the first worker to record an event wins, any other finds it there and records nothing.

- ``new_findings`` (``findings:<run id>``): after an evaluation run, the findings it opened at or
  above the configured severity. Queued by the evaluation runner.
- ``collection_unhealthy`` (``collection:unhealthy:after-<id>``) and ``collection_recovered``
  (``collection:recovered:<id>``): on the transitions collection health computes. Each key names
  the notification before it, so the two alternate and every worker agrees which comes next.
- ``review_waiting`` (``review:<UTC date>``): once a day from 08:00 UTC, items waiting for review
  longer than the configured days, and exceptions and plans expiring within 7 days. A day with
  nothing to report records the key with no deliveries.
- ``weekly_digest`` (``digest:<ISO week>``): Mondays from 08:00 UTC.

A notification gets one delivery per channel that is on and chosen for its event. Nothing is
recorded for an event that is off or goes to no channel that is on.
"""

import hashlib
import logging
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from baluarte.db.models import (
    ComplianceException,
    Evaluation,
    EvaluationRun,
    Finding,
    GitLabProject,
    IssuePlan,
    LabelPlan,
    Notification,
    NotificationDelivery,
    PolicyException,
    RemediationPlan,
)
from baluarte.notifications import messages
from baluarte.notifications.channels import Message
from baluarte.notifications.settings import (
    SEVERITY_ORDER,
    Channel,
    EventName,
    NotificationSettings,
    load,
)
from baluarte.workspaces import current_workspace_id

logger = logging.getLogger(__name__)

# Scheduled messages go out from this hour (UTC); the digest on Mondays.
SEND_HOUR = 8
EXPIRING_DAYS = 7
MAX_LISTED = 10
OUTCOMES = ("pass", "fail", "unknown", "unsupported", "excepted")


def body_digest(message: Message) -> str:
    return hashlib.sha256(f"{message.subject}\n{message.text}".encode()).hexdigest()


def record(
    db: Session,
    event_type: str,
    dedup_key: str,
    message: Message,
    channels: Sequence[Channel],
) -> int | None:
    """Record a notification and a pending delivery per channel; None when the key exists.

    The caller commits.
    """
    notification_id = db.execute(
        insert(Notification)
        .values(
            workspace_id=current_workspace_id(db),
            event_type=event_type,
            dedup_key=dedup_key,
            subject=message.subject,
            body_text=message.text,
            body_html=message.html,
            body_digest=body_digest(message),
        )
        .on_conflict_do_nothing(index_elements=["dedup_key"])
        .returning(Notification.id)
    ).scalar_one_or_none()
    if notification_id is None:
        return None
    for channel in channels:
        db.add(NotificationDelivery(notification_id=notification_id, channel=channel))
    db.flush()
    logger.info(
        "notification %s (%s) recorded for %s",
        notification_id,
        event_type,
        ", ".join(channels) or "no channel",
        extra={"event": "notifications.recorded", "notification_type": event_type},
    )
    return notification_id


def exists(db: Session, dedup_key: str) -> bool:
    return db.scalar(select(Notification.id).where(Notification.dedup_key == dedup_key)) is not None


# New findings ----------------------------------------------------------------------------------


def _at_least(severity: str) -> list[str]:
    return list(SEVERITY_ORDER[SEVERITY_ORDER.index(severity) :])


def watching_findings(db: Session) -> bool:
    return bool(load(db).channels_for("new_findings"))


def open_findings(db: Session, instance_id: int) -> set[int]:
    """IDs of the instance's open findings: taken before a run, to tell which it opened."""
    return set(
        db.scalars(
            select(Finding.id)
            .join(GitLabProject, GitLabProject.id == Finding.project_id)
            .where(GitLabProject.instance_id == instance_id, Finding.state == "open")
        )
    )


def _path(row: Any) -> str:
    """A project's path; a personal project's path names its owner, so it is not shown."""
    if row.namespace_kind == "user":
        return f"personal project {row.gitlab_id}"
    return str(row.path_with_namespace)


def notify_new_findings(
    db: Session,
    run_id: int,
    instance_id: int,
    before: set[int],
    base: str | None = None,
) -> int | None:
    """Record the findings the run opened (new or reopened) at or above the threshold."""
    settings = load(db)
    channels = settings.channels_for("new_findings")
    if not channels:
        return None
    threshold = settings.events.new_findings.min_severity
    rows = db.execute(
        select(
            Finding.id,
            Finding.severity,
            GitLabProject.path_with_namespace,
            GitLabProject.namespace_kind,
            GitLabProject.gitlab_id,
            Finding.policy_name,
            Finding.rule_id,
        )
        .join(GitLabProject, GitLabProject.id == Finding.project_id)
        .where(
            GitLabProject.instance_id == instance_id,
            Finding.state == "open",
            Finding.severity.in_(_at_least(threshold)),
        )
    ).all()
    opened = [row for row in rows if row.id not in before]
    if not opened:
        return None
    opened.sort(key=lambda row: (-SEVERITY_ORDER.index(row.severity), _path(row), row.rule_id))
    listed = [
        messages.FindingLine(row.id, row.severity, _path(row), row.policy_name, row.rule_id)
        for row in opened[:MAX_LISTED]
    ]
    message = messages.new_findings(
        base if base is not None else messages.public_url(), run_id, threshold, len(opened), listed
    )
    return record(db, "new_findings", f"findings:{run_id}", message, channels)


# Collection health -----------------------------------------------------------------------------

COLLECTION_EVENTS = ("collection_unhealthy", "collection_recovered")


def notify_collection_health(db: Session, alert: Any, base: str | None = None) -> int | None:
    """Record the transition to ``alert`` (a ``CollectionAlert`` or None), if it is one."""
    channels = load(db).channels_for("collection_health")
    if not channels:
        return None
    latest = db.execute(
        select(Notification.id, Notification.event_type)
        .where(Notification.event_type.in_(COLLECTION_EVENTS))
        .order_by(Notification.id.desc())
        .limit(1)
    ).first()
    unhealthy_now = latest is not None and latest.event_type == "collection_unhealthy"
    url = base if base is not None else messages.public_url()
    if alert is not None and not unhealthy_now:
        message = messages.collection_unhealthy(
            url,
            alert.reason,
            alert.kinds,
            alert.consecutive_failures,
            alert.failing_since,
            alert.last_success_at,
        )
        key = f"collection:unhealthy:after-{latest.id if latest is not None else 0}"
        return record(db, "collection_unhealthy", key, message, channels)
    if alert is None and unhealthy_now and latest is not None:
        since = db.scalar(select(Notification.created_at).where(Notification.id == latest.id))
        message = messages.collection_recovered(url, since)
        return record(
            db, "collection_recovered", f"collection:recovered:{latest.id}", message, channels
        )
    return None


# Review queue and expiring items ---------------------------------------------------------------


def _system_principal() -> Any:
    from baluarte.auth.dependencies import Principal
    from baluarte.auth.roles import Role

    return Principal(
        user_id=0,
        subject="system:notifications",
        email=None,
        display_name=None,
        roles=frozenset({Role.ADMIN}),
        session_id=0,
        session_expires_at=datetime.max.replace(tzinfo=UTC),
        csrf_token="",
    )


def review_counts(db: Session, older_than: datetime | None) -> tuple[dict[str, int], list[str]]:
    """Review queue items proposed before ``older_than`` (all when None), by kind, and the kinds
    with more waiting than one page (the counts are then a lower bound)."""
    from baluarte.api.v1 import reviews

    result = reviews.queue(_system_principal(), db, reviews.SOURCES)
    counts: Counter[str] = Counter(
        item.kind for item in result.items if older_than is None or item.proposed_at < older_than
    )
    return {kind: counts[kind] for kind in reviews.SOURCES if counts[kind]}, list(result.partial)


def expiring(db: Session, now: datetime, days: int = EXPIRING_DAYS) -> dict[str, int]:
    """Approved exceptions and live plans that expire within ``days``, by kind."""
    until = now + timedelta(days=days)
    counts: dict[str, int] = {}

    def count(statement: Any) -> int:
        return int(db.scalar(statement) or 0)

    counts["exception"] = count(
        select(func.count()).where(
            PolicyException.state == "approved",
            PolicyException.expires_at > now,
            PolicyException.expires_at <= until,
        )
    )
    today = now.astimezone(UTC).date()
    counts["control_exception"] = count(
        select(func.count()).where(
            ComplianceException.state == "approved",
            ComplianceException.expires_on > today,
            ComplianceException.expires_on <= until.astimezone(UTC).date(),
        )
    )
    for kind, model in (
        ("label_plan", LabelPlan),
        ("issue_plan", IssuePlan),
        ("remediation_plan", RemediationPlan),
    ):
        counts[kind] = count(
            select(func.count()).where(
                model.state.in_(("submitted", "approved")),
                model.expires_at > now,
                model.expires_at <= until,
            )
        )
    return {kind: value for kind, value in counts.items() if value}


def review_key(day: date) -> str:
    return f"review:{day.isoformat()}"


def digest_key(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"digest:{year}-W{week:02d}"


def compose_review(db: Session, now: datetime, base: str | None = None) -> int | None:
    """Record today's review reminder, unless it is recorded or the event goes nowhere."""
    settings = load(db)
    channels = settings.channels_for("review_waiting")
    day = now.astimezone(UTC).date()
    if not channels or exists(db, review_key(day)):
        return None
    days = settings.events.review_waiting.older_than_days
    waiting, partial = review_counts(db, now - timedelta(days=days))
    soon = expiring(db, now)
    message = messages.review_waiting(
        base if base is not None else messages.public_url(), day, days, waiting, partial, soon
    )
    # A quiet day is recorded without deliveries, so it is composed once.
    send_to = channels if waiting or soon else []
    return record(db, "review_waiting", review_key(day), message, send_to)


def _results(db: Session) -> tuple[datetime | None, dict[str, int]]:
    from baluarte.scope.exclusions import coverage

    run = db.scalars(
        select(EvaluationRun)
        .where(EvaluationRun.status == "succeeded")
        .order_by(EvaluationRun.started_at.desc(), EvaluationRun.id.desc())
        .limit(1)
    ).first()
    if run is None:
        return None, {}
    rows = db.execute(
        select(Evaluation.outcome, func.count())
        .join(GitLabProject, GitLabProject.id == Evaluation.project_id)
        .where(
            Evaluation.evaluation_run_id == run.id,
            coverage(db, run.instance_id).project_included(),
        )
        .group_by(Evaluation.outcome)
    ).all()
    found = {outcome: int(count) for outcome, count in rows}
    return run.finished_at, {outcome: found[outcome] for outcome in OUTCOMES if found.get(outcome)}


def _controls(db: Session) -> tuple[dict[str, int] | None, bool]:
    from baluarte.compliance import posture

    try:
        with db.begin_nested():
            result = posture.evaluate(db)
    except Exception as exc:
        logger.warning("notifications: the compliance posture failed (%s)", type(exc).__name__)
        return None, True
    if not result.frameworks:
        return None, False
    return result.summary(), False


def compose_digest(db: Session, now: datetime, base: str | None = None) -> int | None:
    """Record this week's digest, unless it is recorded or the event goes nowhere."""
    settings = load(db)
    channels = settings.channels_for("weekly_digest")
    day = now.astimezone(UTC).date()
    key = digest_key(day)
    if not channels or exists(db, key):
        return None
    evaluated_at, results = _results(db)
    findings = {
        str(severity): int(count)
        for severity, count in db.execute(
            select(Finding.severity, func.count())
            .where(Finding.state == "open")
            .group_by(Finding.severity)
        )
    }
    controls, controls_error = _controls(db)
    queue, partial = review_counts(db, None)
    digest = messages.Digest(
        week=key.removeprefix("digest:"),
        evaluated_at=evaluated_at,
        results=results,
        findings=findings,
        controls=controls,
        controls_error=controls_error,
        queue=queue,
        queue_partial=partial,
        expiring=expiring(db, now),
    )
    message = messages.weekly_digest(base if base is not None else messages.public_url(), digest)
    return record(db, "weekly_digest", key, message, channels)


def due(settings: NotificationSettings, now: datetime) -> list[EventName]:
    """The scheduled events whose time has come today (whether or not already recorded)."""
    moment = now.astimezone(UTC)
    if moment.hour < SEND_HOUR:
        return []
    found: list[EventName] = []
    if settings.channels_for("review_waiting"):
        found.append("review_waiting")
    if moment.weekday() == 0 and settings.channels_for("weekly_digest"):
        found.append("weekly_digest")
    return found


def scheduled_keys(events: Iterable[EventName], now: datetime) -> list[str]:
    day = now.astimezone(UTC).date()
    return [review_key(day) if event == "review_waiting" else digest_key(day) for event in events]
