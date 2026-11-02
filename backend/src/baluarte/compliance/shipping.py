"""Audit shipping: copying every audit event, in ID order, to a log store outside Baluarte.

The ``audit_shipping`` setting names one destination, syslog over TLS or Elasticsearch/OpenSearch
(``baluarte.compliance.senders``); its credentials are sealed in ``app_secret``. The worker queues
``audit.ship`` every minute while events are waiting (``ShippingSchedule``). Each run takes the
destination's ``audit_shipment`` row with ``FOR UPDATE SKIP LOCKED``, so only one run ships at
a time, and sends batches of events after its cursor until it has caught up or used its time.

Shipping reads from the database rather than the log stream, so nothing is lost while the
destination is down: the cursor stays where it was and the next run resends from there. A new
destination gets a new cursor and receives the whole retained history. Delivery is at least
once; every event carries its ID and digest, so the receiving side can drop duplicates
(Elasticsearch does, by document ID). While shipping is on, retention never expires an event the
destination has not received.
"""

import hashlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.compliance.senders import (
    DeliveryError,
    ElasticsearchDestination,
    ElasticsearchSender,
    Outcome,
    Record,
    Sender,
    SyslogDestination,
    SyslogSender,
    short,
    tls_context,
)
from baluarte.configuration.crypto import SecretBox, SecretUnavailable, load_secret_box
from baluarte.configuration.store import (
    AUDIT_SHIPPING,
    AUDIT_SHIPPING_ES_API_KEY,
    AUDIT_SHIPPING_ES_PASSWORD,
    delete_secret,
    read_secret_value,
    read_setting,
    secret_stored,
    write_secret,
    write_setting,
)
from baluarte.db.models import AuditEvent, AuditShipment, CollectionJob, StoredSetting
from baluarte.jobs import queue
from baluarte.jobs.queue import ClaimedJob, JobError
from baluarte.logs import register_secret
from baluarte.settings import ConfigurationError

logger = logging.getLogger(__name__)

AUDIT_SHIP = "audit.ship"
BATCH_SIZE = 500
RUN_SECONDS = 50.0
SCHEDULE_SECONDS = 60.0

ShippingType = Literal["syslog_tls", "elasticsearch"]
Auth = Literal["api_key", "basic"]
TYPES: tuple[ShippingType, ...] = ("syslog_tls", "elasticsearch")
HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$|^\[?[0-9A-Fa-f:.]+\]?$")
# Elasticsearch index names: lower case, no leading -, _ or +, none of \ / * ? " < > | , # :
INDEX = re.compile(r"^[a-z0-9][a-z0-9._-]{0,254}$")


class ShippingInvalid(Exception):
    """The settings are invalid (HTTP 422)."""


class ShippingConflict(Exception):
    """The settings cannot be stored now (HTTP 409)."""


@dataclass(frozen=True)
class ShippingConfig:
    enabled: bool
    type: ShippingType
    syslog: SyslogDestination | None
    elasticsearch: ElasticsearchDestination | None

    def destination(self) -> SyslogDestination | ElasticsearchDestination:
        target = self.syslog if self.type == "syslog_tls" else self.elasticsearch
        if target is None:
            raise ShippingInvalid(f"No {self.type} destination is configured")
        return target

    def summary(self) -> str:
        """The destination without credentials, as stored on the cursor and shown in status."""
        target = self.destination()
        if isinstance(target, SyslogDestination):
            return f"syslog_tls {target.host}:{target.port}"
        return f"elasticsearch {target.url.rstrip('/')} index {target.index}"

    def fingerprint(self) -> str:
        return hashlib.sha256(self.summary().encode("utf-8")).hexdigest()


# Settings --------------------------------------------------------------------------------------


def _syslog(raw: dict[str, Any] | None) -> SyslogDestination | None:
    if not raw:
        return None
    return SyslogDestination(
        host=str(raw.get("host", "")),
        port=int(raw.get("port", 6514)),
        server_name=raw.get("server_name") or None,
        ca_pem=raw.get("ca_pem") or None,
    )


def _elasticsearch(raw: dict[str, Any] | None) -> ElasticsearchDestination | None:
    if not raw:
        return None
    auth: Auth = "basic" if raw.get("auth") == "basic" else "api_key"
    return ElasticsearchDestination(
        url=str(raw.get("url", "")),
        index=str(raw.get("index", "baluarte-audit")),
        auth=auth,
        username=raw.get("username") or None,
        ca_pem=raw.get("ca_pem") or None,
    )


def load_config(db: Session) -> ShippingConfig | None:
    value = read_setting(db, AUDIT_SHIPPING)
    if value is None or value.get("type") not in TYPES:
        return None
    return ShippingConfig(
        enabled=bool(value.get("enabled", False)),
        type=value["type"],
        syslog=_syslog(value.get("syslog")),
        elasticsearch=_elasticsearch(value.get("elasticsearch")),
    )


def stored_row(db: Session) -> StoredSetting | None:
    return db.get(StoredSetting, AUDIT_SHIPPING)


def _pem(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    if "-----BEGIN CERTIFICATE-----" not in value:
        raise ShippingInvalid("ca_pem must hold PEM certificates (-----BEGIN CERTIFICATE-----)")
    try:
        tls_context(value)
    except DeliveryError as exc:
        raise ShippingInvalid("ca_pem holds no certificate that could be read") from exc
    return value.strip() + "\n"


def checked_syslog(raw: dict[str, Any] | None) -> SyslogDestination:
    if not raw:
        raise ShippingInvalid("Enter the syslog host")
    host = str(raw.get("host") or "").strip()
    if not HOST.fullmatch(host):
        raise ShippingInvalid("syslog host must be a host name or IP address, without a scheme")
    port = raw.get("port", 6514)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ShippingInvalid("syslog port must be between 1 and 65535")
    server_name = str(raw.get("server_name") or "").strip() or None
    if server_name is not None and not HOST.fullmatch(server_name):
        raise ShippingInvalid("server_name must be a host name")
    return SyslogDestination(host, port, server_name, _pem(raw.get("ca_pem")))


def checked_elasticsearch(raw: dict[str, Any] | None) -> ElasticsearchDestination:
    if not raw:
        raise ShippingInvalid("Enter the Elasticsearch or OpenSearch URL")
    url = str(raw.get("url") or "").strip().rstrip("/")
    try:
        parts = urlsplit(url)
        valid_port = parts.port is None or 1 <= parts.port <= 65535
    except ValueError:
        parts, valid_port = urlsplit(""), False
    if parts.scheme != "https" or not parts.hostname or not valid_port:
        raise ShippingInvalid("The Elasticsearch URL must be an https URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ShippingInvalid("The URL must not hold credentials, a query or a fragment")
    index = str(raw.get("index") or "baluarte-audit").strip()
    if not INDEX.fullmatch(index):
        raise ShippingInvalid(
            "index must be lower case letters, digits, '.', '_' or '-', starting with a letter "
            "or digit"
        )
    raw_auth = raw.get("auth", "api_key")
    if raw_auth not in ("api_key", "basic"):
        raise ShippingInvalid("auth must be api_key or basic")
    auth: Auth = "basic" if raw_auth == "basic" else "api_key"
    username = str(raw.get("username") or "").strip() or None
    if auth == "basic" and username is None:
        raise ShippingInvalid("Enter the username for basic authentication")
    return ElasticsearchDestination(
        url, index, auth, username if auth == "basic" else None, _pem(raw.get("ca_pem"))
    )


def checked(body: dict[str, Any]) -> ShippingConfig:
    kind = body.get("type")
    if kind not in TYPES:
        raise ShippingInvalid("type must be syslog_tls or elasticsearch")
    enabled = bool(body.get("enabled"))
    if kind == "syslog_tls":
        return ShippingConfig(enabled, kind, checked_syslog(body.get("syslog")), None)
    return ShippingConfig(enabled, kind, None, checked_elasticsearch(body.get("elasticsearch")))


def _as_value(config: ShippingConfig) -> dict[str, Any]:
    value: dict[str, Any] = {"enabled": config.enabled, "type": config.type}
    if config.syslog is not None:
        s = config.syslog
        value["syslog"] = {
            "host": s.host,
            "port": s.port,
            "server_name": s.server_name,
            "ca_pem": s.ca_pem,
        }
    if config.elasticsearch is not None:
        e = config.elasticsearch
        value["elasticsearch"] = {
            "url": e.url,
            "index": e.index,
            "auth": e.auth,
            "username": e.username,
            "ca_pem": e.ca_pem,
        }
    return value


def _secret_box() -> SecretBox | None:
    try:
        return load_secret_box()
    except ConfigurationError as exc:
        raise ShippingConflict(str(exc)) from exc


CANNOT_STORE = (
    "BALUARTE_SECRET_KEY_FILE is not configured, so credentials cannot be stored; see "
    "docs/SECURITY.md"
)


@dataclass(frozen=True)
class Credentials:
    api_key: str | None
    password: str | None


def _required_secret(config: ShippingConfig) -> str | None:
    """The stored secret the destination needs, if any."""
    if config.type != "elasticsearch" or config.elasticsearch is None:
        return None
    if config.elasticsearch.auth == "api_key":
        return AUDIT_SHIPPING_ES_API_KEY
    return AUDIT_SHIPPING_ES_PASSWORD


def _same_url(db: Session, config: ShippingConfig) -> bool:
    previous = load_config(db)
    return (
        previous is not None
        and previous.elasticsearch is not None
        and config.elasticsearch is not None
        and previous.elasticsearch.url == config.elasticsearch.url
    )


def credentials_for(
    db: Session,
    config: ShippingConfig,
    *,
    api_key: str | None,
    password: str | None,
    box: SecretBox | None,
) -> Credentials:
    """The credentials to use: those entered, else the stored ones while the URL is unchanged.

    A stored credential is only ever sent to the URL it was entered for.
    """
    needed = _required_secret(config)
    if needed is None:
        return Credentials(None, None)
    value = _entered(needed, api_key, password)
    if value is not None:
        register_secret(value)
    else:
        _require_stored(db, config, needed)
        try:
            value = read_secret_value(db, needed, box)
        except SecretUnavailable as exc:
            raise ShippingConflict(str(exc)) from exc
        if value is None:
            raise ShippingInvalid("The stored credential is missing; enter it again")
    if needed == AUDIT_SHIPPING_ES_API_KEY:
        return Credentials(value, None)
    return Credentials(None, value)


def _entered(needed: str | None, api_key: str | None, password: str | None) -> str | None:
    if needed == AUDIT_SHIPPING_ES_API_KEY:
        return api_key
    if needed == AUDIT_SHIPPING_ES_PASSWORD:
        return password
    return None


def _require_stored(db: Session, config: ShippingConfig, needed: str) -> None:
    if not secret_stored(db, needed) or not _same_url(db, config):
        what = "API key" if needed == AUDIT_SHIPPING_ES_API_KEY else "password"
        raise ShippingInvalid(f"Enter the {what} (again when the URL changes)")


def update(
    db: Session,
    body: dict[str, Any],
    *,
    api_key: str | None,
    password: str | None,
    actor: str,
) -> ShippingConfig:
    """Validate, store and audit the settings. The caller commits."""
    config = checked(body)
    box = _secret_box()
    needed = _required_secret(config)
    entered = _entered(needed, api_key, password)
    if needed is not None and entered is None:
        _require_stored(db, config, needed)
    if entered is not None and box is None:
        raise ShippingConflict(CANNOT_STORE)
    previous = load_config(db)

    write_setting(db, AUDIT_SHIPPING, _as_value(config), actor)
    if needed is not None and entered is not None and box is not None:
        write_secret(db, needed, entered, box, actor)
    for name in (AUDIT_SHIPPING_ES_API_KEY, AUDIT_SHIPPING_ES_PASSWORD):
        if name != needed:
            delete_secret(db, name)
    audit.record(
        db,
        actor=actor,
        action="settings.audit_shipping_updated",
        resource_type="settings",
        resource_id=AUDIT_SHIPPING,
        details={
            "enabled": config.enabled,
            "type": config.type,
            "destination": config.summary(),
            "credential_replaced": entered is not None,
            "previous": (
                {"enabled": previous.enabled, "destination": previous.summary()}
                if previous is not None
                else None
            ),
        },
    )
    return config


# Delivery --------------------------------------------------------------------------------------

SenderFactory = Callable[[ShippingConfig, Credentials], Sender]


def default_sender(
    config: ShippingConfig,
    credentials: Credentials,
    transport: httpx.BaseTransport | None = None,
) -> Sender:
    target = config.destination()
    if isinstance(target, SyslogDestination):
        return SyslogSender(target)
    return ElasticsearchSender(
        target,
        api_key=credentials.api_key,
        password=credentials.password,
        transport=transport,
    )


def event_body(event: AuditEvent) -> dict[str, Any]:
    """The event as the ``baluarte.audit`` log line states it."""
    return {
        "id": event.id,
        "actor": event.actor,
        "action": event.action,
        "resource_type": event.resource_type,
        "resource_id": event.resource_id,
        "details": event.details,
        "details_digest": event.details_digest,
        "occurred_at": event.occurred_at.isoformat(timespec="milliseconds"),
    }


def record_for(event: AuditEvent) -> Record:
    return Record(doc_id=f"baluarte-{event.id}", body=event_body(event))


def test_destination(
    db: Session,
    body: dict[str, Any],
    *,
    api_key: str | None,
    password: str | None,
    sender_factory: SenderFactory = default_sender,
) -> tuple[bool, str]:
    """Send one connection-test message to the destination in ``body``, without saving it."""
    config = checked(body)
    credentials = credentials_for(db, config, api_key=api_key, password=password, box=_secret_box())
    now = db.execute(select(func.now())).scalar_one()
    sent_at = now.isoformat(timespec="milliseconds")
    record = Record(
        doc_id=f"baluarte-connection-test-{int(now.timestamp() * 1000)}",
        body={"type": "connection_test", "occurred_at": sent_at, "sent_at": sent_at},
    )
    try:
        outcome = sender_factory(config, credentials).send([record])
    except DeliveryError as exc:
        return False, str(exc)
    if outcome.delivered != 1:
        return False, outcome.error or "The destination did not accept the message"
    return True, f"A connection test message was delivered to {config.summary()}."


@dataclass(frozen=True)
class RunResult:
    shipped: int
    caught_up: bool
    error: str | None = None
    busy: bool = False


def _cursor(db: Session, config: ShippingConfig) -> AuditShipment | None:
    """The destination's cursor, locked; None when another run holds it."""
    db.execute(
        insert(AuditShipment)
        .values(destination_fingerprint=config.fingerprint(), destination=config.summary())
        .on_conflict_do_nothing(index_elements=["destination_fingerprint"])
    )
    return db.scalars(
        select(AuditShipment)
        .where(AuditShipment.destination_fingerprint == config.fingerprint())
        .with_for_update(skip_locked=True)
    ).one_or_none()


def ship(
    engine: Engine,
    *,
    sender_factory: SenderFactory = default_sender,
    batch_size: int = BATCH_SIZE,
    run_seconds: float = RUN_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> RunResult:
    """Ship waiting events until caught up, out of time, or the destination refuses."""
    deadline = clock() + run_seconds
    with Session(engine) as db:
        config = load_config(db)
        if config is None or not config.enabled:
            return RunResult(0, True)
        try:
            credentials = credentials_for(
                db, config, api_key=None, password=None, box=load_secret_box()
            )
        except (ShippingInvalid, ShippingConflict, ConfigurationError) as exc:
            return _record_failure(engine, config, short(exc))
    sender = sender_factory(config, credentials)
    shipped = 0
    while True:
        with Session(engine) as db:
            cursor = _cursor(db, config)
            if cursor is None:
                db.rollback()
                return RunResult(shipped, False, busy=True)
            events = list(
                db.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.id > cursor.last_shipped_id)
                    .order_by(AuditEvent.id)
                    .limit(batch_size)
                )
            )
            cursor.last_attempt_at = func.now()
            if not events:
                cursor.last_success_at = func.now()
                cursor.consecutive_failures = 0
                cursor.last_error = None
                db.commit()
                return RunResult(shipped, True)
            try:
                outcome = sender.send([record_for(event) for event in events])
            except DeliveryError as exc:
                outcome = Outcome(0, short(exc))
            if outcome.delivered:
                cursor.last_shipped_id = events[outcome.delivered - 1].id
                cursor.last_shipped_at = func.now()
                shipped += outcome.delivered
            if outcome.error is not None:
                cursor.consecutive_failures = cursor.consecutive_failures + 1
                cursor.last_error = outcome.error
                db.commit()
                return RunResult(shipped, False, outcome.error)
            cursor.last_success_at = func.now()
            cursor.consecutive_failures = 0
            cursor.last_error = None
            db.commit()
        if len(events) < batch_size:
            return RunResult(shipped, True)
        if clock() >= deadline:
            return RunResult(shipped, False)


def _record_failure(engine: Engine, config: ShippingConfig, error: str) -> RunResult:
    with Session(engine) as db:
        cursor = _cursor(db, config)
        if cursor is None:
            db.rollback()
            return RunResult(0, False, busy=True)
        cursor.last_attempt_at = func.now()
        cursor.consecutive_failures = cursor.consecutive_failures + 1
        cursor.last_error = error
        db.commit()
    return RunResult(0, False, error)


def handle_ship(_job: ClaimedJob, engine: Engine) -> None:
    result = ship(engine)
    if result.shipped:
        logger.info(
            "audit shipping: shipped %s events",
            result.shipped,
            extra={"event": "audit.shipped", "shipped": result.shipped},
        )
    if result.error is not None:
        logger.warning(
            "audit shipping failed: %s",
            result.error,
            extra={"event": "audit.shipping_failed"},
        )
        # The cursor holds the outcome; the next scheduled run tries again.
        raise JobError(result.error)


# Schedule and status ---------------------------------------------------------------------------


def _pending(db: Session, config: ShippingConfig) -> bool:
    shipped = db.scalar(
        select(AuditShipment.last_shipped_id).where(
            AuditShipment.destination_fingerprint == config.fingerprint()
        )
    )
    return (
        db.scalar(select(AuditEvent.id).where(AuditEvent.id > (shipped or 0)).limit(1)) is not None
    )


class ShippingSchedule:
    """Queues ``audit.ship`` at most once a minute while shipping is on and events are waiting,
    unless one is already queued or running. Keyed by the minute on the database clock, so any
    number of workers queue it once."""

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
            config = load_config(db)
            if config is None or not config.enabled or not _pending(db, config):
                return
            active = db.scalar(
                select(CollectionJob.id)
                .where(
                    CollectionJob.kind == AUDIT_SHIP,
                    CollectionJob.state.in_(("queued", "running")),
                )
                .limit(1)
            )
            if active is not None:
                return
            now = float(db.execute(select(func.extract("epoch", func.now()))).scalar_one())
            minute = int(now // 60)
            queue.enqueue(db, AUDIT_SHIP, f"{AUDIT_SHIP}:{minute}", max_attempts=1)
            db.commit()


@dataclass(frozen=True)
class Status:
    enabled: bool
    type: ShippingType | None
    destination: str | None
    latest_event_id: int
    last_shipped_id: int
    lag_events: int
    oldest_unshipped_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    consecutive_failures: int


def status(db: Session) -> Status:
    config = load_config(db)
    latest = int(db.scalar(select(func.coalesce(func.max(AuditEvent.id), 0))) or 0)
    if config is None or not config.enabled:
        # Off: nothing is waiting, but the destination and last outcome are still shown.
        return _status_off(db, config, latest)
    try:
        summary, fingerprint = config.summary(), config.fingerprint()
    except ShippingInvalid:
        return Status(config.enabled, config.type, None, latest, 0, 0, None, None, None, 0)
    cursor = db.get(AuditShipment, fingerprint)
    shipped = cursor.last_shipped_id if cursor is not None else 0
    lag, oldest = db.execute(
        select(func.count(), func.min(AuditEvent.occurred_at)).where(AuditEvent.id > shipped)
    ).one()
    return Status(
        enabled=config.enabled,
        type=config.type,
        destination=summary,
        latest_event_id=latest,
        last_shipped_id=shipped,
        lag_events=int(lag),
        oldest_unshipped_at=oldest,
        last_success_at=cursor.last_success_at if cursor is not None else None,
        last_error=cursor.last_error if cursor is not None else None,
        consecutive_failures=cursor.consecutive_failures if cursor is not None else 0,
    )


def _status_off(db: Session, config: ShippingConfig | None, latest: int) -> Status:
    if config is None:
        return Status(False, None, None, latest, 0, 0, None, None, None, 0)
    try:
        cursor = db.get(AuditShipment, config.fingerprint())
        summary: str | None = config.summary()
    except ShippingInvalid:
        cursor, summary = None, None
    return Status(
        enabled=False,
        type=config.type,
        destination=summary,
        latest_event_id=latest,
        last_shipped_id=cursor.last_shipped_id if cursor is not None else 0,
        lag_events=0,
        oldest_unshipped_at=None,
        last_success_at=cursor.last_success_at if cursor is not None else None,
        last_error=cursor.last_error if cursor is not None else None,
        consecutive_failures=cursor.consecutive_failures if cursor is not None else 0,
    )


def oldest_unshipped(db: Session) -> datetime | None:
    """While shipping is on: when the oldest event the destination has not received occurred.

    Retention keeps every event from then on. None when shipping is off or nothing waits.
    """
    current = status(db)
    return current.oldest_unshipped_at if current.enabled and current.lag_events else None


def settings_view(db: Session) -> dict[str, Any]:
    """The settings as the API shows them: always complete, without credential values."""
    config = load_config(db)
    row = stored_row(db)
    syslog = config.syslog if config is not None else None
    elasticsearch = config.elasticsearch if config is not None else None
    return {
        "configured": config is not None,
        "enabled": config.enabled if config is not None else False,
        "type": config.type if config is not None else "syslog_tls",
        "syslog": {
            "host": syslog.host if syslog else "",
            "port": syslog.port if syslog else 6514,
            "server_name": syslog.server_name if syslog else None,
            "ca_pem": syslog.ca_pem if syslog else None,
        },
        "elasticsearch": {
            "url": elasticsearch.url if elasticsearch else "",
            "index": elasticsearch.index if elasticsearch else "baluarte-audit",
            "auth": elasticsearch.auth if elasticsearch else "api_key",
            "username": elasticsearch.username if elasticsearch else None,
            "ca_pem": elasticsearch.ca_pem if elasticsearch else None,
        },
        "api_key": "configured" if secret_stored(db, AUDIT_SHIPPING_ES_API_KEY) else "not_set",
        "password": "configured" if secret_stored(db, AUDIT_SHIPPING_ES_PASSWORD) else "not_set",
        "updated_by": row.updated_by if row is not None else None,
        "updated_at": row.updated_at if row is not None else None,
    }
