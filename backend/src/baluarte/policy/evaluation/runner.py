"""The ``policy.evaluate`` job: evaluate every project's effective policies and store the results.

Projects are read in pages of ``PAGE_SIZE``; each page is evaluated, stored and committed in
one transaction, so memory stays bounded and a failure keeps the pages already stored (each is
a true evaluation at its time). Every attempt opens an ``evaluation_run`` first and closes it
with its counts, so a failed evaluation is visible in the evaluation freshness.

The job is queued after every project discovery that finishes (succeeded or partial), after a
policy assignment changes, and on demand. Page transactions take an advisory lock per instance,
so two runs never update the same findings at once.
"""

import logging
import uuid
from datetime import timedelta
from typing import Literal

from sqlalchemy import Engine, exists, func, or_, select, update
from sqlalchemy.orm import Session

from baluarte.db.models import (
    CollectionJob,
    EvaluationRun,
    Finding,
    GitLabInstance,
    GitLabProject,
)
from baluarte.integrations.gitlab.instances import instance_for
from baluarte.integrations.gitlab.settings import GitLabSettings, load_gitlab_settings
from baluarte.jobs import queue
from baluarte.jobs.queue import ClaimedJob, PermanentJobError
from baluarte.policy.assignments import resolve_project
from baluarte.policy.evaluation.project import (
    capability_statuses,
    collection_contexts,
    evaluate_project,
)
from baluarte.policy.evaluation.store import Tally, store_project
from baluarte.policy.exceptions import expire_due
from baluarte.settings import ConfigurationError

logger = logging.getLogger(__name__)

EVALUATE = "policy.evaluate"
PAGE_SIZE = 200
MAX_ATTEMPTS = 3
# First key of the two-key advisory lock that serializes evaluation writes per instance.
_LOCK_CLASS = 21021

INTERRUPTED = "interrupted: the worker stopped before the run finished"


def _settings() -> GitLabSettings:
    try:
        settings = load_gitlab_settings()
    except ConfigurationError as exc:
        raise PermanentJobError(str(exc)) from exc
    if settings is None:
        raise PermanentJobError("GitLab is not configured (BALUARTE_GITLAB_URL is not set)")
    return settings


def start_run(engine: Engine, instance_id: int, job: ClaimedJob | None) -> int:
    """Open a run, closing runs left ``running`` by attempts that are no longer running."""
    with Session(engine) as db:
        job_still_running = (
            select(CollectionJob.id)
            .where(
                CollectionJob.id == EvaluationRun.collection_job_id,
                CollectionJob.state == "running",
                CollectionJob.attempt == EvaluationRun.attempt,
            )
            .exists()
        )
        db.execute(
            update(EvaluationRun)
            .where(
                EvaluationRun.instance_id == instance_id,
                EvaluationRun.status == "running",
                or_(EvaluationRun.collection_job_id.is_(None), ~job_still_running),
            )
            .values(status="failed", finished_at=func.now(), error=INTERRUPTED)
        )
        run = EvaluationRun(
            instance_id=instance_id,
            collection_job_id=job.id if job else None,
            attempt=job.attempt if job else None,
        )
        db.add(run)
        db.commit()
        return run.id


def finish_run(
    engine: Engine,
    run_id: int,
    status: Literal["succeeded", "failed"],
    projects: int,
    tally: Tally,
    error: str | None = None,
) -> None:
    """Close the run. A run already closed as interrupted is left as it is."""
    with Session(engine) as db:
        db.execute(
            update(EvaluationRun)
            .where(EvaluationRun.id == run_id, EvaluationRun.status == "running")
            .values(
                status=status,
                finished_at=func.now(),
                projects=projects,
                counts=tally.counts(),
                error=error,
            )
        )
        db.commit()


def _page(db: Session, instance_id: int, after_id: int) -> list[GitLabProject]:
    """Active projects, and any other project with an open or excepted finding.

    So a finding on a project that left the inventory shows its latest result as unknown.
    """
    has_open_finding = exists().where(
        Finding.project_id == GitLabProject.id, Finding.state.in_(("open", "excepted"))
    )
    return list(
        db.scalars(
            select(GitLabProject)
            .where(
                GitLabProject.instance_id == instance_id,
                GitLabProject.id > after_id,
                or_(GitLabProject.state == "active", has_open_finding),
            )
            .order_by(GitLabProject.id)
            .limit(PAGE_SIZE)
        )
    )


def evaluate_all(
    engine: Engine, settings: GitLabSettings, job: ClaimedJob | None = None
) -> tuple[int, Tally]:
    """Evaluate every project; returns the run ID and its counts."""
    with Session(engine) as db:
        instance_id = instance_for(db, settings.url).id
        db.commit()
    run_id = start_run(engine, instance_id, job)
    tally = Tally()
    projects = 0
    try:
        with Session(engine) as db:
            # Exceptions past their expiry end before anything is judged against them.
            db.execute(select(func.pg_advisory_xact_lock(_LOCK_CLASS, instance_id)))
            expired, reopened = expire_due(db)
            db.commit()
            if reopened:
                tally.findings["exception_expired"] += reopened
            if expired:
                logger.info("policy exceptions expired: %s", expired)
        with Session(engine) as db:
            # Read once, so every page is judged against the same collection freshness.
            collections = collection_contexts(db, instance_id, settings)
        after_id = 0
        while True:
            with Session(engine) as db:
                db.execute(select(func.pg_advisory_xact_lock(_LOCK_CLASS, instance_id)))
                instance = db.get_one(GitLabInstance, instance_id)
                capabilities = capability_statuses(db, instance_id)
                page = _page(db, instance_id, after_id)
                if not page:
                    break
                for project in page:
                    resolution = resolve_project(db, instance, project)
                    results = evaluate_project(db, project, resolution, collections, capabilities)
                    store_project(db, instance_id, project, resolution, results, run_id, tally)
                db.commit()
                after_id = page[-1].id
                projects += len(page)
    except Exception as exc:
        finish_run(
            engine, run_id, "failed", projects, tally, f"unexpected error: {type(exc).__name__}"
        )
        raise
    finish_run(engine, run_id, "succeeded", projects, tally)
    return run_id, tally


def evaluate(job: ClaimedJob, engine: Engine) -> None:
    settings = _settings()
    run_id, tally = evaluate_all(engine, settings, job)
    logger.info("policy evaluation run %s: %s", run_id, tally.counts(), extra={"job_id": job.id})


def queue_after_collection(db: Session, collection_run_id: int) -> int:
    """Queue an evaluation for a finished project discovery; once per collection run."""
    return queue.enqueue(
        db, EVALUATE, f"{EVALUATE}:after-run:{collection_run_id}", max_attempts=MAX_ATTEMPTS
    )


def request_evaluation(db: Session, reason: str) -> int | None:
    """Queue an evaluation unless one is already waiting to start; it will see this change.

    Returns the new job's ID, or None when a queued evaluation already covers the change.
    """
    waiting = db.scalars(
        select(CollectionJob.id)
        .where(CollectionJob.kind == EVALUATE, CollectionJob.state == "queued")
        .limit(1)
    ).first()
    if waiting is not None:
        return None
    return queue.enqueue(
        db,
        EVALUATE,
        f"{EVALUATE}:{reason}:{uuid.uuid4().hex}",
        max_attempts=MAX_ATTEMPTS,
        # Let a burst of changes share one run.
        delay=timedelta(seconds=10),
    )
