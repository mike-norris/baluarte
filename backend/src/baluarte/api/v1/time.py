"""Time tracking: time spent and estimated, collected from GitLab (docs/TIME_TRACKING.md).

Summaries (``time:read``) never name a person. Time per person needs ``time:read_people`` and
covers only the projects in the reader's manager scope (every project for an administrator);
the deployment can turn it off with ``BALUARTE_TIME_PER_PERSON=false``. Each per-person read is
audited as ``time.people_viewed`` with the filters asked for, never with what was returned.
Time is effort data, not evidence of compliance: it never raises a finding.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.api.v1.manager import unprocessable
from baluarte.api.v1.scope import _instance_id
from baluarte.api.v1.tags import tag_filters
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.manager.scopes import PeopleScope, people_scope
from baluarte.settings import ConfigurationError
from baluarte.timetracking import settings as time_settings
from baluarte.timetracking import summary as time_summary
from baluarte.timetracking import timecard as time_card
from baluarte.timetracking.settings import TimeTrackingSettings

router = APIRouter(tags=["time"])

DEFAULT_DAYS = 30
MAX_DAYS = 3 * 366
PAGE_LIMIT = 200


class SummaryRow(BaseModel):
    key: str
    label: str
    spent_seconds: int
    estimate_seconds: int | None
    issues: int
    entries: int


class SummaryOut(BaseModel):
    by: str
    # [from, to): to is exclusive.
    period: dict[str, date]
    rows: list[SummaryRow]
    totals: SummaryRow
    per_person_enabled: bool
    collected_at: datetime | None


class PersonRowOut(BaseModel):
    username: str
    spent_seconds: int
    issues: int
    entries: int


class PeopleOut(BaseModel):
    period: dict[str, date]
    rows: list[PersonRowOut]
    totals: PersonRowOut
    # True for an administrator; otherwise only the projects of the reader's manager scope.
    all_projects: bool
    collected_at: datetime | None


class ProjectRef(BaseModel):
    gitlab_id: int
    path: str


class EntryOut(BaseModel):
    # The GitLab global ID of the time log.
    id: str
    project: ProjectRef
    spent_at: datetime
    seconds: int
    issue_iid: int | None
    merge_request_iid: int | None
    labels: list[str]
    milestone: str | None
    # Set when GitLab no longer returns the log.
    removed_at: datetime | None


class PersonEntries(BaseModel):
    username: str
    period: dict[str, date]
    items: list[EntryOut]
    next_after: int | None


class WorkItemRef(BaseModel):
    title: str | None
    reference: str | None


class TimecardRow(BaseModel):
    # The time entry's ID in Baluarte, and the GitLab global ID of the time log.
    entry_id: int
    timelog_id: str
    # ``logged`` as recorded, or ``removed``: the negative of a log GitLab no longer returns.
    kind: Literal["logged", "removed"]
    developer: str
    username: str | None
    spent_on: date
    spent_at: datetime
    minutes: float
    removed_on: date | None
    project: ProjectRef
    # ``#12`` for an issue, ``!4`` for a merge request.
    work_item: str | None
    title: str | None
    url: str | None
    milestone: str | None
    iteration: str | None
    parent: WorkItemRef | None
    # The issue's top-level ancestor, usually an epic: the "project" the work belongs to.
    top: WorkItemRef | None
    estimate_minutes: float | None
    # False while the issue's context has not been read from GitLab yet.
    context_read: bool


class TimecardTotals(BaseModel):
    minutes: float
    logged_minutes: float
    removed_minutes: float
    rows: int
    people: int


class TimecardOut(BaseModel):
    period: dict[str, date]
    sort: list[str]
    rows: list[TimecardRow]
    totals: TimecardTotals
    per_person: dict[str, float]
    all_projects: bool
    collected_at: datetime | None


class TimeSettingsOut(BaseModel):
    lookback_days: int
    reread_days: int
    retention_days: int | None
    per_person_enabled: bool


def _period(start: date | None, end: date | None, db_today: date) -> tuple[date, date]:
    """[start, end) in days: by default the last 30 days, today included."""
    last = end or db_today
    first = start or last - timedelta(days=DEFAULT_DAYS - 1)
    if first > last:
        raise unprocessable("invalid_range", "from is after to")
    if (last - first).days >= MAX_DAYS:
        raise unprocessable("range_too_long", f"At most {MAX_DAYS} days")
    return first, last + timedelta(days=1)


def _moment(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _filters(
    start: date | None,
    end: date | None,
    group: str | None,
    project: int | None,
    tag: list[str] | None,
    label: str | None,
    milestone: str | None,
) -> tuple[time_summary.Filters, dict[str, date]]:
    first, following = _period(start, end, datetime.now(UTC).date())
    return (
        time_summary.Filters(
            start=_moment(first),
            end=_moment(following),
            group=group,
            project=project,
            tags=tuple(tag_filters(tag)),
            label=label,
            milestone=milestone,
        ),
        {"from": first, "to": following},
    )


def _per_person_enabled() -> bool:
    try:
        return time_settings.per_person_enabled()
    except ConfigurationError as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "misconfigured", "message": str(error)},
        ) from error


def _people_scope(db: Session, principal: Principal, instance_id: int | None) -> PeopleScope:
    """403 ``per_person_disabled`` when turned off; 403 ``out_of_scope`` without a scope."""
    if not _per_person_enabled():
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={
                "code": "per_person_disabled",
                "message": "Time per person is turned off for this deployment",
            },
        )
    found = people_scope(db, instance_id, principal.user_id, principal.roles)
    if not found.all and not found.project_ids:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={
                "code": "out_of_scope",
                "message": "No groups are assigned to you as a manager",
            },
        )
    return found


def _viewed(db: Session, principal: Principal, filters: time_summary.Filters, **more: Any) -> None:
    audit.record(
        db,
        actor=principal.actor,
        action="time.people_viewed",
        resource_type="time",
        resource_id=None,
        details={**filters.as_details(), **more},
    )
    db.commit()


def _refused(error: time_summary.SummaryRefused) -> HTTPException:
    if error.code == "out_of_scope":
        return HTTPException(
            status.HTTP_403_FORBIDDEN, detail={"code": error.code, "message": str(error)}
        )
    if error.code == "unknown_group":
        return HTTPException(
            status.HTTP_404_NOT_FOUND, detail={"code": error.code, "message": str(error)}
        )
    return unprocessable(error.code, str(error))


FromQuery = Annotated[date | None, Query(alias="from")]
GroupQuery = Annotated[str | None, Query(max_length=1000)]
TagQuery = Annotated[list[str] | None, Query(max_length=20)]
TextQuery = Annotated[str | None, Query(max_length=255)]


@router.get(
    "/time/summary",
    response_model=SummaryOut,
    summary="Time spent and estimated",
    description="Time logged in GitLab in [`from`, `to`] (days, by default the last 30), summed "
    "`by` group, project, label, milestone, week or `tag:<key>`. Filters: `group` (full path, "
    "subgroups included), `project` (GitLab ID), repeatable `tag=key:value` (all must match), "
    "`label`, `milestone`. Estimates are those of the issues with time in the period, each "
    "issue once. No person is named.",
)
def read_summary(
    _: Annotated[Principal, Depends(require(Permission.TIME_READ))],
    db: Annotated[Session, Depends(database)],
    by: Annotated[str, Query(max_length=70)] = "project",
    from_: FromQuery = None,
    to: Annotated[date | None, Query()] = None,
    group: GroupQuery = None,
    project: Annotated[int | None, Query(gt=0)] = None,
    tag: TagQuery = None,
    label: TextQuery = None,
    milestone: TextQuery = None,
) -> SummaryOut:
    filters, period = _filters(from_, to, group, project, tag, label, milestone)
    instance_id = _instance_id(db)
    try:
        rows, total = time_summary.summary(db, instance_id, filters, by)
    except time_summary.SummaryRefused as error:
        raise _refused(error) from error
    fields = ("key", "label", "spent_seconds", "estimate_seconds", "issues", "entries")

    def out(row: time_summary.Row) -> SummaryRow:
        return SummaryRow(**{name: getattr(row, name) for name in fields})

    return SummaryOut(
        by=by,
        period=period,
        rows=[out(row) for row in rows],
        totals=out(total),
        per_person_enabled=_per_person_enabled(),
        collected_at=time_summary.collected_at(db, instance_id),
    )


@router.get(
    "/time/people",
    response_model=PeopleOut,
    summary="Time per person",
    description="Needs `time:read_people`; covers the projects of your manager scope "
    "(administrators: every project). 403 `per_person_disabled` when the deployment turned it "
    "off, `out_of_scope` without a scope. Audited as `time.people_viewed` with the filters.",
)
def read_people(
    principal: Annotated[Principal, Depends(require(Permission.TIME_READ_PEOPLE))],
    db: Annotated[Session, Depends(database)],
    from_: FromQuery = None,
    to: Annotated[date | None, Query()] = None,
    group: GroupQuery = None,
    project: Annotated[int | None, Query(gt=0)] = None,
    tag: TagQuery = None,
    label: TextQuery = None,
    milestone: TextQuery = None,
) -> PeopleOut:
    filters, period = _filters(from_, to, group, project, tag, label, milestone)
    instance_id = _instance_id(db)
    scope = _people_scope(db, principal, instance_id)
    try:
        rows, total = time_summary.people(db, instance_id, filters, scope)
    except time_summary.SummaryRefused as error:
        raise _refused(error) from error
    _viewed(db, principal, filters, username=None)
    fields = ("username", "spent_seconds", "issues", "entries")

    def out(row: time_summary.PersonRow) -> PersonRowOut:
        return PersonRowOut(**{name: getattr(row, name) for name in fields})

    return PeopleOut(
        period=period,
        rows=[out(row) for row in rows],
        totals=out(total),
        all_projects=scope.all,
        collected_at=time_summary.collected_at(db, instance_id),
    )


@router.get(
    "/time/people/{username}",
    response_model=PersonEntries,
    summary="One person's time entries",
    description="Newest first, `limit` per page; pass `next_after` as `after` for the next. "
    "Entries GitLab no longer returns are included with `removed_at`. The summary written with "
    "a log is never stored or returned. Same access and audit as `/time/people`.",
)
def read_person(
    principal: Annotated[Principal, Depends(require(Permission.TIME_READ_PEOPLE))],
    db: Annotated[Session, Depends(database)],
    username: Annotated[str, Path(min_length=1, max_length=255)],
    from_: FromQuery = None,
    to: Annotated[date | None, Query()] = None,
    group: GroupQuery = None,
    project: Annotated[int | None, Query(gt=0)] = None,
    tag: TagQuery = None,
    after: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_LIMIT)] = 100,
) -> PersonEntries:
    filters, period = _filters(from_, to, group, project, tag, None, None)
    instance_id = _instance_id(db)
    scope = _people_scope(db, principal, instance_id)
    try:
        found, following = time_summary.entries_of(
            db, instance_id, username, filters, scope, after, limit
        )
    except time_summary.SummaryRefused as error:
        raise _refused(error) from error
    items = [
        EntryOut(
            id=entry.timelog_id,
            project=ProjectRef(
                gitlab_id=project_row.gitlab_id, path=project_row.path_with_namespace
            ),
            spent_at=entry.spent_at,
            seconds=entry.seconds,
            issue_iid=entry.issue_iid,
            merge_request_iid=entry.merge_request_iid,
            labels=list(entry.labels or []),
            milestone=entry.milestone,
            removed_at=entry.removed_at,
        )
        for entry, project_row in found
    ]
    _viewed(db, principal, filters, username=username, after=after)
    return PersonEntries(username=username, period=period, items=items, next_after=following)


def _card_row(row: time_card.Row) -> TimecardRow:
    def ref(title: str | None, reference: str | None) -> WorkItemRef | None:
        return WorkItemRef(title=title, reference=reference) if title or reference else None

    return TimecardRow(
        entry_id=row.entry_id,
        timelog_id=row.timelog_id,
        kind="removed" if row.kind == "removed" else "logged",
        developer=row.developer,
        username=row.username,
        spent_on=row.day,
        spent_at=row.spent_at,
        minutes=row.minutes,
        removed_on=row.removed_on,
        project=ProjectRef(gitlab_id=row.project_gitlab_id, path=row.repository),
        work_item=row.work_item,
        title=row.title,
        url=row.url,
        milestone=row.milestone,
        iteration=row.iteration,
        parent=ref(row.parent, row.parent_reference),
        top=ref(row.top, row.top_reference),
        estimate_minutes=row.estimate_minutes,
        context_read=row.context_read,
    )


@router.get(
    "/time/timecard",
    response_model=TimecardOut,
    summary="Timecard",
    description="Each time entry in [`from`, `to`] (days, by default the last 30) with the "
    "developer, minutes, date and the issue's milestone, iteration, parent, top-level epic and "
    "estimate. A log GitLab no longer returns is followed by a `removed` row with the negative "
    "minutes, so totals balance. Filters: `group`, `project`, `tag` as for summaries, repeatable "
    "`person` (username), and `milestone`, `iteration`, `parent`, `top` matching the shown text "
    "(`(none)` for empty); `removed=false` leaves removed logs out. `sort` lists columns, each "
    "optionally descending with `-` (default `date,developer`). `format=csv` or `html` "
    "downloads the same rows. Same access as `/time/people`; audited as "
    "`time.timecard_viewed`, or `time.timecard_exported` for a download, with the filters.",
    responses={200: {"content": {"text/csv": {}, "text/html": {}}}},
)
def read_timecard(
    principal: Annotated[Principal, Depends(require(Permission.TIME_READ_PEOPLE))],
    db: Annotated[Session, Depends(database)],
    from_: FromQuery = None,
    to: Annotated[date | None, Query()] = None,
    group: GroupQuery = None,
    project: Annotated[int | None, Query(gt=0)] = None,
    tag: TagQuery = None,
    person: Annotated[list[str] | None, Query(max_length=100)] = None,
    milestone: TextQuery = None,
    iteration: TextQuery = None,
    parent: TextQuery = None,
    top: TextQuery = None,
    removed: bool = True,
    sort: Annotated[str | None, Query(max_length=300)] = None,
    format: Literal["json", "csv", "html"] = "json",
) -> Any:
    filters, period = _filters(from_, to, group, project, tag, None, None)
    instance_id = _instance_id(db)
    scope = _people_scope(db, principal, instance_id)
    context = time_card.ContextFilters(
        people=tuple(sorted({p for p in person or [] if p})),
        milestone=milestone,
        iteration=iteration,
        parent=parent,
        top=top,
        removed=removed,
    )
    try:
        keys = time_card.check_sort(sort)
        card = time_card.build(db, instance_id, filters, scope, context, keys)
    except time_summary.SummaryRefused as error:
        raise _refused(error) from error
    details = {**filters.as_details(), **context.as_details(), "sort": list(keys)}
    action = "time.timecard_viewed" if format == "json" else "time.timecard_exported"
    audit.record(
        db,
        actor=principal.actor,
        action=action,
        resource_type="time",
        resource_id=None,
        details={**details, "format": format},
    )
    db.commit()
    if format != "json":
        last = period["to"] - timedelta(days=1)
        name = f"timecard-{period['from'].isoformat()}-{last.isoformat()}.{format}"
        described = {
            "Period": f"{period['from'].isoformat()} to {last.isoformat()}",
            "Developers": ", ".join(context.people),
            "Group": group,
            "Project": project,
            "Tags": ", ".join(tag or []),
            "Milestone": milestone,
            "Iteration": iteration,
            "Parent": parent,
            "Project (top-level epic)": top,
            "Removed logs": "included" if removed else "left out",
            "Sorted by": ", ".join(keys),
            "Generated": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        body = (
            time_card.to_csv(card)
            if format == "csv"
            else time_card.to_html(card, period, described)
        )
        return Response(
            content=body,
            media_type="text/csv; charset=utf-8" if format == "csv" else "text/html; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "Cache-Control": "no-store",
            },
        )
    totals = card.totals
    return TimecardOut(
        period=period,
        sort=list(keys),
        rows=[_card_row(row) for row in card.rows],
        totals=TimecardTotals(
            minutes=totals.minutes,
            logged_minutes=totals.logged_minutes,
            removed_minutes=totals.removed_minutes,
            rows=totals.rows,
            people=totals.people,
        ),
        per_person=card.per_person,
        all_projects=scope.all,
        collected_at=time_summary.collected_at(db, instance_id),
    )


def _settings_out(value: TimeTrackingSettings) -> TimeSettingsOut:
    return TimeSettingsOut(**value.model_dump(), per_person_enabled=_per_person_enabled())


@router.get(
    "/settings/time",
    response_model=TimeSettingsOut,
    tags=["settings"],
    summary="Time tracking settings",
    description="How far back the first collection reads (`lookback_days`), how much recent "
    "time each collection re-reads to notice deleted logs (`reread_days`) and how long entries "
    "are kept (`retention_days`, null: as long as evidence). `per_person_enabled` comes from "
    "the deployment (`BALUARTE_TIME_PER_PERSON`) and cannot be changed here.",
)
def read_time_settings(
    _: Annotated[Principal, Depends(require(Permission.TIME_READ))],
    db: Annotated[Session, Depends(database)],
) -> TimeSettingsOut:
    return _settings_out(time_settings.load(db))


@router.put(
    "/settings/time",
    response_model=TimeSettingsOut,
    tags=["settings"],
    summary="Change the time tracking settings",
    description="Audited as `settings.time_tracking_updated`.",
)
def update_time_settings(
    principal: Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))],
    db: Annotated[Session, Depends(database)],
    body: Annotated[TimeTrackingSettings, Body()],
) -> TimeSettingsOut:
    time_settings.save(db, body, principal.actor)
    db.commit()
    return _settings_out(body)
