from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from gitlab_fake import TOKEN, URL, FakeGitLab, settings
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from baluarte.db.models import CollectionJob, GitLabInstance
from baluarte.integrations.gitlab.capabilities import (
    CapabilitySnapshot,
    detect,
    record_snapshot,
)
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.integrations.gitlab.jobs import DETECT_CAPABILITIES, make_detect_capabilities
from baluarte.jobs import queue
from baluarte.jobs.worker import Worker

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def run(fake: FakeGitLab) -> CapabilitySnapshot:
    client = GitLabClient(settings(), transport=fake.transport, sleep=lambda _: None)
    return detect(client, now=NOW)


def statuses(snapshot: CapabilitySnapshot) -> dict[str, str]:
    return {name: probe.status for name, probe in snapshot.probes.items()}


def test_full_detection_on_a_healthy_instance() -> None:
    fake = FakeGitLab()

    snapshot = run(fake)

    assert snapshot.status == "ok"
    assert (snapshot.version, snapshot.edition, snapshot.tier) == ("17.5.1-ee", "ee", None)
    assert snapshot.checked_at == NOW
    assert statuses(snapshot) == {
        "version": "supported",
        "groups.list": "supported",
        "groups.visibility": "supported",
        "projects.list": "supported",
        "projects.visibility": "supported",
        "projects.default_branch": "supported",
        "protected_branches.read": "supported",
        "merge_request_approvals.read": "supported",
        "license.read": "forbidden",
    }
    assert snapshot.probes["protected_branches.read"].endpoint == (
        "GET /projects/:id/protected_branches"
    )
    assert {request.method for request in fake.requests} == {"GET"}


def test_license_tier_is_read_when_visible() -> None:
    fake = FakeGitLab()
    fake.json("/license", body={"plan": "premium"})

    assert run(fake).tier == "premium"


@pytest.mark.parametrize(
    ("body", "edition"),
    [
        ({"version": "17.5.1", "enterprise": False}, "ce"),
        ({"version": "16.0.0-ee"}, "ee"),
        ({"version": "16.0.0"}, None),
    ],
)
def test_edition_is_only_claimed_when_indicated(
    body: dict[str, object], edition: str | None
) -> None:
    fake = FakeGitLab()
    fake.json("/version", body=body)

    assert run(fake).edition == edition


@pytest.mark.parametrize(
    ("reply", "status"),
    [
        (httpx.Response(403), "forbidden"),
        (httpx.Response(404), "not_found"),
        (httpx.Response(500), "error"),
    ],
)
def test_protected_branch_probe_outcomes_make_the_snapshot_partial(
    reply: httpx.Response, status: str
) -> None:
    fake = FakeGitLab()
    fake.set("/projects/42/protected_branches", reply)

    snapshot = run(fake)

    assert snapshot.status == "partial"
    assert snapshot.probes["protected_branches.read"].status == status
    assert snapshot.probes["protected_branches.read"].http_status == reply.status_code


def test_approvals_are_optional() -> None:
    fake = FakeGitLab()
    fake.json("/projects/42/approvals", 404, {})

    snapshot = run(fake)

    assert snapshot.status == "ok"
    assert snapshot.probes["merge_request_approvals.read"].status == "not_found"


def test_no_visible_projects_skips_project_probes() -> None:
    fake = FakeGitLab()
    fake.json("/projects", body=[])

    snapshot = run(fake)

    assert snapshot.status == "partial"
    for name in ("projects.visibility", "protected_branches.read", "merge_request_approvals.read"):
        assert snapshot.probes[name].status == "skipped"
    assert "/projects/42/protected_branches" not in fake.paths()


def test_missing_response_fields_are_not_supported() -> None:
    fake = FakeGitLab()
    fake.json("/projects", body=[{"id": 42, "path_with_namespace": "platform/api"}])

    snapshot = run(fake)

    assert snapshot.probes["projects.visibility"].status == "error"
    assert "visibility" in (snapshot.probes["projects.visibility"].detail or "")
    assert snapshot.status == "partial"


@pytest.mark.parametrize(
    "reply", [httpx.Response(401), httpx.ConnectError("refused"), httpx.Response(200, json={})]
)
def test_failed_version_probe_fails_the_snapshot(reply: httpx.Response | Exception) -> None:
    fake = FakeGitLab()
    fake.set("/version", reply)

    snapshot = run(fake)

    assert snapshot.status == "failed"
    assert snapshot.error
    assert TOKEN not in snapshot.error
    assert list(snapshot.probes) == ["version"]


@pytest.mark.db
def test_record_snapshot_creates_then_updates_the_instance(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as db:
        record_snapshot(db, URL, run(FakeGitLab()))
        db.commit()
    failing = FakeGitLab()
    failing.set("/version", httpx.Response(503))
    with Session(migrated_engine) as db:
        record_snapshot(db, URL, run(failing))
        db.commit()
        [instance] = db.scalars(select(GitLabInstance)).all()

    # A failure replaces the snapshot (no stale "ok") but keeps the last known version.
    assert instance.version == "17.5.1-ee"
    assert instance.edition == "ee"
    assert instance.capability_snapshot is not None
    assert instance.capability_snapshot["status"] == "failed"
    assert instance.last_checked_at == NOW


@pytest.fixture
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)


def run_job(engine: Engine, fake: FakeGitLab) -> CollectionJob:
    with Session(engine) as db:
        job_id = queue.enqueue(db, DETECT_CAPABILITIES, "test", max_attempts=2)
        db.commit()
    handler = make_detect_capabilities(fake.transport)
    assert Worker(engine, {DETECT_CAPABILITIES: handler}).run_once()
    with Session(engine) as db:
        return db.get_one(CollectionJob, job_id)


@pytest.mark.db
def test_job_detects_and_stores(migrated_engine: Engine, gitlab_env: None) -> None:
    job = run_job(migrated_engine, FakeGitLab())

    assert job.state == "succeeded"
    with Session(migrated_engine) as db:
        instance = db.scalars(select(GitLabInstance)).one()
    assert instance.base_url == URL
    assert instance.capability_snapshot is not None
    assert instance.capability_snapshot["status"] == "ok"


@pytest.mark.db
def test_job_records_failure_and_retries(migrated_engine: Engine, gitlab_env: None) -> None:
    fake = FakeGitLab()
    fake.set("/version", httpx.Response(401))

    job = run_job(migrated_engine, fake)

    assert job.state == "queued"
    assert job.error == "capability detection failed: GitLab rejected the token (401)"
    with Session(migrated_engine) as db:
        snapshot = db.scalars(select(GitLabInstance.capability_snapshot)).one()
    assert snapshot is not None
    assert snapshot["status"] == "failed"


@pytest.mark.db
def test_job_without_configuration_fails_permanently(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BALUARTE_GITLAB_URL", raising=False)

    job = run_job(migrated_engine, FakeGitLab())

    assert job.state == "failed"
    assert job.error is not None
    assert "not configured" in job.error
