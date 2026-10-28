from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from gitlab_fake import TOKEN, URL, FakeGitLab, group, paged, settings
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from baluarte.db.models import CollectionJob, GitLabGroup
from baluarte.integrations.gitlab.client import GitLabClient, InvalidResponse, ServerError
from baluarte.integrations.gitlab.groups import GroupDiscovery, discover_groups
from baluarte.integrations.gitlab.jobs import DISCOVER_GROUPS, make_discover_groups
from baluarte.jobs import queue
from baluarte.jobs.worker import Worker

pytestmark = pytest.mark.db

HOUR = timedelta(hours=1)

TREE = [
    [group(1, "platform", visibility="internal"), group(2, "platform/backend", 1)],
    [group(3, "platform/backend/services", 2), group(4, "security", visibility="public")],
]


def fake_with(pages: list[list[dict[str, object]]]) -> FakeGitLab:
    fake = FakeGitLab()
    fake.set("/groups", paged("/groups", pages))
    return fake


def discover(engine: Engine, fake: FakeGitLab) -> GroupDiscovery:
    client = GitLabClient(settings(), transport=fake.transport, sleep=lambda _: None)
    return discover_groups(client, engine, URL)


def groups(engine: Engine) -> dict[int, GitLabGroup]:
    with Session(engine) as db:
        return {row.gitlab_id: row for row in db.scalars(select(GitLabGroup))}


def age(engine: Engine) -> None:
    """Make every row look as if it was last listed by an earlier run."""
    with Session(engine) as db:
        db.execute(update(GitLabGroup).values(last_seen_at=GitLabGroup.last_seen_at - HOUR))
        db.commit()


def test_discovers_nested_groups_across_pages(migrated_engine: Engine) -> None:
    fake = fake_with(TREE)

    result = discover(migrated_engine, fake)

    stored = groups(migrated_engine)
    assert result.seen == 4
    assert sorted(stored) == [1, 2, 3, 4]
    assert stored[3].full_path == "platform/backend/services"
    assert (stored[1].parent_gitlab_id, stored[2].parent_gitlab_id, stored[3].parent_gitlab_id) == (
        None,
        1,
        2,
    )
    assert (stored[1].visibility, stored[4].visibility) == ("internal", "public")
    assert {row.state for row in stored.values()} == {"active"}
    assert {row.instance_id for row in stored.values()} == {result.instance_id}
    first = fake.requests[0].url.params
    assert (first["all_available"], first["order_by"], first["sort"]) == ("true", "id", "asc")


def test_rediscovery_updates_in_place(migrated_engine: Engine) -> None:
    discover(migrated_engine, fake_with(TREE))
    before = groups(migrated_engine)
    age(migrated_engine)
    renamed = [
        [group(1, "platform", visibility="private"), group(2, "platform/api", 1)],
        TREE[1],
    ]

    discover(migrated_engine, fake_with(renamed))

    after = groups(migrated_engine)
    assert len(after) == 4
    assert after[2].full_path == "platform/api"
    assert after[1].visibility == "private"
    assert after[2].first_seen_at == before[2].first_seen_at
    assert after[2].last_seen_at > before[2].last_seen_at - HOUR


def test_unknown_visibility_is_stored_as_null(migrated_engine: Engine) -> None:
    discover(migrated_engine, fake_with([[group(1, "odd", visibility="secret")]]))

    assert groups(migrated_engine)[1].visibility is None


@pytest.mark.parametrize(
    ("lookup", "state", "bucket"),
    [
        (httpx.Response(404), "missing", "missing"),
        (httpx.Response(403), "inaccessible", "inaccessible"),
        (httpx.Response(200, json=group(4, "security")), "active", "active"),
        (httpx.Response(500), "active", "unchanged"),
    ],
)
def test_unlisted_groups_are_looked_up_before_reconciling(
    migrated_engine: Engine, lookup: httpx.Response, state: str, bucket: str
) -> None:
    discover(migrated_engine, fake_with(TREE))
    age(migrated_engine)
    fake = fake_with([TREE[0], [TREE[1][0]]])
    fake.set("/groups/4", lookup)

    result = discover(migrated_engine, fake)

    stored = groups(migrated_engine)
    assert stored[4].state == state
    assert result.reconciled[bucket] == 1
    assert {stored[n].state for n in (1, 2, 3)} == {"active"}
    assert fake.paths().count("/groups/4") == 1


def test_missing_group_listed_again_is_active(migrated_engine: Engine) -> None:
    discover(migrated_engine, fake_with(TREE))
    age(migrated_engine)
    fake = fake_with([TREE[0]])
    fake.set("/groups/3", httpx.Response(404))
    fake.set("/groups/4", httpx.Response(404))
    discover(migrated_engine, fake)
    assert groups(migrated_engine)[4].state == "missing"

    discover(migrated_engine, fake_with(TREE))

    assert groups(migrated_engine)[4].state == "active"


def test_incomplete_listing_marks_nothing_missing(migrated_engine: Engine) -> None:
    discover(migrated_engine, fake_with(TREE))
    age(migrated_engine)
    pages = paged("/groups", TREE)

    def second_page_fails(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(500)
        assert callable(pages)
        return pages(request)

    fake = FakeGitLab()
    fake.set("/groups", second_page_fails)

    with pytest.raises(ServerError):
        discover(migrated_engine, fake)

    assert {row.state for row in groups(migrated_engine).values()} == {"active"}
    assert not any(path.startswith("/groups/") for path in fake.paths())


def test_group_without_id_is_rejected(migrated_engine: Engine) -> None:
    with pytest.raises(InvalidResponse, match="without an id"):
        discover(migrated_engine, fake_with([[{"full_path": "x"}]]))


@pytest.fixture
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)


def run_job(engine: Engine, fake: FakeGitLab) -> CollectionJob:
    with Session(engine) as db:
        job_id = queue.enqueue(db, DISCOVER_GROUPS, "test")
        db.commit()
    assert Worker(engine, {DISCOVER_GROUPS: make_discover_groups(fake.transport)}).run_once()
    with Session(engine) as db:
        return db.get_one(CollectionJob, job_id)


def test_job_succeeds(migrated_engine: Engine, gitlab_env: None) -> None:
    job = run_job(migrated_engine, fake_with(TREE))

    assert job.state == "succeeded"
    assert len(groups(migrated_engine)) == 4


def test_job_failure_is_retried_with_a_safe_message(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    fake = FakeGitLab()
    fake.set("/groups", httpx.Response(401))

    job = run_job(migrated_engine, fake)

    assert job.state == "queued"
    assert job.error == "group discovery failed: GitLab rejected the token (401)"
