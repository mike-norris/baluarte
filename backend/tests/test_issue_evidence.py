"""Evidence captured for auditable issues (roadmap M12.2)."""

import hashlib
from typing import Any

import httpx
import pytest
from gitlab_fake import FakeGitLab, settings
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_issue_register import add_rule, client_as, collect, issue, serve
from test_issue_register import world as world  # the fixture

from baluarte.auth.roles import Role
from baluarte.db.models import CapturedFile, IssueEvidence, Observation
from baluarte.evidence.observations import verify
from baluarte.integrations.gitlab import issue_evidence
from baluarte.integrations.gitlab.client import GitLabClient

pytestmark = pytest.mark.db

SECRET = "0123456789abcdef0123456789abcdef"
PNG = b"\x89PNG\r\n\x1a\n" + b"evidence" * 10
BASE = "/projects/42/issues"


def full_issue(iid: int, **extra: Any) -> dict[str, Any]:
    return (
        issue(
            iid,
            ["security"],
            description=f"Access review.\n\n![screenshot](/uploads/{SECRET}/review%20one.png)",
            closed_by={"username": "grace"},
        )
        | extra
    )


def evidence_routes(fake: FakeGitLab, iid: int, **overrides: Any) -> None:
    base = f"{BASE}/{iid}"
    routes: dict[str, Any] = {
        base: full_issue(iid),
        f"{base}/notes": [
            {
                "id": 11,
                "author": {"username": "ada"},
                "body": "Reviewed and approved the access list.",
                "system": False,
                "internal": False,
                "created_at": "2026-11-02T09:00:00Z",
            },
            {
                "id": 12,
                "author": {"username": "grace"},
                "body": "changed the description",
                "system": True,
                "created_at": "2026-11-02T10:00:00Z",
            },
        ],
        f"{base}/resource_state_events": [
            {
                "id": 1,
                "user": {"username": "grace"},
                "state": "closed",
                "created_at": "2026-11-03T00:00:00Z",
            }
        ],
        f"{base}/resource_label_events": [
            {
                "id": 2,
                "user": {"username": "ada"},
                "action": "add",
                "label": {"name": "security"},
                "created_at": "2026-11-01T00:00:00Z",
            }
        ],
        f"{base}/resource_milestone_events": [],
        f"{base}/links": [
            {
                "project_id": 42,
                "iid": 7,
                "title": "Parent",
                "state": "opened",
                "link_type": "relates_to",
            }
        ],
        f"{base}/closed_by": [
            {
                "project_id": 42,
                "iid": 5,
                "title": "Fix access list",
                "state": "merged",
                "author": {"username": "ada"},
                "merged_by": {"username": "grace"},
                "merged_at": "2026-11-02T12:00:00Z",
                "merge_commit_sha": "abc123",
            }
        ],
        f"{base}/related_merge_requests": [
            {"project_id": 42, "iid": 5, "title": "Fix access list"}
        ],
        "/projects/42/merge_requests/5/approvals": {
            "approved": True,
            "approved_by": [{"user": {"username": "linus"}}],
            "approvals_required": 1,
        },
        "/projects/42/merge_requests/5/pipelines": [
            {"id": 99, "status": "success", "ref": "fix", "sha": "abc123"}
        ],
        "/projects/42/releases": [
            {"tag_name": "v1.2.0", "name": "1.2.0", "milestones": [{"title": "Q4"}]},
            {"tag_name": "v1.1.0", "name": "1.1.0", "milestones": [{"title": "Q3"}]},
        ],
    }
    routes.update(overrides)
    for path, body in routes.items():
        if isinstance(body, httpx.Response):
            fake.set(path, body)
        else:
            fake.json(path, body=body)
    upload = f"/projects/42/uploads/{SECRET}/review one.png"
    if upload not in overrides:
        fake.set(upload, httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"}))


def capture(engine: Engine, ids: dict[str, int], fake: FakeGitLab) -> issue_evidence.EvidenceTally:
    with GitLabClient(settings(), transport=fake.transport) as client:
        return issue_evidence.collect(client, engine, ids["connection"])


@pytest.fixture
def indexed(migrated_engine: Engine, world: dict[str, int]) -> FakeGitLab:
    add_rule(migrated_engine, name="Security", category="security_finding", labels=["security"])
    fake = FakeGitLab({})
    serve(fake, full_issue(1), issue(2, ["docs"]))
    collect(migrated_engine, world, fake)
    return fake


def test_an_auditable_issue_is_captured_with_its_history_links_and_files(
    migrated_engine: Engine,
    world: dict[str, int],
    indexed: FakeGitLab,
) -> None:
    evidence_routes(indexed, 1)

    tally = capture(migrated_engine, world, indexed)

    assert dict(tally.captured) == {"complete": 1}
    assert tally.files == 1
    with Session(migrated_engine) as db:
        row = db.get_one(IssueEvidence, (world["project"], 1))
        observation = db.get_one(Observation, row.observation_id)
        stored = db.get_one(CapturedFile, hashlib.sha256(PNG).hexdigest())
    assert (row.status, row.gaps) == ("complete", [])
    assert verify(observation)
    assert observation.source_ref == "acme/api#1"
    payload = observation.payload
    assert payload["issue"]["description"].startswith("Access review.")
    assert payload["issue"]["closed_by"] == "grace"
    assert [n["system"] for n in payload["notes"]] == [False, True]
    assert [(e["kind"], e["state"] or e["label"]) for e in payload["history"]] == [
        ("label", "security"),
        ("state", "closed"),
    ]
    assert payload["links"][0]["link_type"] == "relates_to"
    (merge_request,) = payload["merge_requests"]
    assert merge_request["relation"] == "closes"
    assert merge_request["approvals"]["approved_by"] == ["linus"]
    assert merge_request["pipelines"][0]["status"] == "success"
    assert [r["tag_name"] for r in payload["releases"]] == ["v1.2.0"]
    (attachment,) = payload["attachments"]
    assert attachment["filename"] == "review one.png"
    assert attachment["sha256"] == stored.sha256
    assert attachment["referenced_in"] == ["description"]
    assert stored.content == PNG and stored.size_bytes == len(PNG)
    # Issue 2 is not auditable, so nothing of it was read.
    assert not any(path.startswith(f"{BASE}/2") for path in indexed.paths())


def test_unreadable_sources_are_gaps_not_omissions(
    migrated_engine: Engine,
    world: dict[str, int],
    indexed: FakeGitLab,
) -> None:
    evidence_routes(
        indexed,
        1,
        **{
            f"{BASE}/1/notes": httpx.Response(403, json={"message": "403 Forbidden"}),
            f"/projects/42/uploads/{SECRET}/review one.png": httpx.Response(
                302, headers={"Location": "https://objects.example.test/file"}
            ),
        },
    )

    tally = capture(migrated_engine, world, indexed)

    assert dict(tally.captured) == {"partial": 1}
    with Session(migrated_engine) as db:
        row = db.get_one(IssueEvidence, (world["project"], 1))
        payload = db.get_one(Observation, row.observation_id).payload
    assert row.status == "partial"
    assert any(gap.startswith("notes: ") for gap in row.gaps)
    assert "attachments: 1 redirected" in row.gaps
    assert payload["sources"]["notes"] == "forbidden"
    assert payload["attachments"][0]["status"] == "redirected"
    with Session(migrated_engine) as db:
        assert db.execute(select(func.count()).select_from(CapturedFile)).scalar_one() == 0


def test_attachments_over_the_limit_are_not_kept(
    migrated_engine: Engine,
    world: dict[str, int],
    indexed: FakeGitLab,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(issue_evidence.ATTACHMENT_LIMIT, "10")
    evidence_routes(indexed, 1)

    capture(migrated_engine, world, indexed)

    with Session(migrated_engine) as db:
        row = db.get_one(IssueEvidence, (world["project"], 1))
    assert "attachments: 1 too large" in row.gaps


def test_an_issue_gitlab_no_longer_serves_is_a_failed_capture(
    migrated_engine: Engine,
    world: dict[str, int],
    indexed: FakeGitLab,
) -> None:
    tally = capture(migrated_engine, world, indexed)

    assert dict(tally.captured) == {"failed": 1}
    with Session(migrated_engine) as db:
        row = db.get_one(IssueEvidence, (world["project"], 1))
    assert row.status == "failed"
    assert row.gaps[0].startswith("issue: ")


def test_an_unchanged_issue_is_not_read_again_and_a_changed_one_is(
    migrated_engine: Engine,
    world: dict[str, int],
    indexed: FakeGitLab,
) -> None:
    evidence_routes(indexed, 1)
    capture(migrated_engine, world, indexed)
    before = len(indexed.requests)

    again = capture(migrated_engine, world, indexed)
    assert sum(again.captured.values()) == 0
    assert len(indexed.requests) == before

    # GitLab changes the issue: the index notices, and the evidence is captured again.
    serve(indexed, full_issue(1, updated_at="2026-11-20T10:00:00Z"))
    collect(migrated_engine, world, indexed)
    evidence_routes(indexed, 1)
    uploads_before = sum("/uploads/" in p for p in indexed.paths())
    third = capture(migrated_engine, world, indexed)

    assert dict(third.captured) == {"complete": 1}
    # The same upload is not downloaded twice, and the same capture is not stored twice.
    assert sum("/uploads/" in p for p in indexed.paths()) == uploads_before
    assert third.unchanged == 1
    with Session(migrated_engine) as db:
        captures = db.scalars(
            select(Observation).where(Observation.source == issue_evidence.SOURCE)
        ).all()
    assert len(captures) == 1


def test_captured_files_cannot_be_changed_or_deleted(
    migrated_engine: Engine,
    world: dict[str, int],
    indexed: FakeGitLab,
) -> None:
    evidence_routes(indexed, 1)
    capture(migrated_engine, world, indexed)

    with Session(migrated_engine) as db, pytest.raises(DBAPIError, match="cannot be changed"):
        db.execute(text("UPDATE captured_file SET content_type = 'text/html'"))
    with Session(migrated_engine) as db, pytest.raises(DBAPIError, match="cannot be changed"):
        db.execute(text("DELETE FROM captured_file"))


def test_the_api_shows_the_capture_and_serves_cited_files_as_downloads(
    migrated_engine: Engine,
    world: dict[str, int],
    indexed: FakeGitLab,
) -> None:
    reader = client_as(migrated_engine, Role.MANAGER)
    project = world["project"]
    assert reader.get(f"/api/v1/register/issues/{project}/1/evidence").json()["captured"] is False

    evidence_routes(indexed, 1)
    capture(migrated_engine, world, indexed)
    evidence = reader.get(f"/api/v1/register/issues/{project}/1/evidence").json()
    sha = hashlib.sha256(PNG).hexdigest()
    download = reader.get(f"/api/v1/register/files/{sha}")
    unknown = reader.get(f"/api/v1/register/files/{'0' * 64}")

    assert evidence["captured"] is True
    assert evidence["status"] == "complete"
    assert evidence["digest_verified"] is True
    assert evidence["capture"]["attachments"][0]["sha256"] == sha
    assert download.status_code == 200
    assert download.content == PNG
    assert download.headers["content-type"] == "application/octet-stream"
    assert download.headers["content-disposition"] == 'attachment; filename="review_one.png"'
    assert download.headers["x-content-type-options"] == "nosniff"
    assert unknown.status_code == 404
