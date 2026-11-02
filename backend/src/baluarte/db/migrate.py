"""``baluarte-migrate``: wait for PostgreSQL, then apply migrations under an advisory lock.

Safe to run from several replicas or a re-run Job: the lock serialises them and Alembic skips
revisions that are already applied.

With ``BALUARTE_APP_DATABASE_USER`` set, it also creates or updates that login role for the API
and worker and grants it only what they need: it owns nothing, cannot change the schema, and
cannot update or delete audit events. Migrations keep running as the schema owner.
"""

import argparse
import base64
import hashlib
import hmac
import logging
import re
import secrets
import sys
import time
from importlib.resources import files

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from baluarte.config import ConfigurationError, database_url
from baluarte.settings import configure_logging_from_env, env, read_secret

logger = logging.getLogger("baluarte.migrate")

# Arbitrary constant shared by every Baluarte migration runner ("balu" in ASCII).
ADVISORY_LOCK_ID = 0x62616C75

ROLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
SCRAM_ITERATIONS = 4096
# Append-only for the app role: it may add audit events but never change or remove one.
# Retention removes old events through a SECURITY DEFINER function owned by the schema owner.
INSERT_ONLY_TABLES = ("audit_event",)
READ_ONLY_TABLES = ("alembic_version",)
# The only functions the app role may call beyond the PUBLIC defaults.
APP_FUNCTIONS = ("baluarte_expire_audit(timestamptz)",)


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


def scram_verifier(password: str, salt: bytes | None = None) -> str:
    """The SCRAM-SHA-256 verifier PostgreSQL stores for a password (RFC 5802, RFC 7677).

    Sending the verifier instead of the password keeps the password out of the server's
    statement log even when it logs DDL.
    """
    salt = salt if salt is not None else secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, SCRAM_ITERATIONS)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def b64(value: bytes) -> str:
        return base64.b64encode(value).decode("ascii")

    return f"SCRAM-SHA-256${SCRAM_ITERATIONS}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def app_role_settings() -> tuple[str, str] | None:
    """The app role's name and password, or None when the deployment does not set one."""
    name = env("BALUARTE_APP_DATABASE_USER")
    if not name:
        return None
    if not ROLE_NAME.fullmatch(name):
        raise ConfigurationError(
            "BALUARTE_APP_DATABASE_USER must be a lower-case PostgreSQL name "
            "(letters, digits and underscores)"
        )
    password = read_secret("BALUARTE_APP_DATABASE_PASSWORD", required=True)
    if password is None:  # unreachable: required=True raises instead
        raise ConfigurationError("BALUARTE_APP_DATABASE_PASSWORD_FILE is not set")
    return name, password


def ensure_app_role(engine: Engine, name: str, password: str) -> None:
    """Create or update the app's login role and grant it data access only. Safe to re-run.

    Grants are applied to every table on each run, so tables added by later migrations are
    covered. The role is refused if it is the schema owner or a superuser, since grants would
    not limit it.
    """
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": ADVISORY_LOCK_ID})
        if name == connection.execute(text("SELECT current_user")).scalar_one():
            raise ConfigurationError(
                "BALUARTE_APP_DATABASE_USER must differ from the user that runs migrations"
            )
        role = connection.execute(
            text("SELECT rolsuper FROM pg_roles WHERE rolname = :name"), {"name": name}
        ).first()
        verifier = scram_verifier(password)
        # Identifiers cannot be bound parameters: the name is checked against ROLE_NAME and
        # quoted, and the verifier holds only base64 and fixed characters. The driver gets the
        # statement as is, since the verifier's colons would read as bind parameters.
        quoted = connection.execute(text("SELECT quote_ident(:name)"), {"name": name}).scalar_one()
        if role is None:
            connection.exec_driver_sql(f"CREATE ROLE {quoted} LOGIN PASSWORD '{verifier}'")
            logger.info("created database role %s for the API and worker", name)
        elif role.rolsuper:
            raise ConfigurationError(f"Database role {name} is a superuser; use a plain role")
        else:
            connection.exec_driver_sql(f"ALTER ROLE {quoted} LOGIN PASSWORD '{verifier}'")
        database = connection.execute(text("SELECT quote_ident(current_database())")).scalar_one()
        tables = connection.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tableowner = current_user ORDER BY tablename"
            )
        ).scalars()
        statements = [
            f"GRANT CONNECT ON DATABASE {database} TO {quoted}",
            f"GRANT USAGE ON SCHEMA public TO {quoted}",
            f"REVOKE CREATE ON SCHEMA public FROM {quoted}",
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {quoted}",
        ]
        for table in tables:
            target = f"public.{table}"
            statements.append(f"REVOKE ALL ON {target} FROM {quoted}")
            if table in READ_ONLY_TABLES:
                statements.append(f"GRANT SELECT ON {target} TO {quoted}")
            elif table in INSERT_ONLY_TABLES:
                statements.append(f"GRANT SELECT, INSERT ON {target} TO {quoted}")
            else:
                statements.append(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {target} TO {quoted}")
        for function in APP_FUNCTIONS:
            if connection.execute(text("SELECT to_regprocedure(:f)"), {"f": function}).scalar():
                statements.append(f"GRANT EXECUTE ON FUNCTION public.{function} TO {quoted}")
        for statement in statements:
            connection.execute(text(statement))
    logger.info("database role %s may read and write data only", name)


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
        app_role = app_role_settings()
    except ConfigurationError as exc:
        logger.error("%s", exc)
        return 2
    try:
        wait_for_database(engine, args.timeout)
        upgrade(engine)
        if app_role is not None:
            ensure_app_role(engine, *app_role)
    except ConfigurationError as exc:
        logger.error("%s", exc)
        return 2
    except ProgrammingError as exc:
        # Typically a managed database whose migration user lacks CREATEROLE.
        logger.error(
            "could not set up the app database role (%s); create it yourself with LOGIN and no "
            "other attributes, or grant CREATEROLE to the migration user (docs/DEPLOYMENT.md)",
            type(exc.orig).__name__,
        )
        return 2
    except OperationalError as exc:
        logger.error("database unavailable: %s", exc.orig)
        return 1
    finally:
        engine.dispose()
    logger.info("database schema is up to date")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
