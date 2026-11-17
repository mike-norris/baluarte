"""Notifications (FR-024): channels over verified TLS, settings with sealed secrets, the events
that produce a message once each, and delivery with retries. Every server is local."""

import email
import json
import socket
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.message import Message as EmailMessage
from typing import Any

import httpx
import pytest
import test_audit_shipping
import test_compliance
import test_reviews
from notification_fakes import SmtpServer, WebhookServer
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session
from test_evaluation_store import HIGH, World

from baluarte.api.v1.notifications import notification_sender, public_url
from baluarte.auth.roles import Role
from baluarte.compliance.retention import expire
from baluarte.db.models import (
    AuditEvent,
    CollectionJob,
    GitLabProject,
    Notification,
    NotificationDelivery,
    StoredSecret,
)
from baluarte.integrations.gitlab.jobs import CollectionHealth
from baluarte.notifications import delivery, events, messages
from baluarte.notifications import settings as notification_settings
from baluarte.notifications.channels import (
    EmailSender,
    EmailTarget,
    Message,
    SendFailure,
    WebhookSender,
)
from baluarte.notifications.settings import NotificationsUpdate, Secrets

pki = test_audit_shipping.pki
secret_key = test_audit_shipping.secret_key
engine = test_compliance.engine
client = test_compliance.client
gitlab_env = test_reviews.gitlab_env
instance_id = test_reviews.instance_id

BASE = "https://baluarte.example.test"
PASSWORD = "smtp-password-0123"
WEBHOOK = "https://hooks.example.test/services/T000/B000/secret-token-abcdef"
MESSAGE = Message("[Baluarte] Test", "Plain text body.\n", "<p>HTML body.</p>")
ALL = ["email", "webhook"]


def email_settings(**overrides: Any) -> dict[str, Any]:
    return {
        "enabled": True,
        "host": "mail.example.test",
        "port": 587,
        "security": "starttls",
        "username": "baluarte",
        "password": PASSWORD,
        "from_address": "baluarte@example.test",
        "recipients": ["security-team@example.test"],
        "ca_pem": None,
    } | overrides


def events_on(channels: list[str] = ALL, **overrides: Any) -> dict[str, Any]:
    return {
        "new_findings": {"enabled": True, "channels": channels, "min_severity": "high"},
        "collection_health": {"enabled": True, "channels": channels},
        "review_waiting": {"enabled": True, "channels": channels, "older_than_days": 2},
        "weekly_digest": {"enabled": True, "channels": channels},
    } | overrides


def body(
    email_part: dict[str, Any] | None = None,
    webhook: dict[str, Any] | None = None,
    event_part: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "email": email_settings() if email_part is None else email_part,
        "webhook": {"enabled": True, "url": WEBHOOK, "ca_pem": None}
        if webhook is None
        else webhook,
        "events": events_on() if event_part is None else event_part,
    }


def admin(engine: Engine) -> Any:
    api = client(engine, 1, Role.ADMIN)
    api.app.dependency_overrides[public_url] = lambda: BASE  # type: ignore[attr-defined]
    return api


def save(engine: Engine, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    response = admin(engine).put("/api/v1/settings/notifications", json=payload or body())
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def configure(engine: Engine, **events_overrides: Any) -> None:
    """Both channels on (email without a password), every event to both."""
    with Session(engine) as db:
        notification_settings.update(
            db,
            NotificationsUpdate.model_validate(
                body(
                    email_settings(username=None, password=None),
                    event_part=events_on(**events_overrides),
                )
            ),
            "user:1",
        )
        db.commit()


class FakeSender:
    """Records what each channel was asked to send; raises while ``failure`` is set."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Message, Secrets]] = []
        self.failure: SendFailure | None = None

    def factory(self, channel: str, _settings: Any, secrets: Secrets) -> "FakeSender._One":
        return FakeSender._One(self, channel, secrets)

    class _One:
        def __init__(self, owner: "FakeSender", channel: str, secrets: Secrets) -> None:
            self.owner, self.channel, self.secrets = owner, channel, secrets

        def send(self, message: Message) -> None:
            if self.owner.failure is not None:
                raise self.owner.failure
            self.owner.sent.append((self.channel, message, self.secrets))


def audit_events(engine: Engine, action: str) -> list[AuditEvent]:
    with Session(engine) as db:
        return list(
            db.scalars(
                select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.id)
            )
        )


# Email -----------------------------------------------------------------------------------------


@pytest.fixture
def smtp(pki: dict[str, Any]) -> Iterator[SmtpServer]:
    server = SmtpServer(pki, password=PASSWORD)
    yield server
    server.close()


def target(port: int, ca_pem: str | None, **overrides: Any) -> EmailTarget:
    values: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": port,
        "security": "starttls",
        "username": "baluarte",
        "from_address": "baluarte@example.test",
        "recipients": ("a@example.test", "b@example.test"),
        "ca_pem": ca_pem,
    } | overrides
    return EmailTarget(**values)


def parsed(raw: bytes) -> EmailMessage:
    return email.message_from_bytes(raw)


def test_email_over_starttls_is_verified_and_authenticated(
    smtp: SmtpServer, pki: dict[str, Any]
) -> None:
    EmailSender(target(smtp.port, pki["ca_pem"]), PASSWORD).send(MESSAGE)

    assert smtp.authenticated == ["baluarte"]
    # Nothing but the greeting exchange happened before TLS.
    assert smtp.commands[: smtp.commands.index("STARTTLS") + 1] == ["EHLO", "STARTTLS"]
    (raw,) = smtp.messages
    message = parsed(raw)
    assert message["Subject"] == "[Baluarte] Test"
    assert message["From"] == "baluarte@example.test"
    assert message["Auto-Submitted"] == "auto-generated"
    parts = {part.get_content_type(): part.get_payload(decode=True) for part in message.walk()}
    assert parts["text/plain"].replace(b"\r\n", b"\n") == b"Plain text body.\n"
    assert b"<p>HTML body.</p>" in parts["text/html"]
    assert smtp.envelopes == [("baluarte@example.test", ["a@example.test", "b@example.test"])]
    # Recipients are only in the envelope: none sees the others' addresses.
    assert message["To"] == "undisclosed-recipients:;"
    assert message["Cc"] is None and message["Bcc"] is None
    assert b"a@example.test" not in raw and b"b@example.test" not in raw


def test_email_over_implicit_tls(pki: dict[str, Any]) -> None:
    server = SmtpServer(pki, implicit_tls=True, password=PASSWORD)
    try:
        EmailSender(target(server.port, pki["ca_pem"], security="tls"), PASSWORD).send(MESSAGE)
    finally:
        server.close()

    assert server.authenticated == ["baluarte"]
    assert len(server.messages) == 1


def test_an_unverified_server_certificate_is_a_tls_failure(smtp: SmtpServer) -> None:
    with pytest.raises(SendFailure) as failure:
        EmailSender(target(smtp.port, None), PASSWORD).send(MESSAGE)

    assert failure.value.stage == "tls"
    assert smtp.messages == [] and smtp.authenticated == []


def test_a_server_without_starttls_gets_nothing(pki: dict[str, Any]) -> None:
    server = SmtpServer(pki, offer_starttls=False, password=PASSWORD)
    try:
        with pytest.raises(SendFailure) as failure:
            EmailSender(target(server.port, pki["ca_pem"]), PASSWORD).send(MESSAGE)
    finally:
        server.close()

    assert failure.value.stage == "tls"
    assert "does not offer STARTTLS" in str(failure.value)
    assert "AUTH" not in server.commands and server.messages == []


def test_a_wrong_password_is_an_authentication_failure(
    smtp: SmtpServer, pki: dict[str, Any]
) -> None:
    with pytest.raises(SendFailure) as failure:
        EmailSender(target(smtp.port, pki["ca_pem"]), "not-the-password").send(MESSAGE)

    assert failure.value.stage == "auth"
    assert "not-the-password" not in str(failure.value)
    assert smtp.messages == []


def test_refused_recipients_are_a_rejection(pki: dict[str, Any]) -> None:
    server = SmtpServer(pki, password=PASSWORD, reject_recipients=True)
    try:
        with pytest.raises(SendFailure) as failure:
            EmailSender(target(server.port, pki["ca_pem"]), PASSWORD).send(MESSAGE)
    finally:
        server.close()

    assert failure.value.stage == "rejected"
    assert "2 recipients refused (SMTP 550)" in str(failure.value)
    # The reason names no address.
    assert "@" not in str(failure.value)


def test_an_unreachable_server_is_a_connect_failure(pki: dict[str, Any]) -> None:
    # A bound socket that never listens refuses connections, and holding it keeps the port from
    # being handed to anything else while the test runs.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
        with pytest.raises(SendFailure) as failure:
            EmailSender(target(port, pki["ca_pem"]), None, timeout=2).send(MESSAGE)

    assert failure.value.stage == "connect"


# Webhook ---------------------------------------------------------------------------------------


@pytest.fixture
def hook(pki: dict[str, Any]) -> Iterator[WebhookServer]:
    server = WebhookServer(pki)
    yield server
    server.close()


def test_the_webhook_posts_slack_json_over_verified_tls(
    hook: WebhookServer, pki: dict[str, Any]
) -> None:
    WebhookSender(hook.url(), pki["ca_pem"]).send(MESSAGE)

    assert hook.received == [("/hooks/T000/B000/0123456789abcdef", {"text": "Plain text body.\n"})]


def test_an_unverified_webhook_is_a_tls_failure_without_the_url(hook: WebhookServer) -> None:
    with pytest.raises(SendFailure) as failure:
        WebhookSender(hook.url(), None).send(MESSAGE)

    assert failure.value.stage == "tls"
    assert "0123456789abcdef" not in str(failure.value)
    assert hook.received == []


@pytest.mark.parametrize(
    ("status", "stage"), [(401, "auth"), (403, "auth"), (404, "rejected"), (500, "rejected")]
)
def test_webhook_answers_name_the_stage(status: int, stage: str) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(status))

    with pytest.raises(SendFailure) as failure:
        WebhookSender(WEBHOOK, None, transport=transport).send(MESSAGE)

    assert failure.value.stage == stage
    assert f"HTTP {status}" in str(failure.value)
    assert "secret-token" not in str(failure.value)


def test_webhook_errors_never_carry_the_url() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"could not reach {request.url}", request=request)

    with pytest.raises(SendFailure) as failure:
        WebhookSender(WEBHOOK, None, transport=httpx.MockTransport(refuse)).send(MESSAGE)

    assert failure.value.stage == "connect"
    assert "secret-token" not in str(failure.value)
    assert "webhook hooks.example.test" in str(failure.value)


def test_a_plain_http_webhook_is_never_called() -> None:
    called: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda request: called.append(request) or httpx.Response(200))

    with pytest.raises(SendFailure) as failure:
        WebhookSender("http://hooks.example.test/x", None, transport=transport).send(MESSAGE)

    assert failure.value.stage == "config"
    assert called == []


# Settings API ----------------------------------------------------------------------------------


@pytest.mark.db
def test_everything_is_off_until_configured_and_only_admins_see_settings(engine: Engine) -> None:
    response = admin(engine).get("/api/v1/settings/notifications")

    assert response.status_code == 200
    view = response.json()
    assert view["configured"] is False
    assert view["public_url"] == BASE
    assert view["email"]["enabled"] is False and view["email"]["password"] == "not_set"
    assert view["webhook"] == {"enabled": False, "url": "not_set", "host": None, "ca_pem": None}
    assert view["events"]["new_findings"] == {
        "enabled": False,
        "channels": [],
        "min_severity": "high",
    }
    assert view["events"]["review_waiting"]["older_than_days"] == 2
    for role in (Role.AUDITOR, Role.ANALYST, Role.MANAGER):
        assert client(engine, 2, role).get("/api/v1/settings/notifications").status_code == 403
        assert (
            client(engine, 2, role).put("/api/v1/settings/notifications", json=body()).status_code
            == 403
        )
    assert client(engine, 2, Role.AUDITOR).get("/api/v1/notifications/deliveries").json() == {
        "items": []
    }
    assert (
        client(engine, 2, Role.ANALYST).get("/api/v1/notifications/deliveries").status_code == 403
    )
    assert (
        client(engine, 2, Role.MANAGER).get("/api/v1/notifications/deliveries").status_code == 403
    )


@pytest.mark.db
def test_secrets_are_sealed_never_returned_and_audited_without_their_values(
    engine: Engine, secret_key: None
) -> None:
    view = save(engine)

    text_view = json.dumps(view)
    assert PASSWORD not in text_view and "secret-token" not in text_view
    assert view["email"]["password"] == "configured"
    assert view["webhook"] == {
        "enabled": True,
        "url": "configured",
        "host": "hooks.example.test",
        "ca_pem": None,
    }
    assert view["email"]["recipients"] == ["security-team@example.test"]
    with Session(engine) as db:
        sealed = {row.name: bytes(row.ciphertext) for row in db.scalars(select(StoredSecret))}
    assert set(sealed) == {"notifications_smtp_password", "notifications_webhook_url"}
    assert all(PASSWORD.encode() not in value for value in sealed.values())
    (changed,) = audit_events(engine, "notifications.settings_changed")
    recorded = json.dumps(changed.details)
    assert changed.details["secrets"] == {"smtp_password": "replaced", "webhook_url": "replaced"}
    assert changed.details["email"]["recipients"] == 1
    for secret in (PASSWORD, "secret-token", "security-team@example.test"):
        assert secret not in recorded


@pytest.mark.db
def test_stored_secrets_are_kept_moved_or_removed(engine: Engine, secret_key: None) -> None:
    save(engine)
    kept = save(engine, body(email_settings(password=None), {"enabled": True, "url": None}))
    assert kept["email"]["password"] == "configured" and kept["webhook"]["url"] == "configured"

    moved = admin(engine).put(
        "/api/v1/settings/notifications",
        json=body(email_settings(host="smtp.example.test", password=None)),
    )
    assert moved.status_code == 422
    assert moved.json()["detail"].startswith("email.password:")

    removed = admin(engine).put(
        "/api/v1/settings/notifications", json=body(webhook={"enabled": True, "url": ""})
    )
    assert removed.status_code == 422
    assert removed.json()["detail"] == "webhook.url: enter the webhook URL"

    off = save(
        engine,
        body(
            email_settings(enabled=False, username=None, password=None),
            {"enabled": False, "url": ""},
        ),
    )
    assert off["email"]["password"] == "not_set"
    assert off["webhook"] == {"enabled": False, "url": "not_set", "host": None, "ca_pem": None}
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(StoredSecret)) == 0
    last = audit_events(engine, "notifications.settings_changed")[-1]
    assert last.details["secrets"] == {"smtp_password": "removed", "webhook_url": "removed"}


@pytest.mark.db
@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"webhook": {"enabled": True, "url": "http://hooks.example.test/x"}}, "webhook.url"),
        ({"webhook": {"enabled": True, "url": "https://u:p@hooks.example.test/x"}}, "webhook.url"),
        ({"email": email_settings(recipients=[])}, "email.recipients"),
        ({"email": email_settings(recipients=["not an address"])}, "email.recipients"),
        (
            {"email": email_settings(recipients=[f"r{i}@example.test" for i in range(51)])},
            "email.recipients",
        ),
        ({"email": email_settings(from_address="")}, "email.from_address"),
        (
            {"email": email_settings(from_address="Evil\r\nBcc: x@example.test")},
            "email.from_address",
        ),
        ({"email": email_settings(host="smtp://mail.example.test")}, "email.host"),
        ({"email": email_settings(host="")}, "email.host"),
        ({"email": email_settings(port=0)}, "email.port"),
        ({"email": email_settings(ca_pem="not a certificate")}, "email.ca_pem"),
        ({"email": email_settings(password="")}, "email.password"),
    ],
)
def test_invalid_settings_name_the_field(
    engine: Engine, secret_key: None, change: dict[str, Any], field: str
) -> None:
    response = admin(engine).put("/api/v1/settings/notifications", json=body() | change)

    assert response.status_code == 422, response.text
    assert response.json()["detail"].startswith(f"{field}:")
    assert admin(engine).get("/api/v1/settings/notifications").json()["configured"] is False


@pytest.mark.db
@pytest.mark.parametrize(
    "change",
    [
        {"email": email_settings(security="none")},
        {
            "event_part": events_on(
                review_waiting={"enabled": True, "channels": [], "older_than_days": 0}
            )
        },
        {"event_part": events_on(new_findings={"enabled": True, "channels": ["sms"]})},
    ],
)
def test_malformed_settings_are_refused(engine: Engine, change: dict[str, Any]) -> None:
    payload = body(**change) if "event_part" in change else body() | change

    assert admin(engine).put("/api/v1/settings/notifications", json=payload).status_code == 422


@pytest.mark.db
def test_a_secret_cannot_be_stored_without_a_secret_key(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BALUARTE_SECRET_KEY_FILE", raising=False)

    response = admin(engine).put("/api/v1/settings/notifications", json=body())

    assert response.status_code == 409
    assert "BALUARTE_SECRET_KEY_FILE" in response.text
    # Settings without secrets can still be saved.
    save(
        engine,
        body(email_settings(username=None, password=None), {"enabled": False, "url": None}),
    )


@pytest.mark.db
def test_a_test_message_uses_the_form_without_saving_it_and_is_audited(
    engine: Engine, secret_key: None
) -> None:
    fake = FakeSender()
    api = admin(engine)
    api.app.dependency_overrides[notification_sender] = lambda: fake.factory

    sent = api.post("/api/v1/settings/notifications/test", json=body() | {"channel": "webhook"})
    fake.failure = SendFailure("auth", "SMTP mail.example.test:587: refused")
    failed = api.post("/api/v1/settings/notifications/test", json=body() | {"channel": "email"})

    assert sent.status_code == 200, sent.text
    assert sent.json() == {
        "ok": True,
        "channel": "webhook",
        "stage": None,
        "detail": "A test message was sent to webhook hooks.example.test.",
    }
    assert failed.json()["ok"] is False and failed.json()["stage"] == "auth"
    assert failed.json()["detail"].startswith("Authentication failed:")
    channel, message, secrets = fake.sent[0]
    assert channel == "webhook" and secrets.webhook_url == WEBHOOK
    assert message.subject == "[Baluarte] Test message"
    assert f"{BASE}/settings" in message.text
    assert api.get("/api/v1/settings/notifications").json()["configured"] is False
    tests = audit_events(engine, "notifications.test_sent")
    assert [(e.details["channel"], e.details["ok"], e.details["stage"]) for e in tests] == [
        ("webhook", True, None),
        ("email", False, "auth"),
    ]
    assert all("secret-token" not in json.dumps(e.details) for e in tests)


@pytest.mark.db
def test_a_test_message_reaches_a_real_smtp_server(
    engine: Engine, smtp: SmtpServer, pki: dict[str, Any], secret_key: None
) -> None:
    save(engine)
    form = body(
        email_settings(host="127.0.0.1", port=smtp.port, ca_pem=pki["ca_pem"], password=PASSWORD),
        {"enabled": False, "url": None},
    )

    response = admin(engine).post(
        "/api/v1/settings/notifications/test", json=form | {"channel": "email"}
    )

    assert response.json()["ok"] is True, response.text
    assert smtp.authenticated == ["baluarte"]
    assert parsed(smtp.messages[0])["Subject"] == "[Baluarte] Test message"
    # A moved host needs the password entered again, even for a test.
    form["email"]["password"] = None
    moved = admin(engine).post(
        "/api/v1/settings/notifications/test", json=form | {"channel": "email"}
    )
    assert moved.status_code == 422
    assert moved.json()["detail"].startswith("email.password:")


# New findings ----------------------------------------------------------------------------------


@pytest.fixture
def links(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(messages, "public_url", lambda: BASE)


def notifications(engine: Engine) -> list[Notification]:
    with Session(engine) as db:
        return list(db.scalars(select(Notification).order_by(Notification.id)))


def deliveries(engine: Engine) -> list[NotificationDelivery]:
    with Session(engine) as db:
        return list(db.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id)))


@pytest.mark.db
def test_a_run_s_new_findings_are_notified_once_with_links_and_no_personal_data(
    migrated_engine: Engine, secret_key: None, links: None
) -> None:
    world = World(migrated_engine)
    world.assign(HIGH)
    first = world.project(1, "public")
    world.project(2, "private")
    personal = world.project(3, "public")
    with Session(migrated_engine) as db:
        db.execute(
            update(GitLabProject)
            .where(GitLabProject.id == personal)
            .values(namespace_kind="user", path_with_namespace="jdoe/scratch")
        )
        db.commit()
    configure(migrated_engine)

    run = world.run()

    (notification,) = notifications(migrated_engine)
    assert notification.event_type == "new_findings"
    assert notification.dedup_key == f"findings:{run.id}"
    assert notification.subject == "[Baluarte] 2 new findings at high severity or above"
    findings = {f.project_id: f.id for f in world.findings()}
    assert f"{BASE}/findings/{findings[first]}" in notification.body_text
    assert "platform/p1" in notification.body_text
    assert "personal project 3" in notification.body_text
    assert "jdoe" not in notification.body_text and "jdoe" not in notification.body_html
    assert "@" not in notification.body_text
    assert [d.channel for d in deliveries(migrated_engine)] == ["email", "webhook"]
    assert all(d.status == "pending" for d in deliveries(migrated_engine))

    # A second worker recording the same run finds it there; the next run opens nothing new.
    with Session(migrated_engine) as db:
        assert events.notify_new_findings(db, run.id, world.instance_id, set()) is None
    world.run()
    assert len(notifications(migrated_engine)) == 1


@pytest.mark.db
def test_findings_below_the_threshold_or_with_the_event_off_send_nothing(
    migrated_engine: Engine, secret_key: None
) -> None:
    world = World(migrated_engine)
    world.assign(HIGH)
    world.project(1, "public")
    configure(
        migrated_engine,
        new_findings={"enabled": True, "channels": ALL, "min_severity": "critical"},
    )

    world.run()
    assert notifications(migrated_engine) == []

    configure(migrated_engine, new_findings={"enabled": False, "channels": ALL})
    world.project(2, "public")
    world.run()
    assert notifications(migrated_engine) == []


# Collection health -----------------------------------------------------------------------------


@pytest.mark.db
def test_each_health_transition_is_notified_once_however_many_workers_see_it(
    migrated_engine: Engine, gitlab_env: None, secret_key: None, links: None
) -> None:
    from test_collection_health import add_run, all_kinds

    configure(migrated_engine)
    clock = [0.0]
    workers = [
        CollectionHealth(migrated_engine, check_every=60, clock=lambda: clock[0]) for _ in range(2)
    ]

    def check() -> None:
        clock[0] += 61
        for worker in workers:
            worker()

    all_kinds(migrated_engine, "succeeded", 30)
    check()
    assert notifications(migrated_engine) == []

    for minutes in (20, 15, 10):
        all_kinds(migrated_engine, "failed", minutes, error="GitLab could not be reached")
    check()
    check()
    add_run(migrated_engine, "groups", "failed", 1)
    check()
    (unhealthy,) = notifications(migrated_engine)
    assert unhealthy.event_type == "collection_unhealthy"
    assert unhealthy.dedup_key == "collection:unhealthy:after-0"
    assert "failed 3 times in a row" in unhealthy.body_text
    assert f"{BASE}/" in unhealthy.body_text

    all_kinds(migrated_engine, "succeeded", 5)
    check()
    check()
    for minutes in (4, 3, 2):
        all_kinds(migrated_engine, "failed", minutes)
    check()

    kinds = [(n.event_type, n.dedup_key) for n in notifications(migrated_engine)]
    assert kinds == [
        ("collection_unhealthy", "collection:unhealthy:after-0"),
        ("collection_recovered", f"collection:recovered:{unhealthy.id}"),
        ("collection_unhealthy", f"collection:unhealthy:after-{unhealthy.id + 1}"),
    ]


# Review reminder and digest --------------------------------------------------------------------


@pytest.mark.db
def test_the_review_reminder_counts_old_items_once_a_day(
    engine: Engine, gitlab_env: None, instance_id: int, secret_key: None, links: None
) -> None:
    test_reviews.propose_baseline(engine, 1)
    configure(engine)
    later = datetime.now(UTC) + timedelta(days=3)

    with Session(engine) as db:
        assert events.compose_review(db, datetime.now(UTC)) is not None
        db.commit()
    with Session(engine) as db:
        first = events.compose_review(db, later)
        second = events.compose_review(db, later)
        db.commit()

    assert first is not None and second is None
    quiet, reminder = notifications(engine)
    # Today nothing is older than two days: recorded once, sent nowhere.
    assert quiet.subject == "[Baluarte] Nothing waiting for review"
    assert reminder.dedup_key == f"review:{later.date().isoformat()}"
    assert reminder.subject == "[Baluarte] 1 item waiting for review"
    assert f"1 baseline change: {BASE}/reviews" in reminder.body_text
    assert "user-1" not in reminder.body_text and "@" not in reminder.body_text
    assert [(d.notification_id, d.channel) for d in deliveries(engine)] == [
        (reminder.id, "email"),
        (reminder.id, "webhook"),
    ]


def test_scheduled_messages_are_due_from_eight_utc_and_the_digest_on_mondays() -> None:
    settings = notification_settings.NotificationSettings.model_validate(
        {
            "email": {"enabled": True},
            "events": events_on(["email"]),
        }
    )
    monday_early = datetime(2026, 11, 16, 7, 59, tzinfo=UTC)
    monday = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)
    tuesday = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)

    assert events.due(settings, monday_early) == []
    assert events.due(settings, monday) == ["review_waiting", "weekly_digest"]
    assert events.due(settings, tuesday) == ["review_waiting"]
    assert events.scheduled_keys(events.due(settings, monday), monday) == [
        "review:2026-11-16",
        "digest:2026-W47",
    ]
    off = settings.model_copy(
        update={"email": settings.email.model_copy(update={"enabled": False})}
    )
    assert events.due(off, monday) == []


@pytest.mark.db
def test_the_weekly_digest_states_the_posture_once_a_week(
    migrated_engine: Engine, secret_key: None, links: None
) -> None:
    world = World(migrated_engine)
    world.assign(HIGH)
    world.project(1, "public")
    world.project(2, "private")
    world.run()
    configure(migrated_engine)
    monday = datetime(2026, 11, 16, 8, 5, tzinfo=UTC)

    with Session(migrated_engine) as db:
        assert events.compose_digest(db, monday) is not None
        assert events.compose_digest(db, monday + timedelta(days=2)) is None
        db.commit()

    (digest,) = notifications(migrated_engine)
    assert digest.dedup_key == "digest:2026-W47"
    assert digest.subject == "[Baluarte] Weekly digest 2026-W47"
    text_body = digest.body_text
    assert "Rule results" in text_body and "1 pass, 1 fail" in text_body
    assert "Open findings: 1" in text_body and "1 high" in text_body
    assert "No compliance framework is selected" in text_body
    assert "Review queue: 0" in text_body
    assert f"{BASE}/findings" in text_body
    assert "<ul>" in digest.body_html and "@" not in text_body


def test_messages_without_an_address_leave_links_out() -> None:
    message = messages.new_findings(
        None, 7, "high", 1, [messages.FindingLine(3, "high", "acme/api", "default", "visibility")]
    )

    assert "http" not in message.text
    assert "Baluarte's address is not configured" in message.text
    assert message.subject == "[Baluarte] 1 new finding at high severity or above"


def test_html_parts_are_escaped() -> None:
    message = messages.new_findings(
        BASE, 7, "low", 1, [messages.FindingLine(3, "low", "acme/<script>", "p", "r")]
    )

    assert "<script>" not in message.html
    assert "acme/&lt;script&gt;" in message.html


# Delivery --------------------------------------------------------------------------------------


def pending(engine: Engine, channels: list[str], key: str = "findings:1") -> int:
    with Session(engine) as db:
        notification_id = events.record(db, "new_findings", key, MESSAGE, channels)  # type: ignore[arg-type]
        db.commit()
    assert notification_id is not None
    return notification_id


def make_due(engine: Engine) -> None:
    with Session(engine) as db:
        db.execute(
            update(NotificationDelivery)
            .where(NotificationDelivery.status == "pending")
            .values(next_attempt_at=func.now() - timedelta(seconds=1))
        )
        db.commit()


@pytest.mark.db
def test_a_delivery_is_sent_once(migrated_engine: Engine, secret_key: None) -> None:
    configure(migrated_engine)
    pending(migrated_engine, ALL)
    fake = FakeSender()

    first = delivery.deliver(migrated_engine, sender_factory=fake.factory)
    second = delivery.deliver(migrated_engine, sender_factory=fake.factory)

    assert (first.sent, second.sent) == (2, 0)
    assert [(channel, message.subject) for channel, message, _ in fake.sent] == [
        ("email", "[Baluarte] Test"),
        ("webhook", "[Baluarte] Test"),
    ]
    assert fake.sent[1][2].webhook_url == WEBHOOK
    rows = deliveries(migrated_engine)
    assert [(d.status, d.attempts, d.last_error) for d in rows] == [("sent", 1, None)] * 2
    assert all(d.sent_at is not None and d.next_attempt_at is None for d in rows)


@pytest.mark.db
def test_failed_attempts_back_off_then_fail(migrated_engine: Engine, secret_key: None) -> None:
    configure(migrated_engine)
    pending(migrated_engine, ["webhook"])
    fake = FakeSender()
    fake.failure = SendFailure("connect", "webhook hooks.example.test: connection refused")
    waits = []

    for _ in range(delivery.MAX_ATTEMPTS):
        delivery.deliver(migrated_engine, sender_factory=fake.factory)
        with Session(migrated_engine) as db:
            row = db.scalars(select(NotificationDelivery)).one()
            now = db.scalar(select(func.now()))
            if row.next_attempt_at is not None:
                waits.append(round((row.next_attempt_at - now).total_seconds() / 60))
        make_due(migrated_engine)

    (row,) = deliveries(migrated_engine)
    assert waits == [1, 5, 15, 60]
    assert (row.status, row.attempts) == ("failed", 5)
    assert row.last_error == "Could not connect: webhook hooks.example.test: connection refused"
    listed = client(migrated_engine, 2, Role.AUDITOR).get("/api/v1/notifications/deliveries")
    (item,) = listed.json()["items"]
    assert item["event_type"] == "new_findings" and item["status"] == "failed"
    assert item["last_error"].startswith("Could not connect")


@pytest.mark.db
def test_a_delivery_to_a_channel_turned_off_fails_at_once(
    migrated_engine: Engine, secret_key: None
) -> None:
    configure(migrated_engine)
    pending(migrated_engine, ["email"])
    with Session(migrated_engine) as db:
        notification_settings.update(
            db,
            NotificationsUpdate.model_validate(
                body(email_settings(enabled=False, username=None, password=None))
            ),
            "user:1",
        )
        db.commit()

    delivery.deliver(migrated_engine, sender_factory=FakeSender().factory)

    (row,) = deliveries(migrated_engine)
    assert (row.status, row.attempts) == ("failed", 1)
    assert row.last_error == "Not sent: the email channel is turned off"


@pytest.mark.db
def test_a_delivery_another_worker_is_sending_is_skipped(
    migrated_engine: Engine, secret_key: None
) -> None:
    configure(migrated_engine)
    pending(migrated_engine, ["email"])
    fake = FakeSender()

    with Session(migrated_engine) as other:
        other.scalars(select(NotificationDelivery).with_for_update()).one()
        result = delivery.deliver(migrated_engine, sender_factory=fake.factory)
        other.rollback()

    assert result.sent == 0 and fake.sent == []
    assert delivery.deliver(migrated_engine, sender_factory=fake.factory).sent == 1


@pytest.mark.db
def test_a_run_stops_at_its_limits(migrated_engine: Engine, secret_key: None) -> None:
    configure(migrated_engine)
    for run in range(3):
        pending(migrated_engine, ALL, key=f"findings:{run}")
    fake = FakeSender()

    assert delivery.deliver(migrated_engine, sender_factory=fake.factory, max_messages=4).sent == 4
    ticks = iter([0.0, 0.0, 100.0])
    stopped = delivery.deliver(
        migrated_engine, sender_factory=fake.factory, run_seconds=50, clock=lambda: next(ticks)
    )
    assert stopped.sent == 1


@pytest.mark.db
def test_the_schedule_queues_one_job_while_there_is_work(
    migrated_engine: Engine, secret_key: None
) -> None:
    def jobs() -> list[str]:
        with Session(migrated_engine) as db:
            return list(
                db.scalars(
                    select(CollectionJob.idempotency_key).where(
                        CollectionJob.kind == delivery.NOTIFY_DELIVER
                    )
                )
            )

    clock = [0.0]
    schedule = delivery.NotificationSchedule(migrated_engine, clock=lambda: clock[0])
    configure(
        migrated_engine,
        review_waiting={"enabled": False, "channels": []},
        weekly_digest={"enabled": False, "channels": []},
    )
    schedule()
    assert jobs() == []

    pending(migrated_engine, ["email"])
    clock[0] += 61
    schedule()
    clock[0] += 61
    schedule()  # one is already queued
    assert len(jobs()) == 1


@pytest.mark.db
def test_the_deliver_job_composes_what_is_due_and_sends_it(
    migrated_engine: Engine, secret_key: None, monkeypatch: pytest.MonkeyPatch, links: None
) -> None:
    configure(migrated_engine)
    fake = FakeSender()
    monday = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)

    def compose(db: Session, _now: datetime, base: str | None = None) -> int | None:
        return original_digest(db, monday, base)

    original_digest = events.compose_digest
    monkeypatch.setattr(events, "compose_digest", compose)
    monkeypatch.setattr(events, "due", lambda _settings, _now: ["weekly_digest"])

    assert delivery.compose_due(migrated_engine) == 1
    sent = delivery.deliver(migrated_engine, sender_factory=fake.factory)

    assert sent.sent == 2
    assert {message.subject for _, message, _ in fake.sent} == {"[Baluarte] Weekly digest 2026-W47"}


# Retention -------------------------------------------------------------------------------------


@pytest.mark.db
def test_retention_follows_the_audit_period_but_keeps_what_is_still_being_sent(
    migrated_engine: Engine, secret_key: None
) -> None:
    configure(migrated_engine)
    done = pending(migrated_engine, ["email"], key="findings:1")
    waiting = pending(migrated_engine, ["email"], key="findings:2")
    recent = pending(migrated_engine, ["email"], key="findings:3")
    with Session(migrated_engine) as db:
        db.execute(
            update(NotificationDelivery)
            .where(NotificationDelivery.notification_id == done)
            .values(status="sent", sent_at=func.now(), next_attempt_at=None)
        )
        db.execute(
            update(Notification)
            .where(Notification.id.in_((done, waiting)))
            .values(created_at=func.now() - timedelta(days=4000))
        )
        details = expire(db)
        db.commit()

    assert details["deleted"]["notification"] == 1
    assert [n.id for n in notifications(migrated_engine)] == [waiting, recent]
    assert {d.notification_id for d in deliveries(migrated_engine)} == {waiting, recent}
