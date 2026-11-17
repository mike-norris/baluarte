"""Notification settings (``settings:manage``) and recent deliveries. See docs/NOTIFICATIONS.md.

The SMTP password and the webhook URL are sealed with the secret key and never returned; a GET
reports each as ``configured`` or ``not_set``. Deliveries are readable by administrators and by
auditors (``compliance:read``).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte.auth.dependencies import Principal, current_principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import Notification, NotificationDelivery
from baluarte.notifications import messages
from baluarte.notifications import settings as notification_settings
from baluarte.notifications.settings import (
    Channel,
    EventSettings,
    FindingsEvent,
    NotificationsUpdate,
    ReviewEvent,
    SenderFactory,
)

router = APIRouter(tags=["notifications"])

Admin = Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))]
Database = Annotated[Session, Depends(database)]
SecretState = Literal["configured", "not_set"]
LATEST = 50


def notification_sender() -> SenderFactory:
    """How a test message reaches its channel. Overridden in tests."""
    return notification_settings.default_sender


def public_url() -> str | None:
    """Baluarte's address, for links in messages. Overridden in tests."""
    return messages.public_url()


@contextmanager
def notification_errors() -> Iterator[None]:
    try:
        yield
    except notification_settings.NotificationsInvalid as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except notification_settings.NotificationsConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


class EmailOut(BaseModel):
    enabled: bool
    host: str
    port: int
    security: Literal["starttls", "tls"]
    username: str | None
    password: SecretState
    from_address: str
    recipients: list[str]
    ca_pem: str | None


class WebhookOut(BaseModel):
    enabled: bool
    url: SecretState
    # The stored URL's host; its path holds a token and is never shown.
    host: str | None
    ca_pem: str | None


class EventsOut(BaseModel):
    new_findings: FindingsEvent
    collection_health: EventSettings
    review_waiting: ReviewEvent
    weekly_digest: EventSettings


class NotificationsConfig(BaseModel):
    configured: bool
    email: EmailOut
    webhook: WebhookOut
    events: EventsOut
    # Baluarte's address, which links in messages start with; None leaves links out.
    public_url: str | None
    updated_by: str | None
    updated_at: datetime | None


class ChannelTest(NotificationsUpdate):
    channel: Channel


class ChannelTestResult(BaseModel):
    ok: bool
    channel: Channel
    # What failed: config, connect, tls, auth or rejected; None when the message was sent.
    stage: str | None
    detail: str


class DeliveryOut(BaseModel):
    id: int
    notification_id: int
    event_type: str
    subject: str
    channel: Channel
    status: Literal["pending", "sent", "failed"]
    attempts: int
    created_at: datetime
    next_attempt_at: datetime | None
    sent_at: datetime | None
    last_error: str | None


class DeliveryList(BaseModel):
    items: list[DeliveryOut]


def _view(db: Session, url: str | None) -> NotificationsConfig:
    return NotificationsConfig.model_validate(notification_settings.settings_view(db, url))


@router.get(
    "/settings/notifications",
    response_model=NotificationsConfig,
    summary="Notification settings",
)
def read_notifications(
    _: Admin, db: Database, url: Annotated[str | None, Depends(public_url)]
) -> NotificationsConfig:
    return _view(db, url)


@router.put(
    "/settings/notifications",
    response_model=NotificationsConfig,
    summary="Change the notification settings",
    description="A null `email.password` or `webhook.url` keeps the stored one, an empty string "
    "removes it. The SMTP password must be entered again when the host or username changes. "
    "Errors name the field (`email.host: ...`). 409 when no secret key is configured to seal a "
    "secret. Audited as `notifications.settings_changed`.",
)
def write_notifications(
    body: NotificationsUpdate,
    principal: Admin,
    db: Database,
    url: Annotated[str | None, Depends(public_url)],
) -> NotificationsConfig:
    with notification_errors():
        notification_settings.update(db, body, principal.actor)
    db.commit()
    return _view(db, url)


@router.post(
    "/settings/notifications/test",
    response_model=ChannelTestResult,
    summary="Send a test message through one channel",
    description="Sends a test message through `channel` using the settings in the body, without "
    "saving them; secrets left null are the stored ones. TLS is always verified. `stage` says "
    "what failed. Audited as `notifications.test_sent`.",
)
def send_test_message(
    body: ChannelTest,
    principal: Admin,
    db: Database,
    sender: Annotated[SenderFactory, Depends(notification_sender)],
    url: Annotated[str | None, Depends(public_url)],
) -> ChannelTestResult:
    update = NotificationsUpdate.model_validate(body.model_dump(exclude={"channel"}))
    with notification_errors():
        result = notification_settings.send_test(
            db,
            update,
            body.channel,
            messages.connection_test_message(url),
            principal.actor,
            sender,
        )
    db.commit()
    return ChannelTestResult(
        ok=result.ok, channel=result.channel, stage=result.stage, detail=result.detail
    )


def deliveries_reader(
    principal: Annotated[Principal, Depends(current_principal)],
) -> Principal:
    """Administrators, and auditors through ``compliance:read``."""
    allowed = {Permission.SETTINGS_MANAGE, Permission.COMPLIANCE_READ}
    if not allowed & principal.permissions:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
    return principal


@router.get(
    "/notifications/deliveries",
    response_model=DeliveryList,
    summary="The latest notification deliveries",
    description="The latest 50 deliveries, newest first, with their event, channel, status, "
    "times and the last error (redacted). Readable with `settings:manage` or `compliance:read`.",
)
def list_deliveries(
    _: Annotated[Principal, Depends(deliveries_reader)], db: Database
) -> DeliveryList:
    rows = db.execute(
        select(NotificationDelivery, Notification.event_type, Notification.subject)
        .join(Notification, Notification.id == NotificationDelivery.notification_id)
        .order_by(NotificationDelivery.created_at.desc(), NotificationDelivery.id.desc())
        .limit(LATEST)
    ).all()
    return DeliveryList(
        items=[
            DeliveryOut(
                id=row.id,
                notification_id=row.notification_id,
                event_type=event_type,
                subject=subject,
                channel="email" if row.channel == "email" else "webhook",
                status=row.status,  # type: ignore[arg-type]
                attempts=row.attempts,
                created_at=row.created_at,
                next_attempt_at=row.next_attempt_at,
                sent_at=row.sent_at,
                last_error=row.last_error,
            )
            for row, event_type, subject in rows
        ]
    )
