"""The ``notifications`` setting: the channels and which events go where.

- ``email``: SMTP ``host``, ``port``, ``security`` (``starttls`` or ``tls``; never plain text),
  optional ``username`` (its password is sealed), ``from_address``, up to 50 ``recipients`` and
  an optional ``ca_pem`` for a private certificate authority.
- ``webhook``: one https URL receiving Slack-compatible JSON. The URL embeds a token, so it is
  sealed like a password; only its host is kept in the setting, to show.
- ``events``: each event is switched on with the channels it goes to. ``new_findings`` has a
  ``min_severity`` (default ``high``) and ``review_waiting`` an ``older_than_days`` (default 2).

Secrets are write-only: a GET reports each as ``configured`` or ``not_set``. In an update a null
secret keeps the stored one and an empty string removes it. The stored SMTP password is only ever
sent to the host it was entered for: change the host and it must be entered again. Every change
is audited as ``notifications.settings_changed`` and every test as ``notifications.test_sent``.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.compliance.senders import DeliveryError, tls_context
from baluarte.configuration.crypto import SecretBox, SecretUnavailable, load_secret_box
from baluarte.configuration.store import (
    delete_secret,
    read_secret_value,
    read_setting,
    secret_stored,
    write_secret,
    write_setting,
)
from baluarte.db.models import StoredSetting
from baluarte.logs import register_secret
from baluarte.notifications.channels import (
    EmailSender,
    EmailTarget,
    Message,
    Sender,
    SendFailure,
    WebhookSender,
    webhook_host,
)
from baluarte.settings import ConfigurationError

NOTIFICATIONS = "notifications"
SMTP_PASSWORD = "notifications_smtp_password"  # noqa: S105  (a secret's name)
WEBHOOK_URL = "notifications_webhook_url"
MAX_RECIPIENTS = 50
CHANNELS: tuple["Channel", ...] = ("email", "webhook")
SEVERITY_ORDER = ("low", "medium", "high", "critical")

Channel = Literal["email", "webhook"]
Severity = Literal["low", "medium", "high", "critical"]
Security = Literal["starttls", "tls"]

HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$|^\[?[0-9A-Fa-f:.]+\]?$")
# An address as SMTP takes it: no display name, no spaces or line breaks.
ADDRESS = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,}$")

CANNOT_STORE = (
    "BALUARTE_SECRET_KEY_FILE is not configured, so secrets cannot be stored; see docs/SECURITY.md"
)


class NotificationsInvalid(Exception):
    """The settings are invalid (HTTP 422); the message names the field."""


class NotificationsConflict(Exception):
    """The settings cannot be stored now (HTTP 409)."""


# Stored and returned shapes --------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmailSettings(_Model):
    enabled: bool = False
    host: str = ""
    port: Annotated[int, Field(ge=1, le=65535)] = 587
    security: Security = "starttls"
    username: str | None = None
    from_address: str = ""
    recipients: list[str] = Field(default_factory=list)
    ca_pem: str | None = None


class WebhookSettings(_Model):
    enabled: bool = False
    # The host of the stored URL, to show; the URL itself is sealed.
    host: str | None = None
    ca_pem: str | None = None


class EventSettings(_Model):
    enabled: bool = False
    channels: list[Channel] = Field(default_factory=list)


class FindingsEvent(EventSettings):
    min_severity: Severity = "high"


class ReviewEvent(EventSettings):
    older_than_days: Annotated[int, Field(ge=1, le=30)] = 2


class Events(_Model):
    new_findings: FindingsEvent = Field(default_factory=FindingsEvent)
    collection_health: EventSettings = Field(default_factory=EventSettings)
    review_waiting: ReviewEvent = Field(default_factory=ReviewEvent)
    weekly_digest: EventSettings = Field(default_factory=EventSettings)


EventName = Literal["new_findings", "collection_health", "review_waiting", "weekly_digest"]


class NotificationSettings(_Model):
    email: EmailSettings = Field(default_factory=EmailSettings)
    webhook: WebhookSettings = Field(default_factory=WebhookSettings)
    events: Events = Field(default_factory=Events)

    def channels_for(self, event: EventName) -> list[Channel]:
        """The enabled channels an enabled event goes to; empty when it goes nowhere."""
        settings: EventSettings = getattr(self.events, event)
        if not settings.enabled:
            return []
        enabled = {"email": self.email.enabled, "webhook": self.webhook.enabled}
        return [
            channel for channel in CHANNELS if channel in settings.channels and enabled[channel]
        ]

    def any_channel(self) -> bool:
        return self.email.enabled or self.webhook.enabled


# Update shapes (secrets write-only) ------------------------------------------------------------


class EmailUpdate(_Model):
    enabled: bool = False
    host: str = ""
    port: int = 587
    security: Security = "starttls"
    username: str | None = None
    # Null keeps the stored password, an empty string removes it.
    password: str | None = None
    from_address: str = ""
    recipients: list[str] = Field(default_factory=list, max_length=500)
    ca_pem: str | None = None


class WebhookUpdate(_Model):
    enabled: bool = False
    # Null keeps the stored URL, an empty string removes it.
    url: str | None = None
    ca_pem: str | None = None


class EventsUpdate(_Model):
    new_findings: FindingsEvent = Field(default_factory=FindingsEvent)
    collection_health: EventSettings = Field(default_factory=EventSettings)
    review_waiting: ReviewEvent = Field(default_factory=ReviewEvent)
    weekly_digest: EventSettings = Field(default_factory=EventSettings)


class NotificationsUpdate(_Model):
    email: EmailUpdate = Field(default_factory=EmailUpdate)
    webhook: WebhookUpdate = Field(default_factory=WebhookUpdate)
    events: EventsUpdate = Field(default_factory=EventsUpdate)


# Loading ---------------------------------------------------------------------------------------


def load(db: Session) -> NotificationSettings:
    """The stored settings, or the defaults (everything off)."""
    stored = read_setting(db, NOTIFICATIONS)
    if stored is None:
        return NotificationSettings()
    try:
        return NotificationSettings.model_validate(stored)
    except ValidationError:
        # A setting that no longer validates sends nothing rather than something unintended.
        return NotificationSettings()


def stored_row(db: Session) -> StoredSetting | None:
    return db.get(StoredSetting, NOTIFICATIONS)


# Validation ------------------------------------------------------------------------------------


def _pem(value: str | None, field: str) -> str | None:
    if value is None or not value.strip():
        return None
    if "-----BEGIN CERTIFICATE-----" not in value:
        raise NotificationsInvalid(f"{field}: enter PEM certificates (-----BEGIN CERTIFICATE-----)")
    try:
        tls_context(value)
    except DeliveryError as exc:
        raise NotificationsInvalid(f"{field}: no certificate could be read") from exc
    return value.strip() + "\n"


def _address(value: str, field: str) -> str:
    address = value.strip()
    if len(address) > 254 or not ADDRESS.fullmatch(address):
        raise NotificationsInvalid(
            f"{field}: {address or 'an empty value'} is not an email address"
        )
    return address


def _email(update: EmailUpdate) -> EmailSettings:
    host = update.host.strip()
    if host and not HOST.fullmatch(host):
        raise NotificationsInvalid("email.host: enter a host name or IP address, without a scheme")
    if update.enabled and not host:
        raise NotificationsInvalid("email.host: enter the SMTP server's host name")
    if not 1 <= update.port <= 65535:
        raise NotificationsInvalid("email.port: enter a port from 1 to 65535")
    username = (update.username or "").strip() or None
    if username is not None and (len(username) > 255 or any(c in username for c in "\r\n")):
        raise NotificationsInvalid("email.username: at most 255 characters on one line")
    sender = update.from_address.strip()
    if sender:
        sender = _address(sender, "email.from_address")
    elif update.enabled:
        raise NotificationsInvalid("email.from_address: enter the address messages come from")
    recipients: list[str] = []
    for raw in update.recipients:
        if not raw.strip():
            continue
        address = _address(raw, "email.recipients")
        if address.lower() not in {r.lower() for r in recipients}:
            recipients.append(address)
    if len(recipients) > MAX_RECIPIENTS:
        raise NotificationsInvalid(f"email.recipients: at most {MAX_RECIPIENTS} addresses")
    if update.enabled and not recipients:
        raise NotificationsInvalid("email.recipients: enter at least one address")
    return EmailSettings(
        enabled=update.enabled,
        host=host,
        port=update.port,
        security=update.security,
        username=username,
        from_address=sender,
        recipients=recipients,
        ca_pem=_pem(update.ca_pem, "email.ca_pem"),
    )


def checked_webhook_url(url: str) -> str:
    value = url.strip()
    try:
        parts = urlsplit(value)
        valid_port = parts.port is None or 1 <= parts.port <= 65535
    except ValueError:
        parts, valid_port = urlsplit(""), False
    if parts.scheme != "https" or not parts.hostname or not valid_port:
        raise NotificationsInvalid("webhook.url: enter an https URL; plain http is never used")
    if parts.username or parts.password or parts.fragment:
        raise NotificationsInvalid("webhook.url: the URL must not hold a user name or fragment")
    if len(value) > 2000 or any(c.isspace() for c in value):
        raise NotificationsInvalid("webhook.url: at most 2000 characters, without spaces")
    return value


def _events(update: EventsUpdate) -> Events:
    def channels(values: list[Channel]) -> list[Channel]:
        return [channel for channel in CHANNELS if channel in values]

    return Events(
        new_findings=update.new_findings.model_copy(
            update={"channels": channels(update.new_findings.channels)}
        ),
        collection_health=update.collection_health.model_copy(
            update={"channels": channels(update.collection_health.channels)}
        ),
        review_waiting=update.review_waiting.model_copy(
            update={"channels": channels(update.review_waiting.channels)}
        ),
        weekly_digest=update.weekly_digest.model_copy(
            update={"channels": channels(update.weekly_digest.channels)}
        ),
    )


@dataclass(frozen=True)
class Secrets:
    """Secrets for a send: entered with the request, else the stored ones where still valid."""

    smtp_password: str | None
    webhook_url: str | None


@dataclass(frozen=True)
class Checked:
    settings: NotificationSettings
    # What the update does to each stored secret: a new value, "" to remove, None to keep.
    smtp_password: str | None
    webhook_url: str | None


def checked(db: Session, update: NotificationsUpdate) -> Checked:
    """Validate an update against what is stored; ``NotificationsInvalid`` names the field."""
    email = _email(update.email)
    previous = load(db)

    stored_password = secret_stored(db, SMTP_PASSWORD)
    password = update.email.password
    if email.username is None:
        # No username, no password: a stored one is removed.
        password = "" if stored_password else None
    elif password:
        if any(c in password for c in "\r\n") or len(password) > 1024:
            raise NotificationsInvalid("email.password: at most 1024 characters on one line")
    elif password is None and stored_password:
        moved = previous.email.host != email.host or previous.email.username != email.username
        if moved and email.enabled:
            raise NotificationsInvalid(
                "email.password: enter the password again, because the host or username changed"
            )
        if moved:
            # Never send a stored password to a server it was not entered for.
            password = ""
    elif email.enabled:
        raise NotificationsInvalid("email.password: enter the password for this username")

    url = update.webhook.url
    host = previous.webhook.host
    if url is not None and url.strip():
        url = checked_webhook_url(url)
        host = webhook_host(url)
    elif url is not None:
        url, host = "", None
    has_url = (url is not None and url != "") or (url is None and secret_stored(db, WEBHOOK_URL))
    if update.webhook.enabled and not has_url:
        raise NotificationsInvalid("webhook.url: enter the webhook URL")
    webhook = WebhookSettings(
        enabled=update.webhook.enabled,
        host=host if has_url else None,
        ca_pem=_pem(update.webhook.ca_pem, "webhook.ca_pem"),
    )
    return Checked(
        NotificationSettings(email=email, webhook=webhook, events=_events(update.events)),
        password,
        url,
    )


# Storing ---------------------------------------------------------------------------------------


def _secret_box() -> SecretBox | None:
    try:
        return load_secret_box()
    except ConfigurationError as exc:
        raise NotificationsConflict(str(exc)) from exc


def _summary(settings: NotificationSettings) -> dict[str, Any]:
    """What an audit event records: no secrets and no addresses, only their number."""
    return {
        "email": {
            "enabled": settings.email.enabled,
            "server": f"{settings.email.host}:{settings.email.port}"
            if settings.email.host
            else None,
            "security": settings.email.security,
            "authenticated": settings.email.username is not None,
            "recipients": len(settings.email.recipients),
        },
        "webhook": {"enabled": settings.webhook.enabled, "host": settings.webhook.host},
        "events": {
            name: {
                "enabled": event.enabled,
                "channels": list(event.channels),
                **{
                    key: value
                    for key, value in event.model_dump().items()
                    if key not in ("enabled", "channels")
                },
            }
            for name, event in (
                ("new_findings", settings.events.new_findings),
                ("collection_health", settings.events.collection_health),
                ("review_waiting", settings.events.review_waiting),
                ("weekly_digest", settings.events.weekly_digest),
            )
        },
    }


def update(db: Session, body: NotificationsUpdate, actor: str) -> NotificationSettings:
    """Validate, store and audit the settings. The caller commits."""
    result = checked(db, body)
    entering = [value for value in (result.smtp_password, result.webhook_url) if value]
    box = _secret_box()
    if entering and box is None:
        raise NotificationsConflict(CANNOT_STORE)
    previous = load(db)
    existed = stored_row(db) is not None
    write_setting(db, NOTIFICATIONS, result.settings.model_dump(mode="json"), actor)
    changes: dict[str, str] = {}
    for name, value, label in (
        (SMTP_PASSWORD, result.smtp_password, "smtp_password"),
        (WEBHOOK_URL, result.webhook_url, "webhook_url"),
    ):
        if value is None:
            continue
        if value == "":
            if secret_stored(db, name):
                delete_secret(db, name)
                changes[label] = "removed"
        elif box is not None:
            write_secret(db, name, value, box, actor)
            changes[label] = "replaced"
    audit.record(
        db,
        actor=actor,
        action="notifications.settings_changed",
        resource_type="settings",
        resource_id=NOTIFICATIONS,
        details={
            **_summary(result.settings),
            "secrets": changes,
            "previous": _summary(previous) if existed else None,
        },
    )
    return result.settings


# Secrets and senders ---------------------------------------------------------------------------


def stored_secrets(
    db: Session, box: SecretBox | None, channels: tuple[Channel, ...] = CHANNELS
) -> Secrets:
    """The stored secrets of ``channels``, decrypted; raises ``SecretUnavailable`` when one
    cannot be read."""
    return Secrets(
        smtp_password=read_secret_value(db, SMTP_PASSWORD, box) if "email" in channels else None,
        webhook_url=read_secret_value(db, WEBHOOK_URL, box) if "webhook" in channels else None,
    )


def secrets_for_test(db: Session, result: Checked) -> Secrets:
    """Entered secrets, else the stored ones (``checked`` already refused a moved password)."""
    for value in (result.smtp_password, result.webhook_url):
        if value:
            register_secret(value)
    needs_stored = result.smtp_password is None or result.webhook_url is None
    stored = Secrets(None, None)
    if needs_stored:
        try:
            stored = stored_secrets(db, _secret_box())
        except SecretUnavailable as exc:
            raise NotificationsConflict(str(exc)) from exc
    return Secrets(
        smtp_password=(
            result.smtp_password if result.smtp_password is not None else stored.smtp_password
        )
        or None,
        webhook_url=(result.webhook_url if result.webhook_url is not None else stored.webhook_url)
        or None,
    )


def default_sender(channel: Channel, settings: NotificationSettings, secrets: Secrets) -> Sender:
    if channel == "email":
        email = settings.email
        if not email.host or not email.from_address or not email.recipients:
            raise SendFailure("config", "the email channel is not complete")
        return EmailSender(
            EmailTarget(
                host=email.host,
                port=email.port,
                security=email.security,
                username=email.username,
                from_address=email.from_address,
                recipients=tuple(email.recipients),
                ca_pem=email.ca_pem,
            ),
            secrets.smtp_password,
        )
    if not secrets.webhook_url:
        raise SendFailure("config", "no webhook URL is stored")
    return WebhookSender(secrets.webhook_url, settings.webhook.ca_pem)


SenderFactory = Callable[[Channel, NotificationSettings, Secrets], Sender]


@dataclass(frozen=True)
class SendOutcome:
    ok: bool
    channel: Channel
    stage: str | None
    detail: str


def send_test(
    db: Session,
    body: NotificationsUpdate,
    channel: Channel,
    message: Message,
    actor: str,
    sender_factory: SenderFactory = default_sender,
) -> SendOutcome:
    """Send ``message`` through one channel of the settings in ``body``, without saving them.

    Audited as ``notifications.test_sent`` whatever the outcome. The caller commits.
    """
    result = checked(db, body)
    settings = result.settings
    if channel == "webhook" and result.webhook_url == "":
        raise NotificationsInvalid("webhook.url: enter the webhook URL")
    secrets = secrets_for_test(db, result)
    where = (
        f"{settings.email.host}:{settings.email.port}"
        if channel == "email"
        else f"webhook {settings.webhook.host or ''}".strip()
    )
    try:
        sender_factory(channel, settings, secrets).send(message)
        outcome = SendOutcome(True, channel, None, f"A test message was sent to {where}.")
    except SendFailure as exc:
        outcome = SendOutcome(False, channel, exc.stage, str(exc))
    audit.record(
        db,
        actor=actor,
        action="notifications.test_sent",
        resource_type="settings",
        resource_id=NOTIFICATIONS,
        details={
            "channel": channel,
            "ok": outcome.ok,
            "stage": outcome.stage,
            "destination": where,
            "recipients": len(settings.email.recipients) if channel == "email" else None,
        },
    )
    return outcome


# The API's view --------------------------------------------------------------------------------


def settings_view(db: Session, public_url: str | None) -> dict[str, Any]:
    """The settings as the API shows them: always complete, without secret values."""
    settings = load(db)
    row = stored_row(db)
    view: dict[str, Any] = settings.model_dump(mode="json")
    view["email"]["password"] = "configured" if secret_stored(db, SMTP_PASSWORD) else "not_set"
    view["webhook"]["url"] = "configured" if secret_stored(db, WEBHOOK_URL) else "not_set"
    view["configured"] = row is not None
    view["public_url"] = public_url
    view["updated_by"] = row.updated_by if row is not None else None
    view["updated_at"] = row.updated_at if row is not None else None
    return view
