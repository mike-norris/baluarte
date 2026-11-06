"""The only code in Baluarte that changes GitLab: creating and editing labels (ADR-0009).

``LabelWriter`` holds the separate label write token and is built only by the label plan applier
(``baluarte.labels.plans``); collection never sees that token, and the read-only
``GitLabClient`` has no write method. It can do exactly two things:

- ``POST /groups/:id/labels`` with ``name``, ``color`` and ``description``;
- ``PUT /groups/:id/labels/:label_id`` or ``PUT /projects/:id/labels/:label_id`` with ``new_name``,
  ``color`` and ``description`` (GitLab's project endpoint needs ``new_name`` or ``color``, so
  the colour is always sent).

There is no delete. A write is sent once: it is never retried, whatever the answer, because a
repeated POST could create a second label. Redirects are not followed, so the token never leaves
the configured instance. Responses are returned as their status and parsed body; the token,
request bodies and response bodies are never logged.
"""

import logging
from dataclasses import dataclass
from typing import Any

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

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


class LabelWriter:
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

    def __enter__(self) -> "LabelWriter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _send(self, method: str, path: str, data: dict[str, Any]) -> WriteResult:
        try:
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
        return WriteResult(method, path, response.status_code, body)

    def create_group_label(
        self, group_id: int, name: str, color: str, description: str | None
    ) -> WriteResult:
        data = {"name": name, "color": color}
        if description is not None:
            data["description"] = description
        return self._send("POST", f"/groups/{group_id}/labels", data)

    def update_label(
        self,
        owner_type: str,
        owner_id: int,
        label_id: int,
        *,
        color: str,
        new_name: str | None = None,
        description: str | None = None,
    ) -> WriteResult:
        if owner_type not in ("group", "project"):
            raise ValueError("labels belong to a group or a project")
        data = {"color": color}
        if new_name is not None:
            data["new_name"] = new_name
        if description is not None:
            data["description"] = description
        return self._send("PUT", f"/{owner_type}s/{owner_id}/labels/{label_id}", data)

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
