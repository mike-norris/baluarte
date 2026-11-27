"""Evidence items: uploads and references people add (roadmap M15.2,
docs/EVIDENCE_MANAGEMENT.md#evidence-items-m152)."""

import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.compliance.catalogue import FrameworkId
from baluarte.db.models import (
    EVIDENCE_TITLE,
    MAX_EVIDENCE_DESCRIPTION,
    MAX_EVIDENCE_FILE_BYTES,
    MAX_EVIDENCE_URL,
    AppUser,
    CapturedFile,
    EvidenceItem,
)
from baluarte.evidence import items as service
from baluarte.evidence.items import Actor, ControlRef, EvidenceRefused, Fields, NotAllowed

router = APIRouter(tags=["evidence"])

ItemId = Annotated[int, Path(ge=1)]
CONTROL = re.compile(r"^(?P<framework>[a-z0-9_]+):(?P<control>[A-Za-z0-9.()-]{1,20})$")


class UserRef(BaseModel):
    user_id: int
    display_name: str | None


class ControlIn(BaseModel):
    framework: FrameworkId
    control_id: Annotated[str, Field(min_length=1, max_length=20)]
    # The framework version; the current one when omitted.
    version: Annotated[str | None, Field(min_length=1, max_length=40)] = None


class ControlLink(BaseModel):
    framework: str
    version: str
    control_id: str


class ItemIn(BaseModel):
    title: Annotated[str, Field(min_length=EVIDENCE_TITLE[0], max_length=EVIDENCE_TITLE[1])]
    description: Annotated[str, Field(max_length=MAX_EVIDENCE_DESCRIPTION)] = ""
    owner_user_id: int | None = None
    period_start: date | None = None
    period_end: date | None = None
    # The last day the item counts as fresh.
    valid_until: date | None = None
    restricted: bool = False
    controls: Annotated[list[ControlIn], Field(max_length=50)] = []
    # A reference's address; omitted for a file.
    url: Annotated[str | None, Field(max_length=MAX_EVIDENCE_URL)] = None


class NewItemIn(ItemIn):
    kind: Literal["file", "reference"]
    # An accepted item this one corrects.
    supersedes_id: int | None = None


class DecisionIn(BaseModel):
    note: Annotated[str | None, Field(max_length=2000)] = None


class ItemOut(BaseModel):
    id: int
    kind: Literal["file", "reference"]
    title: str
    description: str
    owner: UserRef
    period_start: date | None
    period_end: date | None
    valid_until: date | None
    freshness: Literal["fresh", "stale", "unknown"]
    restricted: bool
    state: Literal["draft", "submitted", "accepted", "rejected"]
    controls: list[ControlLink]
    supersedes_id: int | None
    superseded_by_id: int | None
    filename: str | None
    content_type: str | None
    size_bytes: int | None
    sha256: str | None
    # Set when retention removed the file; ``sha256`` then gives the file it held.
    file_removed_at: datetime | None
    url: str | None
    check_status: Literal["reachable", "unreachable"] | None
    checked_at: datetime | None
    check_http_status: int | None
    check_sha256: str | None
    added_by: UserRef
    added_at: datetime
    submitted_at: datetime | None
    reviewed_by: UserRef | None
    reviewed_at: datetime | None
    review_note: str | None
    development_review: bool


class ItemsPage(BaseModel):
    items: list[ItemOut]
    # Whether the deployment lets references be checked (any host allowed).
    checks_enabled: bool


def _actor(principal: Principal) -> Actor:
    return Actor(principal.user_id, principal.actor)


def _reviewer(principal: Principal) -> bool:
    return Permission.EVIDENCE_REVIEW in principal.permissions


def _refused(error: EvidenceRefused) -> HTTPException:
    code = status.HTTP_422_UNPROCESSABLE_CONTENT if error.invalid else status.HTTP_409_CONFLICT
    return HTTPException(code, detail={"code": error.code, "message": str(error)})


def _forbidden(error: NotAllowed) -> HTTPException:
    return HTTPException(
        status.HTTP_403_FORBIDDEN, detail={"code": "not_allowed", "message": str(error)}
    )


def _outs(db: Session, rows: Sequence[EvidenceItem]) -> list[ItemOut]:
    ids = [row.id for row in rows]
    links = service.controls_of(db, ids)
    newer = service.superseded_by(db, ids)
    people = (
        {row.owner_user_id for row in rows}
        | {row.added_by_user_id for row in rows}
        | {row.reviewed_by_user_id for row in rows if row.reviewed_by_user_id is not None}
    )
    names = dict(
        db.execute(select(AppUser.id, AppUser.display_name).where(AppUser.id.in_(people))).all()
    )
    today: date = db.execute(select(func.current_date())).scalar_one()

    def ref(user_id: int) -> UserRef:
        return UserRef(user_id=user_id, display_name=names.get(user_id))

    return [
        ItemOut(
            id=row.id,
            kind=row.kind,  # type: ignore[arg-type]
            title=row.title,
            description=row.description,
            owner=ref(row.owner_user_id),
            period_start=row.period_start,
            period_end=row.period_end,
            valid_until=row.valid_until,
            freshness=service.freshness(row, today),
            restricted=row.restricted,
            state=row.state,  # type: ignore[arg-type]
            controls=[
                ControlLink(framework=f, version=v, control_id=c) for f, v, c in links[row.id]
            ],
            supersedes_id=row.supersedes_id,
            superseded_by_id=newer.get(row.id),
            filename=row.filename,
            content_type=row.content_type,
            size_bytes=row.size_bytes,
            sha256=row.file_sha256 or row.removed_sha256,
            file_removed_at=row.file_removed_at,
            url=row.url,
            check_status=row.check_status,  # type: ignore[arg-type]
            checked_at=row.checked_at,
            check_http_status=row.check_http_status,
            check_sha256=row.check_sha256,
            added_by=ref(row.added_by_user_id),
            added_at=row.added_at,
            submitted_at=row.submitted_at,
            reviewed_by=ref(row.reviewed_by_user_id)
            if row.reviewed_by_user_id is not None
            else None,
            reviewed_at=row.reviewed_at,
            review_note=row.review_note,
            development_review=row.development_review,
        )
        for row in rows
    ]


def _out(db: Session, row: EvidenceItem) -> ItemOut:
    db.refresh(row)
    [out] = _outs(db, [row])
    return out


def _found(db: Session, item_id: int, principal: Principal) -> EvidenceItem:
    row = db.get(EvidenceItem, item_id)
    if row is None or not service.visible(row, principal.user_id, _reviewer(principal)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such evidence item")
    return row


def _fields(body: ItemIn) -> Fields:
    return Fields(
        title=body.title,
        description=body.description,
        owner_user_id=body.owner_user_id,
        period_start=body.period_start,
        period_end=body.period_end,
        valid_until=body.valid_until,
        restricted=body.restricted,
        controls=[ControlRef(c.framework, c.control_id, c.version) for c in body.controls],
        url=body.url,
    )


@router.get(
    "/evidence-items",
    response_model=ItemsPage,
    summary="Evidence items people added",
    description="Uploads and references, newest first (at most 500), with their controls, "
    "freshness and review. Filter by `state` and by `control` (`framework:control_id`, any "
    "version). Restricted items are listed only for reviewers and whoever added them.",
)
def list_items(
    principal: Annotated[Principal, Depends(require(Permission.EVIDENCE_READ))],
    db: Annotated[Session, Depends(database)],
    state: Literal["draft", "submitted", "accepted", "rejected"] | None = None,
    control: Annotated[str | None, Query(max_length=60)] = None,
) -> ItemsPage:
    ref = None
    if control is not None:
        match = CONTROL.match(control)
        if match is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "control is framework:id")
        ref = ControlRef(match["framework"], match["control"])
    rows = service.items(
        db, user_id=principal.user_id, reviewer=_reviewer(principal), state=state, control=ref
    )
    return ItemsPage(items=_outs(db, rows), checks_enabled=bool(service.allowed_hosts()))


@router.get(
    "/evidence-items/{item_id}",
    response_model=ItemOut,
    summary="One evidence item",
    responses={404: {"description": "No such item, or it is restricted"}},
)
def read_item(
    item_id: ItemId,
    principal: Annotated[Principal, Depends(require(Permission.EVIDENCE_READ))],
    db: Annotated[Session, Depends(database)],
) -> ItemOut:
    return _out(db, _found(db, item_id, principal))


@router.post(
    "/evidence-items",
    response_model=ItemOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add an evidence item as a draft",
    description="A `file` (upload its file next) or a `reference` (with `url`), linked to "
    "controls (the current framework version unless `version` is given). `supersedes_id` "
    "names an accepted item this one corrects. Requires the `X-CSRF-Token` header. Audited.",
)
def add_item(
    body: NewItemIn,
    principal: Annotated[Principal, Depends(require(Permission.EVIDENCE_MANAGE))],
    db: Annotated[Session, Depends(database)],
) -> ItemOut:
    try:
        row = service.create(db, body.kind, _fields(body), _actor(principal), body.supersedes_id)
    except EvidenceRefused as error:
        raise _refused(error) from None
    db.commit()
    return _out(db, row)


@router.put(
    "/evidence-items/{item_id}",
    response_model=ItemOut,
    summary="Edit a draft evidence item",
    description="Only whoever added it, and only while it is a draft. Replaces its fields and "
    "controls. Requires the `X-CSRF-Token` header. Audited.",
)
def edit_item(
    item_id: ItemId,
    body: ItemIn,
    principal: Annotated[Principal, Depends(require(Permission.EVIDENCE_MANAGE))],
    db: Annotated[Session, Depends(database)],
) -> ItemOut:
    row = _found(db, item_id, principal)
    try:
        service.update(db, row, _fields(body), _actor(principal))
    except EvidenceRefused as error:
        raise _refused(error) from None
    except NotAllowed as error:
        raise _forbidden(error) from None
    db.commit()
    return _out(db, row)


@router.put(
    "/evidence-items/{item_id}/file",
    response_model=ItemOut,
    summary="Upload a draft file item's file",
    description="The request body is the file itself (`Content-Type: application/octet-stream`), "
    "at most 25 MiB, with its name in `filename`. Accepted when its content is a PDF, PNG, "
    "JPEG, plain text, CSV, JSON, Word, Excel or PowerPoint file; stored once by SHA-256. "
    "Requires the `X-CSRF-Token` header. Audited.",
    responses={
        413: {"description": "Larger than 25 MiB"},
        422: {"description": "Empty, unnamed, or not an allowed type"},
    },
)
async def upload_file(
    item_id: ItemId,
    request: Request,
    principal: Annotated[Principal, Depends(require(Permission.EVIDENCE_MANAGE))],
    db: Annotated[Session, Depends(database)],
    filename: Annotated[str, Query(min_length=1, max_length=255)],
) -> ItemOut:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_EVIDENCE_FILE_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "A file is at most 25 MiB")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_EVIDENCE_FILE_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "A file is at most 25 MiB")
        chunks.append(chunk)
    row = _found(db, item_id, principal)
    try:
        service.attach(db, row, b"".join(chunks), filename, _actor(principal))
    except EvidenceRefused as error:
        raise _refused(error) from None
    except NotAllowed as error:
        raise _forbidden(error) from None
    db.commit()
    return _out(db, row)


@router.get(
    "/evidence-items/{item_id}/file",
    summary="Download an evidence item's file",
    response_class=Response,
    responses={404: {"description": "No such item or file, or it is restricted"}},
)
def download_file(
    item_id: ItemId,
    principal: Annotated[Principal, Depends(require(Permission.EVIDENCE_READ))],
    db: Annotated[Session, Depends(database)],
) -> Response:
    row = _found(db, item_id, principal)
    stored = db.get(CapturedFile, row.file_sha256) if row.file_sha256 else None
    if stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No file")
    name = row.filename or row.file_sha256 or "evidence"
    return Response(
        content=stored.content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
            "Digest": f"sha-256={stored.sha256}",
        },
    )


@router.delete(
    "/evidence-items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a draft evidence item",
    description="Only whoever added it, and only while it is a draft. Requires the "
    "`X-CSRF-Token` header. Audited.",
)
def delete_item(
    item_id: ItemId,
    principal: Annotated[Principal, Depends(require(Permission.EVIDENCE_MANAGE))],
    db: Annotated[Session, Depends(database)],
) -> Response:
    row = _found(db, item_id, principal)
    try:
        service.delete(db, row, _actor(principal))
    except EvidenceRefused as error:
        raise _refused(error) from None
    except NotAllowed as error:
        raise _forbidden(error) from None
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/evidence-items/{item_id}/submit",
    response_model=ItemOut,
    summary="Submit a draft for review",
    description="Only whoever added it. A file item needs its file, and every item at least one "
    "control. Requires the `X-CSRF-Token` header. Audited.",
)
def submit_item(
    item_id: ItemId,
    principal: Annotated[Principal, Depends(require(Permission.EVIDENCE_MANAGE))],
    db: Annotated[Session, Depends(database)],
) -> ItemOut:
    row = _found(db, item_id, principal)
    try:
        service.submit(db, row, _actor(principal))
    except EvidenceRefused as error:
        raise _refused(error) from None
    except NotAllowed as error:
        raise _forbidden(error) from None
    db.commit()
    return _out(db, row)


def _decide(
    item_id: int, accept: bool, body: DecisionIn, principal: Principal, db: Session
) -> ItemOut:
    row = _found(db, item_id, principal)
    try:
        service.decide(db, row, accept, body.note, _actor(principal))
    except EvidenceRefused as error:
        raise _refused(error) from None
    except NotAllowed as error:
        raise _forbidden(error) from None
    db.commit()
    return _out(db, row)


@router.post(
    "/evidence-items/{item_id}/accept",
    response_model=ItemOut,
    summary="Accept a submitted evidence item",
    description="Never one's own (development mode excepted, and recorded). An accepted item "
    "never changes. Requires the `X-CSRF-Token` header. Audited.",
)
def accept_item(
    item_id: ItemId,
    body: DecisionIn,
    principal: Annotated[Principal, Depends(require(Permission.EVIDENCE_REVIEW))],
    db: Annotated[Session, Depends(database)],
) -> ItemOut:
    return _decide(item_id, True, body, principal, db)


@router.post(
    "/evidence-items/{item_id}/reject",
    response_model=ItemOut,
    summary="Reject a submitted evidence item",
    description="With a `note` saying why; never one's own. Requires the `X-CSRF-Token` header. "
    "Audited.",
)
def reject_item(
    item_id: ItemId,
    body: DecisionIn,
    principal: Annotated[Principal, Depends(require(Permission.EVIDENCE_REVIEW))],
    db: Annotated[Session, Depends(database)],
) -> ItemOut:
    return _decide(item_id, False, body, principal, db)


@router.post(
    "/evidence-items/{item_id}/check",
    response_model=ItemOut,
    summary="Check that a reference can be reached",
    description="Fetches the reference once, over https and only to a host the deployment "
    "allows (`BALUARTE_EVIDENCE_CHECK_HOSTS`), and records whether it was reachable, the HTTP "
    "status and the SHA-256 of what it returned. 409 `check_not_allowed` otherwise. Requires "
    "the `X-CSRF-Token` header. Audited.",
)
def check_item(
    item_id: ItemId,
    principal: Annotated[Principal, Depends(require(Permission.EVIDENCE_MANAGE))],
    db: Annotated[Session, Depends(database)],
) -> ItemOut:
    row = _found(db, item_id, principal)
    try:
        service.check(db, row, _actor(principal))
    except EvidenceRefused as error:
        raise _refused(error) from None
    db.commit()
    return _out(db, row)
