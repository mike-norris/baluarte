import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import ProgrammingError

from baluarte.db import migrate
from baluarte.db.migrate import (
    ConfigurationError,
    ensure_app_role,
    ensure_backup_role,
    scram_verifier,
)

pytestmark = pytest.mark.db


@pytest.fixture
def role(migrated_engine: Engine) -> Iterator[str]:
    """A role name unique to the test; roles are cluster-wide, so it is dropped afterwards."""
    name = f"baluarte_app_{secrets.token_hex(4)}"
    yield name
    with migrated_engine.begin() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": name}
        ).first()
        if exists:
            connection.execute(text(f"DROP OWNED BY {name}"))
            connection.execute(text(f"DROP ROLE {name}"))


def connect_as(database_url: URL, name: str, password: str) -> Engine:
    return create_engine(database_url.set(username=name, password=password))


def test_the_verifier_has_postgres_format_and_a_fresh_salt() -> None:
    # Whether PostgreSQL accepts it is shown by signing in with the password below.
    first, second = scram_verifier("pencil"), scram_verifier("pencil")

    assert first.startswith("SCRAM-SHA-256$4096:")
    assert "pencil" not in first
    assert first != second


def test_the_role_reads_and_writes_data_but_cannot_change_audit_events_or_schema(
    migrated_engine: Engine, database_url: URL, role: str
) -> None:
    ensure_app_role(migrated_engine, role, "app-password-1")
    app = connect_as(database_url, role, "app-password-1")
    try:
        with app.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO audit_event (actor, action, resource_type, details, "
                    "details_digest) VALUES ('t', 'test', 'test', '{}', repeat('0', 64))"
                )
            )
            assert connection.execute(text("SELECT count(*) FROM audit_event")).scalar_one() == 1
            connection.execute(text("UPDATE app_setting SET value = value"))
            assert connection.execute(text("SELECT version_num FROM alembic_version")).first()
        for statement in (
            "DELETE FROM audit_event",
            "UPDATE audit_event SET actor = 'x'",
            "TRUNCATE audit_event",
            "DELETE FROM alembic_version",
            "ALTER TABLE audit_event DISABLE TRIGGER ALL",
            "CREATE TABLE public.intruder (id int)",
        ):
            with pytest.raises(ProgrammingError), app.begin() as connection:
                connection.execute(text(statement))
    finally:
        app.dispose()


def test_the_role_may_expire_old_audit_events_only_through_the_function(
    migrated_engine: Engine, database_url: URL, role: str
) -> None:
    ensure_app_role(migrated_engine, role, "app-password-1")
    app = connect_as(database_url, role, "app-password-1")
    try:
        with app.begin() as connection:
            deleted = connection.execute(
                text("SELECT deleted FROM baluarte_expire_audit(now() - interval '400 days')")
            ).scalar_one()
        assert deleted == 0
    finally:
        app.dispose()


def test_rerunning_updates_the_password_and_covers_new_tables(
    migrated_engine: Engine, database_url: URL, role: str
) -> None:
    ensure_app_role(migrated_engine, role, "first-password")
    with migrated_engine.begin() as connection:
        connection.execute(text("CREATE TABLE public.added_later (id int)"))

    ensure_app_role(migrated_engine, role, "second-password")

    app = connect_as(database_url, role, "second-password")
    try:
        with app.begin() as connection:
            connection.execute(text("INSERT INTO added_later VALUES (1)"))
    finally:
        app.dispose()


def test_the_migration_user_is_refused_as_the_app_role(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        owner = connection.execute(text("SELECT current_user")).scalar_one()

    with pytest.raises(ConfigurationError, match="must differ"):
        ensure_app_role(migrated_engine, owner, "irrelevant")


@pytest.mark.parametrize("name", ["Baluarte", "app; drop table x", "a" * 64, ""])
def test_role_names_are_checked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str) -> None:
    password = tmp_path / "password"
    password.write_text("app-password")
    monkeypatch.setenv("BALUARTE_APP_DATABASE_USER", name)
    monkeypatch.setenv("BALUARTE_APP_DATABASE_PASSWORD_FILE", str(password))

    if name:
        with pytest.raises(ConfigurationError):
            migrate.app_role_settings()
    else:
        assert migrate.app_role_settings() is None


def test_the_password_file_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BALUARTE_APP_DATABASE_USER", "baluarte_app")
    monkeypatch.delenv("BALUARTE_APP_DATABASE_PASSWORD_FILE", raising=False)

    with pytest.raises(ConfigurationError, match="BALUARTE_APP_DATABASE_PASSWORD_FILE"):
        migrate.app_role_settings()


def test_the_backup_role_reads_everything_and_only_records_backups(
    migrated_engine: Engine, database_url: URL, role: str
) -> None:
    with migrated_engine.begin() as connection:
        # Stand-in until the migration that adds backup_run is applied in this test database.
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS public.backup_run "
                "(id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, status text NOT NULL)"
            )
        )
    ensure_backup_role(migrated_engine, role, "backup-password")
    backup = connect_as(database_url, role, "backup-password")
    try:
        with backup.begin() as connection:
            connection.execute(text("SELECT count(*) FROM audit_event")).scalar_one()
            connection.execute(text("SELECT count(*) FROM app_secret")).scalar_one()
            connection.execute(text("INSERT INTO backup_run (status) VALUES ('succeeded')"))
        for statement in (
            "UPDATE backup_run SET status = 'failed'",
            "DELETE FROM backup_run",
            "INSERT INTO app_setting (key, value, updated_by) VALUES ('x', '{}', 'x')",
            "DELETE FROM audit_event",
            "SELECT nextval(pg_get_serial_sequence('public.audit_event', 'id'))",
            "CREATE TABLE public.intruder (id int)",
        ):
            with pytest.raises(ProgrammingError), backup.begin() as connection:
                connection.execute(text(statement))
    finally:
        backup.dispose()


def test_the_backup_role_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BALUARTE_BACKUP_DATABASE_USER", raising=False)

    assert migrate.backup_role_settings() is None
