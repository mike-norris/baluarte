"""Access-control reads after project discovery: approvals, members, tokens and two-factor."""

from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from gitlab_fake import TOKEN, URL, FakeGitLab, group, paged, project, settings
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from baluarte.db.models import CollectionRun, GitLabInstance, GitLabProject, Observation
from baluarte.integrations.gitlab import access, project_reads
from baluarte.integrations.gitlab.access import (
    ACCESS_TOKENS_SOURCE,
    APPROVALS_SOURCE,
    MEMBERS_SOURCE,
    TWO_FACTOR_SOURCE,
)
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.integrations.gitlab.discovery import Discovery
from baluarte.integrations.gitlab.groups import discover_groups
from baluarte.integrations.gitlab.jobs import DISCOVER_PROJECTS, make_discover_projects
from baluarte.integrations.gitlab.projects import discover_projects
from baluarte.jobs import queue
from baluarte.jobs.worker import Worker

pytestmark = pytest.mark.db

GROUPS = [
    group(1, "acme", require_two_factor_authentication=False, two_factor_grace_period=48),
    group(2, "acme/platform", 1, require_two_factor_authentication=True, two_factor_grace_period=8),
    group(3, "acme/web", 1, require_two_factor_authentication=False, two_factor_grace_period=48),
]
PROJECTS = [
    project(10, "acme/platform/api", 2),
    project(11, "acme/web/site", 3),
    project(12, "auditor/scratch", 900, kind="user"),
    project(13, "acme/empty", 1, default_branch=None),
]


def fake_with(groups: list[dict[str, Any]] | None = None) -> FakeGitLab:
    fake = FakeGitLab()
    fake.set("/groups", paged("/groups", [GROUPS if groups is None else groups]))
    fake.set("/projects", paged("/projects", [PROJECTS]))
    return fake


def client(fake: FakeGitLab) -> GitLabClient:
    return GitLabClient(settings(), transport=fake.transport, sleep=lambda _: None)


def discover(engine: Engine, fake: FakeGitLab, *, groups: bool = True) -> Discovery:
    if groups:
        discover_groups(client(fake), engine, URL)
    return discover_projects(client(fake), engine, URL)


def latest(engine: Engine, source: str) -> dict[int, Observation]:
    """The latest observation of ``source`` per project, by GitLab project ID."""
    with Session(engine) as db:
        rows = db.scalars(
            select(Observation).where(Observation.source == source).order_by(Observation.id)
        ).all()
        ids = {p.id: p.gitlab_id for p in db.scalars(select(GitLabProject))}
    return {ids[row.project_id]: row for row in rows if row.project_id is not None}


# Merge request approvals


APPROVAL_SETTINGS = {
    "approvals_before_merge": 0,
    "reset_approvals_on_push": True,
    "disable_overriding_approvers_per_merge_request": False,
    "merge_requests_author_approval": False,
    "merge_requests_disable_committers_approval": True,
    "require_password_to_approve": False,
    "require_reauthentication_to_approve": True,
}
APPROVAL_RULES = [
    {
        "id": 7,
        "name": "Maintainers",
        "rule_type": "regular",
        "approvals_required": 2,
        "eligible_approvers": [{"id": 77, "username": "jane", "name": "Jane Doe"}],
        "users": [{"id": 77, "username": "jane"}],
        "groups": [],
        "applies_to_all_protected_branches": False,
        "protected_branches": [{"id": 3, "name": "main"}, {"id": 4, "name": "release/*"}],
    },
    {
        "id": 8,
        "name": "All Members",
        "rule_type": "any_approver",
        "approvals_required": 1,
        "applies_to_all_protected_branches": True,
        "protected_branches": [],
    },
]


def test_approvals_are_read_without_approvers(migrated_engine: Engine) -> None:
    fake = fake_with()
    fake.json("/projects/10/protected_branches", body=[{"id": 3, "name": "main"}])
    fake.json("/projects/10/approvals", body=APPROVAL_SETTINGS)
    fake.json("/projects/10/approval_rules", body=APPROVAL_RULES)

    result = discover(migrated_engine, fake)

    found = latest(migrated_engine, APPROVALS_SOURCE)
    assert found[10].payload == {
        "branch": "main",
        "branch_protected": True,
        "settings": {
            "merge_requests_author_approval": False,
            "merge_requests_disable_committers_approval": True,
            "reset_approvals_on_push": True,
            "disable_overriding_approvers_per_merge_request": False,
            "require_reauthentication_to_approve": True,
        },
        "rules": [
            {
                "name": "All Members",
                "rule_type": "any_approver",
                "approvals_required": 1,
                "applies_to_all_protected_branches": True,
                "protected_branches": [],
            },
            {
                "name": "Maintainers",
                "rule_type": "regular",
                "approvals_required": 2,
                "applies_to_all_protected_branches": False,
                "protected_branches": ["main", "release/*"],
            },
        ],
    }
    assert "jane" not in str(found[10].payload) and "77" not in str(found[10].payload)
    assert found[10].source_ref == "/projects/10/approvals"
    # An empty repository has no default branch to cover, so it is not read.
    assert 13 not in found and "/projects/13/approvals" not in fake.paths()
    assert result.reads is not None
    assert result.reads["approvals"] == {"not_found": 2, "ok": 1}


def test_approvals_keep_the_older_reauthentication_setting(migrated_engine: Engine) -> None:
    fake = fake_with()
    fake.json("/projects/10/approvals", body={"require_password_to_approve": True})
    fake.json("/projects/10/approval_rules", body=[])

    discover(migrated_engine, fake)

    payload = latest(migrated_engine, APPROVALS_SOURCE)[10].payload
    assert payload is not None
    assert payload["settings"]["require_reauthentication_to_approve"] is True
    assert payload["settings"]["merge_requests_author_approval"] is None
    # The protection read failed (404), so whether the branch is protected is not known.
    assert payload["branch_protected"] is None


@pytest.mark.parametrize(
    ("settings_reply", "rules_reply", "status"),
    [
        (httpx.Response(403), httpx.Response(200, json=[]), "forbidden"),
        (httpx.Response(200, json={}), httpx.Response(404), "not_found"),
        (httpx.Response(200, json=[]), httpx.Response(200, json=[]), "failed"),
        (httpx.Response(200, json={}), httpx.Response(500), "failed"),
    ],
)
def test_approvals_that_cannot_be_read_have_no_payload(
    migrated_engine: Engine,
    settings_reply: httpx.Response,
    rules_reply: httpx.Response,
    status: str,
) -> None:
    fake = fake_with()
    fake.set("/projects/10/approvals", settings_reply)
    fake.set("/projects/10/approval_rules", rules_reply)

    discover(migrated_engine, fake)

    found = latest(migrated_engine, APPROVALS_SOURCE)[10]
    assert (found.collection_status, found.payload) == (status, None)
    assert found.error is not None and TOKEN not in found.error


# Members

MEMBERS = [
    {"id": 1, "username": "owner-one", "name": "Owner", "access_level": 50, "state": "active"},
    {
        "id": 2,
        "username": "maintainer-two",
        "access_level": 40,
        "state": "active",
        "expires_at": "2027-01-31",
        "email": "two@example.test",
    },
    {"id": 3, "username": "developer-three", "access_level": 30, "state": "active"},
    {"id": 4, "username": "left-four", "access_level": 20, "state": "blocked"},
    {"id": 5, "username": "guest-five", "access_level": 10, "state": "active"},
]


def test_members_are_counted_and_only_privileged_and_inactive_named(
    migrated_engine: Engine,
) -> None:
    fake = fake_with()
    fake.set(
        "/projects/10/members/all",
        paged("/projects/10/members/all", [MEMBERS[:3], [*MEMBERS[3:], MEMBERS[2]]]),
    )

    result = discover(migrated_engine, fake)

    found = latest(migrated_engine, MEMBERS_SOURCE)
    assert found[10].payload == {
        "total": 5,
        "privileged_count": 2,
        "by_access_level": {"10": 1, "20": 1, "30": 1, "40": 1, "50": 1},
        "by_state": {"active": 4, "blocked": 1},
        "privileged": [
            {
                "username": "maintainer-two",
                "access_level": 40,
                "state": "active",
                "expires_at": "2027-01-31",
            },
            {"username": "owner-one", "access_level": 50, "state": "active", "expires_at": None},
        ],
        "inactive": [
            {"username": "left-four", "access_level": 20, "state": "blocked", "expires_at": None}
        ],
    }
    for absent in ("developer-three", "guest-five", "two@example.test", "Owner"):
        assert absent not in str(found[10].payload)
    # Every active project is read, an empty repository included.
    assert sorted(found) == [10, 11, 12, 13]
    assert result.reads is not None and result.reads["members"] == {"not_found": 3, "ok": 1}


def test_unknown_member_levels_and_states_are_counted_as_unknown() -> None:
    payload = access.summarise_members(
        [{"id": 1, "username": "odd", "access_level": "40", "state": None}]
    )
    assert payload["by_access_level"] == {"unknown": 1}
    assert payload["by_state"] == {"unknown": 1}
    assert payload["privileged"] == []
    assert [entry["username"] for entry in payload["inactive"]] == ["odd"]


# Project access tokens

TOKENS = [
    {
        "id": 31,
        "name": "deploy",
        "scopes": ["read_registry", "read_api"],
        "access_level": 40,
        "active": True,
        "revoked": False,
        "created_at": "2026-09-01T10:00:00.000Z",
        "expires_at": "2026-12-01",
        "last_used_at": "2026-10-30T08:15:00.000Z",
        "user_id": 900,
    },
    {
        "id": 32,
        "name": "old",
        "scopes": ["api"],
        "access_level": 50,
        "active": False,
        "revoked": True,
        "created_at": "2025-01-01T10:00:00.000Z",
        "expires_at": None,
    },
]


def test_active_project_access_tokens_are_read(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(access, "_today", lambda: date(2026, 11, 3))
    fake = fake_with()
    fake.json("/projects/10/access_tokens", body=TOKENS)
    fake.json("/projects/11/access_tokens", body=[])
    fake.set("/projects/12/access_tokens", httpx.Response(403))

    result = discover(migrated_engine, fake)

    found = latest(migrated_engine, ACCESS_TOKENS_SOURCE)
    assert found[10].payload == {
        "tokens": [
            {
                "id": 31,
                "name": "deploy",
                "scopes": ["read_api", "read_registry"],
                "access_level": 40,
                "created_on": "2026-09-01",
                "expires_at": "2026-12-01",
                "last_used_on": "2026-10-30",
            }
        ],
        "as_of": "2026-11-03",
    }
    # Without tokens the observation does not change from day to day.
    assert found[11].payload == {"tokens": []}
    assert found[12].collection_status == "forbidden"
    assert result.reads is not None
    assert result.reads["access_tokens"] == {"forbidden": 1, "not_found": 1, "ok": 2}


def test_forbidden_reads_do_not_stop_a_reader(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(project_reads, "MAX_CONSECUTIVE_FAILURES", 2)
    fake = fake_with()
    for gitlab_id in (10, 11, 12, 13):
        fake.set(f"/projects/{gitlab_id}/access_tokens", httpx.Response(403))

    discover(migrated_engine, fake)

    assert {
        o.collection_status for o in latest(migrated_engine, ACCESS_TOKENS_SOURCE).values()
    } == {"forbidden"}
    assert fake.paths().count("/projects/13/access_tokens") == 1


def test_repeated_failures_stop_only_that_reader(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(project_reads, "MAX_CONSECUTIVE_FAILURES", 2)
    fake = fake_with()
    for gitlab_id in (10, 11):
        fake.set(f"/projects/{gitlab_id}/members/all", httpx.Response(500))
    for gitlab_id in (10, 11, 12):
        fake.json(f"/projects/{gitlab_id}/protected_branches", body=[])

    result = discover(migrated_engine, fake)

    members = latest(migrated_engine, MEMBERS_SOURCE)
    assert members[12].error == (
        "not read: 2 project member reads in a row failed earlier in this collection"
    )
    assert "/projects/12/members/all" not in fake.paths()
    # Other readers carry on.
    assert "/projects/12/protected_branches" in fake.paths()
    assert result.reads is not None and result.reads["members"] == {"failed": 4}


def test_a_rejected_token_stops_every_reader(migrated_engine: Engine) -> None:
    fake = fake_with()
    fake.set("/projects/10/members/all", httpx.Response(401))

    result = discover(migrated_engine, fake)

    tokens = latest(migrated_engine, ACCESS_TOKENS_SOURCE)
    assert tokens[10].error == "not read: GitLab rejected the token earlier in this collection"
    assert latest(migrated_engine, MEMBERS_SOURCE)[10].collection_status == "forbidden"
    assert not any(path.startswith("/projects/11/") for path in fake.paths())
    assert result.protection == {"protected": 0, "unprotected": 0, "unread": 3}


# Two-factor authentication


def test_two_factor_settings_of_the_groups_above_each_project(migrated_engine: Engine) -> None:
    fake = fake_with()

    result = discover(migrated_engine, fake)

    found = latest(migrated_engine, TWO_FACTOR_SOURCE)
    api = found[10].payload
    assert api is not None
    assert [g["path"] for g in api["groups"]] == ["acme", "acme/platform"]
    assert [g["require_two_factor_authentication"] for g in api["groups"]] == [False, True]
    assert [g["two_factor_grace_period"] for g in api["groups"]] == [48, 8]
    assert all(isinstance(g["observation_id"], int) for g in api["groups"])
    assert (api["ancestry"], api["instance"], api["enforced_by"]) == (
        "complete",
        None,
        "acme/platform",
    )
    assert found[10].source_ref == "/groups/2"
    # A personal project has no groups above it.
    assert found[12].payload == {
        "groups": [],
        "ancestry": "complete",
        "instance": None,
        "enforced_by": None,
    }
    assert found[12].source_ref is None
    # Group settings come from group discovery; no GitLab call per project.
    assert not any("two_factor" in path or "/application" in path for path in fake.paths())
    assert result.reads is not None and result.reads["two_factor"] == {"ok": 4}


def test_a_group_the_token_cannot_see_makes_the_chain_incomplete(migrated_engine: Engine) -> None:
    fake = fake_with([GROUPS[1]])

    discover(migrated_engine, fake)

    payload = latest(migrated_engine, TWO_FACTOR_SOURCE)[10].payload
    assert payload is not None
    assert [g["path"] for g in payload["groups"]] == ["acme/platform"]
    assert payload["ancestry"] == "incomplete"


def test_unreported_group_settings_are_unknown(migrated_engine: Engine) -> None:
    fake = fake_with([group(1, "acme"), group(2, "acme/platform", 1, two_factor_grace_period=True)])

    discover(migrated_engine, fake)

    payload = latest(migrated_engine, TWO_FACTOR_SOURCE)[10].payload
    assert payload is not None
    assert [g["require_two_factor_authentication"] for g in payload["groups"]] == [None, None]
    assert [g["two_factor_grace_period"] for g in payload["groups"]] == [None, None]


def _admin(engine: Engine, admin_access: bool) -> None:
    with Session(engine) as db:
        db.execute(
            update(GitLabInstance).values(
                capability_snapshot={
                    "status": "ok",
                    "token_user": {
                        "username": "auditor",
                        "is_admin": True,
                        "admin_access": admin_access,
                    },
                }
            )
        )
        db.commit()


@pytest.mark.parametrize("admin_access", [True, False])
def test_the_instance_setting_is_read_only_with_admin_access(
    migrated_engine: Engine, admin_access: bool
) -> None:
    fake = fake_with()
    discover(migrated_engine, fake)
    _admin(migrated_engine, admin_access)
    fake.json(
        "/application/settings",
        body={
            "require_two_factor_authentication": True,
            "two_factor_grace_period": 0,
            "signup_enabled": False,
        },
    )

    discover(migrated_engine, fake, groups=False)

    payload = latest(migrated_engine, TWO_FACTOR_SOURCE)[12].payload
    assert payload is not None
    if admin_access:
        assert payload["instance"] == {
            "require_two_factor_authentication": True,
            "two_factor_grace_period": 0,
        }
        assert fake.paths().count("/application/settings") == 1
        assert payload["enforced_by"] == "instance"
    else:
        assert payload["instance"] is None
        assert "/application/settings" not in fake.paths()


def test_an_unreadable_instance_setting_is_unknown(migrated_engine: Engine) -> None:
    fake = fake_with()
    discover(migrated_engine, fake)
    _admin(migrated_engine, True)
    fake.set("/application/settings", httpx.Response(403))

    discover(migrated_engine, fake, groups=False)

    payload = latest(migrated_engine, TWO_FACTOR_SOURCE)[12].payload
    assert payload is not None and payload["instance"] is None


# The discovery run


@pytest.fixture
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)


def run_job(engine: Engine, fake: FakeGitLab) -> CollectionRun:
    with Session(engine) as db:
        job_id = queue.enqueue(db, DISCOVER_PROJECTS, "test")
        db.commit()
    assert Worker(engine, {DISCOVER_PROJECTS: make_discover_projects(fake.transport)}).run_once()
    with Session(engine) as db:
        return db.scalars(
            select(CollectionRun)
            .where(CollectionRun.collection_job_id == job_id)
            .order_by(CollectionRun.id.desc())
        ).one()


def test_answers_about_one_project_do_not_make_the_run_partial(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    fake = fake_with()
    for gitlab_id in (10, 11, 12):
        fake.json(f"/projects/{gitlab_id}/protected_branches", body=[])

    run = run_job(migrated_engine, fake)

    assert run.status == "succeeded"
    assert run.counts["reads"]["access_tokens"] == {"not_found": 4}


def test_failed_reads_make_the_run_partial(migrated_engine: Engine, gitlab_env: None) -> None:
    fake = fake_with()
    for gitlab_id in (10, 11, 12):
        fake.json(f"/projects/{gitlab_id}/protected_branches", body=[])
    fake.set("/projects/11/members/all", httpx.Response(502))

    run = run_job(migrated_engine, fake)

    assert run.status == "partial"
    assert run.error == "members of 1 projects could not be read"
    assert run.counts["reads"]["members"] == {"failed": 1, "not_found": 3}
