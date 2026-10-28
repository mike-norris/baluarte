"""Structured, redacted logging for every Baluarte process.

Every line passes through ``redact`` after formatting, so secrets are masked whether they arrive
in a message, its arguments, an exception or a third-party logger (uvicorn access logs include
query strings, and the OIDC callback's query holds the authorization code).
"""

import json
import logging
import re
import sys
import threading
from datetime import UTC, datetime
from typing import Any, Literal

REDACTED = "[REDACTED]"
MIN_SECRET_LENGTH = 8

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Credentials in URLs: scheme://user:password@host
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@/\s]+@", re.IGNORECASE), rf"\1{REDACTED}@"),
    # Authorization headers
    (re.compile(r"\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE), rf"\1 {REDACTED}"),
    # GitLab tokens: glpat-, gloas-, gldt-, glrt-, glptt-, glcbt-, glft-, glsoat- ...
    (re.compile(r"\bgl[a-z]{2,6}-[A-Za-z0-9_-]{16,}"), REDACTED),
    # JSON Web Tokens
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), REDACTED),
    # Sensitive query, form and key=value parameters
    (
        re.compile(
            r"(?i)\b(private_token|access_token|refresh_token|id_token|token|client_secret"
            r"|password|passwd|secret|code|state|code_verifier)=([^&\s\"',;]+)"
        ),
        rf"\1={REDACTED}",
    ),
]

_secrets: set[str] = set()
_secrets_lock = threading.Lock()


def register_secret(value: str) -> None:
    """Mask this exact value in all future log output. Short values are ignored, since masking
    them would corrupt unrelated text."""
    if len(value) >= MIN_SECRET_LENGTH:
        with _secrets_lock:
            _secrets.add(value)


def redact(text: str) -> str:
    with _secrets_lock:
        known = sorted(_secrets, key=len, reverse=True)
    for secret in known:
        text = text.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_value(value: Any) -> Any:
    """Redact strings inside JSON-like data (dicts, lists, scalars)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value


# Attributes every LogRecord has; anything else was passed through ``extra=``.
_STANDARD = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: UTC timestamp, level, logger, message and any extra fields."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD and not key.startswith("_"):
                entry[key] = redact_value(value)
        if record.exc_info:
            entry["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


LogFormat = Literal["json", "text"]

# Loggers that install their own handlers (uvicorn) are routed through the root handler instead.
_ADOPTED = ("uvicorn", "uvicorn.error", "uvicorn.access")


class _StdoutHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Writes to whatever sys.stdout is at emit time (it is swapped under test capture)."""

    def __init__(self) -> None:
        super().__init__(sys.stdout)

    @property
    def stream(self) -> Any:
        return sys.stdout

    @stream.setter
    def stream(self, _value: Any) -> None:
        pass


def configure_logging(level: str = "INFO", fmt: LogFormat = "json") -> None:
    """Install one redacting stdout handler on the root logger. Safe to call more than once:
    it replaces the handler it installed before and leaves other handlers alone."""
    handler = _StdoutHandler()
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h for h in root.handlers if not isinstance(h, _StdoutHandler)]
    root.addHandler(handler)
    root.setLevel(level)
    for name in _ADOPTED:
        adopted = logging.getLogger(name)
        adopted.handlers.clear()
        adopted.propagate = True
