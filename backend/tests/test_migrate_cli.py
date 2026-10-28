from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import URL

from baluarte.db import migrate


def test_missing_configuration_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BALUARTE_DATABASE_URL", raising=False)

    assert migrate.main([]) == 2


def test_unreachable_database_exits_1_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BALUARTE_DATABASE_URL", "postgresql://baluarte@127.0.0.1:1/baluarte")
    monkeypatch.delenv("BALUARTE_DATABASE_PASSWORD_FILE", raising=False)

    assert migrate.main(["--timeout", "0"]) == 1


@pytest.mark.db
def test_migrates_configured_database(
    monkeypatch: pytest.MonkeyPatch, database_url: URL, tmp_path: Path
) -> None:
    password_file = tmp_path / "password"
    password_file.write_text(database_url.password or "")
    without_password = URL.create(
        "postgresql",
        username=database_url.username,
        host=database_url.host,
        port=database_url.port,
        database=database_url.database,
    )
    monkeypatch.setenv("BALUARTE_DATABASE_URL", without_password.render_as_string())
    monkeypatch.setenv("BALUARTE_DATABASE_PASSWORD_FILE", str(password_file))

    assert migrate.main(["--timeout", "5"]) == 0

    engine = create_engine(database_url)
    try:
        assert "observation" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
