from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from gitlab_fake import TOKEN, URL, FakeGitLab, paged, project, settings
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from baluarte.db.models import CollectionJob, GitLabProject
from baluarte.integrations.gitlab.client import GitLabClient, InvalidResponse, ServerError
from baluarte.integrations.gitlab.discovery import Discovery
from baluarte.integrations.gitlab.jobs import DISCOVER_PROJECTS, make_discover_projects
from baluarte.integrations.gitlab.projects import discover_projects
from baluarte.jobs import queue
from baluarte.jobs.worker import Worker

pytestmark = pytest.mark.db

HOUR = timedelta(hours=1)

PAGES = [
    [
        project(10, "platform/api", 1, visibility="internal"),
        project(11, "platform/backend/billing", 2, archived=True),
    ],
    [
        project(12, "alice/scratch", 900, kind="user", visibility="public"),
        project(13, "platform/empty", 1, default_branch=None),
    ],
]


def fake_with(pages: list[list[dict[str, Any]]]) -> FakeGitLab:
    fake = FakeGitLab()
    fake.set("/projects", paged("/projects", pages))
    return fake


def discover(engine: Engine, fake: FakeGitLab) -> Discovery:
    client = GitLabClient(settings(), transport=fake.transport, sleep=lambda _: None)
    return discover_projects(client, engine, URL)


def projects(engine: Engine) -> dict[int, GitLabProject]:
    with Session(engine) as db:
        return {row.gitlab_id: row for row in db.scalars(select(GitLabProject))}


def age(engine: Engine) -> None:
    with Session(engine) as db:
        db.execute(update(GitLabProject).values(last_seen_at=GitLabProject.last_seen_at - HOUR))
        db.commit()


def test_discovers_projects_with_keyset_pagination(migrated_engine: Engine) -> None:
    fake = fake_with(PAGES)

    result = discover(migrated_engine, fake)

    stored = projects(migrated_engine)
    assert result.seen == 4
    assert sorted(stored) == [10, 11, 12, 13]
    first = fake.requests[0].url.params
    assert (first["pagination"], first["order_by"], first["sort"]) == ("keyset", "id", "asc")
    assert "archived" not in first
    assert (stored[10].namespace_gitlab_id, stored[10].visibility) == (1, "internal")
    assert stored[11].archived is True
    assert (stored[12].namespace_gitlab_id, stored[12].visibility) == (900, "public")
    assert {gitlab_id: row.namespace_kind for gitlab_id, row in stored.items()} == {
        10: "group",
        11: "group",
        12: "user",
        13: "group",
    }
    assert stored[13].default_branch is None
    assert stored[10].default_branch == "main"
    assert {row.state for row in stored.values()} == {"active"}


def test_rediscovery_follows_moves_and_changes(migrated_engine: Engine) -> None:
    discover(migrated_engine, fake_with(PAGES))
    before = projects(migrated_engine)
    age(migrated_engine)
    changed = [
        [
            project(10, "security/api", 4, visibility="private", default_branch="trunk"),
            project(11, "platform/backend/billing", 2, archived=False),
        ],
        [
            # Transferred from a user to a group, and a kind GitLab may add later.
            project(12, "platform/scratch", 1, visibility="public"),
            project(13, "platform/empty", 1, kind="organization", default_branch=None),
        ],
    ]

    discover(migrated_engine, fake_with(changed))

    after = projects(migrated_engine)
    assert len(after) == 4
    assert (after[10].path_with_namespace, after[10].namespace_gitlab_id) == ("security/api", 4)
    assert (after[10].visibility, after[10].default_branch) == ("private", "trunk")
    assert after[11].archived is False
    assert after[10].first_seen_at == before[10].first_seen_at
    assert (after[12].path_with_namespace, after[12].namespace_kind) == (
        "platform/scratch",
        "group",
    )
    # Unrecognised kinds are stored as unknown, never guessed.
    assert after[13].namespace_kind is None


@pytest.mark.parametrize(
    ("lookup", "state", "bucket"),
    [
        (httpx.Response(404), "missing", "missing"),
        (httpx.Response(403), "inaccessible", "inaccessible"),
        (httpx.Response(200, json=project(13, "platform/empty", 1)), "active", "active"),
        (httpx.Response(500), "active", "unchanged"),
    ],
)
def test_unlisted_projects_are_looked_up_before_reconciling(
    migrated_engine: Engine, lookup: httpx.Response, state: str, bucket: str
) -> None:
    discover(migrated_engine, fake_with(PAGES))
    age(migrated_engine)
    fake = fake_with([PAGES[0], [PAGES[1][0]]])
    fake.set("/projects/13", lookup)

    result = discover(migrated_engine, fake)

    stored = projects(migrated_engine)
    assert stored[13].state == state
    assert result.reconciled[bucket] == 1
    assert {stored[n].state for n in (10, 11, 12)} == {"active"}
    lookups = [r for r in fake.requests if r.url.path == "/api/v4/projects/13"]
    assert len(lookups) == 1
    assert lookups[0].url.params["statistics"] == "false"


def test_incomplete_listing_marks_nothing(migrated_engine: Engine) -> None:
    discover(migrated_engine, fake_with(PAGES))
    age(migrated_engine)
    fake = FakeGitLab()
    fake.set("/projects", httpx.Response(502))

    with pytest.raises(ServerError):
        discover(migrated_engine, fake)

    assert {row.state for row in projects(migrated_engine).values()} == {"active"}
    assert set(fake.paths()) == {"/projects"}


@pytest.mark.parametrize(
    "broken",
    [
        {"id": 1, "path_with_namespace": "a/b"},
        {"id": 1, "path_with_namespace": "a/b", "namespace": {"kind": "group"}},
        {"path_with_namespace": "a/b", "namespace": {"id": 1}},
    ],
)
def test_project_without_required_fields_is_rejected(
    migrated_engine: Engine, broken: dict[str, Any]
) -> None:
    with pytest.raises(InvalidResponse, match="namespace"):
        discover(migrated_engine, fake_with([[broken]]))


@pytest.fixture
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)


def run_job(engine: Engine, fake: FakeGitLab) -> CollectionJob:
    with Session(engine) as db:
        job_id = queue.enqueue(db, DISCOVER_PROJECTS, "test")
        db.commit()
    assert Worker(engine, {DISCOVER_PROJECTS: make_discover_projects(fake.transport)}).run_once()
    with Session(engine) as db:
        return db.get_one(CollectionJob, job_id)


def test_job_succeeds(migrated_engine: Engine, gitlab_env: None) -> None:
    job = run_job(migrated_engine, fake_with(PAGES))

    assert job.state == "succeeded"
    assert len(projects(migrated_engine)) == 4


def test_job_failure_is_retried_with_a_safe_message(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    fake = FakeGitLab()
    fake.set("/projects", httpx.Response(403))

    job = run_job(migrated_engine, fake)

    assert job.state == "queued"
    assert job.error == "project discovery failed: the token may not read /projects (403)"
