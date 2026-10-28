"""``baluarte-migrate``: wait for PostgreSQL, then apply migrations under an advisory lock.

Safe to run from several replicas or a re-run Job: the lock serialises them and Alembic skips
revisions that are already applied.
"""

import argparse
import logging
import sys
import time
from importlib.resources import files

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError

from baluarte.config import ConfigurationError, database_url
from baluarte.settings import configure_logging_from_env

logger = logging.getLogger("baluarte.migrate")

# Arbitrary constant shared by every Baluarte migration runner ("balu" in ASCII).
ADVISORY_LOCK_ID = 0x62616C75


def alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(files("baluarte.db") / "migrations"))
    return config


def head_revisions() -> set[str]:
    return set(ScriptDirectory.from_config(alembic_config()).get_heads())


def wait_for_database(engine: Engine, timeout: float, interval: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return
        except OperationalError:
            if time.monotonic() >= deadline:
                raise
            logger.info("database not reachable yet; retrying in %.0fs", interval)
            time.sleep(interval)


def upgrade(engine: Engine, revision: str = "head") -> None:
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": ADVISORY_LOCK_ID})
        config = alembic_config()
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply Baluarte database migrations.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="seconds to wait for the database to accept connections (default 60)",
    )
    args = parser.parse_args(argv)
    configure_logging_from_env()

    try:
        engine = create_engine(database_url(), connect_args={"connect_timeout": 5})
    except ConfigurationError as exc:
        logger.error("%s", exc)
        return 2
    try:
        wait_for_database(engine, args.timeout)
        upgrade(engine)
    except OperationalError as exc:
        logger.error("database unavailable: %s", exc.orig)
        return 1
    finally:
        engine.dispose()
    logger.info("database schema is up to date")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
