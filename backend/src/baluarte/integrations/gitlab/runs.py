"""Collection run bookkeeping: one ``collection_run`` row per job attempt (BAL-015).

A run is opened before GitLab is called and closed with its outcome, so a failed collection is
always recorded and can never leave an earlier success looking current. A run left ``running``
by a worker that died is closed as failed when a later attempt of the same kind starts, but only
if its own job attempt is no longer running.
"""

from typing import Any, Literal

from sqlalchemy import Engine, func, or_, select, update
from sqlalchemy.orm import Session

from baluarte.db.models import CollectionJob, CollectionRun
from baluarte.integrations.gitlab.instances import instance_for
from baluarte.jobs.queue import ClaimedJob

RunKind = Literal["capabilities", "groups", "projects"]
RunOutcome = Literal["succeeded", "partial", "failed"]

INTERRUPTED = "interrupted: the worker stopped before the run finished"


def start_run(engine: Engine, base_url: str, kind: RunKind, job: ClaimedJob | None) -> int:
    with Session(engine) as db:
        instance_id = instance_for(db, base_url).id
        job_still_running = (
            select(CollectionJob.id)
            .where(
                CollectionJob.id == CollectionRun.collection_job_id,
                CollectionJob.state == "running",
                CollectionJob.attempt == CollectionRun.attempt,
            )
            .exists()
        )
        db.execute(
            update(CollectionRun)
            .where(
                CollectionRun.instance_id == instance_id,
                CollectionRun.kind == kind,
                CollectionRun.status == "running",
                or_(CollectionRun.collection_job_id.is_(None), ~job_still_running),
            )
            .values(status="failed", finished_at=func.now(), error=INTERRUPTED)
        )
        run = CollectionRun(
            instance_id=instance_id,
            kind=kind,
            collection_job_id=job.id if job else None,
            attempt=job.attempt if job else None,
        )
        db.add(run)
        db.commit()
        return run.id


def finish_run(
    engine: Engine,
    run_id: int,
    status: RunOutcome,
    *,
    seen: int = 0,
    counts: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Close the run. A run already closed as interrupted is left as it is."""
    with Session(engine) as db:
        db.execute(
            update(CollectionRun)
            .where(CollectionRun.id == run_id, CollectionRun.status == "running")
            .values(
                status=status,
                finished_at=func.now(),
                seen=seen,
                counts=counts or {},
                error=error,
            )
        )
        db.commit()
