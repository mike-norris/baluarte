"""The GitHub REST client (roadmap M13.2, docs/GITHUB_INTEGRATION.md).

Collection only reads: every request to the REST API is a GET. The one exception is minting a
GitHub App installation access token (``POST /app/installations/{id}/access_tokens``), which
authenticates and changes nothing in the organization.

Rate limits follow GitHub's documented rules (Rate limits for the REST API): a response with
``x-ratelimit-remaining: 0`` waits until ``x-ratelimit-reset``; a secondary limit honours
``retry-after``, or waits a minute and backs off. A wait longer than ``max_wait_seconds`` is not
slept through: the request fails as ``RateLimited`` and the job retries later. Server errors and
dropped connections are retried with backoff. Redirects are not followed.

Failures raise the same error classes as the GitLab client, so the shared discovery and
reconciliation code treats both providers alike.
"""

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt

from baluarte import __version__
from baluarte.integrations.gitlab.client import (
    Forbidden,
    GitLabError,
    InvalidResponse,
    NotFound,
    RateLimited,
    ServerError,
    TlsError,
    Unauthorized,
    Unreachable,
    _is_tls_failure,
)

logger = logging.getLogger(__name__)

API_URL = "https://api.github.com"
WEB_HOST = "github.com"
API_VERSION = "2022-11-28"
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 1.0
SECONDARY_WAIT_SECONDS = 60.0
MAX_WAIT_SECONDS = 300.0
PER_PAGE = 100
# Installation tokens last an hour; one is renewed this long before it expires.
TOKEN_MARGIN = timedelta(minutes=5)

__all__ = [
    "API_URL",
    "Forbidden",
    "GitHubClient",
    "GitLabError",
    "NotFound",
    "RateLimited",
    "Unauthorized",
]


def organization_of(url: str) -> str:
    """The organization login of a connection URL, ``https://github.com/<org>``.

    Raises ValueError for anything else, so a connection only ever names one organization on
    GitHub.com.
    """
    parts = urlsplit(url.strip())
    path = [p for p in parts.path.split("/") if p]
    if parts.scheme != "https" or (parts.hostname or "").lower() != WEB_HOST or len(path) != 1:
        raise ValueError("A GitHub connection URL is https://github.com/<organization>")
    login = path[0]
    if not all(c.isalnum() or c == "-" for c in login) or len(login) > 39:
        raise ValueError("Not a GitHub organization name")
    return login


def connection_url(organization: str) -> str:
    return f"https://{WEB_HOST}/{organization_of(f'https://{WEB_HOST}/{organization}')}"


class TokenAuth:
    """A fine-grained personal access token."""

    def __init__(self, token: str) -> None:
        self._token = token

    def header(self, http: httpx.Client) -> str:
        del http
        return f"Bearer {self._token}"


class AppAuth:
    """A GitHub App installation: a JWT signed with the App's private key is exchanged for an
    installation access token, cached until shortly before it expires."""

    def __init__(
        self,
        app_id: int,
        installation_id: int,
        private_key: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.app_id = app_id
        self.installation_id = installation_id
        self._key = private_key
        self._clock = clock
        self._token: str | None = None
        self._expires: datetime | None = None

    def app_jwt(self) -> str:
        now = self._clock()
        claims = {
            # Backdated a minute for clock drift; GitHub allows at most ten minutes.
            "iat": int((now - timedelta(seconds=60)).timestamp()),
            "exp": int((now + timedelta(minutes=9)).timestamp()),
            "iss": str(self.app_id),
        }
        try:
            return jwt.encode(claims, self._key, algorithm="RS256")
        except (ValueError, TypeError, jwt.PyJWTError) as error:
            raise Unauthorized("The GitHub App private key could not be used") from error

    def header(self, http: httpx.Client) -> str:
        now = self._clock()
        if self._token is None or self._expires is None or now >= self._expires - TOKEN_MARGIN:
            path = f"/app/installations/{self.installation_id}/access_tokens"
            try:
                response = http.post(path, headers={"Authorization": f"Bearer {self.app_jwt()}"})
            except httpx.TransportError as error:
                raise Unreachable(f"could not reach GitHub ({type(error).__name__})") from error
            _raise_for(response, path)
            body = _json(response, path)
            token = body.get("token") if isinstance(body, dict) else None
            expires = body.get("expires_at") if isinstance(body, dict) else None
            if not isinstance(token, str) or not isinstance(expires, str):
                raise InvalidResponse(f"{path}: no installation token in the response")
            self._token = token
            self._expires = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        return f"Bearer {self._token}"


Auth = TokenAuth | AppAuth


def _json(response: httpx.Response, endpoint: str) -> Any:
    try:
        return response.json()
    except ValueError as error:
        raise InvalidResponse(f"{endpoint}: the response is not JSON") from error


def _message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    raw = body.get("message") if isinstance(body, dict) else None
    return raw[:200] if isinstance(raw, str) else ""


def _rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    return response.status_code == 403 and (
        response.headers.get("x-ratelimit-remaining") == "0"
        or "retry-after" in response.headers
        or "rate limit" in _message(response).lower()
    )


def _raise_for(response: httpx.Response, endpoint: str) -> None:
    status = response.status_code
    if 200 <= status < 300:
        return
    detail = _message(response)
    text = f"GitHub answered {status} for {endpoint}" + (f": {detail}" if detail else "")
    if 300 <= status < 400:
        raise InvalidResponse(f"GitHub redirected {endpoint} ({status}); not followed", status)
    if status == 401:
        raise Unauthorized(text, status)
    if _rate_limited(response):
        raise RateLimited(text, status)
    if status == 403:
        raise Forbidden(text, status)
    if status == 404:
        raise NotFound(text, status)
    if status >= 500:
        raise ServerError(text, status)
    raise GitLabError(text, status)


@dataclass
class RateLimit:
    """The latest primary rate limit GitHub reported, for diagnostics."""

    limit: int | None = None
    remaining: int | None = None
    reset: int | None = None
    resource: str | None = None


def _int(value: str | None) -> int | None:
    return int(value) if value is not None and value.isdigit() else None


class GitHubClient:
    def __init__(
        self,
        auth: Auth,
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], float] = time.time,
        max_wait_seconds: float = MAX_WAIT_SECONDS,
    ) -> None:
        self._auth = auth
        self._sleep = sleep
        self._now = wall_clock
        self._max_wait = max_wait_seconds
        self.rate = RateLimit()
        self._http = httpx.Client(
            base_url=API_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": f"baluarte/{__version__}",
            },
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            transport=transport,
        )

    @property
    def acts_as_user(self) -> bool:
        """Whether requests act as a user (a token), not as an App installation."""
        return isinstance(self._auth, TokenAuth)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _wait_for(self, response: httpx.Response, attempt: int) -> float:
        """How long to wait before retrying a rate-limited response."""
        retry_after = response.headers.get("retry-after", "")
        if retry_after.isdigit():
            return float(retry_after)
        reset = _int(response.headers.get("x-ratelimit-reset"))
        if response.headers.get("x-ratelimit-remaining") == "0" and reset is not None:
            return max(float(reset) - float(self._now()), 0.0) + 1.0
        return float(SECONDARY_WAIT_SECONDS * 2 ** (attempt - 1))

    def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET ``path`` (relative to the API, or a next-page URL on it); a 2xx response."""
        if path.startswith("http") and not path.startswith(API_URL + "/"):
            raise InvalidResponse("a next-page link pointed away from the GitHub API")
        endpoint = path.removeprefix(API_URL).split("?", 1)[0]
        for attempt in range(1, MAX_ATTEMPTS + 1):
            headers = {"Authorization": self._auth.header(self._http)}
            try:
                response = self._http.get(path, params=params, headers=headers)
            except httpx.TransportError as error:
                if _is_tls_failure(error):
                    raise TlsError("TLS verification failed for api.github.com") from error
                if attempt == MAX_ATTEMPTS:
                    raise Unreachable(
                        f"could not reach GitHub ({type(error).__name__}) after {attempt} attempts"
                    ) from error
                self._sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
                continue
            self.rate = RateLimit(
                _int(response.headers.get("x-ratelimit-limit")),
                _int(response.headers.get("x-ratelimit-remaining")),
                _int(response.headers.get("x-ratelimit-reset")),
                response.headers.get("x-ratelimit-resource"),
            )
            if _rate_limited(response) and attempt < MAX_ATTEMPTS:
                wait = self._wait_for(response, attempt)
                if wait > self._max_wait:
                    _raise_for(response, endpoint)
                logger.warning("github: GET %s rate limited; waiting %.0fs", endpoint, wait)
                self._sleep(wait)
                continue
            if response.status_code >= 500 and attempt < MAX_ATTEMPTS:
                self._sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
                continue
            _raise_for(response, endpoint)
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return _json(self.get(path, params), path)

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> Iterator[list[Any]]:
        """Each page of a list endpoint, following the ``Link: rel="next"`` header."""
        next_path: str | None = path
        query: dict[str, Any] | None = {"per_page": PER_PAGE, **(params or {})}
        while next_path is not None:
            response = self.get(next_path, query)
            page = _json(response, path)
            if not isinstance(page, list):
                raise InvalidResponse(f"{path}: expected a list")
            yield page
            following = response.links.get("next", {}).get("url")
            next_path = following if isinstance(following, str) else None
            # A next-page link carries its own query.
            query = None
