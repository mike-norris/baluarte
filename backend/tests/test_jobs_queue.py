from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from baluarte.db.models import CollectionJob
from baluarte.jobs import queue

pytestmark = pytest.mark.db

LEASE = timedelta(minutes=5)
KINDS = ["inventory.sync"]


def no_jitter() -> float:
    return 0.5


def add(engine: Engine, key: str, **options: Any) -> int:
    with Session(engine) as db:
        job_id = queue.enqueue(db, "inventory.sync", key, {"group": key}, **options)
        db.commit()
        return job_id


def claim(engine: Engine, owner: str = "worker-a", kinds: list[str] = KINDS) -> queue.ClaimedJob:
    with Session(engine) as db:
        job = queue.claim(db, owner, LEASE, kinds)
    assert job is not None
    return job


def nothing_to_claim(engine: Engine, kinds: list[str] = KINDS) -> bool:
    with Session(engine) as db:
        return queue.claim(db, "worker-z", LEASE, kinds) is None


def row(engine: Engine, job_id: int) -> CollectionJob:
    with Session(engine, expire_on_commit=False) as db:
        return db.get_one(CollectionJob, job_id)


def make_due(engine: Engine, job_id: int) -> None:
    with Session(engine) as db:
        db.execute(
            update(CollectionJob)
            .where(CollectionJob.id == job_id)
            .values(run_after=func.now() - timedelta(seconds=1))
        )
        db.commit()


def expire_lease(engine: Engine, job_id: int) -> None:
    with Session(engine) as db:
        db.execute(
            update(CollectionJob)
            .where(CollectionJob.id == job_id)
            .values(lease_until=func.now() - timedelta(seconds=1))
        )
        db.commit()


@pytest.mark.parametrize(
    ("attempt", "seconds"),
    [(1, 30), (2, 60), (3, 120), (7, 1920), (8, 3600), (30, 3600)],
)
def test_backoff_doubles_up_to_an_hour(attempt: int, seconds: int) -> None:
    assert queue.backoff(attempt, no_jitter) == timedelta(seconds=seconds)


@pytest.mark.parametrize(("draw", "factor"), [(0.0, 0.8), (1.0, 1.2)])
def test_backoff_jitter_is_bounded(draw: float, factor: float) -> None:
    assert queue.backoff(1, lambda: draw) == timedelta(seconds=30 * factor)


def test_enqueue_is_idempotent(migrated_engine: Engine) -> None:
    first = add(migrated_engine, "group-1")
    again = add(migrated_engine, "group-1")

    assert first == again
    with Session(migrated_engine) as db:
        assert db.scalar(select(func.count()).select_from(CollectionJob)) == 1


def test_enqueue_after_completion_is_still_a_no_op(migrated_engine: Engine) -> None:
    job_id = add(migrated_engine, "group-1")
    with Session(migrated_engine) as db:
        queue.complete(db, claim(migrated_engine))

    assert add(migrated_engine, "group-1") == job_id
    assert row(migrated_engine, job_id).state == "succeeded"


@pytest.mark.parametrize(
    ("kind", "key", "options"),
    [("", "k", {}), ("k", "", {}), ("k", "x" * 201, {}), ("k", "k", {"max_attempts": 0})],
)
def test_enqueue_validates_input(
    migrated_engine: Engine, kind: str, key: str, options: dict[str, Any]
) -> None:
    with Session(migrated_engine) as db, pytest.raises(ValueError):
        queue.enqueue(db, kind, key, **options)


def test_claim_takes_due_jobs_in_order(migrated_engine: Engine) -> None:
    first = add(migrated_engine, "group-1")
    second = add(migrated_engine, "group-2")
    add(migrated_engine, "later", delay=timedelta(hours=1))

    job = claim(migrated_engine)

    assert (job.id, job.attempt, job.payload) == (first, 1, {"group": "group-1"})
    assert claim(migrated_engine).id == second
    assert nothing_to_claim(migrated_engine)
    stored = row(migrated_engine, first)
    assert stored.state == "running"
    assert stored.lease_owner == "worker-a"
    assert stored.started_at is not None


def test_claim_only_takes_kinds_the_worker_handles(migrated_engine: Engine) -> None:
    add(migrated_engine, "group-1")

    assert nothing_to_claim(migrated_engine, kinds=["export.build"])
    assert nothing_to_claim(migrated_engine, kinds=[])
    assert claim(migrated_engine).kind == "inventory.sync"


def test_concurrent_claims_never_share_a_job(migrated_engine: Engine) -> None:
    first = add(migrated_engine, "group-1")
    second = add(migrated_engine, "group-2")

    # Hold the first job's row lock in an open transaction, as a worker mid-claim would.
    with Session(migrated_engine) as holder:
        locked = holder.execute(
            select(CollectionJob).where(CollectionJob.id == first).with_for_update()
        ).scalar_one()
        assert locked.id == first

        job = claim(migrated_engine, owner="worker-b")

        assert job.id == second


def test_complete_records_success(migrated_engine: Engine) -> None:
    job_id = add(migrated_engine, "group-1")
    job = claim(migrated_engine)

    with Session(migrated_engine) as db:
        assert queue.complete(db, job)

    stored = row(migrated_engine, job_id)
    assert (stored.state, stored.lease_owner, stored.lease_until) == ("succeeded", None, None)
    assert stored.finished_at is not None


def test_failure_is_retried_with_backoff(migrated_engine: Engine) -> None:
    job_id = add(migrated_engine, "group-1")
    job = claim(migrated_engine)

    with Session(migrated_engine) as db:
        assert queue.fail(db, job, "GitLab returned 502", retry=True, rng=no_jitter)

    stored = row(migrated_engine, job_id)
    assert (stored.state, stored.attempt, stored.error) == ("queued", 1, "GitLab returned 502")
    with migrated_engine.connect() as connection:
        delay = connection.execute(
            text("SELECT run_after - now() FROM collection_job WHERE id = :id"), {"id": job_id}
        ).scalar_one()
    assert timedelta(seconds=25) < delay <= timedelta(seconds=30)
    assert nothing_to_claim(migrated_engine)

    make_due(migrated_engine, job_id)
    assert claim(migrated_engine).attempt == 2


def test_job_fails_after_its_last_attempt(migrated_engine: Engine) -> None:
    job_id = add(migrated_engine, "group-1", max_attempts=2)
    for _ in range(2):
        make_due(migrated_engine, job_id)
        with Session(migrated_engine) as db:
            queue.fail(db, claim(migrated_engine), "still failing", retry=True)

    stored = row(migrated_engine, job_id)
    assert (stored.state, stored.attempt) == ("failed", 2)
    assert stored.finished_at is not None


def test_permanent_failure_is_not_retried(migrated_engine: Engine) -> None:
    job_id = add(migrated_engine, "group-1")

    with Session(migrated_engine) as db:
        queue.fail(db, claim(migrated_engine), "group no longer exists", retry=False)

    assert row(migrated_engine, job_id).state == "failed"


def test_error_messages_are_truncated(migrated_engine: Engine) -> None:
    job_id = add(migrated_engine, "group-1")

    with Session(migrated_engine) as db:
        queue.fail(db, claim(migrated_engine), "x" * 5000, retry=False)

    assert len(row(migrated_engine, job_id).error or "") == queue.MAX_ERROR_LENGTH


def test_expired_lease_is_requeued_and_reclaimed(migrated_engine: Engine) -> None:
    job_id = add(migrated_engine, "group-1")
    stale = claim(migrated_engine, owner="worker-a")
    expire_lease(migrated_engine, job_id)

    assert nothing_to_claim(migrated_engine)  # reclaim is subject to backoff
    stored = row(migrated_engine, job_id)
    assert (stored.state, stored.lease_owner, stored.error) == ("queued", None, queue.LEASE_EXPIRED)

    make_due(migrated_engine, job_id)
    fresh = claim(migrated_engine, owner="worker-b")
    assert (fresh.id, fresh.attempt) == (job_id, 2)

    # The first worker wakes up: its writes are fenced out and the new attempt is untouched.
    with Session(migrated_engine) as db:
        assert not queue.renew(db, stale, LEASE)
        assert not queue.complete(db, stale)
        assert not queue.fail(db, stale, "late", retry=False)
    stored = row(migrated_engine, job_id)
    assert (stored.state, stored.lease_owner, stored.attempt) == ("running", "worker-b", 2)


def test_expired_lease_on_last_attempt_fails_the_job(migrated_engine: Engine) -> None:
    job_id = add(migrated_engine, "group-1", max_attempts=1)
    claim(migrated_engine)
    expire_lease(migrated_engine, job_id)

    with Session(migrated_engine) as db:
        assert queue.expire_leases(db) == 1
        db.commit()

    stored = row(migrated_engine, job_id)
    assert (stored.state, stored.error) == ("failed", queue.LEASE_EXPIRED)


def test_renew_extends_the_lease(migrated_engine: Engine) -> None:
    job_id = add(migrated_engine, "group-1")
    job = claim(migrated_engine)
    expire_lease(migrated_engine, job_id)

    with Session(migrated_engine) as db:
        assert queue.renew(db, job, LEASE)

    assert nothing_to_claim(migrated_engine)
    assert row(migrated_engine, job_id).state == "running"


@pytest.mark.parametrize(
    "values",
    [
        {"state": "running"},
        {"lease_owner": "worker-a"},
        {"attempt": 6},
    ],
)
def test_schema_rejects_inconsistent_jobs(migrated_engine: Engine, values: dict[str, Any]) -> None:
    job_id = add(migrated_engine, "group-1")

    with Session(migrated_engine) as db:
        with pytest.raises(IntegrityError):
            db.execute(update(CollectionJob).where(CollectionJob.id == job_id).values(**values))
