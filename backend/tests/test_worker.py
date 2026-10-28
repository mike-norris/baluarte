import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from baluarte.db.migrate import upgrade
from baluarte.db.models import CollectionJob
from baluarte.jobs import queue, worker
from baluarte.jobs.handlers import Handler
from baluarte.jobs.worker import Worker, healthcheck, wait_for_migrations


def test_healthcheck_reads_the_heartbeat_age(tmp_path: Path) -> None:
    beat = tmp_path / "heartbeat"
    assert healthcheck(beat) == 1

    beat.touch()
    assert healthcheck(beat) == 0

    stale = time.time() - 600
    os.utime(beat, (stale, stale))
    assert healthcheck(beat) == 1


@pytest.fixture(autouse=True)
def keep_signal_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("baluarte.jobs.worker.signal.signal", lambda *_: None)


@pytest.mark.parametrize("value", ["0", "-1", "soon"])
def test_worker_settings_are_validated(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("BALUARTE_DATABASE_URL", "postgresql://baluarte@127.0.0.1:1/baluarte")
    monkeypatch.setenv("BALUARTE_WORKER_POLL_SECONDS", value)

    assert worker.main([]) == 2


def test_worker_without_database_settings_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BALUARTE_DATABASE_URL", raising=False)

    assert worker.main([]) == 2


db = pytest.mark.db


def enqueue(engine: Engine, kind: str = "inventory.sync", key: str = "group-1") -> int:
    with Session(engine) as db:
        job_id = queue.enqueue(db, kind, key, {"group": key}, max_attempts=3)
        db.commit()
        return job_id


def job(engine: Engine, job_id: int) -> CollectionJob:
    with Session(engine, expire_on_commit=False) as db:
        return db.get_one(CollectionJob, job_id)


@db
def test_successful_handler_completes_the_job(migrated_engine: Engine) -> None:
    seen: list[queue.ClaimedJob] = []
    job_id = enqueue(migrated_engine)

    assert Worker(migrated_engine, {"inventory.sync": seen.append}).run_once()

    assert [claimed.payload for claimed in seen] == [{"group": "group-1"}]
    assert job(migrated_engine, job_id).state == "succeeded"


@db
def test_idle_worker_claims_nothing(migrated_engine: Engine) -> None:
    enqueue(migrated_engine, kind="export.build")

    assert not Worker(migrated_engine, {"inventory.sync": lambda _: None}).run_once()
    assert not Worker(migrated_engine, {}).run_once()


def _raise(error: Exception) -> Handler:
    def handler(_: queue.ClaimedJob) -> None:
        raise error

    return handler


@db
@pytest.mark.parametrize(
    ("error", "state", "message"),
    [
        (queue.JobError("GitLab returned 502"), "queued", "GitLab returned 502"),
        (queue.PermanentJobError("group was deleted"), "failed", "group was deleted"),
        (
            RuntimeError("https://gitlab.example.test/?private_token=glpat-secret"),
            "queued",
            "unexpected error: RuntimeError",
        ),
    ],
    ids=["retryable", "permanent", "unexpected"],
)
def test_handler_failures_are_recorded(
    migrated_engine: Engine,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    state: str,
    message: str,
) -> None:
    job_id = enqueue(migrated_engine)

    Worker(migrated_engine, {"inventory.sync": _raise(error)}).run_once()

    stored = job(migrated_engine, job_id)
    assert (stored.state, stored.error) == (state, message)
    assert "glpat-secret" not in caplog.text


@db
def test_lease_is_renewed_while_the_handler_runs(migrated_engine: Engine) -> None:
    job_id = enqueue(migrated_engine)
    lease_seen: list[datetime] = []

    def slow(_: queue.ClaimedJob) -> None:
        for _round in range(2):
            lease_until = job(migrated_engine, job_id).lease_until
            assert lease_until is not None
            lease_seen.append(lease_until)
            time.sleep(0.6)

    runner = Worker(migrated_engine, {"inventory.sync": slow}, lease=timedelta(seconds=0.6))
    assert runner.renew_interval == pytest.approx(0.2)

    runner.run_once()

    first, last = lease_seen
    assert last > first
    assert job(migrated_engine, job_id).state == "succeeded"


@db
def test_run_loop_stops_when_asked(migrated_engine: Engine, tmp_path: Path) -> None:
    beat = tmp_path / "heartbeat"
    for key in ("a", "b"):
        enqueue(migrated_engine, key=key)
    runner = Worker(
        migrated_engine,
        {"inventory.sync": lambda _: None},
        poll_interval=0.05,
        heartbeat_file=beat,
    )

    thread = threading.Thread(target=runner.run)
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with Session(migrated_engine) as db:
            states = set(db.scalars(select(CollectionJob.state)))
        if states == {"succeeded"}:
            break
        time.sleep(0.05)
    runner.stop.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert states == {"succeeded"}
    assert healthcheck(beat) == 0


@db
def test_waits_for_migrations(empty_engine: Engine) -> None:
    with pytest.raises(TimeoutError):
        wait_for_migrations(empty_engine, timeout=0.2, interval=0.05)

    upgrade(empty_engine)
    wait_for_migrations(empty_engine, timeout=0.2, interval=0.05)


def test_unreachable_database_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BALUARTE_DATABASE_URL", "postgresql://baluarte@127.0.0.1:1/baluarte")
    monkeypatch.delenv("BALUARTE_DATABASE_PASSWORD_FILE", raising=False)
    monkeypatch.delenv("BALUARTE_WORKER_POLL_SECONDS", raising=False)

    assert worker.main(["--timeout", "0"]) == 1
