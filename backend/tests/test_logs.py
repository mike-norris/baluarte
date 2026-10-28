import json
import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from baluarte.logs import (
    REDACTED,
    JsonFormatter,
    TextFormatter,
    configure_logging,
    redact,
    redact_value,
    register_secret,
)
from baluarte.settings import (
    ConfigurationError,
    configure_logging_from_env,
    load_logging_settings,
    read_secret,
)

JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEifQ.c2lnbmF0dXJlLWJ5dGVz"
# Token-shaped fakes are assembled at runtime so secret scanners do not flag the source.
FAKE_PAT = "glpat" + "-" + "AbCdEf1234567890xyz_-"
FAKE_RUNNER = "glrt" + "-" + "0123456789abcdefABCD"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "connect postgresql://baluarte:hunter2-password@db:5432/baluarte",
            f"connect postgresql://baluarte:{REDACTED}@db:5432/baluarte",
        ),
        ("Authorization: Bearer abcdefgh12345678", f"Authorization: Bearer {REDACTED}"),
        ("auth basic dXNlcjpwYXNzd29yZA==", f"auth basic {REDACTED}"),
        (f"token {FAKE_PAT} used", f"token {REDACTED} used"),
        (f"runner {FAKE_RUNNER}", f"runner {REDACTED}"),
        (f"id_token {JWT}", f"id_token {REDACTED}"),
        (
            'GET /api/v1/auth/callback?state=s-123&code=c-456 HTTP/1.1"',
            f'GET /api/v1/auth/callback?state={REDACTED}&code={REDACTED} HTTP/1.1"',
        ),
        ("/api/v4/projects?private_token=abc123", f"/api/v4/projects?private_token={REDACTED}"),
        ("client_secret=xyz&password=p", f"client_secret={REDACTED}&password={REDACTED}"),
        ("GET /api/v1/jobs?state=failed", f"GET /api/v1/jobs?state={REDACTED}"),
        ("nothing sensitive in 42 projects", "nothing sensitive in 42 projects"),
        ("glpat-short", "glpat-short"),
    ],
)
def test_redact_patterns(text: str, expected: str) -> None:
    assert redact(text) == expected


def test_registered_secrets_are_masked_wherever_they_appear() -> None:
    register_secret("Opaque!Value#2026")

    assert redact("failed with Opaque!Value#2026 in body") == f"failed with {REDACTED} in body"


def test_short_values_are_not_registered() -> None:
    register_secret("abc")

    assert redact("abc abcabc") == "abc abcabc"


def test_redact_value_walks_json() -> None:
    value = {"url": "https://u:pw-secret@h/", "items": [FAKE_PAT, 3], "n": None}

    assert redact_value(value) == {
        "url": f"https://u:{REDACTED}@h/",
        "items": [REDACTED, 3],
        "n": None,
    }


def make_record(message: str, *args: object, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("baluarte.test", logging.WARNING, __file__, 1, message, args, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_fields_and_redaction() -> None:
    record = make_record(
        "calling %s",
        "https://gitlab.example.test/?private_token=abc",
        job_id=7,
        header="Bearer abcdefgh12345678",
    )

    entry = json.loads(JsonFormatter().format(record))

    assert entry["level"] == "WARNING"
    assert entry["logger"] == "baluarte.test"
    assert entry["time"].endswith("+00:00")
    assert entry["message"] == f"calling https://gitlab.example.test/?private_token={REDACTED}"
    assert entry["job_id"] == 7
    assert entry["header"] == f"Bearer {REDACTED}"


def test_exceptions_are_redacted() -> None:
    try:
        raise RuntimeError("password=letmein-now")
    except RuntimeError:
        record = logging.LogRecord(
            "baluarte.test", logging.ERROR, __file__, 1, "boom", None, sys.exc_info()
        )

    entry = json.loads(JsonFormatter().format(record))
    text = TextFormatter().format(record)

    assert "letmein-now" not in entry["exception"]
    assert "RuntimeError" in entry["exception"]
    assert "letmein-now" not in text


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_configure_logging_routes_uvicorn_through_one_redacting_handler(
    restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    access = logging.getLogger("uvicorn.access")
    access.addHandler(logging.NullHandler())
    access.propagate = False

    configure_logging("INFO", "json")
    configure_logging("INFO", "json")
    access.info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:5000",
        "GET",
        "/api/v1/auth/callback?code=abc123&state=xyz789",
        "1.1",
        303,
    )

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["logger"] == "uvicorn.access"
    assert "abc123" not in entry["message"]
    assert "xyz789" not in entry["message"]


def test_text_format(restore_logging: None, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING", "text")
    logging.getLogger("baluarte.test").info("hidden")
    logging.getLogger("baluarte.test").warning("visible token=abc")

    out = capsys.readouterr().out
    assert "hidden" not in out
    assert f"WARNING baluarte.test visible token={REDACTED}" in out


@pytest.mark.parametrize(
    ("variables", "level", "fmt"),
    [
        ({}, "INFO", "json"),
        ({"BALUARTE_LOG_LEVEL": "debug", "BALUARTE_LOG_FORMAT": "TEXT"}, "DEBUG", "text"),
    ],
)
def test_logging_settings(
    monkeypatch: pytest.MonkeyPatch, variables: dict[str, str], level: str, fmt: str
) -> None:
    monkeypatch.delenv("BALUARTE_LOG_LEVEL", raising=False)
    monkeypatch.delenv("BALUARTE_LOG_FORMAT", raising=False)
    for name, value in variables.items():
        monkeypatch.setenv(name, value)

    settings = load_logging_settings()

    assert (settings.level, settings.format) == (level, fmt)


@pytest.mark.parametrize(
    ("name", "value"), [("BALUARTE_LOG_LEVEL", "TRACE"), ("BALUARTE_LOG_FORMAT", "xml")]
)
def test_invalid_logging_settings(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigurationError, match=name):
        load_logging_settings()


def test_invalid_logging_settings_fall_back_and_say_so(
    monkeypatch: pytest.MonkeyPatch, restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BALUARTE_LOG_LEVEL", "TRACE")

    configure_logging_from_env()

    entry = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert entry["level"] == "ERROR"
    assert "BALUARTE_LOG_LEVEL" in entry["message"]


def test_read_secret(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("file-secret-value-1\n")
    monkeypatch.delenv("BALUARTE_EXAMPLE_TOKEN", raising=False)
    monkeypatch.setenv("BALUARTE_EXAMPLE_TOKEN_FILE", str(path))

    assert read_secret("BALUARTE_EXAMPLE_TOKEN") == "file-secret-value-1"
    assert redact("leaked file-secret-value-1") == f"leaked {REDACTED}"


def test_read_secret_optional_and_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BALUARTE_EXAMPLE_TOKEN", raising=False)
    monkeypatch.delenv("BALUARTE_EXAMPLE_TOKEN_FILE", raising=False)

    assert read_secret("BALUARTE_EXAMPLE_TOKEN") is None
    with pytest.raises(ConfigurationError, match="BALUARTE_EXAMPLE_TOKEN_FILE is not set"):
        read_secret("BALUARTE_EXAMPLE_TOKEN", required=True)


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("direct", "must not be set directly"),
        ("missing", "cannot be read"),
        ("empty", "is empty"),
    ],
)
def test_read_secret_rejects_unsafe_or_broken_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, setup: str, message: str
) -> None:
    path = tmp_path / "token"
    monkeypatch.delenv("BALUARTE_EXAMPLE_TOKEN", raising=False)
    monkeypatch.setenv("BALUARTE_EXAMPLE_TOKEN_FILE", str(path))
    if setup == "direct":
        monkeypatch.setenv("BALUARTE_EXAMPLE_TOKEN", "in-the-environment")
    elif setup == "empty":
        path.write_text("\n")

    with pytest.raises(ConfigurationError, match=message) as raised:
        read_secret("BALUARTE_EXAMPLE_TOKEN")
    assert "in-the-environment" not in str(raised.value)
