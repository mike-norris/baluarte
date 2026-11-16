"""The review queue: everything waiting for a second person's decision, in one response.

Each kind is read through its own list endpoint's query, with that endpoint's read permission;
a kind the caller may not read is left out, as its list would refuse them. ``can_decide`` applies
the decide permission and the rule that nobody decides their own proposal outside development
mode; the decide endpoints still check both.
"""

from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from baluarte.api.v1 import controls as controls_api
from baluarte.api.v1 import exceptions as exceptions_api
from baluarte.api.v1 import issue_plans as issue_plans_api
from baluarte.api.v1 import labels as labels_api
from baluarte.api.v1 import policy_changes as policy_changes_api
from baluarte.api.v1 import remediation as remediation_api
from baluarte.api.v1 import scope as scope_api
from baluarte.auth.dependencies import Principal, current_principal, database
from baluarte.auth.roles import Permission
from baluarte.compliance import exceptions as compliance_exceptions
from baluarte.settings import own_decision_refused

router = APIRouter(prefix="/reviews", tags=["reviews"])

ReviewKind = Literal[
    "baseline_change",
    "taxonomy",
    "label_plan",
    "issue_plan",
    "remediation_plan",
    "exception",
    "scope_exclusion",
    "mapping_change",
    "control_exception",
]

# One page per kind; a kind with more waiting is listed in ``partial``.
PAGE = 200

READ: dict[ReviewKind, Permission] = {
    "baseline_change": Permission.FINDINGS_READ,
    "taxonomy": Permission.FINDINGS_READ,
    "label_plan": Permission.FINDINGS_READ,
    "issue_plan": Permission.FINDINGS_READ,
    "remediation_plan": Permission.FINDINGS_READ,
    "exception": Permission.FINDINGS_READ,
    "scope_exclusion": Permission.INVENTORY_READ,
    "mapping_change": Permission.FINDINGS_READ,
    "control_exception": Permission.COMPLIANCE_READ,
}

DECIDE: dict[ReviewKind, Permission] = {
    "baseline_change": Permission.POLICY_WRITE,
    "taxonomy": Permission.POLICY_WRITE,
    "label_plan": Permission.REMEDIATION_APPROVE,
    "issue_plan": Permission.REMEDIATION_APPROVE,
    "remediation_plan": Permission.REMEDIATION_APPROVE,
    "exception": Permission.EXCEPTION_APPROVE,
    "scope_exclusion": Permission.POLICY_WRITE,
    "mapping_change": Permission.POLICY_WRITE,
    "control_exception": Permission.SETTINGS_MANAGE,
}


class Proposer(BaseModel):
    user_id: int | None
    # A display name where one is known; otherwise the client names the user by ID.
    display_name: str | None


class ReviewItem(BaseModel):
    kind: ReviewKind
    id: int
    title: str
    proposed_by: Proposer
    proposed_at: datetime
    expires_at: datetime | None
    can_decide: bool
    # Set for a control mapping change, whose page is the control's.
    framework: str | None = None
    control_id: str | None = None


class ReviewQueue(BaseModel):
    items: list[ReviewItem]
    # Kinds with more waiting than one page; the queue is then a lower bound.
    partial: list[ReviewKind]


class _Draft(BaseModel):
    """An item before ``can_decide`` is worked out."""

    id: int
    title: str
    proposed_by: Proposer
    proposed_at: datetime
    expires_at: datetime | None
    framework: str | None = None
    control_id: str | None = None


Reader = Callable[[Principal, Session], tuple[list[_Draft], bool]]


def _baseline_changes(principal: Principal, db: Session) -> tuple[list[_Draft], bool]:
    page = policy_changes_api.list_changes(
        principal, db, state="proposed", kind=None, policy_name=None, before_id=None, limit=PAGE
    )
    return [
        _Draft(
            id=change.id,
            title=f"{change.policy_name} on {change.target_path}"
            if change.target_path
            else change.policy_name,
            proposed_by=Proposer(
                user_id=change.proposed_by.user_id, display_name=change.proposed_by.display_name
            ),
            proposed_at=change.proposed_at,
            expires_at=None,
        )
        for change in page.items
    ], page.next_before_id is not None


def _taxonomies(principal: Principal, db: Session) -> tuple[list[_Draft], bool]:
    view = labels_api.read_taxonomy(principal, db, as_of=None)
    return [
        _Draft(
            id=version.id,
            title=f"{version.name} v{version.version}",
            proposed_by=Proposer(
                user_id=version.proposed_by.user_id,
                display_name=version.proposed_by.display_name,
            ),
            proposed_at=version.proposed_at,
            expires_at=None,
        )
        for version in view.versions
        if version.state == "proposed"
    ], False


def _label_plans(principal: Principal, db: Session) -> tuple[list[_Draft], bool]:
    page = labels_api.list_plans(principal, db, state="submitted", after_id=None, limit=PAGE)
    return [
        _Draft(
            id=plan.id,
            title=f"Label plan {plan.id}: {plan.steps} steps",
            proposed_by=Proposer(
                user_id=plan.created_by.user_id, display_name=plan.created_by.display_name
            ),
            proposed_at=plan.created_at,
            expires_at=plan.expires_at,
        )
        for plan in page.items
    ], page.next_after_id is not None


def _issue_plans(principal: Principal, db: Session) -> tuple[list[_Draft], bool]:
    page = issue_plans_api.list_plans(principal, db, state="submitted", after_id=None, limit=PAGE)
    return [
        _Draft(
            id=plan.id,
            title=f"Issue plan {plan.id}: {plan.steps} steps",
            proposed_by=Proposer(
                user_id=plan.created_by.user_id, display_name=plan.created_by.display_name
            ),
            proposed_at=plan.created_at,
            expires_at=plan.expires_at,
        )
        for plan in page.items
    ], page.next_after_id is not None


def _remediation_plans(principal: Principal, db: Session) -> tuple[list[_Draft], bool]:
    page = remediation_api.list_plans(principal, db, state="submitted", after_id=None, limit=PAGE)
    names = labels_api._people(db, {plan.created_by_user_id for plan in page.items})
    return [
        _Draft(
            id=plan.id,
            title=f"Remediation plan {plan.id}: {plan.steps} steps",
            proposed_by=Proposer(
                user_id=plan.created_by_user_id, display_name=names.get(plan.created_by_user_id)
            ),
            proposed_at=plan.submitted_at or plan.created_at,
            expires_at=plan.expires_at,
        )
        for plan in page.items
    ], page.next_after_id is not None


def _exceptions(principal: Principal, db: Session) -> tuple[list[_Draft], bool]:
    page = exceptions_api.list_exceptions(
        principal,
        db,
        state="requested",
        project=None,
        policy=None,
        rule=None,
        after_id=None,
        limit=PAGE,
    )
    return [
        _Draft(
            id=item.id,
            title=f"{item.policy_name} {item.rule_id} on {item.project.path}",
            proposed_by=Proposer(
                user_id=item.requested_by.user_id, display_name=item.requested_by.display_name
            ),
            proposed_at=item.requested_at,
            expires_at=None,
        )
        for item in page.items
    ], page.next_after_id is not None


def _scope_exclusions(principal: Principal, db: Session) -> tuple[list[_Draft], bool]:
    page = scope_api.list_exclusions(principal, db, state="proposed")
    return [
        _Draft(
            id=item.id,
            title=f"Stop monitoring {item.target_path or item.target_type}",
            proposed_by=Proposer(
                user_id=item.proposed_by.user_id, display_name=item.proposed_by.display_name
            ),
            proposed_at=item.proposed_at,
            expires_at=None,
        )
        for item in page.items
    ], False


def _mapping_changes(principal: Principal, db: Session) -> tuple[list[_Draft], bool]:
    page = controls_api.list_changes(
        principal, db, state="proposed", framework=None, control_id=None, before_id=None, limit=PAGE
    )
    return [
        _Draft(
            id=change.id,
            title=f"{change.framework.upper()} {change.control_id}",
            proposed_by=Proposer(
                user_id=change.proposed_by.user_id, display_name=change.proposed_by.display_name
            ),
            proposed_at=change.proposed_at,
            expires_at=None,
            framework=change.framework,
            control_id=change.control_id,
        )
        for change in page.items
    ], page.next_before_id is not None


def _control_exceptions(principal: Principal, db: Session) -> tuple[list[_Draft], bool]:
    del principal  # the caller has checked compliance:read, which guards this list
    rows = [row for row in compliance_exceptions.listing(db) if row.state == "proposed"]
    names = labels_api._people(db, {row.requested_by_user_id for row in rows})
    return [
        _Draft(
            id=row.id,
            title=f"Control {row.control_id}",
            proposed_by=Proposer(
                user_id=row.requested_by_user_id,
                display_name=names.get(row.requested_by_user_id)
                if row.requested_by_user_id is not None
                else None,
            ),
            proposed_at=row.requested_at,
            expires_at=None,
        )
        for row in rows
    ], False


SOURCES: dict[ReviewKind, Reader] = {
    "baseline_change": _baseline_changes,
    "taxonomy": _taxonomies,
    "label_plan": _label_plans,
    "issue_plan": _issue_plans,
    "remediation_plan": _remediation_plans,
    "exception": _exceptions,
    "scope_exclusion": _scope_exclusions,
    "mapping_change": _mapping_changes,
    "control_exception": _control_exceptions,
}

# Unexpiring items sort after any that expire.
_NEVER = datetime.combine(date.max, time.min, tzinfo=UTC)


def _sort_key(item: ReviewItem) -> tuple[datetime, datetime]:
    return (item.expires_at or _NEVER, item.proposed_at)


def queue(principal: Principal, db: Session, kinds: Iterable[ReviewKind]) -> ReviewQueue:
    items: list[ReviewItem] = []
    partial: list[ReviewKind] = []
    for kind in kinds:
        if READ[kind] not in principal.permissions:
            continue
        drafts, more = SOURCES[kind](principal, db)
        if more:
            partial.append(kind)
        allowed = DECIDE[kind] in principal.permissions
        items.extend(
            ReviewItem(
                kind=kind,
                can_decide=allowed
                and not own_decision_refused(draft.proposed_by.user_id, principal.user_id),
                **draft.model_dump(),
            )
            for draft in drafts
        )
    items.sort(key=_sort_key)
    return ReviewQueue(items=items, partial=partial)


@router.get(
    "",
    response_model=ReviewQueue,
    summary="Everything waiting for a second person's decision",
    description="Baseline changes, label taxonomies, label, issue and remediation plans, "
    "exceptions, monitoring exclusions, control mapping changes and compliance exceptions that "
    "wait for review, soonest to expire first, then oldest. Only kinds the caller may read are "
    "included. `mine=true` keeps the caller's own proposals; `can_decide` says whether the "
    "caller may decide each item. Kinds with more than one page waiting are listed in `partial`.",
)
def list_reviews(
    principal: Annotated[Principal, Depends(current_principal)],
    db: Annotated[Session, Depends(database)],
    mine: Annotated[bool, Query()] = False,
) -> ReviewQueue:
    result = queue(principal, db, SOURCES)
    if mine:
        result.items = [
            item for item in result.items if item.proposed_by.user_id == principal.user_id
        ]
    return result
