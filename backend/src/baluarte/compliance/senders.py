"""Delivering audit events to a log store outside Baluarte: syslog over TLS, or the bulk API of
Elasticsearch or OpenSearch.

TLS is always verified, against the system's trust store or the CA certificates the
administrator entered; nothing is ever sent in plain text. A sender takes a batch in event ID
order and returns how many of its first events were delivered. Anything less than the whole
batch carries the reason, and the caller resends from the first event not delivered, so delivery
is at least once.
"""

import json
import socket
import ssl
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import httpx

from baluarte.logs import redact

# RFC 5424 PRI for facility 13 (log audit), severity 5 (notice).
SYSLOG_PRI = 13 * 8 + 5
SYSLOG_APP_NAME = "baluarte"
SYSLOG_MSGID = "audit"
UTF8_BOM = "﻿".encode()
MAX_ERROR = 500


class DeliveryError(Exception):
    """Nothing could be delivered; the message is safe to store and show."""


@dataclass(frozen=True)
class Record:
    """One event to deliver: its document ID at the destination and its JSON body."""

    doc_id: str
    body: dict[str, Any]


@dataclass(frozen=True)
class Outcome:
    # How many of the batch's first records were delivered.
    delivered: int
    # Why the rest were not; None when the whole batch was delivered.
    error: str | None = None


class Sender(Protocol):
    def send(self, records: Sequence[Record]) -> Outcome: ...


@dataclass(frozen=True)
class SyslogDestination:
    host: str
    port: int = 6514
    # The name the server's certificate must carry, when it differs from ``host``.
    server_name: str | None = None
    ca_pem: str | None = None


@dataclass(frozen=True)
class ElasticsearchDestination:
    url: str
    index: str = "baluarte-audit"
    auth: Literal["api_key", "basic"] = "api_key"
    username: str | None = None
    ca_pem: str | None = None


def tls_context(ca_pem: str | None) -> ssl.SSLContext:
    """A verifying client context: hostname and chain checked, TLS 1.2 or later."""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if ca_pem:
        try:
            context.load_verify_locations(cadata=ca_pem)
        except (ssl.SSLError, ValueError) as exc:
            raise DeliveryError("The CA certificates could not be read") from exc
    return context


def short(error: BaseException | str) -> str:
    """A one-line, redacted reason, short enough to store."""
    text = error if isinstance(error, str) else (str(error) or type(error).__name__)
    return redact(" ".join(text.split()))[:MAX_ERROR]


# Syslog ----------------------------------------------------------------------------------------


def _hostname() -> str:
    name = "".join(c for c in socket.gethostname() if 33 <= ord(c) <= 126)
    return name[:255] or "-"


def _timestamp(body: dict[str, Any]) -> str:
    raw = body.get("occurred_at")
    try:
        moment = datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        moment = None
    moment = moment or datetime.now(UTC)
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def syslog_frame(body: dict[str, Any], hostname: str) -> bytes:
    """One RFC 5424 message with RFC 5425 octet-counting framing; the MSG is the event's JSON."""
    header = f"<{SYSLOG_PRI}>1 {_timestamp(body)} {hostname} {SYSLOG_APP_NAME} - {SYSLOG_MSGID} - "
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    message = header.encode("ascii") + UTF8_BOM + payload.encode("utf-8")
    return f"{len(message)} ".encode("ascii") + message


class SyslogSender:
    """Sends a batch over one TLS connection. TCP gives no per-message acknowledgement, so a
    batch counts as delivered once every byte is written and the connection is closed cleanly."""

    def __init__(
        self,
        destination: SyslogDestination,
        *,
        timeout: float = 10.0,
        connect: Callable[[tuple[str, int], float], socket.socket] = socket.create_connection,
    ) -> None:
        self.destination = destination
        self.timeout = timeout
        self.connect = connect

    def send(self, records: Sequence[Record]) -> Outcome:
        if not records:
            return Outcome(0)
        destination = self.destination
        context = tls_context(destination.ca_pem)
        hostname = _hostname()
        try:
            raw = self.connect((destination.host, destination.port), self.timeout)
            with context.wrap_socket(
                raw, server_hostname=destination.server_name or destination.host
            ) as connection:
                connection.settimeout(self.timeout)
                for record in records:
                    connection.sendall(syslog_frame(record.body, hostname))
        except (OSError, ssl.SSLError) as exc:
            raise DeliveryError(
                f"syslog {destination.host}:{destination.port}: {short(exc)}"
            ) from exc
        return Outcome(len(records))


# Elasticsearch / OpenSearch --------------------------------------------------------------------

DELIVERED_STATUSES = (200, 201, 409)


class ElasticsearchSender:
    """Indexes a batch with one ``_bulk`` request of ``create`` actions. The document ID is
    fixed per event, so a resent event is refused with 409, which counts as delivered."""

    def __init__(
        self,
        destination: ElasticsearchDestination,
        *,
        api_key: str | None = None,
        password: str | None = None,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.destination = destination
        self.api_key = api_key
        self.password = password
        self.timeout = timeout
        self.transport = transport

    def _client(self) -> httpx.Client:
        headers = {"Content-Type": "application/x-ndjson", "Accept": "application/json"}
        auth: httpx.BasicAuth | None = None
        if self.destination.auth == "api_key" and self.api_key:
            headers["Authorization"] = f"ApiKey {self.api_key}"
        elif self.destination.auth == "basic" and self.destination.username:
            auth = httpx.BasicAuth(self.destination.username, self.password or "")
        return httpx.Client(
            verify=tls_context(self.destination.ca_pem),
            transport=self.transport,
            timeout=self.timeout,
            headers=headers,
            auth=auth,
            follow_redirects=False,
        )

    def _body(self, records: Sequence[Record]) -> bytes:
        lines = []
        for record in records:
            action = {"create": {"_index": self.destination.index, "_id": record.doc_id}}
            document = dict(record.body)
            document.setdefault("@timestamp", _timestamp(record.body))
            lines.append(json.dumps(action, separators=(",", ":")))
            lines.append(json.dumps(document, separators=(",", ":"), ensure_ascii=False))
        return ("\n".join(lines) + "\n").encode("utf-8")

    def send(self, records: Sequence[Record]) -> Outcome:
        if not records:
            return Outcome(0)
        where = f"{self.destination.url} index {self.destination.index}"
        try:
            with self._client() as client:
                response = client.post(
                    self.destination.url.rstrip("/") + "/_bulk", content=self._body(records)
                )
        except httpx.HTTPError as exc:
            raise DeliveryError(f"{where}: {short(exc)}") from exc
        if response.status_code != 200:
            raise DeliveryError(f"{where}: HTTP {response.status_code} {_reason(response)}")
        try:
            items = response.json().get("items")
        except (ValueError, AttributeError) as exc:
            raise DeliveryError(f"{where}: the bulk response is not JSON") from exc
        if not isinstance(items, list) or len(items) != len(records):
            raise DeliveryError(f"{where}: the bulk response does not list every event")
        for position, item in enumerate(items):
            result = item.get("create") if isinstance(item, dict) else None
            code = result.get("status") if isinstance(result, dict) else None
            if code in DELIVERED_STATUSES:
                continue
            detail = result.get("error") if isinstance(result, dict) else None
            reason = detail.get("reason") if isinstance(detail, dict) else detail
            error = f"{where}: event {records[position].doc_id} refused (HTTP {code}): {reason}"
            if position == 0:
                raise DeliveryError(short(error))
            return Outcome(position, short(error))
        return Outcome(len(records))


def _reason(response: httpx.Response) -> str:
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return short(response.reason_phrase or "")
    if isinstance(error, dict):
        return short(str(error.get("reason") or error.get("type") or ""))
    return short(str(error or response.reason_phrase or ""))
