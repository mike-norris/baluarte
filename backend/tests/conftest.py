import os
import secrets
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url

from baluarte.db.migrate import upgrade


def _admin_url() -> URL:
    raw = os.environ.get("BALUARTE_TEST_DATABASE_URL", "")
    if not raw:
        message = "BALUARTE_TEST_DATABASE_URL is not set"
        if os.environ.get("BALUARTE_REQUIRE_DB_TESTS") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return make_url(raw).set(drivername="postgresql+psycopg")


@pytest.fixture
def database_url() -> Iterator[URL]:
    """URL of a new, empty database that is dropped after the test."""
    admin_url = _admin_url()
    name = f"baluarte_test_{secrets.token_hex(6)}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield admin_url.set(database=name)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def empty_engine(database_url: URL) -> Iterator[Engine]:
    engine = create_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def migrated_engine(empty_engine: Engine) -> Engine:
    upgrade(empty_engine)
    return empty_engine
