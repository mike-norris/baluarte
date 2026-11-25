"""Template change sets and saved selections (roadmap M14.3).

Reading needs ``findings:read``. Previewing a selection, saving one and creating or withdrawing a
change set need ``changes:plan``; approving or rejecting needs ``changes:approve`` and
someone other than the creator (two different people for a sensitive set). Every change needs
the CSRF header and is audited. Building and deciding never call the provider
(docs/CONFIGURATION_TEMPLATES.md#change-sets).

Applying, continuing, cancelling and rolling back (roadmap M14.4) need ``changes:approve`` and
queue jobs that write with the template write token, only while ``BALUARTE_TEMPLATE_WRITES`` is
true (docs/CONFIGURATION_TEMPLATES.md#execution).
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import (
    AppUser,
    GitLabInstance,
    SavedSelection,
    TemplateChangeItem,
    TemplateChangeSet,
)
from baluarte.policy.evaluation.project import capability_statuses, collection_contexts
from baluarte.templates import change_sets, execute
from baluarte.templates.change_sets import (
    DEFAULT_HOURS,
    MAX_NOTE,
    ChangeSetConflict,
    ChangeSetRefused,
    ChangeSetStale,
    NothingToChange,
    effective_state,
    source_of,
)
from baluarte.templates.drift import ProjectRef
from baluarte.templates.loader import TemplateInvalid
from baluarte.templates.selection import Selection, select_projects
from baluarte.templates.verify import verify
from baluarte.workspaces import current_connection, settings_of, workspace_of

router = APIRouter(tags=["templates"])

Reader = Annotated[Principal, Depends(require(Permission.FINDINGS_READ))]
Planner = Annotated[Principal, Depends(require(Permission.CHANGES_PLAN))]
Approver = Annotated[Principal, Depends(require(Permission.CHANGES_APPROVE))]
Note = Annotated[str, Field(min_length=1, max_length=MAX_NOTE)]
PREVIEW_LISTED = 200


def _connection(db: Session) -> GitLabInstance:
    instance = current_connection(db)
    if instance is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No connection is configured")
    return instance


def _invalid(invalid: TemplateInvalid) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        {
            "message": "; ".join(f"{e.path}: {e.message}" for e in invalid.errors),
            "errors": [{"path": e.path, "message": e.message} for e in invalid.errors],
        },
    )


# --- selections ---


class Preview(BaseModel):
    projects: int
    listed: list[ProjectRef]
    truncated: bool


@router.post(
    "/template-selections/preview",
    response_model=Preview,
    summary="The projects a selection targets",
    description="Active, monitored, unarchived projects of the current connection meeting every "
    "criterion given, by path; at most 200 listed. Requires the `X-CSRF-Token` header.",
    responses={404: {"description": "No connection is configured"}},
)
def preview_selection(
    body: Selection, _: Planner, db: Annotated[Session, Depends(database)]
) -> Preview:
    instance = _connection(db)
    if body.empty():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Give at least one selection criterion"
        )
    collections = collection_contexts(db, instance.id, settings_of(instance))
    found = select_projects(db, instance, body, collections, capability_statuses(db, instance.id))
    return Preview(
        projects=len(found),
        listed=[
            ProjectRef(id=row.id, gitlab_id=row.gitlab_id, path=row.path_with_namespace)
            for row in found[:PREVIEW_LISTED]
        ],
        truncated=len(found) > PREVIEW_LISTED,
    )


class SavedIn(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    selection: Selection


class SavedOut(BaseModel):
    id: int
    name: str
    selection: Selection
    created_at: datetime


class SavedList(BaseModel):
    selections: list[SavedOut]


def _saved_out(row: SavedSelection) -> SavedOut:
    return SavedOut(
        id=row.id,
        name=row.name,
        selection=Selection.model_validate(row.selection),
        created_at=row.created_at,
    )


@router.get(
    "/template-selections",
    response_model=SavedList,
    summary="Saved selections of the current connection",
)
def list_selections(_: Reader, db: Annotated[Session, Depends(database)]) -> SavedList:
    instance = current_connection(db)
    if instance is None:
        return SavedList(selections=[])
    rows = db.scalars(
        select(SavedSelection)
        .where(SavedSelection.instance_id == instance.id)
        .order_by(SavedSelection.name)
    ).all()
    return SavedList(selections=[_saved_out(row) for row in rows])


@router.post(
    "/template-selections",
    response_model=SavedOut,
    status_code=status.HTTP_201_CREATED,
    summary="Save a selection under a name",
    description="Requires the `X-CSRF-Token` header. Audited.",
    responses={409: {"description": "The name is taken"}},
)
def save_selection(
    body: SavedIn, principal: Planner, db: Annotated[Session, Depends(database)]
) -> SavedOut:
    instance = _connection(db)
    if body.selection.empty():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Give at least one selection criterion"
        )
    row = SavedSelection(
        workspace_id=workspace_of(db, instance.id),
        instance_id=instance.id,
        name=body.name.strip(),
        selection=body.selection.model_dump(mode="json"),
        created_by_user_id=principal.user_id,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "A selection has that name") from None
    audit.record(
        db,
        actor=principal.actor,
        action="saved_selection.created",
        resource_type="saved_selection",
        resource_id=str(row.id),
        details={"name": row.name, "selection": row.selection},
    )
    db.commit()
    return _saved_out(row)


@router.delete(
    "/template-selections/{selection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a saved selection",
    description="Change sets keep the criteria they were built from. Requires the "
    "`X-CSRF-Token` header. Audited.",
    responses={404: {"description": "No such selection"}},
)
def delete_selection(
    selection_id: Annotated[int, Path(gt=0)],
    principal: Planner,
    db: Annotated[Session, Depends(database)],
) -> None:
    row = db.scalars(select(SavedSelection).where(SavedSelection.id == selection_id)).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such selection")
    audit.record(
        db,
        actor=principal.actor,
        action="saved_selection.deleted",
        resource_type="saved_selection",
        resource_id=str(row.id),
        details={"name": row.name, "selection": row.selection},
    )
    db.delete(row)
    db.commit()


# --- change sets ---


class ChangeSetIn(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    selection: Selection
    template: Annotated[
        str | None, Field(description="A template version, name@version", max_length=80)
    ] = None
    settings: Annotated[
        dict[str, Any] | None,
        Field(description="Settings in the template `settings` format, instead of a template"),
    ] = None
    effective: Annotated[
        bool,
        Field(
            description="Each project's own effective settings, instead of a template or "
            "settings: remediates drift"
        ),
    ] = False
    expires_in_hours: Annotated[int, Field(ge=1, le=change_sets.MAX_CHANGE_SET_HOURS)] = (
        DEFAULT_HOURS
    )


class UserRef(BaseModel):
    user_id: int
    display_name: str | None


class ChangeSetSummary(BaseModel):
    id: int
    connection_id: int
    title: str
    # The stored state, with a proposed or approved set past its end shown as expired.
    state: str
    digest: str
    targets: int
    changes: int
    skipped: int
    sensitive: bool
    # Approvals still needed while proposed: 2 or 1 for a sensitive set, else 1; 0 otherwise.
    approvals_needed: int
    source: str | None
    # template, settings, or effective (each project's own effective settings).
    source_kind: Literal["template", "settings", "effective"]
    # None, with created_by_system, for a set Baluarte proposed itself (automatic remediation).
    created_by: UserRef | None
    created_by_system: bool
    created_at: datetime
    expires_at: datetime
    first_approved_by: UserRef | None
    first_approved_at: datetime | None
    first_approval_note: str | None
    decided_by: UserRef | None
    decided_at: datetime | None
    decision_note: str | None
    development_decision: bool
    ended_at: datetime | None
    # Applying (M14.4). Targets with at least one change, and how many the stages allow so far.
    changing_targets: int
    stage_targets: int
    applied_by: UserRef | None
    apply_started_at: datetime | None
    finished_at: datetime | None
    cancelled_by: UserRef | None
    cancelled_at: datetime | None
    rollback_by: UserRef | None
    rollback_started_at: datetime | None
    # Why the last run stopped before its stage was done.
    halt_reason: str | None


class ChangeItemOut(BaseModel):
    id: int
    position: int
    target_index: int
    project_path: str
    setting: str
    before: Any
    after: Any
    irreversible: str | None
    status: str
    attempts: int
    request_status: int | None
    observed: Any
    message: str | None
    finished_at: datetime | None
    rollback_status: str | None
    rollback_message: str | None
    rolled_back_at: datetime | None


class Irreversible(BaseModel):
    project: str
    setting: str
    why: str


class VerificationOut(BaseModel):
    item_id: int
    project_path: str
    setting: str
    after: Any
    status: Literal["verified", "drift", "awaiting_collection", "unknown"]
    observed: Any
    observed_at: datetime | None
    reason: str


class ChangeSetOut(ChangeSetSummary):
    document: dict[str, Any]
    # Progress per change, once applying started.
    items: list[ChangeItemOut]
    # Changes that cannot be fully undone; applying needs them acknowledged.
    irreversible: list[Irreversible]
    # Why this deployment cannot write this set now (writes_disabled, token_missing,
    # not_primary, busy); empty when it can.
    write_refusals: list[str]
    # Each applied change that was not rolled back, checked against evidence collected since.
    verification: list[VerificationOut]


class ChangeSetList(BaseModel):
    change_sets: list[ChangeSetSummary]


def _source_kind(source: dict[str, Any]) -> Literal["template", "settings", "effective"]:
    if source.get("effective"):
        return "effective"
    return "template" if source["template"] is not None else "settings"


def _summaries(
    db: Session, rows: list[TemplateChangeSet], states: dict[int, str]
) -> list[dict[str, Any]]:
    ids = {
        user
        for row in rows
        for user in (
            row.created_by_user_id,
            row.first_approved_by_user_id,
            row.decided_by_user_id,
            row.applied_by_user_id,
            row.cancelled_by_user_id,
            row.rollback_by_user_id,
        )
        if user is not None
    }
    names = (
        dict(db.execute(select(AppUser.id, AppUser.display_name).where(AppUser.id.in_(ids))).all())
        if ids
        else {}
    )

    def user(user_id: int | None) -> UserRef | None:
        return UserRef(user_id=user_id, display_name=names.get(user_id)) if user_id else None

    found = []
    for row in rows:
        state = states[row.id]
        needed = 0
        if state == "proposed":
            needed = 1 if not row.sensitive or row.first_approved_at else 2
        found.append(
            {
                "id": row.id,
                "connection_id": row.instance_id,
                "title": row.title,
                "state": state,
                "digest": row.digest,
                "targets": row.targets,
                "changes": row.changes,
                "skipped": row.skipped,
                "sensitive": row.sensitive,
                "approvals_needed": needed,
                "source": row.document["source"]["template"],
                "source_kind": _source_kind(row.document["source"]),
                "created_by": user(row.created_by_user_id),
                "created_by_system": row.created_by_system,
                "created_at": row.created_at,
                "expires_at": row.expires_at,
                "first_approved_by": user(row.first_approved_by_user_id),
                "first_approved_at": row.first_approved_at,
                "first_approval_note": row.first_approval_note,
                "decided_by": user(row.decided_by_user_id),
                "decided_at": row.decided_at,
                "decision_note": row.decision_note,
                "development_decision": row.development_decision,
                "ended_at": row.ended_at,
                "changing_targets": execute.changing_targets(row),
                "stage_targets": row.stage_targets,
                "applied_by": user(row.applied_by_user_id),
                "apply_started_at": row.apply_started_at,
                "finished_at": row.finished_at,
                "cancelled_by": user(row.cancelled_by_user_id),
                "cancelled_at": row.cancelled_at,
                "rollback_by": user(row.rollback_by_user_id),
                "rollback_started_at": row.rollback_started_at,
                "halt_reason": row.halt_reason,
            }
        )
    return found


def _out(db: Session, row: TemplateChangeSet) -> ChangeSetOut:
    state = change_sets.current_state(db, row)
    summary = _summaries(db, [row], {row.id: state})[0]
    items = list(
        db.scalars(
            select(TemplateChangeItem)
            .where(TemplateChangeItem.change_set_id == row.id)
            .order_by(TemplateChangeItem.position)
        )
    )
    instance = db.get(GitLabInstance, row.instance_id)
    checked = verify(db, instance, items) if instance is not None else []
    return ChangeSetOut(
        **summary,
        document=row.document,
        items=[ChangeItemOut.model_validate(item, from_attributes=True) for item in items],
        irreversible=[Irreversible(**found) for found in execute.irreversible_changes(row)],
        write_refusals=execute.write_refusals(db, row),
        verification=[VerificationOut(**vars(found)) for found in checked],
    )


@router.get(
    "/template-change-sets",
    response_model=ChangeSetList,
    summary="Template change sets",
    description="Newest first, at most 200; filter by `state`.",
)
def list_change_sets(
    _: Reader,
    db: Annotated[Session, Depends(database)],
    state: Annotated[
        Literal["proposed", "approved", "rejected", "withdrawn", "expired"] | None, Query()
    ] = None,
) -> ChangeSetList:
    current = effective_state().label("current")
    statement = select(TemplateChangeSet, current).order_by(TemplateChangeSet.id.desc())
    if state is not None:
        statement = statement.where(effective_state() == state)
    pairs = db.execute(statement.limit(200)).all()
    rows = [row for row, _ in pairs]
    states = {row.id: value for row, value in pairs}
    return ChangeSetList(
        change_sets=[ChangeSetSummary(**item) for item in _summaries(db, rows, states)]
    )


@router.get(
    "/template-change-sets/{change_set_id}",
    response_model=ChangeSetOut,
    summary="A change set with its document",
    responses={404: {"description": "No such change set"}},
)
def read_change_set(
    change_set_id: Annotated[int, Path(gt=0)], _: Reader, db: Annotated[Session, Depends(database)]
) -> ChangeSetOut:
    row = db.scalars(select(TemplateChangeSet).where(TemplateChangeSet.id == change_set_id)).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such change set")
    return _out(db, row)


@router.post(
    "/template-change-sets",
    response_model=ChangeSetOut,
    status_code=status.HTTP_201_CREATED,
    summary="Build a change set from stored evidence",
    description="For the current connection: the selection's projects, each setting of the "
    "template version, the settings given, or each project's own effective settings "
    "(`effective`, which remediates drift), with before and after values, skips and warnings. "
    "Nothing is changed on the provider. Requires the `X-CSRF-Token` header. Audited.",
    responses={
        404: {"description": "No connection is configured"},
        422: {
            "description": "Invalid body or settings, unknown template, an empty selection, no "
            "matching project, more than 500 projects, or nothing to change"
        },
    },
)
def create_change_set(
    body: ChangeSetIn, principal: Planner, db: Annotated[Session, Depends(database)]
) -> ChangeSetOut:
    instance = _connection(db)
    try:
        source = source_of(db, body.template, body.settings, body.effective)
        row = change_sets.create(
            db,
            instance,
            body.title.strip(),
            body.selection,
            source,
            body.expires_in_hours,
            principal.user_id,
            principal.actor,
        )
    except TemplateInvalid as invalid:
        raise _invalid(invalid) from None
    except (ChangeSetRefused, NothingToChange) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    db.commit()
    return _out(db, row)


class ApproveIn(BaseModel):
    digest: Annotated[str, Field(pattern="^[0-9a-f]{64}$", description="The digest reviewed")]
    note: Note | None = None


class NoteIn(BaseModel):
    note: Note


def _locked(db: Session, change_set_id: int) -> TemplateChangeSet:
    visible = db.scalars(
        select(TemplateChangeSet.id).where(TemplateChangeSet.id == change_set_id)
    ).first()
    row = change_sets.locked(db, change_set_id) if visible is not None else None
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such change set")
    return row


_TRANSITIONS: dict[int | str, dict[str, str]] = {
    403: {"description": "Missing permission, CSRF failure, or a decision that must be another's"},
    404: {"description": "No such change set"},
    409: {
        "description": "Not allowed in the current state, a digest that is not the set's, or a "
        "set that today's evidence no longer gives"
    },
}
ChangeSetId = Annotated[int, Path(gt=0)]


def _decided(db: Session, row: TemplateChangeSet, action: Any) -> ChangeSetOut:
    try:
        action()
    except (ChangeSetConflict, ChangeSetStale) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    db.commit()
    return _out(db, row)


@router.post(
    "/template-change-sets/{change_set_id}/approve",
    response_model=ChangeSetOut,
    summary="Approve a proposed change set",
    description="Names the digest reviewed. Someone other than the creator; a sensitive set "
    "needs a second approval from a third person, and stays proposed after the first. Refused "
    "when today's evidence no longer gives the same set. Requires the `X-CSRF-Token` header.",
    responses=_TRANSITIONS,
)
def approve_change_set(
    change_set_id: ChangeSetId,
    body: ApproveIn,
    principal: Approver,
    db: Annotated[Session, Depends(database)],
) -> ChangeSetOut:
    row = _locked(db, change_set_id)
    return _decided(
        db,
        row,
        lambda: change_sets.approve(
            db, row, body.digest, principal.user_id, principal.actor, body.note
        ),
    )


@router.post(
    "/template-change-sets/{change_set_id}/reject",
    response_model=ChangeSetOut,
    summary="Reject a proposed change set",
    description="With a note. Requires the `X-CSRF-Token` header.",
    responses=_TRANSITIONS,
)
def reject_change_set(
    change_set_id: ChangeSetId,
    body: NoteIn,
    principal: Approver,
    db: Annotated[Session, Depends(database)],
) -> ChangeSetOut:
    row = _locked(db, change_set_id)
    return _decided(
        db,
        row,
        lambda: change_sets.reject(db, row, principal.user_id, principal.actor, body.note),
    )


@router.post(
    "/template-change-sets/{change_set_id}/withdraw",
    response_model=ChangeSetOut,
    summary="Withdraw a proposed change set",
    description="Only its creator. Requires the `X-CSRF-Token` header.",
    responses=_TRANSITIONS,
)
def withdraw_change_set(
    change_set_id: ChangeSetId, principal: Planner, db: Annotated[Session, Depends(database)]
) -> ChangeSetOut:
    row = _locked(db, change_set_id)
    return _decided(
        db, row, lambda: change_sets.withdraw(db, row, principal.user_id, principal.actor)
    )


class ApplyIn(BaseModel):
    stage_targets: Annotated[
        int, Field(ge=1, description="How many targets (projects) the first stage applies")
    ]
    acknowledge_irreversible: Annotated[
        bool, Field(description="Required true when the set has changes that cannot be undone")
    ] = False


class ContinueIn(BaseModel):
    more_targets: Annotated[
        int | None, Field(ge=1, description="How many more targets; omitted for all that remain")
    ] = None


def _run(db: Session, row: TemplateChangeSet, action: Any) -> ChangeSetOut:
    try:
        action()
    except execute.ApplyRefused as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    return _out(db, row)


_RUNS: dict[int | str, dict[str, str]] = {
    403: {"description": "Missing permission or CSRF failure"},
    404: {"description": "No such change set"},
    409: {
        "description": "Not allowed in the current state, template writes off, no template "
        "write token, not the primary connection, another set of the connection running, a "
        "stale set, or unacknowledged irreversible changes"
    },
}


@router.post(
    "/template-change-sets/{change_set_id}/apply",
    response_model=ChangeSetOut,
    summary="Start applying an approved change set",
    description="Applies the first `stage_targets` targets and pauses, or finishes when none "
    "remain. Only while `BALUARTE_TEMPLATE_WRITES` is true and a template write token is "
    "stored, to the primary GitLab connection, before the set expires and while today's "
    "evidence still gives it. Changes that cannot be fully undone must be acknowledged. "
    "Requires the `X-CSRF-Token` header.",
    responses=_RUNS,
)
def apply_change_set(
    change_set_id: ChangeSetId,
    body: ApplyIn,
    principal: Approver,
    db: Annotated[Session, Depends(database)],
) -> ChangeSetOut:
    row = _locked(db, change_set_id)
    return _run(
        db,
        row,
        lambda: execute.start(
            db,
            row,
            body.stage_targets,
            body.acknowledge_irreversible,
            principal.user_id,
            principal.actor,
        ),
    )


@router.post(
    "/template-change-sets/{change_set_id}/continue",
    response_model=ChangeSetOut,
    summary="Continue a paused change set",
    description="Applies `more_targets` more targets, or all that remain. Also restarts a run "
    "that stopped (`halt_reason`). Requires the `X-CSRF-Token` header.",
    responses=_RUNS,
)
def continue_change_set(
    change_set_id: ChangeSetId,
    body: ContinueIn,
    principal: Approver,
    db: Annotated[Session, Depends(database)],
) -> ChangeSetOut:
    row = _locked(db, change_set_id)
    return _run(db, row, lambda: execute.resume(db, row, body.more_targets, principal.actor))


@router.post(
    "/template-change-sets/{change_set_id}/cancel",
    response_model=ChangeSetOut,
    summary="Cancel an applying or paused change set",
    description="Stops before the next change; changes not yet applied are cancelled and what "
    "was applied stays (roll back to undo it). Requires the `X-CSRF-Token` header.",
    responses=_RUNS,
)
def cancel_change_set(
    change_set_id: ChangeSetId, principal: Approver, db: Annotated[Session, Depends(database)]
) -> ChangeSetOut:
    row = _locked(db, change_set_id)
    return _run(db, row, lambda: execute.cancel(db, row, principal.user_id, principal.actor))


@router.post(
    "/template-change-sets/{change_set_id}/rollback",
    response_model=ChangeSetOut,
    summary="Roll back a paused, cancelled or finished change set",
    description="Undoes the applied changes in reverse order, each only while GitLab still "
    "shows the value written. Requires the `X-CSRF-Token` header.",
    responses=_RUNS,
)
def rollback_change_set(
    change_set_id: ChangeSetId, principal: Approver, db: Annotated[Session, Depends(database)]
) -> ChangeSetOut:
    row = _locked(db, change_set_id)
    return _run(
        db, row, lambda: execute.start_rollback(db, row, principal.user_id, principal.actor)
    )
