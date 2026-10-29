from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from gitlab_fake import TOKEN, URL, FakeGitLab, paged, project, settings
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from baluarte.db.models import CollectionJob, GitLabProject, Observation
from baluarte.integrations.gitlab import branches
from baluarte.integrations.gitlab.branches import PROTECTION_SOURCE
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


# Default-branch protection, read after the listing.

PROTECTED_RULE = {
    "id": 1,
    "name": "main",
    "push_access_levels": [
        {"access_level": 40, "access_level_description": "Maintainers"},
        {"access_level": None, "user_id": 77, "access_level_description": "Jane Doe"},
    ],
    "merge_access_levels": [{"access_level": 30, "group_id": 5}],
    "unprotect_access_levels": [{"access_level": 40}],
    "allow_force_push": False,
    "code_owner_approval_required": True,
}


def protections(engine: Engine) -> dict[int, Observation]:
    """The latest protection observation of each project, by GitLab ID."""
    ids = {row.id: gitlab_id for gitlab_id, row in projects(engine).items()}
    found: dict[int, Observation] = {}
    with Session(engine) as db:
        for row in db.scalars(
            select(Observation)
            .where(Observation.source == PROTECTION_SOURCE)
            .order_by(Observation.id)
        ):
            assert row.project_id is not None
            found[ids[row.project_id]] = row
    return found


def protection_fake(replies: dict[int, Any]) -> FakeGitLab:
    """The standard listing, with protected-branch replies by GitLab project ID."""
    fake = fake_with(PAGES)
    for gitlab_id, reply in replies.items():
        path = f"/projects/{gitlab_id}/protected_branches"
        if isinstance(reply, list):
            fake.json(path, body=reply)
        else:
            fake.set(path, reply)
    return fake


def test_default_branch_protection_is_read_for_each_project(migrated_engine: Engine) -> None:
    fake = protection_fake(
        {
            10: [PROTECTED_RULE, {"id": 2, "name": "release/*"}],
            11: [{"id": 3, "name": "*"}],
            12: [{"id": 4, "name": "develop"}],
        }
    )

    result = discover(migrated_engine, fake)

    found = protections(migrated_engine)
    # Project 13 is an empty repository: there is no branch to read.
    assert sorted(found) == [10, 11, 12]
    assert "/projects/13/protected_branches" not in fake.paths()
    assert found[10].payload == {
        "branch": "main",
        "protected": True,
        "rules": [
            {
                "name": "main",
                "push_access_levels": [
                    {"kind": "role", "access_level": 40},
                    {"kind": "user", "access_level": None},
                ],
                "merge_access_levels": [{"kind": "group", "access_level": 30}],
                "unprotect_access_levels": [{"kind": "role", "access_level": 40}],
                "allow_force_push": False,
                "code_owner_approval_required": True,
            }
        ],
    }
    # Names and IDs of the people allowed are not evidence.
    assert "Jane" not in str(found[10].payload) and "77" not in str(found[10].payload)
    # A wildcard rule covers the branch; a rule for another branch does not.
    assert found[11].payload is not None and found[11].payload["protected"] is True
    assert [rule["name"] for rule in found[11].payload["rules"]] == ["*"]
    assert found[12].payload == {"branch": "main", "protected": False, "rules": []}
    assert {row.collection_status for row in found.values()} == {"ok"}
    assert found[10].source_ref == "/projects/10/protected_branches"
    assert result.protection == {"protected": 2, "unprotected": 1, "unread": 0}


@pytest.mark.parametrize(
    ("reply", "status", "error"),
    [
        (httpx.Response(403), "forbidden", "the token may not read"),
        (httpx.Response(404), "not_found", "was not found (404)"),
        (httpx.Response(500), "failed", "GitLab returned 500"),
        (httpx.Response(200, json={"not": "a list"}), "failed", "did not return a list"),
    ],
)
def test_protection_that_cannot_be_read_is_recorded_without_failing_discovery(
    migrated_engine: Engine, reply: httpx.Response, status: str, error: str
) -> None:
    fake = protection_fake({10: reply, 11: [{"id": 1, "name": "main"}], 12: []})

    result = discover(migrated_engine, fake)

    found = protections(migrated_engine)
    assert (found[10].collection_status, found[10].payload) == (status, None)
    assert found[10].error is not None and error in found[10].error
    assert TOKEN not in found[10].error
    # Other projects are still read, and the inventory is unaffected.
    assert found[11].collection_status == "ok"
    assert projects(migrated_engine)[10].last_error is None
    assert result.protection == {"protected": 1, "unprotected": 1, "unread": 1}


def test_a_rejected_token_stops_further_reads(migrated_engine: Engine) -> None:
    fake = protection_fake({10: httpx.Response(401)})

    result = discover(migrated_engine, fake)

    found = protections(migrated_engine)
    assert found[10].error == "GitLab rejected the token (401)"
    assert {found[n].collection_status for n in (11, 12)} == {"failed"}
    assert found[11].error == "not read: GitLab rejected the token earlier in this collection"
    assert "/projects/11/protected_branches" not in fake.paths()
    assert "/projects/12/protected_branches" not in fake.paths()
    assert result.protection == {"protected": 0, "unprotected": 0, "unread": 3}


def test_repeated_failures_stop_further_reads(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(branches, "MAX_CONSECUTIVE_FAILURES", 2)
    fake = protection_fake({10: httpx.Response(500), 11: httpx.Response(500)})

    discover(migrated_engine, fake)

    found = protections(migrated_engine)
    assert found[12].error is not None and "2 protected-branch reads in a row" in found[12].error
    assert "/projects/12/protected_branches" not in fake.paths()


def test_unchanged_protection_is_not_stored_again(migrated_engine: Engine) -> None:
    def count() -> int:
        with Session(migrated_engine) as db:
            rows = db.scalars(select(Observation).where(Observation.source == PROTECTION_SOURCE))
            return len(rows.all())

    discover(migrated_engine, protection_fake({10: [PROTECTED_RULE]}))
    discover(migrated_engine, protection_fake({10: [PROTECTED_RULE]}))
    assert count() == 3

    discover(migrated_engine, protection_fake({10: []}))

    assert count() == 4
    assert protections(migrated_engine)[10].payload == {
        "branch": "main",
        "protected": False,
        "rules": [],
    }


def test_inactive_projects_are_not_read(migrated_engine: Engine) -> None:
    discover(migrated_engine, fake_with(PAGES))
    age(migrated_engine)
    fake = fake_with([PAGES[0], [PAGES[1][1]]])
    fake.set("/projects/12", httpx.Response(404))

    discover(migrated_engine, fake)

    assert projects(migrated_engine)[12].state == "missing"
    assert "/projects/12/protected_branches" not in fake.paths()


def test_protected_branches_are_read_page_by_page(migrated_engine: Engine) -> None:
    fake = fake_with(PAGES)
    fake.set(
        "/projects/10/protected_branches",
        paged(
            "/projects/10/protected_branches",
            [[{"id": 1, "name": "develop"}], [{"id": 2, "name": "main"}]],
        ),
    )

    discover(migrated_engine, fake)

    payload = protections(migrated_engine)[10].payload
    assert payload is not None and payload["protected"] is True
    assert fake.paths().count("/projects/10/protected_branches") == 2


@pytest.mark.parametrize(
    ("rule", "branch", "covered"),
    [
        ("main", "main", True),
        ("main", "main2", False),
        ("*", "release/1.0", True),
        ("release/*", "release/1.0", True),
        ("release/*", "release", False),
        ("*-stable", "16-5-stable", True),
        ("feature.x", "featureXx", False),
        ("feature.x", "feature.x", True),
        ("v[1]*", "v[1]-rc", True),
        ("v[1]*", "v1", False),
    ],
)
def test_rule_names_match_branches_like_gitlab(rule: str, branch: str, covered: bool) -> None:
    assert branches.matches(rule, branch) is covered
