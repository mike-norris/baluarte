"""Runtime configuration read from the environment.

Only database settings exist so far; the full settings model arrives with BAL-010.
"""

import os
from pathlib import Path

from sqlalchemy.engine import URL, make_url


class ConfigurationError(RuntimeError):
    """Required configuration is missing or invalid."""


def database_url() -> URL:
    """Build the database URL.

    ``BALUARTE_DATABASE_URL`` holds the URL without a password. The password is read from the
    file named by ``BALUARTE_DATABASE_PASSWORD_FILE`` so it never appears in the environment.
    """
    raw = os.environ.get("BALUARTE_DATABASE_URL", "").strip()
    if not raw:
        raise ConfigurationError("BALUARTE_DATABASE_URL is not set")
    try:
        url = make_url(raw)
    except Exception as exc:
        raise ConfigurationError("BALUARTE_DATABASE_URL is not a valid database URL") from exc
    if url.get_backend_name() != "postgresql":
        raise ConfigurationError("BALUARTE_DATABASE_URL must use PostgreSQL")
    url = url.set(drivername="postgresql+psycopg")

    password_file = os.environ.get("BALUARTE_DATABASE_PASSWORD_FILE", "").strip()
    if password_file:
        try:
            password = Path(password_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigurationError("BALUARTE_DATABASE_PASSWORD_FILE cannot be read") from exc
        url = url.set(password=password)
    return url
