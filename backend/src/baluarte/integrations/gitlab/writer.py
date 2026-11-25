"""The base of Baluarte's GitLab writers: a separate write token and a send-once request.

Only two classes build on it, each with its own token and a fixed list of requests:
``LabelWriter`` (labels and issues, ADR-0009 and ADR-0011) and ``RemediationWriter`` (approved
remediation plans, ADR-0012). Collection never sees either token, and the read-only
``GitLabClient`` has no write method.

A write is sent once: it is never retried, whatever the answer, because a repeated request could
change GitLab twice. Redirects are not followed, so the token never leaves the configured
instance. Responses are returned as their status and parsed body; the token, request bodies and
response bodies are never logged.
"""

import logging
from dataclasses import dataclass
from typing import Any, Self

import httpx

from baluarte import __version__
from baluarte.integrations.gitlab.settings import GitLabSettings
from baluarte.logs import register_secret

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WriteResult:
    method: str
    # The API path without host or query, safe to store and audit.
    path: str
    # The HTTP status, or None when GitLab could not be reached.
    status: int | None
    body: Any = None
    error: str | None = None
    # Seconds GitLab asked to wait before retrying (Retry-After on a 429), when it said.
    retry_after: float | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


class GitLabWriter:
    def __init__(
        self,
        settings: GitLabSettings,
        token: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        register_secret(token)
        self._http = httpx.Client(
            base_url=settings.api_url,
            headers={
                "PRIVATE-TOKEN": token,
                "Accept": "application/json",
                "User-Agent": f"baluarte/{__version__}",
            },
            timeout=httpx.Timeout(settings.timeout_seconds),
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _send(
        self, method: str, path: str, data: dict[str, Any], *, as_json: bool = False
    ) -> WriteResult:
        """One request; ``as_json`` sends ``data`` as a JSON body (for arrays, including an
        empty one, which a form cannot express)."""
        try:
            if as_json:
                response = self._http.request(method, path, json=data)
            else:
                response = self._http.request(method, path, data=data)
        except httpx.TransportError as exc:
            logger.warning("gitlab: %s %s failed (%s)", method, path, type(exc).__name__)
            return WriteResult(
                method, path, None, error=f"could not reach GitLab ({type(exc).__name__})"
            )
        try:
            body = response.json()
        except ValueError:
            body = None
        logger.info("gitlab: %s %s -> %s", method, path, response.status_code)
        retry_after = None
        try:
            retry_after = float(response.headers.get("retry-after", ""))
        except ValueError:
            pass
        return WriteResult(method, path, response.status_code, body, retry_after=retry_after)

    def token_self(self) -> WriteResult:
        """``GET /personal_access_tokens/self`` with the write token: its scopes and expiry."""
        path = "/personal_access_tokens/self"
        try:
            response = self._http.get(path)
        except httpx.TransportError as exc:
            return WriteResult(
                "GET", path, None, error=f"could not reach GitLab ({type(exc).__name__})"
            )
        try:
            body = response.json()
        except ValueError:
            body = None
        return WriteResult("GET", path, response.status_code, body)
