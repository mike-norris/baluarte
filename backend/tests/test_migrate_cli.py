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
def test_migrates_configured_database(monkeypatch: pytest.MonkeyPatch, database_url: URL) -> None:
    monkeypatch.setenv("BALUARTE_DATABASE_URL", database_url.render_as_string(hide_password=False))
    monkeypatch.delenv("BALUARTE_DATABASE_PASSWORD_FILE", raising=False)

    assert migrate.main(["--timeout", "5"]) == 0

    engine = create_engine(database_url)
    try:
        assert "observation" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
