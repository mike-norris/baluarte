from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from gitlab_fake import TOKEN, URL, FakeGitLab, group, paged
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session

from baluarte.db.models import CollectionJob, CollectionRun, GitLabGroup
from baluarte.integrations.gitlab.freshness import report
from baluarte.integrations.gitlab.instances import instance_for
from baluarte.integrations.gitlab.jobs import (
    DETECT_CAPABILITIES,
    DISCOVER_GROUPS,
    DISCOVER_PROJECTS,
    Schedule,
    make_detect_capabilities,
    make_discover_groups,
    scheduler,
)
from baluarte.integrations.gitlab.runs import INTERRUPTED, finish_run, start_run
from baluarte.integrations.gitlab.settings import GitLabSettings, load_gitlab_settings
from baluarte.jobs import queue
from baluarte.jobs.worker import Worker
from baluarte.settings import ConfigurationError

TREE = [[group(1, "platform"), group(2, "platform/api", 1)]]


@pytest.fixture
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("BALUARTE_GITLAB_SYNC_INTERVAL_MINUTES", raising=False)


def runs(engine: Engine) -> list[CollectionRun]:
    with Session(engine) as db:
        return list(db.scalars(select(CollectionRun).order_by(CollectionRun.id)))


def run_job(engine: Engine, kind: str, handler: Any) -> CollectionJob:
    with Session(engine) as db:
        job_id = queue.enqueue(db, kind, f"test-{kind}")
        db.commit()
    assert Worker(engine, {kind: handler}).run_once()
    with Session(engine) as db:
        return db.get_one(CollectionJob, job_id)


def groups_fake(pages: list[list[dict[str, Any]]] = TREE) -> FakeGitLab:
    fake = FakeGitLab()
    fake.set("/groups", paged("/groups", pages))
    return fake


# Settings


@pytest.mark.parametrize(("raw", "minutes"), [(None, 360), ("0", 0), ("15", 15), ("10080", 10080)])
def test_sync_interval(
    monkeypatch: pytest.MonkeyPatch, gitlab_env: None, raw: str | None, minutes: int
) -> None:
    if raw is not None:
        monkeypatch.setenv("BALUARTE_GITLAB_SYNC_INTERVAL_MINUTES", raw)

    settings = load_gitlab_settings()

    assert settings is not None
    assert settings.sync_interval_minutes == minutes


@pytest.mark.parametrize("raw", ["5", "10081", "hourly", "-1"])
def test_invalid_sync_interval(monkeypatch: pytest.MonkeyPatch, gitlab_env: None, raw: str) -> None:
    monkeypatch.setenv("BALUARTE_GITLAB_SYNC_INTERVAL_MINUTES", raw)

    with pytest.raises(ConfigurationError, match="SYNC_INTERVAL"):
        load_gitlab_settings()


# Runs


@pytest.mark.db
def test_handlers_record_successful_runs(migrated_engine: Engine, gitlab_env: None) -> None:
    run_job(migrated_engine, DISCOVER_GROUPS, make_discover_groups(groups_fake().transport))
    run_job(migrated_engine, DETECT_CAPABILITIES, make_detect_capabilities(FakeGitLab().transport))

    groups_run, capabilities_run = runs(migrated_engine)
    assert (groups_run.kind, groups_run.status, groups_run.seen) == ("groups", "succeeded", 2)
    assert groups_run.counts == {"active": 0, "inaccessible": 0, "missing": 0, "unchanged": 0}
    assert groups_run.finished_at is not None
    assert groups_run.attempt == 1
    assert groups_run.collection_job_id is not None
    assert (capabilities_run.kind, capabilities_run.status) == ("capabilities", "succeeded")
    assert capabilities_run.counts == {"not_supported": ["license.read"]}


@pytest.mark.db
def test_failed_discovery_is_recorded_before_the_retry(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    fake = FakeGitLab()
    fake.set("/groups", httpx.Response(401))

    job = run_job(migrated_engine, DISCOVER_GROUPS, make_discover_groups(fake.transport))

    [run] = runs(migrated_engine)
    assert job.state == "queued"
    assert run.status == "failed"
    assert run.error == "group discovery failed: GitLab rejected the token (401)"


@pytest.mark.db
def test_failed_capability_detection_is_recorded(migrated_engine: Engine, gitlab_env: None) -> None:
    fake = FakeGitLab()
    fake.set("/version", httpx.Response(503))

    run_job(migrated_engine, DETECT_CAPABILITIES, make_detect_capabilities(fake.transport))

    [run] = runs(migrated_engine)
    assert (run.status, run.error) == ("failed", "GitLab returned 503 for /version")


@pytest.mark.db
def test_unexpected_errors_close_the_run_by_type(
    migrated_engine: Engine, gitlab_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_: object) -> None:
        raise RuntimeError("https://gitlab.example.test/?private_token=secret")

    monkeypatch.setattr("baluarte.integrations.gitlab.jobs.discover_groups", explode)

    run_job(migrated_engine, DISCOVER_GROUPS, make_discover_groups(groups_fake().transport))

    [run] = runs(migrated_engine)
    assert (run.status, run.error) == ("failed", "unexpected error: RuntimeError")


@pytest.mark.db
def test_unconfirmed_resources_make_a_partial_run_and_record_the_error(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    run_job(migrated_engine, DISCOVER_GROUPS, make_discover_groups(groups_fake().transport))
    with Session(migrated_engine) as db:
        db.execute(
            update(GitLabGroup).values(last_seen_at=GitLabGroup.last_seen_at - timedelta(hours=1))
        )
        db.commit()
    fake = groups_fake([[group(1, "platform")]])
    fake.set("/groups/2", httpx.Response(500))
    handler = make_discover_groups(fake.transport)

    with Session(migrated_engine) as db:
        queue.enqueue(db, DISCOVER_GROUPS, "second")
        db.commit()
    Worker(migrated_engine, {DISCOVER_GROUPS: handler}).run_once()

    latest = runs(migrated_engine)[-1]
    assert latest.status == "partial"
    assert latest.counts["unchanged"] == 1
    assert latest.error == "1 groups could not be confirmed"
    with Session(migrated_engine) as db:
        api = db.scalars(select(GitLabGroup).where(GitLabGroup.gitlab_id == 2)).one()
    assert (api.state, api.last_error) == (
        "active",
        "could not confirm: GitLab returned 500 for /groups/2",
    )
    assert api.last_error_at is not None

    # Listed again: the error clears.
    with Session(migrated_engine) as db:
        queue.enqueue(db, DISCOVER_GROUPS, "third")
        db.commit()
    Worker(
        migrated_engine, {DISCOVER_GROUPS: make_discover_groups(groups_fake().transport)}
    ).run_once()
    with Session(migrated_engine) as db:
        api = db.scalars(select(GitLabGroup).where(GitLabGroup.gitlab_id == 2)).one()
    assert (api.last_error, api.last_error_at) == (None, None)


def claimed(job_id: int, attempt: int) -> queue.ClaimedJob:
    return queue.ClaimedJob(
        id=job_id,
        kind=DISCOVER_GROUPS,
        idempotency_key="k",
        payload={},
        attempt=attempt,
        max_attempts=5,
        lease_owner="w",
    )


@pytest.mark.db
def test_runs_cut_short_are_closed_as_interrupted(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as db:
        job_id = queue.enqueue(db, DISCOVER_GROUPS, "k")
        db.commit()
    abandoned = start_run(migrated_engine, URL, "groups", None)
    crashed_attempt = start_run(migrated_engine, URL, "groups", claimed(job_id, 1))
    # The job is retried: attempt 2 is running now.
    with Session(migrated_engine) as db:
        db.execute(
            update(CollectionJob)
            .where(CollectionJob.id == job_id)
            .values(state="running", attempt=2, lease_owner="w", lease_until=func.now())
        )
        db.commit()
    current = start_run(migrated_engine, URL, "groups", claimed(job_id, 2))
    concurrent_other_kind = start_run(migrated_engine, URL, "projects", None)
    # A second run for the same running attempt does not close the first.
    sibling = start_run(migrated_engine, URL, "groups", claimed(job_id, 2))

    by_id = {run.id: run for run in runs(migrated_engine)}
    assert by_id[abandoned].status == "failed"
    assert by_id[abandoned].error == INTERRUPTED
    assert by_id[crashed_attempt].status == "failed"
    assert by_id[current].status == "running"
    assert by_id[sibling].status == "running"
    assert by_id[concurrent_other_kind].status == "running"

    # Closing an interrupted run later does not overwrite it.
    finish_run(migrated_engine, crashed_attempt, "succeeded", seen=9)
    assert {run.id: run for run in runs(migrated_engine)}[crashed_attempt].status == "failed"


# Schedule


@pytest.mark.db
def test_schedule_queues_each_collection_once_per_interval(migrated_engine: Engine) -> None:
    ticks = iter([0.0, 0.0, 10.0, 10.0, 100.0, 100.0])
    first = Schedule(migrated_engine, 360, check_every=60, clock=lambda: next(ticks))
    other_worker = Schedule(migrated_engine, 360)

    first()
    first()  # within check_every: no database round trip
    other_worker()
    first()  # same bucket: nothing new

    with Session(migrated_engine) as db:
        jobs = list(db.scalars(select(CollectionJob).order_by(CollectionJob.id)))
        epoch = float(db.execute(select(func.extract("epoch", func.now()))).scalar_one())
    bucket = int(epoch // (360 * 60))
    assert [job.kind for job in jobs] == [DETECT_CAPABILITIES, DISCOVER_GROUPS, DISCOVER_PROJECTS]
    assert {job.idempotency_key for job in jobs} == {
        f"{kind}:scheduled:{bucket}"
        for kind in (DETECT_CAPABILITIES, DISCOVER_GROUPS, DISCOVER_PROJECTS)
    }
    assert {job.max_attempts for job in jobs} == {3}


@pytest.mark.db
def test_worker_calls_the_schedule(migrated_engine: Engine) -> None:
    calls: list[int] = []

    def tick() -> None:
        calls.append(1)
        worker.stop.set()

    worker = Worker(migrated_engine, {}, poll_interval=0.01, tick=tick)
    worker.run()

    assert calls == [1]


def test_scheduler_only_when_configured(monkeypatch: pytest.MonkeyPatch, gitlab_env: None) -> None:
    engine: Any = object()

    assert isinstance(scheduler(engine), Schedule)
    monkeypatch.setenv("BALUARTE_GITLAB_SYNC_INTERVAL_MINUTES", "0")
    assert scheduler(engine) is None
    monkeypatch.setenv("BALUARTE_GITLAB_SYNC_INTERVAL_MINUTES", "1")
    assert scheduler(engine) is None
    monkeypatch.delenv("BALUARTE_GITLAB_URL")
    assert scheduler(engine) is None


# Freshness


def add_run(
    engine: Engine, kind: str, status: str, finished_hours_ago: float, **extra: Any
) -> None:
    with Session(engine) as db:
        instance = instance_for(db, URL)
        db.add(
            CollectionRun(
                instance_id=instance.id,
                kind=kind,
                status=status,
                started_at=func.now() - timedelta(hours=finished_hours_ago, minutes=5),
                finished_at=None
                if status == "running"
                else func.now() - timedelta(hours=finished_hours_ago),
                **extra,
            )
        )
        db.commit()


def freshness(engine: Engine, interval: int = 360) -> Any:
    settings = GitLabSettings(url=URL, token=TOKEN, sync_interval_minutes=interval)
    with Session(engine) as db:
        return report(db, instance_for(db, URL).id, settings)


@pytest.mark.db
def test_freshness_never(migrated_engine: Engine) -> None:
    result = freshness(migrated_engine)

    assert result.status == "never"
    assert {kind: c.status for kind, c in result.collections.items()} == {
        "capabilities": "never",
        "groups": "never",
        "projects": "never",
    }
    assert result.stale_after_minutes == 720


@pytest.mark.db
def test_freshness_per_kind(migrated_engine: Engine) -> None:
    add_run(migrated_engine, "capabilities", "succeeded", 1)
    add_run(migrated_engine, "groups", "succeeded", 2)
    add_run(migrated_engine, "groups", "failed", 1, error="group discovery failed: boom")
    add_run(migrated_engine, "projects", "partial", 1, error="1 projects could not be confirmed")
    add_run(migrated_engine, "projects", "running", 0)

    result = freshness(migrated_engine)

    collections = result.collections
    assert collections["capabilities"].status == "fresh"
    # The newer failure wins over the older success.
    assert collections["groups"].status == "failed"
    assert collections["groups"].last_run.error == "group discovery failed: boom"
    assert collections["groups"].last_success_at is not None
    assert collections["projects"].status == "partial"
    assert collections["projects"].running is True
    assert result.status == "failed"


@pytest.mark.db
def test_freshness_stale_follows_the_interval(migrated_engine: Engine) -> None:
    for kind in ("capabilities", "groups", "projects"):
        add_run(migrated_engine, kind, "succeeded", 13)

    assert freshness(migrated_engine, interval=360).status == "stale"
    assert freshness(migrated_engine, interval=720).status == "fresh"
    assert freshness(migrated_engine, interval=0).status == "fresh"
    assert freshness(migrated_engine, interval=0).stale_after_minutes == 1440


@pytest.mark.db
def test_freshness_counts_resources(migrated_engine: Engine, gitlab_env: None) -> None:
    run_job(migrated_engine, DISCOVER_GROUPS, make_discover_groups(groups_fake().transport))
    with Session(migrated_engine) as db:
        db.execute(update(GitLabGroup).where(GitLabGroup.gitlab_id == 1).values(state="missing"))
        db.execute(
            update(GitLabGroup)
            .where(GitLabGroup.gitlab_id == 2)
            .values(last_error="could not confirm")
        )
        db.commit()

    groups = freshness(migrated_engine).resources["groups"]

    assert (groups.active, groups.missing, groups.inaccessible, groups.with_errors) == (1, 1, 0, 1)


@pytest.mark.db
def test_freshness_not_configured(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as db:
        result = report(db, None, None)

    assert (result.status, result.collections) == ("not_configured", {})
