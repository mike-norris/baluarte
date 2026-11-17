"""Sending one message to email (SMTP) or a chat webhook.

TLS is always required and always verified, against the system's trust store or the CA
certificates the administrator entered (as for audit shipping): SMTP with STARTTLS or implicit
TLS, and an https webhook. A failure names the stage that failed (connect, TLS, authentication,
or the server rejected the message) with a short, redacted reason; it never carries the SMTP
password or the webhook URL, which embeds its token.
"""

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Literal, Protocol
from urllib.parse import urlsplit

import httpx

from baluarte.compliance.senders import DeliveryError, short, tls_context

Stage = Literal["config", "connect", "tls", "auth", "rejected"]
STAGE_TEXT: dict[Stage, str] = {
    "config": "Not configured",
    "connect": "Could not connect",
    "tls": "TLS failed",
    "auth": "Authentication failed",
    "rejected": "The server rejected the message",
}
SMTP_TIMEOUT = 15.0
WEBHOOK_TIMEOUT = 10.0


class SendFailure(Exception):
    """The message was not sent; ``str()`` is safe to store and show."""

    def __init__(self, stage: Stage, detail: str) -> None:
        self.stage: Stage = stage
        self.detail = short(detail)
        super().__init__(f"{STAGE_TEXT[stage]}: {self.detail}")


@dataclass(frozen=True)
class Message:
    subject: str
    text: str
    html: str


class Sender(Protocol):
    def send(self, message: Message) -> None: ...


@dataclass(frozen=True)
class EmailTarget:
    host: str
    port: int
    security: Literal["starttls", "tls"]
    username: str | None
    from_address: str
    recipients: tuple[str, ...]
    ca_pem: str | None


def _context(ca_pem: str | None) -> ssl.SSLContext:
    try:
        return tls_context(ca_pem)
    except DeliveryError as exc:
        raise SendFailure("tls", str(exc)) from exc


def email_message(target: EmailTarget, message: Message) -> EmailMessage:
    """Plain text with a simple HTML alternative, marked as automatically generated.

    With more than one recipient the To header is the empty group ``undisclosed-recipients:;``;
    the recipients are named only in the SMTP envelope (``send`` passes them as ``to_addrs``).
    """
    email = EmailMessage()
    email["Subject"] = message.subject
    email["From"] = target.from_address
    # Recipients go only in the SMTP envelope, so nobody sees the others' addresses.
    email["To"] = (
        target.recipients[0] if len(target.recipients) == 1 else "undisclosed-recipients:;"
    )
    email["Date"] = formatdate(usegmt=True)
    domain = target.from_address.rpartition("@")[2] or "baluarte.invalid"
    email["Message-ID"] = make_msgid(domain=domain)
    email["Auto-Submitted"] = "auto-generated"
    email.set_content(message.text)
    email.add_alternative(message.html, subtype="html")
    return email


class EmailSender:
    def __init__(
        self,
        target: EmailTarget,
        password: str | None,
        *,
        timeout: float = SMTP_TIMEOUT,
    ) -> None:
        self.target = target
        self.password = password
        self.timeout = timeout

    def _where(self) -> str:
        return f"SMTP {self.target.host}:{self.target.port}"

    def _open(self, context: ssl.SSLContext) -> smtplib.SMTP:
        target = self.target
        try:
            if target.security == "tls":
                return smtplib.SMTP_SSL(
                    target.host, target.port, timeout=self.timeout, context=context
                )
            return smtplib.SMTP(target.host, target.port, timeout=self.timeout)
        except ssl.SSLError as exc:
            raise SendFailure("tls", f"{self._where()}: {short(exc)}") from exc
        except (OSError, smtplib.SMTPException) as exc:
            raise SendFailure("connect", f"{self._where()}: {short(exc)}") from exc

    def _starttls(self, smtp: smtplib.SMTP, context: ssl.SSLContext) -> None:
        try:
            smtp.ehlo()
            if not smtp.has_extn("starttls"):
                raise SendFailure("tls", f"{self._where()}: the server does not offer STARTTLS")
            smtp.starttls(context=context)
            smtp.ehlo()
        except ssl.SSLError as exc:
            raise SendFailure("tls", f"{self._where()}: {short(exc)}") from exc
        except smtplib.SMTPServerDisconnected as exc:
            raise SendFailure("connect", f"{self._where()}: {short(exc)}") from exc
        except smtplib.SMTPException as exc:
            raise SendFailure("tls", f"{self._where()}: {short(exc)}") from exc
        except OSError as exc:
            raise SendFailure("connect", f"{self._where()}: {short(exc)}") from exc

    def _login(self, smtp: smtplib.SMTP) -> None:
        username = self.target.username
        if username is None:
            return
        try:
            smtp.login(username, self.password or "")
        except smtplib.SMTPAuthenticationError as exc:
            raise SendFailure(
                "auth", f"{self._where()}: the server refused the username or password"
            ) from exc
        except smtplib.SMTPNotSupportedError as exc:
            raise SendFailure(
                "auth", f"{self._where()}: the server does not offer authentication"
            ) from exc
        except smtplib.SMTPServerDisconnected as exc:
            raise SendFailure("connect", f"{self._where()}: {short(exc)}") from exc
        except smtplib.SMTPException as exc:
            raise SendFailure("auth", f"{self._where()}: {short(exc)}") from exc
        except OSError as exc:
            raise SendFailure("connect", f"{self._where()}: {short(exc)}") from exc

    def send(self, message: Message) -> None:
        context = _context(self.target.ca_pem)
        smtp = self._open(context)
        try:
            if self.target.security == "starttls":
                self._starttls(smtp, context)
            self._login(smtp)
            try:
                smtp.send_message(
                    email_message(self.target, message),
                    from_addr=self.target.from_address,
                    to_addrs=list(self.target.recipients),
                )
            except (
                smtplib.SMTPRecipientsRefused,
                smtplib.SMTPSenderRefused,
                smtplib.SMTPDataError,
            ) as exc:
                raise SendFailure("rejected", f"{self._where()}: {_smtp_reason(exc)}") from exc
            except smtplib.SMTPServerDisconnected as exc:
                raise SendFailure("connect", f"{self._where()}: {short(exc)}") from exc
            except smtplib.SMTPException as exc:
                raise SendFailure("rejected", f"{self._where()}: {short(exc)}") from exc
            except OSError as exc:
                raise SendFailure("connect", f"{self._where()}: {short(exc)}") from exc
        finally:
            try:
                smtp.quit()
            except (smtplib.SMTPException, OSError):
                smtp.close()


def _smtp_reason(exc: smtplib.SMTPException) -> str:
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = sorted({code for code, _text in exc.recipients.values()})
        return f"{len(exc.recipients)} recipients refused (SMTP {', '.join(map(str, codes))})"
    if isinstance(exc, smtplib.SMTPSenderRefused | smtplib.SMTPDataError):
        error = exc.smtp_error
        text = error.decode("utf-8", "replace") if isinstance(error, bytes) else str(error or "")
        return short(f"SMTP {exc.smtp_code} {text}")
    return short(exc)


def webhook_host(url: str) -> str:
    """The host a webhook URL points at: safe to show, unlike its path, which holds a token."""
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _tls_cause(exc: BaseException) -> ssl.SSLError | None:
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, ssl.SSLError):
            return seen
        seen = seen.__cause__ or seen.__context__
    return None


class WebhookSender:
    """Posts Slack-compatible JSON (``{"text": ...}``), which Mattermost and others accept."""

    def __init__(
        self,
        url: str,
        ca_pem: str | None,
        *,
        timeout: float = WEBHOOK_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url
        self.ca_pem = ca_pem
        self.timeout = timeout
        self.transport = transport

    def send(self, message: Message) -> None:
        where = f"webhook {webhook_host(self.url)}"
        if urlsplit(self.url).scheme != "https":
            raise SendFailure("config", f"{where}: the URL must be https")
        context = _context(self.ca_pem)
        try:
            with httpx.Client(
                verify=context,
                transport=self.transport,
                timeout=self.timeout,
                follow_redirects=False,
                headers={"Accept": "application/json, text/plain"},
            ) as client:
                response = client.post(self.url, json={"text": message.text})
        except httpx.HTTPError as exc:
            tls = _tls_cause(exc)
            if tls is not None:
                reason = getattr(tls, "reason", None) or type(tls).__name__
                raise SendFailure("tls", f"{where}: {short(str(reason))}") from exc
            # The URL holds a token: never let it reach the stored reason.
            reason = str(exc).replace(self.url, "[webhook URL]") or type(exc).__name__
            raise SendFailure("connect", f"{where}: {reason}") from exc
        if response.status_code in (401, 403):
            raise SendFailure("auth", f"{where}: HTTP {response.status_code}")
        if not 200 <= response.status_code < 300:
            raise SendFailure("rejected", f"{where}: HTTP {response.status_code}")
