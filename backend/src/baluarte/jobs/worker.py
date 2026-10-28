"""The ``baluarte-worker`` process: claims jobs, runs handlers and records outcomes.

The worker claims only kinds it has a handler for, so during a rolling upgrade an older worker
leaves newer kinds queued for a newer worker. On SIGTERM it finishes the current job and exits;
if it is killed first, the lease expires and another worker retries the job.
"""

import argparse
import logging
import os
import signal
import socket
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from types import FrameType

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import Session

from baluarte.config import ConfigurationError, database_url
from baluarte.db.migrate import head_revisions, wait_for_database
from baluarte.jobs import queue
from baluarte.jobs.handlers import HANDLERS, Handler

logger = logging.getLogger(__name__)

HEARTBEAT_FILE = Path("/tmp/baluarte-worker.heartbeat")  # noqa: S108  (tmpfs in containers)
# The heartbeat file is touched at least this often, between jobs and while a job runs.
HEARTBEAT_INTERVAL = timedelta(seconds=30)
HEALTHY_WITHIN = timedelta(seconds=120)


def _seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number of seconds") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return value


class Worker:
    def __init__(
        self,
        engine: Engine,
        handlers: Mapping[str, Handler],
        *,
        lease: timedelta = timedelta(minutes=5),
        poll_interval: float = 5.0,
        heartbeat_file: Path | None = None,
        owner: str | None = None,
    ) -> None:
        self.engine = engine
        self.handlers = dict(handlers)
        self.lease = lease
        self.poll_interval = poll_interval
        self.heartbeat_file = heartbeat_file
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.stop = threading.Event()
        # Renew well before expiry, and often enough to keep the liveness heartbeat fresh.
        self.renew_interval = min(lease / 3, HEARTBEAT_INTERVAL).total_seconds()

    def beat(self) -> None:
        if self.heartbeat_file is not None:
            self.heartbeat_file.touch()

    def run(self) -> None:
        logger.info("worker %s handling kinds: %s", self.owner, sorted(self.handlers) or "none")
        while not self.stop.is_set():
            self.beat()
            try:
                worked = self.run_once()
            except SQLAlchemyError as exc:
                logger.warning("worker: database error (%s); retrying", type(exc).__name__)
                worked = False
            if not worked:
                self.stop.wait(self.poll_interval)
        logger.info("worker %s stopped", self.owner)

    def run_once(self) -> bool:
        """Claim and run at most one job. Returns whether a job was claimed."""
        with Session(self.engine) as db:
            job = queue.claim(db, self.owner, self.lease, self.handlers.keys())
        if job is None:
            return False
        logger.info("job %s (%s): attempt %s started", job.id, job.kind, job.attempt)
        renewing = threading.Event()
        renewer = threading.Thread(target=self._renew, args=(job, renewing), daemon=True)
        renewer.start()
        try:
            outcome = self._execute(job)
        finally:
            renewing.set()
            renewer.join()
        with Session(self.engine) as db:
            if outcome is None:
                recorded = queue.complete(db, job)
            else:
                message, retry = outcome
                recorded = queue.fail(db, job, message, retry=retry)
        if recorded:
            logger.info(
                "job %s (%s): attempt %s %s",
                job.id,
                job.kind,
                job.attempt,
                "succeeded" if outcome is None else "failed",
            )
        return True

    def _execute(self, job: queue.ClaimedJob) -> tuple[str, bool] | None:
        """Run the handler; return None on success or (stored message, retry) on failure."""
        try:
            self.handlers[job.kind](job)
        except queue.PermanentJobError as exc:
            return str(exc) or type(exc).__name__, False
        except queue.JobError as exc:
            return str(exc) or type(exc).__name__, True
        except Exception as exc:
            # The exception text may contain URLs, tokens or response bodies: record the type.
            logger.error("job %s (%s): unexpected %s", job.id, job.kind, type(exc).__name__)
            return f"unexpected error: {type(exc).__name__}", True
        return None

    def _renew(self, job: queue.ClaimedJob, done: threading.Event) -> None:
        while not done.wait(self.renew_interval):
            self.beat()
            try:
                with Session(self.engine) as db:
                    if not queue.renew(db, job, self.lease):
                        return
            except SQLAlchemyError as exc:
                logger.warning("job %s: lease renewal failed (%s)", job.id, type(exc).__name__)


def wait_for_migrations(engine: Engine, timeout: float, interval: float = 2.0) -> None:
    """Block until the schema is at the revision this code expects, or raise TimeoutError."""
    deadline = time.monotonic() + timeout
    heads = head_revisions()
    while True:
        try:
            with engine.connect() as connection:
                applied = set(
                    connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
                )
        except ProgrammingError:
            applied = set()
        if applied == heads:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("database schema is not at the expected revision")
        logger.info("waiting for migrations (%s of %s)", sorted(applied), sorted(heads))
        time.sleep(interval)


def healthcheck(path: Path = HEARTBEAT_FILE, within: timedelta = HEALTHY_WITHIN) -> int:
    """Exit status for container probes: 0 if the worker loop has beaten recently."""
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return 1
    return 0 if age <= within.total_seconds() else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Baluarte background worker.")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["run", "healthcheck"],
        default="run",
        help="run the worker (default) or check its heartbeat for a container probe",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="seconds to wait for the database and migrations at startup (default 120)",
    )
    args = parser.parse_args(argv)
    if args.command == "healthcheck":
        return healthcheck()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        poll = _seconds("BALUARTE_WORKER_POLL_SECONDS", 5.0)
        lease = timedelta(seconds=_seconds("BALUARTE_WORKER_LEASE_SECONDS", 300.0))
        engine = create_engine(
            database_url(), pool_pre_ping=True, connect_args={"connect_timeout": 5}
        )
    except ConfigurationError as exc:
        logger.error("%s", exc)
        return 2

    worker = Worker(
        engine, HANDLERS, lease=lease, poll_interval=poll, heartbeat_file=HEARTBEAT_FILE
    )
    # Healthy while waiting for the database and migrations; the --timeout bounds that wait.
    worker.beat()

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        logger.info("received %s; finishing the current job", signal.Signals(signum).name)
        worker.stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        wait_for_database(engine, args.timeout)
        wait_for_migrations(engine, args.timeout)
        worker.run()
    except OperationalError as exc:
        logger.error("database unavailable (%s)", type(exc.orig).__name__)
        return 1
    except TimeoutError as exc:
        logger.error("%s", exc)
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
