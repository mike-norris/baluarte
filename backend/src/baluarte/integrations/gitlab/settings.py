"""GitLab connection settings: one self-managed instance (ADR-0004), token read from a file."""

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from baluarte.settings import ConfigurationError, env, read_secret


@dataclass(frozen=True)
class GitLabSettings:
    # Instance URL without a trailing slash, e.g. https://gitlab.example.internal or
    # https://example.internal/gitlab for a relative-URL install.
    url: str
    token: str = field(repr=False)
    timeout_seconds: float = 15.0

    @property
    def api_url(self) -> str:
        return self.url + "/api/v4"


def _url(raw: str) -> str:
    parts = urlsplit(raw)
    if parts.scheme != "https" or not parts.hostname:
        raise ConfigurationError("BALUARTE_GITLAB_URL must be an https URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigurationError(
            "BALUARTE_GITLAB_URL must not contain credentials, a query or a fragment"
        )
    return raw.rstrip("/")


def load_gitlab_settings() -> GitLabSettings | None:
    """Return the settings, or None when no GitLab URL is configured."""
    raw = env("BALUARTE_GITLAB_URL")
    if not raw:
        return None
    url = _url(raw)
    token = read_secret("BALUARTE_GITLAB_TOKEN", required=True)
    if token is None:  # unreachable: required=True raises instead
        raise ConfigurationError("BALUARTE_GITLAB_TOKEN_FILE is not set")
    timeout_raw = env("BALUARTE_GITLAB_TIMEOUT_SECONDS")
    try:
        timeout = float(timeout_raw) if timeout_raw else 15.0
    except ValueError as exc:
        raise ConfigurationError("BALUARTE_GITLAB_TIMEOUT_SECONDS must be a number") from exc
    if not 1 <= timeout <= 120:
        raise ConfigurationError("BALUARTE_GITLAB_TIMEOUT_SECONDS must be between 1 and 120")
    return GitLabSettings(url=url, token=token, timeout_seconds=timeout)
