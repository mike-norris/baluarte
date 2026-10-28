"""Reading settings and secrets from the environment.

Secrets are only ever read from files (``<NAME>_FILE``), typically a Docker or Kubernetes secret
mount. Setting a secret directly in the environment is a ConfigurationError, because
environment variables leak through process listings, crash reports and ``docker inspect``.
Every secret read here is registered with the log redactor.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from baluarte.logs import LogFormat, configure_logging, register_secret

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigurationError(RuntimeError):
    """Required configuration is missing or invalid."""


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def read_secret(name: str, *, required: bool = False) -> str | None:
    """Read the secret ``name`` from the file named by ``<name>_FILE``.

    Returns None when the file variable is unset and the secret is optional.
    """
    if env(name):
        raise ConfigurationError(f"{name} must not be set directly; set {name}_FILE instead")
    path = env(f"{name}_FILE")
    if not path:
        if required:
            raise ConfigurationError(f"{name}_FILE is not set")
        return None
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigurationError(f"{name}_FILE cannot be read") from exc
    if not value:
        raise ConfigurationError(f"{name}_FILE is empty")
    register_secret(value)
    return value


@dataclass(frozen=True)
class LoggingSettings:
    level: str
    format: LogFormat


def load_logging_settings() -> LoggingSettings:
    level = (env("BALUARTE_LOG_LEVEL") or "INFO").upper()
    if level not in LOG_LEVELS:
        raise ConfigurationError(f"BALUARTE_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}")
    fmt = (env("BALUARTE_LOG_FORMAT") or "json").lower()
    if fmt == "json":
        return LoggingSettings(level, "json")
    if fmt == "text":
        return LoggingSettings(level, "text")
    raise ConfigurationError("BALUARTE_LOG_FORMAT must be json or text")


def configure_logging_from_env() -> None:
    """Configure logging from BALUARTE_LOG_*; invalid values fall back to INFO and JSON."""
    try:
        settings = load_logging_settings()
    except ConfigurationError as exc:
        configure_logging()
        logging.getLogger(__name__).error("%s; logging at INFO in json", exc)
        return
    configure_logging(settings.level, settings.format)
