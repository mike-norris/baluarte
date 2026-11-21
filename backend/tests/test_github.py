"""GitHub connections, authentication, rate limits and discovery (roadmap M13.2).

GitHub is scripted with an ``httpx.MockTransport``; nothing here reaches github.com.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from test_connections import client_as
from test_connections import environment as environment  # the fixture

from baluarte.auth.roles import Role
from baluarte.connections import payload_for
from baluarte.db.models import (
    CollectionJob,
    CollectionRun,
    GitLabGroup,
    GitLabInstance,
    GitLabProject,
    Observation,
    ProviderMembership,
    ProviderTeam,
)
from baluarte.integrations.github.branches import BRANCH_SOURCE
from baluarte.integrations.github.client import (
    AppAuth,
    GitHubClient,
    RateLimited,
    TokenAuth,
    organization_of,
)
from baluarte.integrations.github.jobs import COLLECT, make_collect
from baluarte.jobs import queue
from baluarte.jobs.worker import Worker
from baluarte.policy.evaluation.runner import EVALUATE

TOKEN = "github_pat_example"
ORG = {"id": 900, "login": "acme", "name": "Acme", "two_factor_requirement_enabled": True}


def repo(repo_id: int, name: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": repo_id,
        "name": name,
        "full_name": f"acme/{name}",
        "owner": {"id": 900, "login": "acme"},
        "visibility": "private",
        "archived": False,
        "default_branch": "main",
        **extra,
    }


Handler = Callable[[httpx.Request], httpx.Response]


class FakeGitHub:
    """Answers by method and path; records every request."""

    def __init__(self, routes: dict[str, Handler | Any]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.method} {request.url.path}"
        role = request.url.params.get("role")
        if role and f"{key}?role={role}" in self.routes:
            key = f"{key}?role={role}"
        found = self.routes.get(key)
        if found is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return found(request) if callable(found) else httpx.Response(200, json=found)


def github(**overrides: Any) -> FakeGitHub:
    routes: dict[str, Any] = {
        "GET /orgs/acme": ORG,
        "GET /orgs/acme/repos": [repo(1, "api"), repo(2, "web", visibility="public")],
        "GET /orgs/acme/teams": [
            {"id": 31, "slug": "platform", "name": "Platform", "privacy": "closed"}
        ],
        "GET /orgs/acme/members?role=admin": [{"id": 501, "login": "ada"}],
        "GET /orgs/acme/members?role=member": [{"id": 502, "login": "grace"}],
        "GET /orgs/acme/members": [{"id": 501, "login": "ada"}],
        "GET /orgs/acme/teams/platform/members?role=maintainer": [{"id": 501, "login": "ada"}],
        "GET /orgs/acme/teams/platform/members?role=member": [],
        "GET /repos/acme/api/branches/main/protection": {
            "required_pull_request_reviews": {"required_approving_review_count": 1}
        },
        "GET /repos/acme/api/rules/branches/main": [],
        "GET /repos/acme/web/rules/branches/main": [],
        "GET /user": {"login": "ada"},
        "GET /user/memberships/orgs/acme": {"role": "admin"},
    }
    routes.update(overrides)
    return FakeGitHub(routes)


def add(engine: Engine, **body: Any) -> httpx.Response:
    http = client_as(engine, Role.ADMIN)
    return http.post(
        "/api/v1/connections/github",
        json={"organization": "acme", "secret": TOKEN, **body},
        headers={"X-CSRF-Token": "csrf"},
    )


def collect(engine: Engine, fake: FakeGitHub, connection_id: int) -> None:
    http = client_as(engine, Role.ADMIN)
    queued = http.post(
        f"/api/v1/connections/{connection_id}/collect", headers={"X-CSRF-Token": "csrf"}
    )
    assert queued.status_code == 202, queued.text
    assert Worker(engine, {COLLECT: make_collect(fake.transport)}).run_once()


def test_organization_urls_are_github_com_only() -> None:
    assert organization_of("https://github.com/acme") == "acme"
    for bad in (
        "http://github.com/acme",
        "https://github.example.test/acme",
        "https://github.com/acme/api",
        "https://github.com/",
    ):
        with pytest.raises(ValueError):
            organization_of(bad)


@pytest.mark.db
def test_a_github_organization_is_added_and_discovered(
    migrated_engine: Engine, environment: None
) -> None:
    added = add(migrated_engine, name="Acme on GitHub")
    assert added.status_code == 201, added.text
    connection = added.json()
    assert (connection["provider"], connection["url"], connection["auth_kind"]) == (
        "github",
        "https://github.com/acme",
        "token",
    )
    assert TOKEN not in added.text

    fake = github()
    collect(migrated_engine, fake, connection["id"])

    assert {r.method for r in fake.requests} == {"GET"}
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in fake.requests)
    assert all(r.headers["X-GitHub-Api-Version"] == "2022-11-28" for r in fake.requests)
    with Session(migrated_engine) as db:
        instance = db.get_one(GitLabInstance, connection["id"])
        snapshot = instance.capability_snapshot or {}
        assert snapshot["status"] == "ok"
        assert snapshot["token_user"] == {"username": "ada", "is_admin": True, "admin_access": None}
        assert snapshot["probes"]["branch_protection.read"]["status"] == "supported"
        org = db.scalars(select(GitLabGroup).where(GitLabGroup.instance_id == instance.id)).one()
        assert (org.full_path, org.require_two_factor_authentication) == ("acme", True)
        repos = db.scalars(
            select(GitLabProject)
            .where(GitLabProject.instance_id == instance.id)
            .order_by(GitLabProject.gitlab_id)
        ).all()
        assert [(r.path_with_namespace, r.visibility, r.namespace_kind) for r in repos] == [
            ("acme/api", "private", "group"),
            ("acme/web", "public", "group"),
        ]
        (team,) = db.scalars(select(ProviderTeam)).all()
        assert (team.slug, team.state) == ("platform", "active")
        members = {
            (m.subject_kind, m.login, m.role) for m in db.scalars(select(ProviderMembership))
        }
        assert members == {
            ("organization", "ada", "admin"),
            ("organization", "grace", "member"),
            ("team", "ada", "maintainer"),
        }
        runs = {
            r.kind: r.status
            for r in db.scalars(
                select(CollectionRun).where(CollectionRun.instance_id == instance.id)
            )
        }
        assert runs == {"capabilities": "succeeded", "groups": "succeeded", "projects": "succeeded"}
        sources = set(
            db.scalars(
                select(Observation.source).where(Observation.instance_id == instance.id)
            ).all()
        )
        assert {"GET /orgs/:org", "GET /orgs/:org/repos", "capabilities"} <= sources
        assert BRANCH_SOURCE in sources
        # The collection queues an evaluation of the connection (M13.3).
        queued = db.scalars(select(CollectionJob.payload).where(CollectionJob.kind == EVALUATE))
        assert any((p or {}).get("connection_id") == instance.id for p in queued)


@pytest.mark.db
def test_a_repository_no_longer_listed_is_reconciled_and_a_team_goes_missing(
    migrated_engine: Engine, environment: None
) -> None:
    connection_id = add(migrated_engine).json()["id"]
    collect(migrated_engine, github(), connection_id)
    later = github(
        **{
            "GET /orgs/acme/repos": [repo(1, "api")],
            "GET /orgs/acme/teams": [],
            "GET /repositories/2": lambda _: httpx.Response(404, json={"message": "Not Found"}),
        }
    )
    with Session(migrated_engine) as db:
        queue.enqueue(db, COLLECT, "second-read", payload_for(connection_id))
        db.commit()
    assert Worker(migrated_engine, {COLLECT: make_collect(later.transport)}).run_once()
    with Session(migrated_engine) as db:
        states = dict(db.execute(select(GitLabProject.gitlab_id, GitLabProject.state)).all())
        assert states == {1: "active", 2: "missing"}
        assert db.scalars(select(ProviderTeam.state)).one() == "missing"


@pytest.mark.db
def test_github_connections_are_refused_to_gitlab_jobs_and_validated(
    migrated_engine: Engine, environment: None
) -> None:
    from baluarte import connections

    connection_id = add(migrated_engine).json()["id"]
    with Session(migrated_engine) as db, pytest.raises(connections.ConnectionRefused):
        connections.settings_for(db, connection_id, None)
    assert add(migrated_engine).status_code == 409
    assert add(migrated_engine, organization="acme/api").status_code == 422
    app_without_ids = add(migrated_engine, organization="other", auth_kind="app")
    assert app_without_ids.status_code == 422


def _key() -> tuple[str, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


def test_an_app_installation_token_is_minted_with_a_signed_jwt_and_reused() -> None:
    pem, public = _key()
    now = datetime.now(UTC)
    minted: list[str] = []

    def mint(request: httpx.Request) -> httpx.Response:
        claims = jwt.decode(
            request.headers["Authorization"].removeprefix("Bearer "), public, algorithms=["RS256"]
        )
        assert claims["iss"] == "77"
        assert claims["exp"] - claims["iat"] <= 600
        minted.append(request.url.path)
        expires = (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        return httpx.Response(201, json={"token": "ghs_installation", "expires_at": expires})

    fake = github(**{"POST /app/installations/88/access_tokens": mint})
    auth = AppAuth(77, 88, pem, clock=lambda: now)
    with GitHubClient(auth, transport=fake.transport) as client:
        client.get_json("/orgs/acme")
        client.get_json("/orgs/acme/teams")
    assert minted == ["/app/installations/88/access_tokens"]
    gets = [r for r in fake.requests if r.method == "GET"]
    assert all(r.headers["Authorization"] == "Bearer ghs_installation" for r in gets)


def test_rate_limits_wait_for_reset_or_fail_when_too_long() -> None:
    calls = {"n": 0}
    slept: list[float] = []

    def limited(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                403,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1030"},
                json={"message": "API rate limit exceeded"},
            )
        return httpx.Response(200, json=ORG, headers={"x-ratelimit-remaining": "4999"})

    fake = github(**{"GET /orgs/acme": limited})
    with GitHubClient(
        TokenAuth(TOKEN), transport=fake.transport, sleep=slept.append, wall_clock=lambda: 1000
    ) as client:
        assert client.get_json("/orgs/acme")["login"] == "acme"
        assert client.rate.remaining == 4999
    assert slept == [31.0]

    secondary = github(
        **{
            "GET /orgs/acme": lambda _: httpx.Response(
                429, headers={"retry-after": "3600"}, json={"message": "secondary rate limit"}
            )
        }
    )
    with (
        GitHubClient(TokenAuth(TOKEN), transport=secondary.transport, sleep=slept.append) as client,
        pytest.raises(RateLimited),
    ):
        client.get_json("/orgs/acme")


def test_pagination_follows_link_headers_on_the_api_only() -> None:
    def first(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[repo(1, "api")],
            headers={"Link": '<https://api.github.com/orgs/acme/repos?page=2>; rel="next"'},
        )

    pages = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        pages["n"] += 1
        return (
            first(request)
            if request.url.params.get("page") != "2"
            else httpx.Response(200, json=[repo(2, "web")])
        )

    fake = github(**{"GET /orgs/acme/repos": handler})
    with GitHubClient(TokenAuth(TOKEN), transport=fake.transport) as client:
        listed = [r["id"] for page in client.paginate("/orgs/acme/repos") for r in page]
    assert listed == [1, 2]
    assert fake.requests[0].url.params["per_page"] == "100"

    evil = github(
        **{
            "GET /orgs/acme/repos": lambda _: httpx.Response(
                200, json=[], headers={"Link": '<https://evil.example.test/x>; rel="next"'}
            )
        }
    )
    with (
        GitHubClient(TokenAuth(TOKEN), transport=evil.transport) as client,
        pytest.raises(Exception, match="pointed away"),
    ):
        list(client.paginate("/orgs/acme/repos"))
