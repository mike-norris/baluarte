from pathlib import Path

import pytest

from baluarte.config import ConfigurationError, database_url


def test_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BALUARTE_DATABASE_URL", raising=False)

    with pytest.raises(ConfigurationError, match="not set"):
        database_url()


def test_rejects_non_postgres_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BALUARTE_DATABASE_URL", "sqlite:///baluarte.db")

    with pytest.raises(ConfigurationError, match="PostgreSQL"):
        database_url()


def test_rejects_malformed_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BALUARTE_DATABASE_URL", "not a url")

    with pytest.raises(ConfigurationError, match="valid"):
        database_url()


def test_uses_psycopg_and_reads_password_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    password_file = tmp_path / "password"
    password_file.write_text("s3cret-value\n")
    monkeypatch.setenv("BALUARTE_DATABASE_URL", "postgresql://baluarte@db:5432/baluarte")
    monkeypatch.setenv("BALUARTE_DATABASE_PASSWORD_FILE", str(password_file))

    url = database_url()

    assert url.drivername == "postgresql+psycopg"
    assert url.password == "s3cret-value"
    assert "s3cret-value" not in str(url)
    assert "s3cret-value" not in repr(url)


def test_unreadable_password_file_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BALUARTE_DATABASE_URL", "postgresql://baluarte@db/baluarte")
    monkeypatch.setenv("BALUARTE_DATABASE_PASSWORD_FILE", str(tmp_path / "missing"))

    with pytest.raises(ConfigurationError, match="cannot be read"):
        database_url()


def test_rejects_password_in_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BALUARTE_DATABASE_URL", "postgresql://baluarte:inline-secret@db/baluarte")
    monkeypatch.delenv("BALUARTE_DATABASE_PASSWORD_FILE", raising=False)

    with pytest.raises(ConfigurationError, match="must not contain a password") as raised:
        database_url()
    assert "inline-secret" not in str(raised.value)


def test_rejects_password_in_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BALUARTE_DATABASE_URL", "postgresql://baluarte@db/baluarte")
    monkeypatch.setenv("BALUARTE_DATABASE_PASSWORD", "inline-secret")

    with pytest.raises(ConfigurationError, match="BALUARTE_DATABASE_PASSWORD_FILE instead"):
        database_url()
