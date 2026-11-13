"""Minimal read-only GitLab client: REST, and GraphQL queries sent with GET.

- Only GET requests: v0.1 never writes to GitLab (ADR-0003). GraphQL queries go to
  ``/api/graphql`` as GET query parameters, which GitLab accepts for queries but never for
  mutations, so the GraphQL helper cannot change anything either.
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

import json
import logging
import ssl
import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlencode

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
GRAPHQL_PATH = "/api/graphql"
GRAPHQL_ENDPOINT = f"GET {GRAPHQL_PATH}"
# The encoded query string of a GraphQL GET, kept well under common 8 KiB request-line limits.
MAX_GRAPHQL_REQUEST_BYTES = 6000
# GraphQL error messages are shown and stored; longer ones are cut.
MAX_GRAPHQL_MESSAGE = 200


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


class GraphQLError(InvalidResponse):
    """GraphQL answered with ``errors``: the query as a whole was refused or failed."""

    reason = "graphql_error"


class UnsupportedField(GraphQLError):
    """The query names a field this GitLab version or edition does not have."""

    reason = "unsupported"


class GraphQLForbidden(Forbidden):
    """GraphQL refused the object: hidden from the token, or not licensed for its tier."""


def _graphql_error(errors: Any) -> GitLabError:
    first = errors[0] if isinstance(errors, list) and errors else None
    raw = first.get("message") if isinstance(first, dict) else None
    message = raw if isinstance(raw, str) else "the query failed"
    if len(message) > MAX_GRAPHQL_MESSAGE:
        message = message[: MAX_GRAPHQL_MESSAGE - 1] + "…"
    lowered = message.lower()
    if (
        "doesn't exist on type" in lowered
        or "does not exist on type" in lowered
        or "no such type" in lowered
    ):
        return UnsupportedField(f"GraphQL: {message}")
    if "permission" in lowered or "not licensed" in lowered:
        return GraphQLForbidden(f"GraphQL: {message}")
    return GraphQLError(f"GraphQL: {message}")


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
        # Built from the configured URL only, so a query is never sent to another host.
        self._graphql_url = settings.url + GRAPHQL_PATH
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
        return self._send(path, params, endpoint)

    def _send(self, path: str, params: dict[str, Any] | None, endpoint: str) -> httpx.Response:
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

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a GraphQL query with GET and return its ``data``.

        Raises UnsupportedField when GitLab does not know a field, GraphQLForbidden when it
        refuses the object, and GraphQLError for any other ``errors``, even when some ``data``
        came back: a partly answered query is not evidence.
        """
        if not query.lstrip().startswith(("query", "{")):
            raise ValueError("only GraphQL queries can be sent")
        params = {"query": query}
        if variables:
            params["variables"] = json.dumps(variables, separators=(",", ":"), sort_keys=True)
        if len(urlencode(params)) > MAX_GRAPHQL_REQUEST_BYTES:
            raise ValueError(f"GraphQL request exceeds {MAX_GRAPHQL_REQUEST_BYTES} bytes")
        response = self._send(self._graphql_url, params, GRAPHQL_PATH)
        body = self._json(response, GRAPHQL_PATH)
        if not isinstance(body, dict):
            raise InvalidResponse(f"{GRAPHQL_ENDPOINT} did not return a JSON object")
        if body.get("errors"):
            raise _graphql_error(body["errors"])
        data = body.get("data")
        if not isinstance(data, dict):
            raise InvalidResponse(f"{GRAPHQL_ENDPOINT} returned no data")
        return data

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
