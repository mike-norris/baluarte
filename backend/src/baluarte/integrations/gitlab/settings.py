"""GitLab connection settings: one self-managed instance (ADR-0004).

The deployment sets them in the environment, with the token read from a file, or an administrator
enters them in the web UI (ADR-0008). The environment wins; both go through the same checks.
"""

from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from baluarte.settings import ConfigurationError, env, read_secret


@dataclass(frozen=True)
class GitLabSettings:
    # Instance URL without a trailing slash, e.g. https://gitlab.example.internal or
    # https://example.internal/gitlab for a relative-URL install.
    url: str
    token: str = field(repr=False)
    timeout_seconds: float = 15.0
    # How often the worker schedules full collection; 0 turns scheduling off.
    sync_interval_minutes: int = 360
    # Where the settings came from: the environment, or the web UI (ADR-0008).
    source: Literal["deployment", "database"] = "deployment"

    @property
    def api_url(self) -> str:
        return self.url + "/api/v4"


def gitlab_url(raw: str, name: str = "BALUARTE_GITLAB_URL") -> str:
    parts = urlsplit(raw)
    if parts.scheme != "https" or not parts.hostname:
        raise ConfigurationError(f"{name} must be an https URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigurationError(f"{name} must not contain credentials, a query or a fragment")
    return raw.rstrip("/")


def checked_timeout(value: float, name: str = "BALUARTE_GITLAB_TIMEOUT_SECONDS") -> float:
    if not 1 <= value <= 120:
        raise ConfigurationError(f"{name} must be between 1 and 120")
    return float(value)


def checked_interval(value: int, name: str = "BALUARTE_GITLAB_SYNC_INTERVAL_MINUTES") -> int:
    if value != 0 and not 15 <= value <= 10080:
        raise ConfigurationError(f"{name} must be 0 (off) or between 15 and 10080")
    return value


def deployment_gitlab_present() -> bool:
    """Whether the environment configures GitLab (and so wins over the web UI)."""
    return bool(env("BALUARTE_GITLAB_URL"))


def load_gitlab_settings() -> GitLabSettings | None:
    """The effective settings: the deployment's, else those entered in the web UI.

    None when neither configures GitLab.
    """
    # Imported here: the store builds GitLabSettings, so a module-level import would be circular.
    from baluarte.configuration.effective import effective_gitlab_settings

    return effective_gitlab_settings()


def load_deployment_gitlab_settings() -> GitLabSettings | None:
    """The environment's settings, or None when no GitLab URL is set."""
    raw = env("BALUARTE_GITLAB_URL")
    if not raw:
        return None
    url = gitlab_url(raw)
    token = read_secret("BALUARTE_GITLAB_TOKEN", required=True)
    if token is None:  # unreachable: required=True raises instead
        raise ConfigurationError("BALUARTE_GITLAB_TOKEN_FILE is not set")
    timeout_raw = env("BALUARTE_GITLAB_TIMEOUT_SECONDS")
    try:
        timeout = float(timeout_raw) if timeout_raw else 15.0
    except ValueError as exc:
        raise ConfigurationError("BALUARTE_GITLAB_TIMEOUT_SECONDS must be a number") from exc
    checked_timeout(timeout)
    interval_raw = env("BALUARTE_GITLAB_SYNC_INTERVAL_MINUTES")
    try:
        interval = int(interval_raw) if interval_raw else 360
    except ValueError as exc:
        raise ConfigurationError(
            "BALUARTE_GITLAB_SYNC_INTERVAL_MINUTES must be a whole number of minutes"
        ) from exc
    checked_interval(interval)
    return GitLabSettings(
        url=url, token=token, timeout_seconds=timeout, sync_interval_minutes=interval
    )
