"""A scripted GitLab API for tests, served through httpx.MockTransport."""

from collections.abc import Callable
from typing import Any

import httpx

from baluarte.integrations.gitlab.settings import GitLabSettings

TOKEN = "fake-gitlab-token-value"
URL = "https://gitlab.example.test"

Reply = httpx.Response | Callable[[httpx.Request], httpx.Response] | Exception


def healthy_routes() -> dict[str, list[Reply]]:
    return {
        "/api/v4/version": [httpx.Response(200, json={"version": "17.5.1-ee", "revision": "abc"})],
        "/api/v4/personal_access_tokens/self": [
            httpx.Response(
                200,
                json={
                    "id": 9,
                    "name": "baluarte",
                    "scopes": ["read_api"],
                    "active": True,
                    "revoked": False,
                    "expires_at": "2027-06-30",
                },
            )
        ],
        "/api/v4/user": [
            httpx.Response(
                200, json={"id": 5, "username": "baluarte-bot", "state": "active", "bot": True}
            )
        ],
        "/api/v4/groups": [httpx.Response(200, json=[{"id": 1, "full_path": "platform"}])],
    }


class FakeGitLab:
    """Replies per path in order; the last reply for a path repeats. Unknown paths are 404."""

    def __init__(self, routes: dict[str, list[Reply]] | None = None) -> None:
        self.routes = healthy_routes() if routes is None else routes
        self.requests: list[httpx.Request] = []

    def set(self, path: str, *replies: Reply) -> None:
        self.routes["/api/v4" + path] = list(replies)

    def json(self, path: str, status: int = 200, body: Any = None) -> None:
        self.set(path, httpx.Response(status, json=body))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        replies = self.routes.get(request.url.path)
        if not replies:
            return httpx.Response(404, json={"message": "404 Not Found"})
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(request)
        return reply

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def paths(self) -> list[str]:
        return [request.url.path.removeprefix("/api/v4") for request in self.requests]


def settings(url: str = URL) -> GitLabSettings:
    return GitLabSettings(url=url, token=TOKEN, timeout_seconds=5.0)
