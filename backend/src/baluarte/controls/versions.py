"""Framework versions as data (roadmap M15.1, docs/EVIDENCE_MANAGEMENT.md#framework-versions-m151).

Each edition of a framework is a ``framework_version`` row and its controls ``framework_control``
rows; one version per framework is current. Records (mapping changes, issue mappings, and later
assessments and evidence) pin the version they were made against. The built-in catalogue
(``baluarte.controls.catalogue``) describes the current versions, and a test keeps the two equal.

An organization may describe a control in its own words for its own use. Each description is a
new row (the latest is in force, an empty one clears it) and is audited; it never becomes part of
the built-in catalogue or the evidence of what a framework says.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte.audit import record
from baluarte.db.models import (
    MAX_CONTROL_DESCRIPTION,
    FrameworkControl,
    FrameworkControlDescription,
    FrameworkVersion,
)
from baluarte.workspaces import current_workspace_id


class UnknownControl(LookupError):
    """No such control in that framework version."""


@dataclass(frozen=True)
class Description:
    text: str
    added_by_user_id: int
    added_at: str


def versions(db: Session, framework: str | None = None) -> list[FrameworkVersion]:
    query = select(FrameworkVersion).order_by(FrameworkVersion.framework, FrameworkVersion.id)
    if framework is not None:
        query = query.where(FrameworkVersion.framework == framework)
    return list(db.scalars(query))


def current(db: Session, framework: str) -> FrameworkVersion:
    """The framework's current version. Every known framework has one (seeded by migration)."""
    return db.scalars(
        select(FrameworkVersion).where(
            FrameworkVersion.framework == framework, FrameworkVersion.current
        )
    ).one()


def current_version_id(db: Session, framework: str) -> int:
    return current(db, framework).id


def version(db: Session, framework: str, label: str) -> FrameworkVersion | None:
    return db.scalars(
        select(FrameworkVersion).where(
            FrameworkVersion.framework == framework, FrameworkVersion.version == label
        )
    ).one_or_none()


def controls(db: Session, version_id: int) -> list[FrameworkControl]:
    return list(
        db.scalars(
            select(FrameworkControl)
            .where(FrameworkControl.framework_version_id == version_id)
            .order_by(FrameworkControl.id)
        )
    )


def control_row(db: Session, version_id: int, control_id: str) -> FrameworkControl | None:
    return db.scalars(
        select(FrameworkControl).where(
            FrameworkControl.framework_version_id == version_id,
            FrameworkControl.control_id == control_id,
        )
    ).one_or_none()


def descriptions(db: Session, control_ids: list[int]) -> dict[int, FrameworkControlDescription]:
    """The description in force for each control, by ``framework_control.id``; empty ones are
    left out."""
    if not control_ids:
        return {}
    rows = db.scalars(
        select(FrameworkControlDescription)
        .where(FrameworkControlDescription.framework_control_id.in_(control_ids))
        .order_by(FrameworkControlDescription.id)
    )
    latest: dict[int, FrameworkControlDescription] = {}
    for row in rows:
        latest[row.framework_control_id] = row
    return {key: row for key, row in latest.items() if row.description}


def describe(
    db: Session, row: FrameworkControl, text: str, actor: str, user_id: int
) -> FrameworkControlDescription:
    """Record the organization's description of a control (an empty text clears it)."""
    cleaned = text.strip()
    if len(cleaned) > MAX_CONTROL_DESCRIPTION:
        raise ValueError(f"A description is at most {MAX_CONTROL_DESCRIPTION} characters")
    added = FrameworkControlDescription(
        workspace_id=current_workspace_id(db),
        framework_control_id=row.id,
        description=cleaned,
        added_by=actor,
        added_by_user_id=user_id,
    )
    db.add(added)
    db.flush()
    owner = db.get_one(FrameworkVersion, row.framework_version_id)
    record(
        db,
        actor=actor,
        action="framework_control.described"
        if cleaned
        else "framework_control.description_cleared",
        resource_type="framework_control",
        resource_id=f"{owner.framework}:{owner.version}:{row.control_id}",
        details={"description_id": added.id, "length": len(cleaned)},
    )
    return added
