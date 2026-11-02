"""Collection reliability: scheduled retries, the unhealthy alert and its log, and the population
statement (which projects the inventory can cover)."""

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from gitlab_fake import TOKEN, URL, FakeGitLab, settings
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session
from test_audit import client_as

from baluarte.auth.roles import Role
from baluarte.db.models import CollectionJob, CollectionRun, GitLabInstance
from baluarte.integrations.gitlab.capabilities import (
    CapabilitySnapshot,
    TokenUser,
    detect,
    record_snapshot,
)
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.integrations.gitlab.freshness import FAILURE_ALERT_AFTER, population, report
from baluarte.integrations.gitlab.instances import instance_for
from baluarte.integrations.gitlab.jobs import (
    CollectionHealth,
    Schedule,
    scheduled_retry_delays,
    ticks,
)
from baluarte.integrations.gitlab.settings import GitLabSettings
from baluarte.jobs import queue

LEASE = timedelta(minutes=5)


def no_jitter() -> float:
    return 0.5


@pytest.fixture
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)
    monkeypatch.setenv("BALUARTE_GITLAB_SYNC_INTERVAL_MINUTES", "60")


# Retries


@pytest.mark.parametrize(("attempt", "seconds"), [(1, 60), (2, 300), (3, 900), (4, 900), (9, 900)])
def test_a_job_with_retry_delays_waits_the_listed_delay(attempt: int, seconds: int) -> None:
    payload = {queue.RETRY_DELAYS: [60, 300, 900]}
    assert queue.retry_delay(attempt, payload, no_jitter) == timedelta(seconds=seconds)


@pytest.mark.parametrize("payload", [None, {}, {queue.RETRY_DELAYS: []}, {queue.RETRY_DELAYS: "x"}])
def test_other_jobs_keep_the_default_backoff(payload: dict[str, Any] | None) -> None:
    assert queue.retry_delay(2, payload, no_jitter) == queue.backoff(2, no_jitter)


def test_retry_delays_keep_the_jitter() -> None:
    payload = {queue.RETRY_DELAYS: [300]}
    assert queue.retry_delay(1, payload, lambda: 0.0) == timedelta(seconds=240)
    assert queue.retry_delay(1, payload, lambda: 1.0) == timedelta(seconds=360)


@pytest.mark.parametrize(
    ("interval", "delays"),
    [(15, (60, 300)), (41, (60, 300)), (42, (60, 300, 900)), (60, (60, 300, 900))],
)
def test_scheduled_retries_fit_in_half_the_interval(interval: int, delays: tuple[int, ...]) -> None:
    assert scheduled_retry_delays(interval) == delays


@pytest.mark.db
def test_enqueue_rejects_bad_retry_delays(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as db, pytest.raises(ValueError, match="retry_delays"):
        queue.enqueue(db, "inventory.sync", "bad", retry_delays=[60, 0])


@pytest.mark.db
def test_a_failed_scheduled_collection_retries_after_a_minute(migrated_engine: Engine) -> None:
    Schedule(migrated_engine, 60)()
    with Session(migrated_engine) as db:
        job = queue.claim(db, "worker-a", LEASE, ["gitlab.detect_capabilities"])
    assert job is not None and job.max_attempts == 4

    with Session(migrated_engine) as db:
        assert queue.fail(db, job, "GitLab could not be reached", retry=True, rng=no_jitter)
    with migrated_engine.connect() as connection:
        delay = connection.execute(
            text("SELECT run_after - now() FROM collection_job WHERE id = :id"), {"id": job.id}
        ).scalar_one()
    assert timedelta(seconds=55) < delay <= timedelta(seconds=60)


@pytest.mark.db
def test_an_expired_lease_also_uses_the_job_s_delays(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as db:
        job_id = queue.enqueue(db, "inventory.sync", "k", max_attempts=4, retry_delays=[300])
        db.commit()
    with Session(migrated_engine) as db:
        assert queue.claim(db, "worker-a", LEASE, ["inventory.sync"]) is not None
        db.execute(
            text("UPDATE collection_job SET lease_until = now() - interval '1 second' WHERE id=:i"),
            {"i": job_id},
        )
        db.commit()
    with Session(migrated_engine) as db:
        assert queue.expire_leases(db, no_jitter) == 1
        db.commit()
        delay = db.execute(
            text("SELECT run_after - now() FROM collection_job WHERE id = :id"), {"id": job_id}
        ).scalar_one()
    assert timedelta(seconds=295) < delay <= timedelta(seconds=300)


@pytest.mark.db
def test_manual_collection_keeps_the_short_backoff(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    api = client_as(migrated_engine, Role.ADMIN)
    response = api.post("/api/v1/gitlab/groups/discover", headers={"X-CSRF-Token": "csrf"})
    assert response.status_code == 202, response.text
    with Session(migrated_engine) as db:
        job = db.scalars(select(CollectionJob)).one()
    assert queue.RETRY_DELAYS not in job.payload


# The unhealthy alert


def add_run(engine: Engine, kind: str, status: str, minutes_ago: float, **extra: Any) -> None:
    with Session(engine) as db:
        instance = instance_for(db, URL)
        db.add(
            CollectionRun(
                instance_id=instance.id,
                kind=kind,
                status=status,
                started_at=func.now() - timedelta(minutes=minutes_ago, seconds=30),
                finished_at=func.now() - timedelta(minutes=minutes_ago),
                **extra,
            )
        )
        db.commit()


def all_kinds(engine: Engine, status: str, minutes_ago: float, **extra: Any) -> None:
    for kind in ("capabilities", "groups", "projects"):
        add_run(engine, kind, status, minutes_ago, **extra)


def freshness(engine: Engine, interval: int = 60) -> Any:
    configured = GitLabSettings(url=URL, token=TOKEN, sync_interval_minutes=interval)
    with Session(engine) as db:
        return report(db, instance_for(db, URL).id, configured)


@pytest.mark.db
def test_failures_below_the_threshold_raise_no_alert(migrated_engine: Engine) -> None:
    all_kinds(migrated_engine, "succeeded", 30)
    for minutes in range(FAILURE_ALERT_AFTER - 1):
        all_kinds(migrated_engine, "failed", 20 - minutes, error="GitLab could not be reached")

    result = freshness(migrated_engine)

    assert result.collections["groups"].consecutive_failures == FAILURE_ALERT_AFTER - 1
    assert result.alert is None


@pytest.mark.db
def test_consecutive_failures_raise_a_failing_alert(migrated_engine: Engine) -> None:
    all_kinds(migrated_engine, "succeeded", 50)
    all_kinds(migrated_engine, "failed", 60, error="before the last success")
    for minutes in (30, 25, 10):
        all_kinds(migrated_engine, "failed", minutes, error=f"unreachable {minutes}")

    result = freshness(migrated_engine)
    alert = result.alert

    assert alert is not None
    assert alert.reason == "failing"
    assert alert.kinds == ["capabilities", "groups", "projects"]
    # Only failures after the last success count.
    assert alert.consecutive_failures == 3
    # The earliest failure across the failing kinds.
    assert alert.failing_since == min(
        c.failing_since for c in result.collections.values() if c.failing_since is not None
    )
    # The oldest last success across the failing kinds.
    assert alert.last_success_at == min(
        c.last_success_at for c in result.collections.values() if c.last_success_at is not None
    )
    assert alert.last_error == "unreachable 10"
    assert "failed 3 times in a row" in alert.message
    assert "Check that Baluarte can reach GitLab" in alert.message


@pytest.mark.db
def test_a_success_clears_the_alert(migrated_engine: Engine) -> None:
    for minutes in (40, 30, 20):
        all_kinds(migrated_engine, "failed", minutes)
    assert freshness(migrated_engine).alert is not None

    all_kinds(migrated_engine, "succeeded", 1)

    assert freshness(migrated_engine).alert is None


@pytest.mark.db
def test_never_succeeding_is_failing_too(migrated_engine: Engine) -> None:
    for minutes in (30, 20, 10):
        add_run(migrated_engine, "projects", "failed", minutes)

    alert = freshness(migrated_engine).alert

    assert alert is not None
    assert (alert.reason, alert.kinds, alert.last_success_at) == ("failing", ["projects"], None)
    assert "has not succeeded" in alert.message


@pytest.mark.db
def test_an_old_success_is_overdue_only_when_scheduled(migrated_engine: Engine) -> None:
    # Two intervals of 60 minutes have passed since the last success, with no runs since.
    all_kinds(migrated_engine, "succeeded", 121)

    alert = freshness(migrated_engine, interval=60).alert

    assert alert is not None and alert.reason == "overdue"
    assert "Check that the worker is running" in alert.message
    assert freshness(migrated_engine, interval=180).alert is None
    assert freshness(migrated_engine, interval=0).alert is None


# Transition logging


def events(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [getattr(record, "event", "") for record in caplog.records if hasattr(record, "event")]


@pytest.mark.db
def test_unhealthy_and_recovered_are_each_logged_once(
    migrated_engine: Engine, gitlab_env: None, caplog: pytest.LogCaptureFixture
) -> None:
    clock = [0.0]
    health = CollectionHealth(migrated_engine, check_every=60, clock=lambda: clock[0])
    caplog.set_level(logging.WARNING, logger="baluarte.integrations.gitlab.jobs")

    def check() -> None:
        clock[0] += 61
        health()

    all_kinds(migrated_engine, "succeeded", 30)
    check()
    assert events(caplog) == []

    for minutes in (20, 15, 10):
        all_kinds(migrated_engine, "failed", minutes, error="GitLab could not be reached")
    check()
    check()
    add_run(migrated_engine, "groups", "failed", 1)
    check()
    assert events(caplog) == [CollectionHealth.UNHEALTHY]
    unhealthy = next(r for r in caplog.records if getattr(r, "event", "") == health.UNHEALTHY)
    assert unhealthy.levelno == logging.WARNING
    assert unhealthy.__dict__["reason"] == "failing"
    # Logged when it became unhealthy, not again for the later failure.
    assert unhealthy.__dict__["consecutive_failures"] == 3

    all_kinds(migrated_engine, "succeeded", 0)
    check()
    check()
    assert events(caplog) == [CollectionHealth.UNHEALTHY, CollectionHealth.RECOVERED]


@pytest.mark.db
def test_the_health_check_waits_between_checks(
    migrated_engine: Engine, gitlab_env: None, caplog: pytest.LogCaptureFixture
) -> None:
    for minutes in (20, 15, 10):
        all_kinds(migrated_engine, "failed", minutes)
    health = CollectionHealth(migrated_engine, check_every=60, clock=lambda: 0.0)
    caplog.set_level(logging.WARNING, logger="baluarte.integrations.gitlab.jobs")

    health()
    assert events(caplog) == [CollectionHealth.UNHEALTHY]
    all_kinds(migrated_engine, "succeeded", 0)
    health()  # within check_every: not rechecked yet
    assert events(caplog) == [CollectionHealth.UNHEALTHY]


def test_ticks_run_each_callable_in_order() -> None:
    calls: list[str] = []
    ticks(lambda: calls.append("schedule"), lambda: calls.append("health"))()
    assert calls == ["schedule", "health"]


# Population


def detect_with_user(body: dict[str, Any] | None, admin_read: int = 200) -> CapabilitySnapshot:
    fake = FakeGitLab()
    fake.json("/application/statistics", admin_read, {"projects": "107"})
    if body is None:
        fake.json("/user", 403, {"message": "403 Forbidden"})
    else:
        fake.json("/user", 200, body)
    client = GitLabClient(settings(), transport=fake.transport, sleep=lambda _: None)
    return detect(client)


def test_detection_records_the_token_user() -> None:
    admin = detect_with_user({"id": 1, "username": "root", "is_admin": True})
    # Admin Mode on, token without the admin_mode scope: an administrator, but no admin access.
    held_back = detect_with_user({"id": 2, "username": "auditor", "is_admin": True}, 403)
    member = detect_with_user({"id": 5, "username": "auditor", "state": "active"})
    unread = detect_with_user(None)

    assert admin.token_user == TokenUser(username="root", is_admin=True, admin_access=True)
    assert held_back.token_user == TokenUser(username="auditor", is_admin=True, admin_access=False)
    assert held_back.probes["admin.read"].status == "forbidden"
    # GitLab leaves is_admin out for anyone who is not an administrator.
    assert member.token_user == TokenUser(username="auditor", is_admin=False, admin_access=False)
    assert "admin.read" not in member.probes
    assert unread.token_user is None
    assert unread.probes["user.read"].status == "forbidden"


@pytest.mark.db
def test_a_failed_detection_keeps_the_last_known_token_user(migrated_engine: Engine) -> None:
    ok = detect_with_user({"id": 5, "username": "auditor"})
    failed = CapabilitySnapshot(
        status="failed",
        checked_at=ok.checked_at,
        version=None,
        edition=None,
        tier=None,
        error="GitLab could not be reached",
        probes={},
    )
    with Session(migrated_engine) as db:
        record_snapshot(db, URL, ok)
        instance = record_snapshot(db, URL, failed)
        db.commit()
        assert instance.capability_snapshot["status"] == "failed"
        assert instance.capability_snapshot["token_user"] == {
            "username": "auditor",
            "is_admin": False,
            "admin_access": False,
        }


def test_population_statements() -> None:
    admin = population({"token_user": {"username": "root", "is_admin": True, "admin_access": True}})
    held_back = population(
        {"token_user": {"username": "auditor", "is_admin": True, "admin_access": False}}
    )
    # A snapshot from before admin access was checked does not prove it either.
    older = population({"token_user": {"username": "root", "is_admin": True}})
    member = population({"token_user": {"username": "auditor", "is_admin": False}})
    unknown = population(None)

    assert (admin.basis, admin.is_admin, admin.statement) == (
        "instance",
        True,
        "Inventory covers every group and project on the instance.",
    )
    assert (member.basis, member.token_username, member.is_admin) == (
        "token_visibility",
        "auditor",
        False,
    )
    assert member.statement == (
        "Inventory covers only the groups and projects visible to auditor. auditor is not a GitLab "
        "administrator, so projects this account cannot see are not included and are not checked."
    )
    assert (held_back.basis, held_back.is_admin, held_back.admin_access) == (
        "token_visibility",
        True,
        False,
    )
    assert held_back.statement == (
        "Inventory covers only the groups and projects visible to auditor. auditor is a GitLab "
        "administrator, but this token could not use administrator access (with Admin Mode on, "
        "the token needs the admin_mode scope), so projects this account cannot see are not "
        "included and are not checked."
    )
    assert older.basis == "token_visibility"
    assert (unknown.basis, unknown.token_username, unknown.is_admin) == ("unknown", None, None)


@pytest.mark.db
def test_the_freshness_api_states_the_population(migrated_engine: Engine, gitlab_env: None) -> None:
    with Session(migrated_engine) as db:
        record_snapshot(db, URL, detect_with_user({"id": 5, "username": "auditor"}))
        db.commit()
    api = client_as(migrated_engine, Role.AUDITOR)

    body = api.get("/api/v1/gitlab/freshness").json()

    assert body["population"]["basis"] == "token_visibility"
    assert body["population"]["token_username"] == "auditor"
    assert body["population"]["is_admin"] is False
    assert body["alert"] is None
    with Session(migrated_engine) as db:
        assert db.scalars(select(GitLabInstance)).one().capability_snapshot is not None
