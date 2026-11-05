"""Reading recorded changes, now or as they stood at a past moment.

A change's observations share its project, source and ``source_ref``, so its version at a moment
is the newest of them collected by then, and a change exists from when it was recorded. Its
outcome then is judged from that version, the same way as at collection.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from baluarte.db.models import ChangeRecord, GitLabProject, Observation
from baluarte.integrations.gitlab.changes import CHANGE_SOURCES, by_sha, judge, source_ref


def record_ref(record: ChangeRecord, project_gitlab_id: int) -> str:
    """The ``source_ref`` of the change's observations."""
    return source_ref(
        project_gitlab_id,
        {"kind": record.kind, "iid": record.iid, "id": record.deployment_id, "sha": record.sha},
    )


def history(db: Session, record: ChangeRecord, project_gitlab_id: int) -> list[Observation]:
    """Every observation of the change, newest first."""
    return list(
        db.scalars(
            select(Observation)
            .where(
                Observation.project_id == record.project_id,
                Observation.source == CHANGE_SOURCES[record.kind],
                Observation.source_ref == record_ref(record, project_gitlab_id),
            )
            .order_by(Observation.collected_at.desc(), Observation.id.desc())
        )
    )


def versions_at(
    db: Session, rows: Sequence[tuple[ChangeRecord, GitLabProject]], as_of: datetime
) -> dict[int, Observation]:
    """Each change's newest observation collected by ``as_of``, by change ID."""
    keys = {
        (record.project_id, CHANGE_SOURCES[record.kind], record_ref(record, project.gitlab_id)): (
            record.id
        )
        for record, project in rows
    }
    if not keys:
        return {}
    found = db.scalars(
        select(Observation)
        .where(
            tuple_(Observation.project_id, Observation.source, Observation.source_ref).in_(
                list(keys)
            ),
            Observation.collected_at <= as_of,
        )
        .ext(distinct_on(Observation.project_id, Observation.source, Observation.source_ref))
        .order_by(
            Observation.project_id,
            Observation.source,
            Observation.source_ref,
            Observation.collected_at.desc(),
            Observation.id.desc(),
        )
    )
    return {keys[(o.project_id or 0, o.source, o.source_ref or "")]: o for o in found}


def project_changes_at(
    db: Session, project: GitLabProject, as_of: datetime
) -> list[dict[str, Any]]:
    """The payloads of the project's merge requests and direct pushes as they stood then."""
    rows = [
        (record, project)
        for record in db.scalars(
            select(ChangeRecord).where(
                ChangeRecord.project_id == project.id,
                ChangeRecord.kind != "deployment",
                ChangeRecord.created_at <= as_of,
            )
        )
    ]
    return [o.payload for o in versions_at(db, rows, as_of).values() if o.payload is not None]


def outcome_at(
    db: Session,
    record: ChangeRecord,
    project: GitLabProject,
    payload: dict[str, Any],
    as_of: datetime,
) -> tuple[str, list[str]]:
    """The change's outcome judged from its version then."""
    shas = by_sha(project_changes_at(db, project, as_of)) if record.kind == "deployment" else {}
    return judge(payload, shas, project.default_branch)
