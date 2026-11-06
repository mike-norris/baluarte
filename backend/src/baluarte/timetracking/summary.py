"""Time spent and estimated, summed by group, project, label, milestone, tag or week.

Read from the stored time entries (``time_entry``) and issue estimates (``issue_estimate``):

- An entry counts when it was spent in the period, is not marked removed, and its project is in
  monitoring scope now and matches the filters.
- ``estimate_seconds`` of a row is the estimate of every issue with time logged under that row
  in the period, each issue once. Merge requests carry no estimate.
- By ``label``, an entry counts under each of its issue's or merge request's labels, so the rows
  can add up to more than the total; entries without labels are under ``(no label)``.
- By ``group``, a project counts under the child group of the ``group`` filter that holds it (the
  filter group itself for its own projects), or under its top-level group without the filter.

Per-person figures (:func:`people`, :func:`entries_of`) take a :class:`PeopleScope` and never
include a project outside it.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.orm import Session

from baluarte import tags as tag_index
from baluarte.db.models import GitLabProject, IssueEstimate, TimeEntry
from baluarte.hierarchy import GroupTree
from baluarte.manager.scopes import PeopleScope
from baluarte.scope.exclusions import coverage

NO_LABEL = "(no label)"
NO_MILESTONE = "(no milestone)"
NO_VALUE = "(none)"
NO_GROUP = "(personal projects)"
GROUPINGS = ("group", "project", "label", "milestone", "week")


class SummaryRefused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Filters:
    start: datetime
    end: datetime
    group: str | None = None
    project: int | None = None
    tags: tuple[tuple[str, str], ...] = ()
    label: str | None = None
    milestone: str | None = None

    def as_details(self) -> dict[str, Any]:
        """For the audit trail: what was asked, never what was returned."""
        return {
            "from": self.start.isoformat(),
            "to": self.end.isoformat(),
            "group": self.group,
            "project": self.project,
            "tags": [f"{k}:{v}" for k, v in self.tags],
            "label": self.label,
            "milestone": self.milestone,
        }


@dataclass
class Row:
    key: str
    label: str
    spent_seconds: int = 0
    estimate_seconds: int | None = None
    issues: int = 0
    entries: int = 0
    _issues: set[tuple[int, int]] = field(default_factory=set, repr=False)


@dataclass
class Selection:
    """The projects the filters allow, with the tree and tags used to group them."""

    project_ids: set[int]
    tree: GroupTree
    index: tag_index.TagIndex
    group_id: int | None


def select_projects(db: Session, instance_id: int | None, filters: Filters) -> Selection:
    """Projects in monitoring scope matching the group, project and tag filters."""
    tree = GroupTree.load(db, instance_id)
    index = tag_index.index(db, instance_id, tree)
    scope = coverage(db, instance_id)
    allowed = {
        node.row_id
        for node in tree.projects.values()
        if scope.for_project(
            node.gitlab_id,
            node.group_gitlab_id or 0,
            None if node.group_gitlab_id is not None else "user",
        )
        is None
    }
    group_id: int | None = None
    if filters.group is not None:
        group_id = next((g for g, path in tree.paths.items() if path == filters.group), None)
        if group_id is None:
            raise SummaryRefused("unknown_group", f"No group {filters.group} in the inventory")
        allowed &= tree.projects_under([group_id])
    if filters.project is not None:
        node = tree.project_by_gitlab_id(filters.project)
        allowed &= {node.row_id} if node is not None else set()
    if filters.tags:
        allowed &= index.projects_with(filters.tags)
    return Selection(allowed, tree, index, group_id)


def _entries(
    instance_id: int | None, filters: Filters, project_ids: Iterable[int]
) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [
        TimeEntry.instance_id == (instance_id or 0),
        TimeEntry.removed_at.is_(None),
        TimeEntry.spent_at >= filters.start,
        TimeEntry.spent_at < filters.end,
        TimeEntry.project_id.in_(list(project_ids)),
    ]
    if filters.label is not None:
        conditions.append(TimeEntry.labels.contains([filters.label]))
    if filters.milestone is not None:
        conditions.append(TimeEntry.milestone == filters.milestone)
    return conditions


def _week(moment: datetime) -> str:
    return (moment.date() - timedelta(days=moment.weekday())).isoformat()


def _group_key(selection: Selection, project_id: int) -> tuple[str, str]:
    node = selection.tree.projects[project_id]
    if node.group_gitlab_id is None:
        return "personal", NO_GROUP
    chain = selection.tree.ancestors(node.group_gitlab_id)
    if selection.group_id is None:
        chosen = chain[-1] if chain else node.group_gitlab_id
    elif selection.group_id in chain:
        index = chain.index(selection.group_id)
        chosen = chain[index - 1] if index > 0 else selection.group_id
    else:
        chosen = node.group_gitlab_id
    return str(chosen), selection.tree.paths.get(chosen, str(chosen))


def _keys(
    by: str,
    selection: Selection,
    project_id: int,
    labels: Sequence[str],
    milestone: str | None,
    spent_at: datetime,
) -> list[tuple[str, str]]:
    if by == "project":
        node = selection.tree.projects[project_id]
        return [(str(node.gitlab_id), node.path)]
    if by == "group":
        return [_group_key(selection, project_id)]
    if by == "label":
        return [(label, label) for label in labels] or [("", NO_LABEL)]
    if by == "milestone":
        return [(milestone, milestone)] if milestone else [("", NO_MILESTONE)]
    if by == "week":
        week = _week(spent_at)
        return [(week, f"Week of {week}")]
    key = by.removeprefix("tag:")
    applied = selection.index.by_project.get(project_id, {}).get(key)
    return [(applied.value, applied.value)] if applied else [("", NO_VALUE)]


def check_grouping(by: str) -> str:
    if by in GROUPINGS:
        return by
    key = by.removeprefix("tag:")
    if by.startswith("tag:") and tag_index.valid_name(key):
        return by
    raise SummaryRefused(
        "invalid_grouping", "by is group, project, label, milestone, week or tag:<key>"
    )


def _estimates(db: Session, issues: Iterable[tuple[int, int]]) -> dict[tuple[int, int], int]:
    wanted = list(issues)
    found: dict[tuple[int, int], int] = {}
    for start in range(0, len(wanted), 1000):
        chunk = wanted[start : start + 1000]
        for project_id, iid, seconds in db.execute(
            select(
                IssueEstimate.project_id, IssueEstimate.issue_iid, IssueEstimate.estimate_seconds
            ).where(
                func.row(IssueEstimate.project_id, IssueEstimate.issue_iid).in_(
                    [func.row(p, i) for p, i in chunk]
                )
            )
        ):
            found[(project_id, iid)] = seconds
    return found


def _aggregate(
    db: Session, statement: Select[Any], by: str, selection: Selection
) -> tuple[list[Row], Row]:
    rows: dict[str, Row] = {}
    total = Row("", "Total")
    for project_id, issue_iid, labels, milestone, day, seconds, count in db.execute(statement):
        issue = (project_id, issue_iid) if issue_iid is not None else None
        for key, label in _keys(by, selection, project_id, labels or [], milestone, day):
            row = rows.setdefault(key, Row(key, label))
            row.spent_seconds += seconds
            row.entries += count
            if issue is not None:
                row._issues.add(issue)
        total.spent_seconds += seconds
        total.entries += count
        if issue is not None:
            total._issues.add(issue)
    estimates = _estimates(db, total._issues)
    for row in [*rows.values(), total]:
        row.issues = len(row._issues)
        row.estimate_seconds = sum(estimates.get(issue, 0) for issue in row._issues)
    return sorted(rows.values(), key=lambda r: (-r.spent_seconds, r.label)), total


def _by_entry(instance_id: int | None, filters: Filters, project_ids: Iterable[int]) -> Select[Any]:
    day = func.date_trunc("day", TimeEntry.spent_at)
    return (
        select(
            TimeEntry.project_id,
            TimeEntry.issue_iid,
            TimeEntry.labels,
            TimeEntry.milestone,
            day,
            func.sum(TimeEntry.seconds),
            func.count(),
        )
        .where(*_entries(instance_id, filters, project_ids))
        .group_by(
            TimeEntry.project_id, TimeEntry.issue_iid, TimeEntry.labels, TimeEntry.milestone, day
        )
    )


def summary(
    db: Session, instance_id: int | None, filters: Filters, by: str
) -> tuple[list[Row], Row]:
    """Rows by ``by`` and the total, for everyone's time together."""
    selection = select_projects(db, instance_id, filters)
    return _aggregate(
        db, _by_entry(instance_id, filters, selection.project_ids), check_grouping(by), selection
    )


@dataclass
class PersonRow:
    username: str
    spent_seconds: int = 0
    issues: int = 0
    entries: int = 0
    _issues: set[tuple[int, int]] = field(default_factory=set, repr=False)


def people(
    db: Session, instance_id: int | None, filters: Filters, scope: PeopleScope
) -> tuple[list[PersonRow], PersonRow]:
    """Time per person in the projects the filters and ``scope`` allow."""
    selection = select_projects(db, instance_id, filters)
    allowed = {p for p in selection.project_ids if scope.allows(p)}
    statement = (
        select(
            TimeEntry.username,
            TimeEntry.project_id,
            TimeEntry.issue_iid,
            func.sum(TimeEntry.seconds),
            func.count(),
        )
        .where(*_entries(instance_id, filters, allowed))
        .group_by(TimeEntry.username, TimeEntry.project_id, TimeEntry.issue_iid)
    )
    rows: dict[str, PersonRow] = {}
    total = PersonRow("")
    for username, project_id, issue_iid, seconds, count in db.execute(statement):
        name = username or "(unknown)"
        row = rows.setdefault(name, PersonRow(name))
        for target in (row, total):
            target.spent_seconds += seconds
            target.entries += count
            if issue_iid is not None:
                target._issues.add((project_id, issue_iid))
    for row in [*rows.values(), total]:
        row.issues = len(row._issues)
    return sorted(rows.values(), key=lambda r: (-r.spent_seconds, r.username)), total


def entries_of(
    db: Session,
    instance_id: int | None,
    username: str,
    filters: Filters,
    scope: PeopleScope,
    after: int | None,
    limit: int,
) -> tuple[list[tuple[TimeEntry, GitLabProject]], int | None]:
    """One person's entries, newest first, in the projects ``scope`` allows; removed ones too."""
    selection = select_projects(db, instance_id, filters)
    allowed = [p for p in selection.project_ids if scope.allows(p)]
    statement = (
        select(TimeEntry, GitLabProject)
        .join(GitLabProject, GitLabProject.id == TimeEntry.project_id)
        .where(
            TimeEntry.instance_id == (instance_id or 0),
            TimeEntry.username == username,
            TimeEntry.spent_at >= filters.start,
            TimeEntry.spent_at < filters.end,
            TimeEntry.project_id.in_(allowed),
        )
        .order_by(TimeEntry.id.desc())
        .limit(limit + 1)
    )
    if after is not None:
        statement = statement.where(TimeEntry.id < after)
    found = [(entry, project) for entry, project in db.execute(statement).tuples()]
    following = found[limit - 1][0].id if len(found) > limit else None
    return found[:limit], following


def estimates_for(
    db: Session, instance_id: int | None, projects: set[int], since: datetime, until: datetime
) -> dict[int, int]:
    """Per project, the estimate of the issues with time logged in [since, until), each once."""
    if not projects:
        return {}
    issues = (
        select(TimeEntry.project_id, TimeEntry.issue_iid)
        .where(
            TimeEntry.instance_id == (instance_id or 0),
            TimeEntry.removed_at.is_(None),
            TimeEntry.spent_at >= since,
            TimeEntry.spent_at < until,
            TimeEntry.project_id.in_(projects),
            TimeEntry.issue_iid.is_not(None),
        )
        .distinct()
        .subquery()
    )
    return dict(
        db.execute(
            select(IssueEstimate.project_id, func.sum(IssueEstimate.estimate_seconds))
            .join(
                issues,
                (issues.c.project_id == IssueEstimate.project_id)
                & (issues.c.issue_iid == IssueEstimate.issue_iid),
            )
            .group_by(IssueEstimate.project_id)
        ).tuples()
    )


def collected_at(db: Session, instance_id: int | None) -> datetime | None:
    """When time was last read, or None when it never was."""
    return db.execute(
        select(func.max(TimeEntry.updated_at)).where(TimeEntry.instance_id == (instance_id or 0))
    ).scalar_one_or_none()
