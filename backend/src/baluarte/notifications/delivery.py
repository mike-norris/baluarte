"""The ``notify.deliver`` job and the schedule that queues it.

``NotificationSchedule`` runs in the worker's per-loop hook. At most once a minute it queues
``notify.deliver`` (keyed by the minute on the database clock, so any number of workers queue it
once) when a delivery is due or a scheduled message (the review reminder, the weekly digest) has
not been composed yet, unless one is already queued or running.

Each run first composes the scheduled messages that are due, then sends due deliveries one at a
time: each is locked with ``FOR UPDATE SKIP LOCKED`` while it is sent, so two workers never send
the same one. A failed attempt is retried after 1, 5, 15 and 60 minutes, then the delivery is
``failed``. A run sends at most ``MAX_PER_RUN`` messages and stops after ``RUN_SECONDS``.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from baluarte.compliance.senders import short
from baluarte.configuration.crypto import SecretUnavailable, load_secret_box
from baluarte.db.models import CollectionJob, Notification, NotificationDelivery
from baluarte.jobs import queue
from baluarte.jobs.queue import ClaimedJob
from baluarte.notifications import events
from baluarte.notifications.channels import Message, SendFailure
from baluarte.notifications.settings import (
    Channel,
    NotificationSettings,
    SenderFactory,
    default_sender,
    load,
    stored_secrets,
)
from baluarte.settings import ConfigurationError

logger = logging.getLogger(__name__)

NOTIFY_DELIVER = "notify.deliver"
# Minutes to wait after each failed attempt; after the last, the delivery has failed.
RETRY_MINUTES = (1, 5, 15, 60)
MAX_ATTEMPTS = len(RETRY_MINUTES) + 1
MAX_PER_RUN = 20
RUN_SECONDS = 50.0
SCHEDULE_SECONDS = 60.0
# A delivery whose channel was turned off fails at once, with a reason starting so.
STAGE_OFF = "Not sent"


@dataclass(frozen=True)
class RunResult:
    sent: int
    failed: int
    retrying: int


def compose_due(engine: Engine) -> int:
    """Record the scheduled messages whose time has come; returns how many were recorded."""
    with Session(engine) as db:
        now: datetime = db.execute(select(func.now())).scalar_one()
        due = events.due(load(db), now)
    composers = {"review_waiting": events.compose_review, "weekly_digest": events.compose_digest}
    composed = 0
    for event in due:
        with Session(engine) as db:
            try:
                if composers[event](db, now) is not None:
                    composed += 1
                db.commit()
            except Exception as exc:
                # One message failing to compose must not hold up the others or the deliveries.
                db.rollback()
                logger.warning(
                    "notifications: composing %s failed (%s)",
                    event,
                    type(exc).__name__,
                    extra={"event": "notifications.compose_failed"},
                )
    return composed


def _attempt(
    db: Session,
    row: NotificationDelivery,
    settings: NotificationSettings,
    sender_factory: SenderFactory,
) -> str | None:
    """Send one delivery; None when sent, else the reason, safe to store."""
    channel: Channel = "email" if row.channel == "email" else "webhook"
    enabled = settings.email.enabled if channel == "email" else settings.webhook.enabled
    if not enabled:
        return f"{STAGE_OFF}: the {channel} channel is turned off"
    notification = db.get_one(Notification, row.notification_id)
    try:
        secrets = stored_secrets(db, load_secret_box(), (channel,))
        sender = sender_factory(channel, settings, secrets)
        sender.send(Message(notification.subject, notification.body_text, notification.body_html))
    except SendFailure as exc:
        return str(exc)
    except (SecretUnavailable, ConfigurationError) as exc:
        return short(f"Not configured: {exc}")
    return None


def deliver(
    engine: Engine,
    *,
    sender_factory: SenderFactory = default_sender,
    max_messages: int = MAX_PER_RUN,
    run_seconds: float = RUN_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> RunResult:
    """Send the deliveries that are due, oldest first, within the run's limits."""
    deadline = clock() + run_seconds
    sent = failed = retrying = 0
    for _ in range(max_messages):
        if clock() >= deadline:
            break
        with Session(engine) as db:
            row = db.scalars(
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.status == "pending",
                    NotificationDelivery.next_attempt_at <= func.now(),
                )
                .order_by(NotificationDelivery.next_attempt_at, NotificationDelivery.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).first()
            if row is None:
                db.rollback()
                break
            error = _attempt(db, row, load(db), sender_factory)
            row.attempts = row.attempts + 1
            if error is None:
                row.status = "sent"
                row.sent_at = func.now()
                row.next_attempt_at = None
                row.last_error = None
                sent += 1
            else:
                row.last_error = short(error)
                if row.attempts >= MAX_ATTEMPTS or error.startswith(STAGE_OFF):
                    row.status = "failed"
                    row.next_attempt_at = None
                    failed += 1
                else:
                    row.next_attempt_at = func.now() + timedelta(
                        minutes=RETRY_MINUTES[row.attempts - 1]
                    )
                    retrying += 1
                logger.warning(
                    "notification delivery %s (%s) attempt %s failed: %s",
                    row.id,
                    row.channel,
                    row.attempts,
                    row.last_error,
                    extra={"event": "notifications.delivery_failed", "channel": row.channel},
                )
            db.commit()
    return RunResult(sent, failed, retrying)


def handle_deliver(_job: ClaimedJob, engine: Engine) -> None:
    composed = compose_due(engine)
    result = deliver(engine)
    if result.sent or result.failed or composed:
        logger.info(
            "notifications: %s composed, %s sent, %s failed, %s to retry",
            composed,
            result.sent,
            result.failed,
            result.retrying,
            extra={"event": "notifications.delivered", "sent": result.sent},
        )
    # A failed attempt is retried by the delivery's own schedule, not by the job.


def _work_waiting(db: Session, now: datetime) -> bool:
    pending = db.scalar(
        select(NotificationDelivery.id)
        .where(
            NotificationDelivery.status == "pending",
            NotificationDelivery.next_attempt_at <= func.now(),
        )
        .limit(1)
    )
    if pending is not None:
        return True
    settings = load(db)
    keys = events.scheduled_keys(events.due(settings, now), now)
    return any(not events.exists(db, key) for key in keys)


class NotificationSchedule:
    """Queues ``notify.deliver`` at most once a minute while there is something to send."""

    def __init__(
        self,
        engine: Engine,
        check_every: float = SCHEDULE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine = engine
        self.check_every = check_every
        self.clock = clock
        self._next_check = 0.0

    def __call__(self) -> None:
        if self.clock() < self._next_check:
            return
        self._next_check = self.clock() + self.check_every
        with Session(self.engine) as db:
            now: datetime = db.execute(select(func.now())).scalar_one()
            if not _work_waiting(db, now):
                return
            active = db.scalar(
                select(CollectionJob.id)
                .where(
                    CollectionJob.kind == NOTIFY_DELIVER,
                    CollectionJob.state.in_(("queued", "running")),
                )
                .limit(1)
            )
            if active is not None:
                return
            minute = int(now.timestamp() // 60)
            queue.enqueue(db, NOTIFY_DELIVER, f"{NOTIFY_DELIVER}:{minute}", max_attempts=1)
            db.commit()
