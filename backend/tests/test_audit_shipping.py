"""Audit shipping: the senders, the settings API, the shipping job, its schedule, status,
posture and the retention guard. Nothing here reaches the network: syslog goes to a TLS server
on 127.0.0.1 and Elasticsearch to an httpx MockTransport."""

import base64
import json
import os
import socket
import ssl
import threading
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session
from test_compliance import client, old_event, save_profile
from test_compliance import engine as engine

from baluarte.api.v1.audit_shipping import audit_shipping_sender
from baluarte.auth.roles import Role
from baluarte.compliance import shipping
from baluarte.compliance.retention import expire
from baluarte.compliance.senders import (
    SYSLOG_PRI,
    DeliveryError,
    ElasticsearchDestination,
    ElasticsearchSender,
    Outcome,
    Record,
    SyslogDestination,
    SyslogSender,
    syslog_frame,
)
from baluarte.db.models import AuditEvent, AuditShipment, CollectionJob, StoredSecret

API_KEY = "es-api-key-for-tests-0123456789"
SYSLOG = {"host": "siem.example.test", "port": 6514, "server_name": None, "ca_pem": None}
ELASTIC = {
    "url": "https://logs.example.test:9200",
    "index": "baluarte-audit",
    "auth": "api_key",
    "username": None,
    "ca_pem": None,
}


def syslog_body(**overrides: Any) -> dict[str, Any]:
    return {"enabled": True, "type": "syslog_tls", "syslog": SYSLOG} | overrides


def elastic_body(**overrides: Any) -> dict[str, Any]:
    return {
        "enabled": True,
        "type": "elasticsearch",
        "elasticsearch": ELASTIC,
        "api_key": API_KEY,
    } | overrides


@pytest.fixture
def secret_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("BALUARTE_SECRET_KEY_PREVIOUS_FILE", raising=False)
    key = tmp_path / "secret-key"
    key.write_text(base64.b64encode(os.urandom(32)).decode() + "\n")
    monkeypatch.setenv("BALUARTE_SECRET_KEY_FILE", str(key))


# Syslog over TLS


def test_a_syslog_frame_is_rfc5424_with_octet_counting() -> None:
    body = {"id": 7, "action": "auth.login", "occurred_at": "2026-11-02T10:00:00.123+00:00"}

    frame = syslog_frame(body, "baluarte-worker")

    length, _, message = frame.partition(b" ")
    assert int(length) == len(message)
    assert SYSLOG_PRI == 109
    assert message.startswith(
        b"<109>1 2026-11-02T10:00:00.123Z baluarte-worker baluarte - audit - \xef\xbb\xbf"
    )
    assert json.loads(message.split(b"\xef\xbb\xbf", 1)[1]) == body


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """A private CA and a server certificate for 127.0.0.1 and siem.example.test."""
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test audit CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    import ipaddress

    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "siem.example.test")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("siem.example.test"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    directory = tmp_path_factory.mktemp("pki")
    cert_path, key_path = directory / "server.pem", directory / "server.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return {
        "ca_pem": ca.public_bytes(serialization.Encoding.PEM).decode(),
        "cert": cert_path,
        "key": key_path,
    }


class SyslogServer:
    """Accepts TLS connections on 127.0.0.1 and keeps the bytes each one sent."""

    def __init__(self, pki: dict[str, Any]) -> None:
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(pki["cert"], pki["key"])
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.received: list[bytes] = []
        self.handshake_failures = 0
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                raw, _ = self.listener.accept()
            except OSError:
                return
            try:
                with self.context.wrap_socket(raw, server_side=True) as connection:
                    chunks = []
                    while chunk := connection.recv(65536):
                        chunks.append(chunk)
                    self.received.append(b"".join(chunks))
            except (ssl.SSLError, OSError):
                self.handshake_failures += 1

    def close(self) -> None:
        self.listener.close()
        self.thread.join(timeout=5)


@pytest.fixture
def syslog_server(pki: dict[str, Any]) -> Iterator[SyslogServer]:
    server = SyslogServer(pki)
    yield server
    server.close()


def records(*ids: int) -> list[Record]:
    return [Record(f"baluarte-{i}", {"id": i, "action": "test.event"}) for i in ids]


def frames(data: bytes) -> list[dict[str, Any]]:
    found = []
    while data:
        length, _, rest = data.partition(b" ")
        message, data = rest[: int(length)], rest[int(length) :]
        found.append(json.loads(message.split(b"\xef\xbb\xbf", 1)[1]))
    return found


def wait_for(condition: Any) -> None:
    deadline = datetime.now(UTC) + timedelta(seconds=5)
    while not condition() and datetime.now(UTC) < deadline:
        threading.Event().wait(0.01)


def test_syslog_delivers_a_batch_over_verified_tls(
    syslog_server: SyslogServer, pki: dict[str, Any]
) -> None:
    sender = SyslogSender(
        SyslogDestination(
            "127.0.0.1", syslog_server.port, server_name="siem.example.test", ca_pem=pki["ca_pem"]
        )
    )

    assert sender.send(records(1, 2, 3)) == Outcome(3)

    wait_for(lambda: syslog_server.received)
    assert [event["id"] for event in frames(syslog_server.received[0])] == [1, 2, 3]


def test_syslog_refuses_a_server_it_cannot_verify(
    syslog_server: SyslogServer, pki: dict[str, Any]
) -> None:
    untrusted = SyslogSender(SyslogDestination("127.0.0.1", syslog_server.port))
    wrong_name = SyslogSender(
        SyslogDestination(
            "127.0.0.1", syslog_server.port, server_name="other.example.test", ca_pem=pki["ca_pem"]
        )
    )

    for sender in (untrusted, wrong_name):
        with pytest.raises(DeliveryError, match=r"CERTIFICATE_VERIFY_FAILED|certificate"):
            sender.send(records(1))
    assert syslog_server.received == []


def test_syslog_reports_an_unreachable_destination() -> None:
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()

    with pytest.raises(DeliveryError, match=f"syslog 127.0.0.1:{port}"):
        SyslogSender(SyslogDestination("127.0.0.1", port), timeout=2).send(records(1))


def test_an_unreadable_ca_is_refused() -> None:
    with pytest.raises(DeliveryError, match="CA certificates"):
        SyslogSender(SyslogDestination("127.0.0.1", 1, ca_pem="not a certificate")).send(records(1))


# Elasticsearch / OpenSearch


def bulk_handler(statuses: Sequence[int], seen: list[httpx.Request]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        items = [
            {"create": {"status": code, "error": {"reason": "mapping failed"}}}
            if code >= 400 and code != 409
            else {"create": {"status": code}}
            for code in statuses
        ]
        return httpx.Response(200, json={"errors": any(c >= 400 for c in statuses), "items": items})

    return httpx.MockTransport(handle)


def elastic(transport: httpx.MockTransport, **overrides: Any) -> ElasticsearchSender:
    destination = ElasticsearchDestination("https://logs.example.test:9200", "baluarte-audit")
    return ElasticsearchSender(destination, api_key=API_KEY, transport=transport, **overrides)


def test_elasticsearch_bulk_creates_one_document_per_event() -> None:
    seen: list[httpx.Request] = []

    outcome = elastic(bulk_handler([201, 409], seen)).send(records(5, 6))

    assert outcome == Outcome(2)
    (request,) = seen
    assert str(request.url) == "https://logs.example.test:9200/_bulk"
    assert request.headers["Authorization"] == f"ApiKey {API_KEY}"
    assert request.headers["Content-Type"] == "application/x-ndjson"
    lines = [json.loads(line) for line in request.content.decode().splitlines()]
    assert lines[0] == {"create": {"_index": "baluarte-audit", "_id": "baluarte-5"}}
    assert lines[1]["id"] == 5 and "@timestamp" in lines[1]
    assert lines[2] == {"create": {"_index": "baluarte-audit", "_id": "baluarte-6"}}


def test_elasticsearch_reports_how_many_were_delivered_before_a_refusal() -> None:
    seen: list[httpx.Request] = []

    outcome = elastic(bulk_handler([201, 400, 201], seen)).send(records(1, 2, 3))

    assert outcome.delivered == 1
    assert outcome.error is not None and "baluarte-2" in outcome.error
    with pytest.raises(DeliveryError, match="mapping failed"):
        elastic(bulk_handler([400], seen)).send(records(1))


def test_elasticsearch_errors_never_carry_the_credential() -> None:
    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"reason": f"bad key {request.headers['Authorization']}"}}
        )

    from baluarte.logs import register_secret

    register_secret(API_KEY)
    with pytest.raises(DeliveryError) as error:
        elastic(httpx.MockTransport(unauthorized)).send(records(1))
    assert "HTTP 401" in str(error.value)
    assert API_KEY not in str(error.value)


def test_basic_authentication() -> None:
    seen: list[httpx.Request] = []
    destination = ElasticsearchDestination(
        "https://logs.example.test:9200", auth="basic", username="shipper"
    )
    sender = ElasticsearchSender(
        destination, password="s3cret-password", transport=bulk_handler([201], seen)
    )

    sender.send(records(1))

    expected = base64.b64encode(b"shipper:s3cret-password").decode()
    assert seen[0].headers["Authorization"] == f"Basic {expected}"


# Settings API


@pytest.mark.db
def test_settings_are_complete_before_anything_is_saved(engine: Engine) -> None:
    admin = client(engine, 1, Role.ADMIN)

    response = admin.get("/api/v1/settings/audit-shipping")

    assert response.status_code == 200
    assert response.json() == {
        "configured": False,
        "enabled": False,
        "type": "syslog_tls",
        "syslog": {"host": "", "port": 6514, "server_name": None, "ca_pem": None},
        "elasticsearch": {
            "url": "",
            "index": "baluarte-audit",
            "auth": "api_key",
            "username": None,
            "ca_pem": None,
        },
        "api_key": "not_set",
        "password": "not_set",
        "updated_by": None,
        "updated_at": None,
    }
    assert client(engine, 2, Role.AUDITOR).get("/api/v1/settings/audit-shipping").status_code == 403
    status = client(engine, 2, Role.AUDITOR).get("/api/v1/compliance/audit-shipping")
    assert status.status_code == 200
    assert status.json()["enabled"] is False
    assert status.json()["lag_events"] == 0


@pytest.mark.db
def test_saving_syslog_settings(engine: Engine, pki: dict[str, Any]) -> None:
    admin = client(engine, 1, Role.ADMIN)

    saved = admin.put(
        "/api/v1/settings/audit-shipping",
        json=syslog_body(syslog=SYSLOG | {"ca_pem": pki["ca_pem"]}),
    )

    assert saved.status_code == 200, saved.text
    assert saved.json()["configured"] is True
    assert saved.json()["syslog"]["host"] == "siem.example.test"
    assert saved.json()["syslog"]["ca_pem"].startswith("-----BEGIN CERTIFICATE-----")
    assert saved.json()["updated_by"] == "user:1"
    with Session(engine) as db:
        event = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc())).first()
        assert event is not None
        assert event.action == "settings.audit_shipping_updated"
        assert event.details["destination"] == "syslog_tls siem.example.test:6514"


@pytest.mark.db
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"syslog": SYSLOG | {"host": "tcp://siem.example.test"}}, "host"),
        ({"syslog": SYSLOG | {"ca_pem": "not a pem"}}, "PEM"),
        ({"syslog": None}, "syslog host"),
        (
            {"type": "elasticsearch", "elasticsearch": ELASTIC | {"url": "http://logs.test"}},
            "https",
        ),
        (
            {
                "type": "elasticsearch",
                "elasticsearch": ELASTIC | {"url": "https://u:p@logs.example.test"},
            },
            "credentials",
        ),
        (
            {"type": "elasticsearch", "elasticsearch": ELASTIC | {"index": "Upper"}},
            "index",
        ),
        (
            {"type": "elasticsearch", "elasticsearch": ELASTIC | {"auth": "basic"}},
            "username",
        ),
    ],
)
def test_invalid_settings_are_refused(
    engine: Engine, secret_key: None, change: dict[str, Any], message: str
) -> None:
    response = client(engine, 1, Role.ADMIN).put(
        "/api/v1/settings/audit-shipping", json=syslog_body(api_key=API_KEY) | change
    )

    assert response.status_code == 422, response.text
    assert message in response.text


@pytest.mark.db
def test_the_api_key_is_sealed_and_must_be_entered_again_for_a_new_url(
    engine: Engine, secret_key: None
) -> None:
    admin = client(engine, 1, Role.ADMIN)

    missing = admin.put("/api/v1/settings/audit-shipping", json=elastic_body(api_key=None))
    saved = admin.put("/api/v1/settings/audit-shipping", json=elastic_body())
    kept = admin.put(
        "/api/v1/settings/audit-shipping", json=elastic_body(api_key=None, enabled=False)
    )
    moved = admin.put(
        "/api/v1/settings/audit-shipping",
        json=elastic_body(
            api_key=None, elasticsearch=ELASTIC | {"url": "https://other.example.test"}
        ),
    )

    assert missing.status_code == 422 and "API key" in missing.text
    assert saved.status_code == 200, saved.text
    assert saved.json()["api_key"] == "configured"
    assert API_KEY not in saved.text
    assert kept.status_code == 200 and kept.json()["enabled"] is False
    assert moved.status_code == 422
    with Session(engine) as db:
        sealed = db.get(StoredSecret, "audit_shipping_es_api_key")
        assert sealed is not None
        assert API_KEY.encode() not in bytes(sealed.ciphertext)
        assert all(
            API_KEY not in json.dumps(event.details) for event in db.scalars(select(AuditEvent))
        )
    # Switching to syslog drops the stored credential.
    assert admin.put("/api/v1/settings/audit-shipping", json=syslog_body()).status_code == 200
    assert admin.get("/api/v1/settings/audit-shipping").json()["api_key"] == "not_set"


@pytest.mark.db
def test_a_credential_cannot_be_stored_without_a_secret_key(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BALUARTE_SECRET_KEY_FILE", raising=False)

    response = client(engine, 1, Role.ADMIN).put(
        "/api/v1/settings/audit-shipping", json=elastic_body()
    )

    assert response.status_code == 409
    assert "BALUARTE_SECRET_KEY_FILE" in response.text


class FakeSender:
    """Records what it was asked to deliver; fails while ``error`` is set."""

    def __init__(self) -> None:
        self.batches: list[list[Record]] = []
        self.error: str | None = None
        self.accept: int | None = None
        self.credentials: list[shipping.Credentials] = []

    def factory(self, _config: Any, credentials: shipping.Credentials) -> "FakeSender":
        self.credentials.append(credentials)
        return self

    def send(self, batch: Sequence[Record]) -> Outcome:
        self.batches.append(list(batch))
        if self.error is not None and self.accept is None:
            raise DeliveryError(self.error)
        if self.accept is not None:
            return Outcome(self.accept, self.error)
        return Outcome(len(batch))

    def ids(self) -> list[int]:
        return [record.body["id"] for batch in self.batches for record in batch]


@pytest.mark.db
def test_the_connection_test_sends_one_message_without_saving(
    engine: Engine, secret_key: None
) -> None:
    fake = FakeSender()
    admin = client(engine, 1, Role.ADMIN)
    admin.app.dependency_overrides[audit_shipping_sender] = lambda: fake.factory  # type: ignore[attr-defined]

    ok = admin.post("/api/v1/settings/audit-shipping/test", json=elastic_body())
    fake.error = "logs.example.test: HTTP 401 unauthorized"
    failed = admin.post("/api/v1/settings/audit-shipping/test", json=elastic_body())

    assert ok.status_code == 200
    assert ok.json()["ok"] is True
    assert "elasticsearch https://logs.example.test:9200" in ok.json()["detail"]
    assert failed.json() == {"ok": False, "detail": "logs.example.test: HTTP 401 unauthorized"}
    assert fake.batches[0][0].body["type"] == "connection_test"
    assert fake.credentials[0].api_key == API_KEY
    assert admin.get("/api/v1/settings/audit-shipping").json()["configured"] is False


@pytest.mark.db
def test_the_connection_test_reaches_a_real_tls_listener(
    engine: Engine, syslog_server: SyslogServer, pki: dict[str, Any]
) -> None:
    body = syslog_body(
        syslog={
            "host": "127.0.0.1",
            "port": syslog_server.port,
            "server_name": "siem.example.test",
            "ca_pem": pki["ca_pem"],
        }
    )

    response = client(engine, 1, Role.ADMIN).post("/api/v1/settings/audit-shipping/test", json=body)

    assert response.json()["ok"] is True, response.text
    wait_for(lambda: syslog_server.received)
    assert frames(syslog_server.received[0])[0]["type"] == "connection_test"


# Shipping


def configure(engine: Engine, **overrides: Any) -> None:
    response = client(engine, 1, Role.ADMIN).put(
        "/api/v1/settings/audit-shipping", json=syslog_body(**overrides)
    )
    assert response.status_code == 200, response.text


def event_count(engine: Engine) -> int:
    with Session(engine) as db:
        return int(db.scalar(select(func.count()).select_from(AuditEvent)) or 0)


@pytest.mark.db
def test_shipping_sends_every_event_in_order_and_resumes_after_a_failure(engine: Engine) -> None:
    configure(engine)
    with Session(engine) as db:
        for days in (3, 2, 1):
            old_event(db, days)
        db.commit()
    fake = FakeSender()

    first = shipping.ship(engine, sender_factory=fake.factory, batch_size=2)

    total = event_count(engine)
    assert first == shipping.RunResult(total, True)
    assert fake.ids() == sorted(fake.ids())
    assert len(fake.ids()) == total
    assert fake.batches[0][0].doc_id == f"baluarte-{fake.ids()[0]}"
    assert set(fake.batches[0][0].body) == {
        "id",
        "actor",
        "action",
        "resource_type",
        "resource_id",
        "details",
        "details_digest",
        "occurred_at",
    }
    status = client(engine, 1, Role.AUDITOR).get("/api/v1/compliance/audit-shipping").json()
    assert status["lag_events"] == 0
    assert status["last_shipped_id"] == status["latest_event_id"]
    assert status["destination"] == "syslog_tls siem.example.test:6514"

    # The destination goes down: nothing moves, and the error is kept.
    with Session(engine) as db:
        old_event(db, 0, "new.event")
        db.commit()
    fake.error = "syslog siem.example.test:6514: Connection refused"
    failed = shipping.ship(engine, sender_factory=fake.factory)
    assert failed.error == fake.error and failed.shipped == 0
    status = client(engine, 1, Role.AUDITOR).get("/api/v1/compliance/audit-shipping").json()
    assert status["lag_events"] == 1
    assert status["consecutive_failures"] == 1
    assert status["last_error"] == fake.error
    assert status["oldest_unshipped_at"] is not None

    # It comes back: the waiting event is delivered.
    fake.error = None
    fake.batches.clear()
    assert shipping.ship(engine, sender_factory=fake.factory).shipped == 1
    status = client(engine, 1, Role.AUDITOR).get("/api/v1/compliance/audit-shipping").json()
    assert status["lag_events"] == 0 and status["consecutive_failures"] == 0
    assert status["last_error"] is None


@pytest.mark.db
def test_a_partial_delivery_moves_the_cursor_to_the_last_event_delivered(engine: Engine) -> None:
    configure(engine)
    fake = FakeSender()
    fake.accept, fake.error = 1, "event refused"

    result = shipping.ship(engine, sender_factory=fake.factory)

    assert result.shipped == 1 and result.error == "event refused"
    with Session(engine) as db:
        cursor = db.scalars(select(AuditShipment)).one()
        assert cursor.last_shipped_id == fake.ids()[0]
        assert cursor.last_error == "event refused"


@pytest.mark.db
def test_a_new_destination_receives_the_whole_history(engine: Engine) -> None:
    configure(engine)
    fake = FakeSender()
    shipping.ship(engine, sender_factory=fake.factory)
    shipped_before = len(fake.ids())

    configure(engine, syslog=SYSLOG | {"host": "siem2.example.test"})
    fake.batches.clear()
    shipping.ship(engine, sender_factory=fake.factory)

    assert len(fake.ids()) == event_count(engine)
    assert len(fake.ids()) > shipped_before
    with Session(engine) as db:
        assert len(db.scalars(select(AuditShipment)).all()) == 2


@pytest.mark.db
def test_nothing_ships_while_shipping_is_off(engine: Engine) -> None:
    configure(engine, enabled=False)
    fake = FakeSender()

    assert shipping.ship(engine, sender_factory=fake.factory) == shipping.RunResult(0, True)
    assert fake.batches == []


def test_the_job_fails_with_the_reason_so_the_next_run_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from baluarte.jobs.queue import JobError

    error = "syslog siem.example.test:6514: timed out"
    monkeypatch.setattr(shipping, "ship", lambda _engine: shipping.RunResult(0, False, error))

    with pytest.raises(JobError, match="timed out"):
        shipping.handle_ship(None, None)  # type: ignore[arg-type]


@pytest.mark.db
def test_the_schedule_queues_one_job_while_events_wait(engine: Engine) -> None:
    clock = [0.0]
    schedule = shipping.ShippingSchedule(engine, check_every=60, clock=lambda: clock[0])

    def queued() -> int:
        with Session(engine) as db:
            return len(
                db.scalars(
                    select(CollectionJob).where(CollectionJob.kind == shipping.AUDIT_SHIP)
                ).all()
            )

    schedule()
    assert queued() == 0  # shipping is not configured

    configure(engine)
    clock[0] = 61
    schedule()
    clock[0] = 62
    schedule()  # within the minute: not checked again
    assert queued() == 1

    clock[0] = 200
    schedule()  # one is still queued
    assert queued() == 1

    with Session(engine) as db:
        db.execute(
            update(CollectionJob)
            .where(CollectionJob.kind == shipping.AUDIT_SHIP)
            .values(state="succeeded", finished_at=func.now())
        )
        db.commit()
    shipping.ship(engine, sender_factory=FakeSender().factory)
    clock[0] = 400
    schedule()  # caught up: nothing to do
    assert queued() == 1


# Posture and retention


@pytest.mark.db
def test_log_shipping_posture(engine: Engine) -> None:
    save_profile(engine, ["soc2"])
    auditor = client(engine, 2, Role.AUDITOR)

    def control() -> dict[str, Any]:
        controls = auditor.get("/api/v1/compliance/posture").json()["controls"]
        return next(c for c in controls if c["id"] == "log-shipping")

    off = control()
    assert off["status"] == "not_met"
    assert "attested" in off["detail"]

    configure(engine)
    fake = FakeSender()
    shipping.ship(engine, sender_factory=fake.factory)
    caught_up = control()
    assert caught_up["status"] == "met"
    assert "siem.example.test" in caught_up["detail"]

    # Events that have waited more than 15 minutes, with no recent success.
    with Session(engine) as db:
        old_event(db, 1, "stuck.event")
        db.execute(update(AuditShipment).values(last_success_at=func.now() - timedelta(hours=1)))
        db.commit()
    stuck = control()
    assert stuck["status"] == "not_met"
    assert "waited" in stuck["detail"]

    # Shipping off, with the platform's collector attested instead.
    configure(engine, enabled=False)
    attested = client(engine, 1, Role.ADMIN).post(
        "/api/v1/compliance/attestations",
        json={
            "control_id": "log-shipping",
            "statement": "The cluster log collector ships baluarte.audit to the SIEM",
        },
    )
    assert attested.status_code == 201, attested.text
    by_collector = control()
    assert by_collector["status"] == "met"
    assert "collector" in by_collector["detail"]
    assert by_collector["attestation"]["id"] == attested.json()["id"]


@pytest.mark.db
def test_retention_keeps_events_the_destination_has_not_received(engine: Engine) -> None:
    save_profile(engine, ["soc2"])
    with Session(engine) as db:
        shipped_id = old_event(db, 500).id
        db.commit()
    configure(engine)
    fake = FakeSender()
    shipping.ship(engine, sender_factory=fake.factory)
    with Session(engine) as db:
        waiting_id = old_event(db, 450).id
        db.commit()
    # The newer, older-dated event is after the cursor, so it has not shipped yet.
    assert waiting_id > shipped_id

    with Session(engine) as db:
        details = expire(db)
        db.commit()

    assert details["audit_held_for_shipping"] is True
    with Session(engine) as db:
        assert db.get(AuditEvent, shipped_id) is None
        assert db.get(AuditEvent, waiting_id) is not None

    shipping.ship(engine, sender_factory=fake.factory)
    with Session(engine) as db:
        details = expire(db)
        db.commit()
        assert details["audit_held_for_shipping"] is False
        assert db.get(AuditEvent, waiting_id) is None
