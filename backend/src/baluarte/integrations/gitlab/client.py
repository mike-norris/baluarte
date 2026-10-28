"""Minimal read-only GitLab REST client.

- Only GET requests: v0.1 never writes to GitLab (ADR-0003).
- The token is sent in the ``PRIVATE-TOKEN`` header. Redirects are never followed, so a
  misconfigured or hostile endpoint cannot have the token forwarded to another host.
- 429, 502, 503, 504, timeouts and connection errors are retried a bounded number of times
  with exponential backoff, honouring ``Retry-After`` up to a cap.
- ``paginate`` follows the ``Link: rel="next"`` header, which GitLab sends for both offset and
  keyset pagination, and refuses a next-page URL outside the configured API so the token is
  never sent elsewhere.
- Failures raise a GitLabError subclass whose message is safe to store and show: it never
  contains the token, response bodies or query strings.
"""

import logging
import ssl
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from baluarte import __version__
from baluarte.integrations.gitlab.settings import GitLabSettings

logger = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 502, 503, 504})
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 0.5
MAX_RETRY_AFTER_SECONDS = 30.0
PER_PAGE = 100
# Guards against a next link that never ends: 100,000 pages of 100 items.
MAX_PAGES = 100_000


class GitLabError(Exception):
    """A GitLab request failed. ``reason`` is a stable code; the message is safe to show."""

    reason = "error"

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Unreachable(GitLabError):
    reason = "unreachable"


class TlsError(GitLabError):
    reason = "tls_error"


class Unauthorized(GitLabError):
    reason = "unauthorized"


class Forbidden(GitLabError):
    reason = "forbidden"


class NotFound(GitLabError):
    reason = "not_found"


class RateLimited(GitLabError):
    reason = "rate_limited"


class ServerError(GitLabError):
    reason = "server_error"


class InvalidResponse(GitLabError):
    reason = "invalid_response"


def _is_tls_failure(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ssl.SSLError):
            return True
        current = current.__cause__ or current.__context__
    return False


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After", "")
    try:
        seconds = float(value)
    except ValueError:
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


class GitLabClient:
    def __init__(
        self,
        settings: GitLabSettings,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sleep = sleep
        self._api_prefix = settings.api_url + "/"
        self._http = httpx.Client(
            base_url=settings.api_url,
            headers={
                "PRIVATE-TOKEN": settings.token,
                "Accept": "application/json",
                "User-Agent": f"baluarte/{__version__}",
            },
            timeout=httpx.Timeout(settings.timeout_seconds),
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "GitLabClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET ``path`` (relative to /api/v4) and return a 2xx response, or raise GitLabError."""
        # Next-page links are absolute; messages and logs use the API-relative path.
        endpoint = path.split("?", 1)[0].removeprefix(self._api_prefix[:-1])
        for attempt in range(1, MAX_ATTEMPTS + 1):
            delay = BACKOFF_SECONDS * 2 ** (attempt - 1)
            try:
                response = self._http.get(path, params=params)
            except httpx.TransportError as exc:
                if _is_tls_failure(exc):
                    raise TlsError(
                        "TLS verification failed; check the certificate chain or SSL_CERT_FILE"
                    ) from exc
                if attempt == MAX_ATTEMPTS:
                    raise Unreachable(
                        f"could not reach GitLab ({type(exc).__name__}) after {attempt} attempts"
                    ) from exc
                logger.warning("gitlab: GET %s failed (%s); retrying", endpoint, type(exc).__name__)
                self._sleep(delay)
                continue
            if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                wait = _retry_after(response) or delay
                logger.warning(
                    "gitlab: GET %s returned %s; retrying in %.1fs",
                    endpoint,
                    response.status_code,
                    wait,
                )
                self._sleep(wait)
                continue
            return self._check(response, endpoint)
        raise AssertionError("unreachable")  # pragma: no cover

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._json(self.get(path, params), path)

    def paginate(
        self, path: str, params: dict[str, Any] | None = None
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield each page of a list endpoint, following ``rel="next"`` links to the end."""
        url: str = path
        query: dict[str, Any] | None = {"per_page": PER_PAGE, **(params or {})}
        seen: set[str] = set()
        for _page in range(MAX_PAGES):
            response = self.get(url, query)
            items = self._json(response, path)
            if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
                raise InvalidResponse(f"GET {path} did not return a list of objects")
            yield items
            following = response.links.get("next", {}).get("url")
            if not following:
                return
            if not following.startswith(self._api_prefix):
                raise InvalidResponse(f"GET {path}: next-page link points outside the GitLab API")
            if following in seen:
                raise InvalidResponse(f"GET {path}: next-page link repeats")
            seen.add(following)
            # The next link carries every query parameter, including the cursor.
            url, query = following, None
        raise InvalidResponse(f"GET {path}: more than {MAX_PAGES} pages")

    @staticmethod
    def _json(response: httpx.Response, path: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise InvalidResponse(f"GET {path} did not return JSON") from exc

    @staticmethod
    def _check(response: httpx.Response, endpoint: str) -> httpx.Response:
        status = response.status_code
        if 200 <= status < 300:
            return response
        if status == 401:
            raise Unauthorized("GitLab rejected the token (401)", status)
        if status == 403:
            raise Forbidden(f"the token may not read {endpoint} (403)", status)
        if status == 404:
            raise NotFound(f"{endpoint} was not found (404)", status)
        if status == 429:
            raise RateLimited("GitLab rate limit exceeded (429)", status)
        if status >= 500:
            raise ServerError(f"GitLab returned {status} for {endpoint}", status)
        if 300 <= status < 400:
            raise InvalidResponse(
                f"GitLab redirected {endpoint} ({status}); check BALUARTE_GITLAB_URL", status
            )
        raise InvalidResponse(f"unexpected status {status} for {endpoint}", status)
