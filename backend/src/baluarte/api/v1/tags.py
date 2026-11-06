"""Tags: ``key=value`` pairs kept in Baluarte to group projects (docs/TAGS.md).

Anyone with ``inventory:read`` sees tags; ``tags:write`` creates, edits, assigns and unassigns
them. Every change is audited as ``tags.*``. Tags never change GitLab.
"""

from collections import Counter
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte import tags
from baluarte.api.v1.controls import Person
from baluarte.api.v1.scope import _instance_id, _required_instance
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import AppUser, Tag
from baluarte.policy.changes import Actor

router = APIRouter(prefix="/tags", tags=["tags"])

Name = Annotated[str, Field(min_length=1, max_length=63)]

# The ``tag`` filter of list endpoints: repeatable ``key:value``, all of which must apply.
TagFilter = Annotated[
    list[str] | None,
    Query(
        max_length=20,
        description="Repeatable `key:value`; every one must apply (directly or inherited).",
    ),
]


def tag_filters(values: list[str] | None) -> list[tuple[str, str]]:
    """Parse ``tag`` filters; 422 ``invalid_tag_filter`` for one that is not ``key:value``."""
    found: list[tuple[str, str]] = []
    for value in values or []:
        parsed = tags.parse_filter(value)
        if parsed is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "invalid_tag_filter",
                    "message": f"tag is key:value, not {value!r}",
                },
            )
        found.append(parsed)
    return list(dict.fromkeys(found))


class TagOut(BaseModel):
    id: int
    key: str
    value: str
    description: str | None
    color: str | None
    created_by: Person
    created_at: datetime
    # How many groups and projects the tag is assigned to directly.
    groups: int
    projects: int


class AssignmentOut(BaseModel):
    id: int
    target_type: Literal["group", "project"]
    target_gitlab_id: int
    target_path: str
    assigned_by: Person
    assigned_at: datetime


class TagDetail(TagOut):
    assignments: list[AssignmentOut]


class TagsPage(BaseModel):
    items: list[TagOut]


class TagIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Name
    value: Name
    description: Annotated[str | None, Field(max_length=500)] = None
    color: Annotated[str | None, Field(max_length=7)] = None


class TagUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: Annotated[str | None, Field(max_length=500)] = None
    color: Annotated[str | None, Field(max_length=7)] = None


class TargetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["group", "project"]
    gitlab_id: Annotated[int, Field(gt=0)]


class TargetsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets: list[TargetIn] = Field(min_length=1, max_length=tags.MAX_TARGETS)


def _names(db: Session, ids: set[int]) -> dict[int, str | None]:
    if not ids:
        return {}
    return dict(
        db.execute(select(AppUser.id, AppUser.display_name).where(AppUser.id.in_(ids))).all()
    )


def _person(actor: str, user_id: int | None, names: dict[int, str | None]) -> Person:
    return Person(
        actor=actor, user_id=user_id, display_name=names.get(user_id) if user_id else None
    )


def tags_out(db: Session, rows: list[Tag]) -> list[TagOut]:
    counts: Counter[tuple[int, str]] = Counter(
        (row.tag_id, row.target_type) for row in tags.open_assignments(db, [t.id for t in rows])
    )
    names = _names(db, {row.created_by_user_id for row in rows if row.created_by_user_id})
    return [
        TagOut(
            id=row.id,
            key=row.key,
            value=row.value,
            description=row.description,
            color=row.color,
            created_by=_person(row.created_by, row.created_by_user_id, names),
            created_at=row.created_at,
            groups=counts[(row.id, "group")],
            projects=counts[(row.id, "project")],
        )
        for row in rows
    ]


def _detail(db: Session, row: Tag) -> TagDetail:
    assignments = tags.open_assignments(db, [row.id])
    names = _names(db, {a.assigned_by_user_id for a in assignments if a.assigned_by_user_id})
    return TagDetail(
        **tags_out(db, [row])[0].model_dump(),
        assignments=[
            AssignmentOut(
                id=a.id,
                target_type=a.target_type,  # type: ignore[arg-type]
                target_gitlab_id=a.target_gitlab_id,
                target_path=a.target_path,
                assigned_by=_person(a.assigned_by, a.assigned_by_user_id, names),
                assigned_at=a.assigned_at,
            )
            for a in assignments
        ],
    )


def _refused(error: tags.TagRefused) -> HTTPException:
    code = (
        status.HTTP_422_UNPROCESSABLE_CONTENT
        if error.code.startswith("invalid_")
        else status.HTTP_409_CONFLICT
    )
    return HTTPException(
        code, detail={"code": error.code, "message": str(error), "reasons": error.reasons}
    )


def _tag(db: Session, tag_id: int) -> Tag:
    row = db.get(Tag, tag_id)
    if row is None or row.instance_id != _instance_id(db):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such tag")
    return row


def _targets(body: TargetsIn) -> list[tags.Target]:
    return [tags.Target(t.type, t.gitlab_id) for t in body.targets]


@router.get(
    "",
    response_model=TagsPage,
    summary="Tags",
    description="Every tag of the configured instance by key and value, with how many groups "
    "and projects it is assigned to directly.",
)
def list_tags(
    _: Annotated[Principal, Depends(require(Permission.INVENTORY_READ))],
    db: Annotated[Session, Depends(database)],
) -> TagsPage:
    rows = list(
        db.scalars(
            select(Tag)
            .where(Tag.instance_id == (_instance_id(db) or 0))
            .order_by(Tag.key, Tag.value)
        )
    )
    return TagsPage(items=tags_out(db, rows))


@router.post(
    "",
    response_model=TagDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a tag",
    description="Keys and values are lowercase letters, digits, `.`, `_` and `-`, starting with "
    "a letter or digit (422 `invalid_name`); colour is `#RRGGBB`. 409 `tag_exists`.",
)
def create_tag(
    principal: Annotated[Principal, Depends(require(Permission.TAGS_WRITE))],
    db: Annotated[Session, Depends(database)],
    body: Annotated[TagIn, Body()],
) -> TagDetail:
    instance_id = _required_instance(db)
    try:
        row = tags.create(
            db,
            instance_id,
            body.key,
            body.value,
            body.description,
            body.color,
            Actor(principal.user_id, principal.actor),
        )
    except tags.TagRefused as error:
        raise _refused(error) from error
    db.commit()
    return _detail(db, row)


@router.get("/{tag_id}", response_model=TagDetail, summary="A tag and its assignments")
def read_tag(
    _: Annotated[Principal, Depends(require(Permission.INVENTORY_READ))],
    db: Annotated[Session, Depends(database)],
    tag_id: Annotated[int, Path(gt=0)],
) -> TagDetail:
    return _detail(db, _tag(db, tag_id))


@router.patch(
    "/{tag_id}",
    response_model=TagDetail,
    summary="Change a tag's description or colour",
    description="Only the fields sent change; send null to clear one. Key and value are fixed: "
    "create another tag instead.",
)
def update_tag(
    principal: Annotated[Principal, Depends(require(Permission.TAGS_WRITE))],
    db: Annotated[Session, Depends(database)],
    tag_id: Annotated[int, Path(gt=0)],
    body: Annotated[TagUpdate, Body()],
) -> TagDetail:
    row = _tag(db, tag_id)
    sent = body.model_fields_set
    try:
        tags.update(
            db,
            row.id,
            Actor(principal.user_id, principal.actor),
            description=body.description,
            color=body.color,
            set_description="description" in sent,
            set_color="color" in sent,
        )
    except tags.TagRefused as error:
        raise _refused(error) from error
    db.commit()
    return _detail(db, row)


@router.post(
    "/{tag_id}/assign",
    response_model=TagDetail,
    summary="Assign a tag to groups and projects",
    description="A group's tag applies to its subgroups and projects. Targets that already have "
    "the tag are left as they are. 409 `unknown_target` or `key_conflict` (another value of the "
    "same key is assigned directly; reasons list the paths).",
)
def assign_tag(
    principal: Annotated[Principal, Depends(require(Permission.TAGS_WRITE))],
    db: Annotated[Session, Depends(database)],
    tag_id: Annotated[int, Path(gt=0)],
    body: Annotated[TargetsIn, Body()],
) -> TagDetail:
    row = _tag(db, tag_id)
    try:
        tags.assign(db, row.id, _targets(body), Actor(principal.user_id, principal.actor))
    except tags.TagRefused as error:
        raise _refused(error) from error
    db.commit()
    return _detail(db, row)


@router.post(
    "/{tag_id}/unassign",
    response_model=TagDetail,
    summary="Remove a tag from groups and projects",
    description="Targets without the tag are ignored. The assignment's history is kept.",
)
def unassign_tag(
    principal: Annotated[Principal, Depends(require(Permission.TAGS_WRITE))],
    db: Annotated[Session, Depends(database)],
    tag_id: Annotated[int, Path(gt=0)],
    body: Annotated[TargetsIn, Body()],
) -> TagDetail:
    row = _tag(db, tag_id)
    tags.unassign(db, row.id, _targets(body), Actor(principal.user_id, principal.actor))
    db.commit()
    return _detail(db, row)
