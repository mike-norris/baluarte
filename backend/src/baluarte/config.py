"""Database connection settings. Shared helpers live in baluarte.settings."""

from sqlalchemy.engine import URL, make_url

from baluarte.settings import ConfigurationError, env, read_secret

__all__ = ["ConfigurationError", "database_url"]


def database_url() -> URL:
    """Build the database URL.

    ``BALUARTE_DATABASE_URL`` holds the URL without a password. The password is read from the
    file named by ``BALUARTE_DATABASE_PASSWORD_FILE`` so it never appears in the environment.
    """
    raw = env("BALUARTE_DATABASE_URL")
    if not raw:
        raise ConfigurationError("BALUARTE_DATABASE_URL is not set")
    try:
        url = make_url(raw)
    except Exception as exc:
        raise ConfigurationError("BALUARTE_DATABASE_URL is not a valid database URL") from exc
    if url.get_backend_name() != "postgresql":
        raise ConfigurationError("BALUARTE_DATABASE_URL must use PostgreSQL")
    url = url.set(drivername="postgresql+psycopg")

    if url.password is not None:
        raise ConfigurationError(
            "BALUARTE_DATABASE_URL must not contain a password; use BALUARTE_DATABASE_PASSWORD_FILE"
        )
    password = read_secret("BALUARTE_DATABASE_PASSWORD")
    if password is not None:
        url = url.set(password=password)
    return url
