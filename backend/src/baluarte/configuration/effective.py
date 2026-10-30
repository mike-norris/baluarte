"""The settings in force: the deployment's environment first, then the web UI (ADR-0008).

Precedence is per section. When the environment configures sign-in (or GitLab) at all, that
section comes only from the environment and the web UI cannot change it. Otherwise the section
stored in the database applies.

Stored results are cached for ``CACHE_SECONDS``, so every API and worker process picks up a change
made in the web UI within that time without a restart; the process that made the change calls
``invalidate`` and sees it at once. Failures, and the environment's settings, are not cached.
"""

import logging
import threading
import time
from collections.abc import Callable

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from baluarte.auth.settings import AuthSettings, deployment_auth_present, load_auth_settings
from baluarte.configuration.crypto import load_secret_box
from baluarte.configuration.store import load_stored_auth, load_stored_gitlab
from baluarte.db.session import get_engine
from baluarte.integrations.gitlab.settings import (
    GitLabSettings,
    deployment_gitlab_present,
    load_deployment_gitlab_settings,
)
from baluarte.settings import ConfigurationError

logger = logging.getLogger(__name__)

CACHE_SECONDS = 10.0


class SettingsUnavailable(ConfigurationError):
    """The stored settings could not be read, e.g. because the database is unreachable."""


def default_engine() -> Engine | None:
    try:
        return get_engine()
    except ConfigurationError:
        return None


# The database holding stored settings, or None when there is none. Tests replace it.
engine_provider: Callable[[], Engine | None] = default_engine

_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}


def invalidate() -> None:
    with _lock:
        _cache.clear()


def _cached[T](key: str, load: Callable[[], T]) -> T:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < CACHE_SECONDS:
            return hit[1]  # type: ignore[return-value]
    value = load()
    with _lock:
        _cache[key] = (now, value)
    return value


def _from_store[T](read: Callable[[Session], T | None]) -> T | None:
    engine = engine_provider()
    if engine is None:
        return None
    try:
        with Session(engine) as db:
            return read(db)
    except SQLAlchemyError as exc:
        raise SettingsUnavailable(
            f"Stored settings could not be read ({type(exc).__name__})"
        ) from exc


def effective_auth_settings() -> AuthSettings | None:
    """Sign-in settings in force, or None when neither the deployment nor the UI sets them."""

    if deployment_auth_present():
        return load_auth_settings()

    def load() -> AuthSettings | None:
        box = load_secret_box()
        return _from_store(lambda db: load_stored_auth(db, box))

    return _cached("auth", load)


def effective_gitlab_settings() -> GitLabSettings | None:
    """GitLab settings in force, or None when neither the deployment nor the UI sets them."""

    if deployment_gitlab_present():
        return load_deployment_gitlab_settings()

    def load() -> GitLabSettings | None:
        box = load_secret_box()
        return _from_store(lambda db: load_stored_gitlab(db, box))

    return _cached("gitlab", load)
