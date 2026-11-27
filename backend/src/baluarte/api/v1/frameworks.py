"""Framework versions and their controls (roadmap M15.1, docs/EVIDENCE_MANAGEMENT.md).

Each framework edition is data: records pin the version they were made against, and a new
version is added beside the old one. An organization may describe a control in its own words for
its own use; descriptions are kept per workspace, audited, and never part of the catalogue.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.compliance.catalogue import FrameworkId
from baluarte.controls import versions
from baluarte.db.models import (
    MAX_CONTROL_DESCRIPTION,
    AppUser,
    FrameworkControl,
    FrameworkControlDescription,
    FrameworkVersion,
)

router = APIRouter(tags=["controls"])

VersionLabel = Annotated[str, Path(min_length=1, max_length=40)]
ControlId = Annotated[str, Path(min_length=1, max_length=20)]


class VersionOut(BaseModel):
    framework: FrameworkId
    version: str
    edition: str
    publisher: str
    licence_note: str
    # Whether the catalogue may carry the publisher's control text; titles are Baluarte's
    # paraphrases otherwise.
    text_redistributable: bool
    current: bool
    added_at: datetime
    controls: int


class VersionsPage(BaseModel):
    versions: list[VersionOut]


class DescriptionOut(BaseModel):
    text: str
    added_by: str | None
    added_at: datetime


class FrameworkControlOut(BaseModel):
    control_id: str
    title: str
    area: str
    # ``baluarte`` (a paraphrase) or ``publisher`` (the standard's own text, where allowed).
    text_source: str
    # The organization's own description, for its own use; null when none is in force.
    description: DescriptionOut | None


class VersionDetail(VersionOut):
    items: list[FrameworkControlOut]


class DescriptionIn(BaseModel):
    # Empty clears the description.
    description: Annotated[str, Field(max_length=MAX_CONTROL_DESCRIPTION)]


def _version_out(row: FrameworkVersion, count: int) -> VersionOut:
    return VersionOut(
        framework=row.framework,  # type: ignore[arg-type]
        version=row.version,
        edition=row.edition,
        publisher=row.publisher,
        licence_note=row.licence_note,
        text_redistributable=row.text_redistributable,
        current=row.current,
        added_at=row.added_at,
        controls=count,
    )


def _description_out(db: Session, row: FrameworkControlDescription | None) -> DescriptionOut | None:
    if row is None:
        return None
    user = db.get(AppUser, row.added_by_user_id)
    return DescriptionOut(
        text=row.description,
        added_by=user.display_name if user else None,
        added_at=row.added_at,
    )


def _found(db: Session, framework: str, label: str) -> FrameworkVersion:
    row = versions.version(db, framework, label)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such framework version")
    return row


def _control_out(
    db: Session, row: FrameworkControl, described: FrameworkControlDescription | None
) -> FrameworkControlOut:
    return FrameworkControlOut(
        control_id=row.control_id,
        title=row.title,
        area=row.area,
        text_source=row.text_source,
        description=_description_out(db, described),
    )


@router.get(
    "/frameworks",
    response_model=VersionsPage,
    summary="Framework versions",
    description="Every edition of each framework Baluarte knows, with its publisher, licence "
    "note and number of controls. One version per framework is current; records keep the "
    "version they were made against.",
)
def list_versions(
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
) -> VersionsPage:
    counts = dict(
        db.execute(
            select(FrameworkControl.framework_version_id, func.count()).group_by(
                FrameworkControl.framework_version_id
            )
        ).all()
    )
    return VersionsPage(
        versions=[_version_out(row, counts.get(row.id, 0)) for row in versions.versions(db)]
    )


@router.get(
    "/frameworks/{framework}/{version}",
    response_model=VersionDetail,
    summary="One framework version and its controls",
    description="The controls of a framework version, with the organization's own description "
    "of each where one is in force.",
    responses={404: {"description": "No such framework version"}},
)
def read_version(
    framework: FrameworkId,
    version: VersionLabel,
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
) -> VersionDetail:
    row = _found(db, framework, version)
    rows = versions.controls(db, row.id)
    described = versions.descriptions(db, [c.id for c in rows])
    return VersionDetail(
        **_version_out(row, len(rows)).model_dump(),
        items=[_control_out(db, c, described.get(c.id)) for c in rows],
    )


@router.put(
    "/frameworks/{framework}/{version}/controls/{control_id}/description",
    response_model=FrameworkControlOut,
    summary="Describe a control in the organization's own words",
    description="Records the organization's description of a control, for its own use; an "
    "empty description clears it. Each description is kept, the latest is in force, and it "
    "never becomes part of the built-in catalogue. Requires the `X-CSRF-Token` header. Audited.",
    responses={404: {"description": "No such framework version or control"}},
)
def describe_control(
    framework: FrameworkId,
    version: VersionLabel,
    control_id: ControlId,
    body: DescriptionIn,
    principal: Annotated[Principal, Depends(require(Permission.POLICY_WRITE))],
    db: Annotated[Session, Depends(database)],
) -> FrameworkControlOut:
    owner = _found(db, framework, version)
    row = versions.control_row(db, owner.id, control_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such control in that version")
    versions.describe(db, row, body.description, principal.actor, principal.user_id)
    db.commit()
    return _control_out(db, row, versions.descriptions(db, [row.id]).get(row.id))
