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
        "/api/v4/version": [
            httpx.Response(
                200, json={"version": "17.5.1-ee", "revision": "abc", "enterprise": True}
            )
        ],
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
        "/api/v4/groups": [
            httpx.Response(200, json=[{"id": 1, "full_path": "platform", "visibility": "private"}])
        ],
        "/api/v4/projects": [
            httpx.Response(
                200,
                json=[
                    {
                        "id": 42,
                        "path_with_namespace": "platform/api",
                        "visibility": "internal",
                        "default_branch": "main",
                    }
                ],
            )
        ],
        "/api/v4/projects/42/protected_branches": [
            httpx.Response(200, json=[{"id": 1, "name": "main"}])
        ],
        "/api/v4/projects/42/approvals": [httpx.Response(200, json={"approvals_before_merge": 0})],
        "/api/v4/license": [httpx.Response(403, json={"message": "403 Forbidden"})],
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


def paged(path: str, pages: list[list[dict[str, Any]]], base: str = URL) -> Reply:
    """Serve ``pages`` with ``page`` query parameters and GitLab-style ``Link`` headers."""

    def reply(request: httpx.Request) -> httpx.Response:
        number = int(request.url.params.get("page", "1"))
        headers = {}
        if number < len(pages):
            following = f"{base}/api/v4{path}?page={number + 1}&per_page=100"
            headers["Link"] = (
                f'<{following}>; rel="next", <{base}/api/v4{path}?page=1>; rel="first"'
            )
        return httpx.Response(200, json=pages[number - 1], headers=headers)

    return reply


def group(
    gitlab_id: int, full_path: str, parent: int | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        "id": gitlab_id,
        "full_path": full_path,
        "name": full_path.rsplit("/", 1)[-1],
        "parent_id": parent,
        "visibility": "private",
    } | extra


def project(
    gitlab_id: int, path: str, namespace_id: int, kind: str = "group", **extra: Any
) -> dict[str, Any]:
    return {
        "id": gitlab_id,
        "path_with_namespace": path,
        "name": path.rsplit("/", 1)[-1],
        "namespace": {"id": namespace_id, "kind": kind, "full_path": path.rsplit("/", 1)[0]},
        "visibility": "private",
        "archived": False,
        "default_branch": "main",
    } | extra
