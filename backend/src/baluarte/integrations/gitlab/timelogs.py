"""Read time logged in GitLab and issue estimates (docs/TIME_TRACKING.md, ADR-0010).

The ``gitlab.collect_time`` job runs after each project discovery, as a collection run of kind
``time``, for every active, unarchived project in monitoring scope. Every request is a GET with
the read-only token; GraphQL queries are sent with GET as well.

- Time logs: GraphQL ``project(fullPath) { timelogs(startTime, endTime, first, after) }`` with
  each log's ``id``, ``spentAt``, ``timeSpent``, ``summary``, ``user { username name }`` and the
  ``issue`` or ``mergeRequest`` it was logged on (iid, labels, milestone). A project's first
  collection reads back ``lookback_days``; later ones read from ``reread_days`` ago, so a log
  deleted in GitLab is noticed and marked removed. A log backdated further than that is not
  picked up until it falls in a later window. The results are also filtered by ``spentAt`` here,
  since GitLab's date filters have been inexact on some versions.
- Estimates: ``GET /projects/:id/issues?scope=all&updated_after=...`` with each issue's
  ``time_stats`` (``time_estimate``, ``total_time_spent``), state, labels and milestone.
- Display names, for timecards: from each log's ``user { name }``, and for people with no log
  read again, ``GET /users?username=...``.
- Issue context, for timecards: GraphQL ``project(fullPath) { workItems(iids) }`` with each
  issue's type, title, parent, ancestors (the first is the top-level one, usually an epic),
  milestone, iteration and estimate. Read for issues with logged time that have no context yet,
  were updated since the last read, or were last read more than ``CONTEXT_MAX_AGE`` ago. On a
  GitLab without iterations the query is sent again without them, and iterations are recorded
  as unsupported rather than as none.

The summary of a log is never stored: only its SHA-256, in the observation and the time entry.
Each read is an observation (``TIMELOG_SOURCE`` or ``ESTIMATE_SOURCE``). A read GitLab refuses,
or a field it does not know, is recorded with its status and leaves the time unknown, never zero.
A 401 stops the collection.
"""

import hashlib
import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from baluarte.db.models import IssueContext, IssueEstimate, Observation, TimeCursor, TimeEntry
from baluarte.evidence.observations import Observed, Provenance, payload_digest, record
from baluarte.integrations.gitlab.client import (
    Forbidden,
    GitLabClient,
    GitLabError,
    NotFound,
    Unauthorized,
    UnsupportedField,
)
from baluarte.integrations.gitlab.project_reads import ProjectTarget, targets
from baluarte.timetracking.settings import TimeTrackingSettings

logger = logging.getLogger(__name__)

TIMELOG_SOURCE = "GET /api/graphql project.timelogs"
ESTIMATE_SOURCE = "GET /projects/:id/issues (time_stats)"
CONTEXT_SOURCE = "GET /api/graphql project.workItems"
PROBE = "timelogs.read"
PAGE_SIZE = 100
MAX_PAGES = 200
STOPPED_BY_REJECTION = "not read: GitLab rejected the token earlier in this collection"
CONTEXT_BATCH = 50
# At most this many issues' context is read per project and collection; the rest follow later.
CONTEXT_LIMIT = 1000
CONTEXT_MAX_AGE = timedelta(days=1)
# People whose display name is looked up per collection, when no log read gave it.
NAME_LOOKUPS = 50

_WORK = "iid labels { nodes { title } } milestone { title }"
QUERY = (
    "query($path: ID!, $start: Time, $end: Time, $after: String) {"
    " project(fullPath: $path) {"
    f" timelogs(startTime: $start, endTime: $end, first: {PAGE_SIZE}, after: $after) {{"
    " pageInfo { hasNextPage endCursor }"
    " nodes { id spentAt timeSpent summary user { username name }"
    f" issue {{ {_WORK} }} mergeRequest {{ {_WORK} }} }} }} }} }}"
)
_ITEM = "iid title webUrl reference(full: true) workItemType { name }"
_ITERATION = (
    " ... on WorkItemWidgetIteration {"
    " iteration { title startDate dueDate iterationCadence { title } } }"
)


def context_query(iterations: bool) -> str:
    """The work item query for issue context, with or without the iteration widget."""
    return (
        "query($path: ID!, $iids: [String!]) {"
        " project(fullPath: $path) {"
        f" workItems(iids: $iids, first: {CONTEXT_BATCH}) {{ nodes {{"
        " iid title webUrl workItemType { name } widgets { type"
        f" ... on WorkItemWidgetHierarchy {{ parent {{ {_ITEM} }}"
        f" ancestors {{ nodes {{ {_ITEM} }} }} }}"
        " ... on WorkItemWidgetMilestone { milestone { title } }"
        " ... on WorkItemWidgetTimeTracking { timeEstimate }"
        f"{_ITERATION if iterations else ''} }} }} }} }} }}"
    )


# The smallest query, for the capability probe.
PROBE_QUERY = (
    "query($path: ID!) { project(fullPath: $path) {"
    " timelogs(first: 1) { nodes { id spentAt timeSpent } } } }"
)


def iso(moment: datetime | None) -> str | None:
    return moment.isoformat().replace("+00:00", "Z") if moment is not None else None


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def summary_digest(summary: Any) -> str | None:
    if not isinstance(summary, str) or not summary:
        return None
    return hashlib.sha256(summary.encode()).hexdigest()


def _titles(part: Any) -> list[str]:
    nodes = (part or {}).get("labels", {}) if isinstance(part, dict) else {}
    nodes = nodes.get("nodes") if isinstance(nodes, dict) else None
    return sorted(
        {n["title"] for n in nodes or [] if isinstance(n, dict) and isinstance(n.get("title"), str)}
    )


def _milestone(part: Any) -> str | None:
    milestone = part.get("milestone") if isinstance(part, dict) else None
    title = milestone.get("title") if isinstance(milestone, dict) else None
    return title if isinstance(title, str) else None


def _iid(part: Any) -> int | None:
    raw = part.get("iid") if isinstance(part, dict) else None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def timelog_entry(node: Mapping[str, Any]) -> dict[str, Any] | None:
    """One log as recorded; None for a node without an ID, time or duration."""
    log_id, spent_at, seconds = (
        node.get("id"),
        parse_time(node.get("spentAt")),
        node.get("timeSpent"),
    )
    if not isinstance(log_id, str) or spent_at is None or type(seconds) is not int:
        return None
    user = node.get("user")
    username = user.get("username") if isinstance(user, dict) else None
    name = user.get("name") if isinstance(user, dict) else None
    issue, merge_request = node.get("issue"), node.get("mergeRequest")
    work = issue if isinstance(issue, dict) else merge_request
    return {
        "id": log_id,
        "spent_at": iso(spent_at),
        "seconds": seconds,
        "username": username if isinstance(username, str) else None,
        "user_name": name if isinstance(name, str) and name else None,
        "issue_iid": _iid(issue),
        "merge_request_iid": None if isinstance(issue, dict) else _iid(merge_request),
        "labels": _titles(work),
        "milestone": _milestone(work),
        "summary_sha256": summary_digest(node.get("summary")),
    }


def read_timelogs(
    client: GitLabClient, target: ProjectTarget, start: datetime, end: datetime
) -> list[dict[str, Any]] | None:
    """Every log spent in [start, end], sorted; None when GitLab has no such project.

    Raises GitLabError (UnsupportedField when GitLab does not know a field).
    """
    found: dict[str, dict[str, Any]] = {}
    after: str | None = None
    for _page in range(MAX_PAGES):
        variables: dict[str, Any] = {"path": target.path, "start": iso(start), "end": iso(end)}
        if after is not None:
            variables["after"] = after
        project = client.graphql(QUERY, variables).get("project")
        if not isinstance(project, dict):
            return None
        logs = project.get("timelogs") or {}
        for node in logs.get("nodes") or []:
            entry = timelog_entry(node) if isinstance(node, dict) else None
            spent = parse_time(entry["spent_at"]) if entry else None
            if entry is not None and spent is not None and start <= spent <= end:
                found[entry["id"]] = entry
        info = logs.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return sorted(found.values(), key=lambda e: (e["spent_at"], e["id"]))
        after = info.get("endCursor")
        if not isinstance(after, str):
            raise GitLabError("timelogs: a next page without a cursor")
    raise GitLabError(f"timelogs: more than {MAX_PAGES} pages")


def issue_entry(item: Mapping[str, Any]) -> dict[str, Any] | None:
    iid, stats = item.get("iid"), item.get("time_stats")
    if type(iid) is not int or not isinstance(stats, dict):
        return None
    estimate, spent = stats.get("time_estimate"), stats.get("total_time_spent")
    milestone = item.get("milestone")
    labels = item.get("labels")
    return {
        "iid": iid,
        "state": item.get("state") if isinstance(item.get("state"), str) else None,
        "estimate_seconds": estimate if type(estimate) is int else 0,
        "spent_seconds": spent if type(spent) is int else 0,
        "labels": sorted(label for label in labels or [] if isinstance(label, str)),
        "milestone": milestone.get("title") if isinstance(milestone, dict) else None,
        "updated_at": item.get("updated_at") if isinstance(item.get("updated_at"), str) else None,
    }


def read_estimates(
    client: GitLabClient, target: ProjectTarget, updated_after: datetime
) -> list[dict[str, Any]]:
    """Issues updated since ``updated_after`` with their estimate and time spent."""
    found: list[dict[str, Any]] = []
    params = {"scope": "all", "updated_after": iso(updated_after), "order_by": "updated_at"}
    for page in client.paginate(f"/projects/{target.gitlab_id}/issues", params):
        for item in page:
            entry = issue_entry(item)
            if entry is not None:
                found.append(entry)
    return sorted(found, key=lambda e: e["iid"])


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _item(node: Any) -> dict[str, Any] | None:
    """A parent or ancestor work item: type, reference and title."""
    if not isinstance(node, dict):
        return None
    kind = node.get("workItemType")
    return {
        "type": _text(kind.get("name")) if isinstance(kind, dict) else None,
        "reference": _text(node.get("reference")),
        "title": _text(node.get("title")),
    }


def iteration_label(iteration: Any) -> str | None:
    """An iteration's title, or its cadence and dates when it has no title."""
    if not isinstance(iteration, dict):
        return None
    title = _text(iteration.get("title"))
    if title:
        return title
    cadence = iteration.get("iterationCadence")
    cadence_title = _text(cadence.get("title")) if isinstance(cadence, dict) else None
    start, due = _text(iteration.get("startDate")), _text(iteration.get("dueDate"))
    dates = f"{start} to {due}" if start and due else start or due
    if cadence_title and dates:
        return f"{cadence_title}: {dates}"
    return cadence_title or dates


def context_entry(node: Mapping[str, Any], iterations: bool) -> dict[str, Any] | None:
    """One issue's context as recorded; None for a node without an iid."""
    iid = _iid(node)
    if iid is None:
        return None
    kind = node.get("workItemType")
    found: dict[str, Any] = {
        "iid": iid,
        "type": _text(kind.get("name")) if isinstance(kind, dict) else None,
        "title": _text(node.get("title")),
        "web_url": _text(node.get("webUrl")),
        "parent": None,
        "top": None,
        "milestone": None,
        "iteration_status": "ok" if iterations else "unsupported",
        "iteration": None,
        "estimate_seconds": None,
    }
    for widget in node.get("widgets") or []:
        if not isinstance(widget, dict):
            continue
        if "parent" in widget or "ancestors" in widget:
            found["parent"] = _item(widget.get("parent"))
            ancestors = widget.get("ancestors")
            nodes = ancestors.get("nodes") if isinstance(ancestors, dict) else None
            # GitLab lists ancestors from the top down.
            found["top"] = _item(nodes[0]) if isinstance(nodes, list) and nodes else None
        if "milestone" in widget:
            found["milestone"] = _milestone(widget)
        if "timeEstimate" in widget and type(widget["timeEstimate"]) is int:
            found["estimate_seconds"] = widget["timeEstimate"]
        if "iteration" in widget:
            found["iteration"] = iteration_label(widget["iteration"])
    return found


def read_contexts(
    client: GitLabClient, target: ProjectTarget, iids: list[int]
) -> list[dict[str, Any]] | None:
    """The context of each issue GitLab returns; None when it has no such project.

    Tries the query with iterations first and, when GitLab does not know them, without.
    Raises GitLabError (UnsupportedField when GitLab has no work items).
    """
    iterations = True
    found: list[dict[str, Any]] = []
    for start in range(0, len(iids), CONTEXT_BATCH):
        variables = {
            "path": target.path,
            "iids": [str(i) for i in iids[start : start + CONTEXT_BATCH]],
        }
        try:
            project = client.graphql(context_query(iterations), variables).get("project")
        except UnsupportedField:
            if not iterations:
                raise
            iterations = False
            project = client.graphql(context_query(iterations), variables).get("project")
        if not isinstance(project, dict):
            return None
        items = project.get("workItems") or {}
        for node in items.get("nodes") or []:
            entry = context_entry(node, iterations) if isinstance(node, dict) else None
            if entry is not None:
                found.append(entry)
    return sorted(found, key=lambda e: e["iid"])


def _status(error: GitLabError) -> str:
    if isinstance(error, UnsupportedField):
        return "unsupported"
    if isinstance(error, Forbidden):
        return "forbidden"
    if isinstance(error, NotFound):
        return "not_found"
    return "failed"


@dataclass
class ProjectTime:
    """What was read for one project."""

    target: ProjectTarget
    history_since: datetime
    start: datetime
    end: datetime
    issues_after: datetime
    timelogs: list[dict[str, Any]] | None = None
    timelog_status: str = "failed"
    timelog_error: str | None = None
    issues: list[dict[str, Any]] | None = None
    issue_status: str = "failed"
    issue_error: str | None = None
    # Issues whose context was asked for, and what came back.
    context_iids: list[int] = field(default_factory=list)
    contexts: list[dict[str, Any]] | None = None
    context_status: str = "skipped"
    context_error: str | None = None


@dataclass
class TimeCollection:
    projects: int = 0
    timelogs: Counter[str] = field(default_factory=Counter)
    estimates: Counter[str] = field(default_factory=Counter)
    entries: Counter[str] = field(default_factory=Counter)
    contexts: Counter[str] = field(default_factory=Counter)
    names: Counter[str] = field(default_factory=Counter)
    rejected: bool = False

    @property
    def failed(self) -> int:
        return self.timelogs.get("failed", 0)

    def as_counts(self) -> dict[str, Any]:
        return {
            "projects": self.projects,
            "timelogs": dict(sorted(self.timelogs.items())),
            "estimates": dict(sorted(self.estimates.items())),
            "entries": dict(sorted(self.entries.items())),
            "contexts": dict(sorted(self.contexts.items())),
            "names": dict(sorted(self.names.items())),
        }


def _window(
    cursor: TimeCursor | None, now: datetime, settings: TimeTrackingSettings
) -> tuple[datetime, datetime, datetime]:
    """(history_since, start, issues_after) for the next read."""
    if cursor is None:
        since = now - timedelta(days=settings.lookback_days)
        return since, since, since
    start = max(cursor.history_since, now - timedelta(days=settings.reread_days))
    issues_after = cursor.issues_updated_after or cursor.history_since
    # A little overlap, so an issue updated as the last collection read is not missed.
    return cursor.history_since, start, issues_after - timedelta(minutes=5)


@dataclass(frozen=True)
class KnownIssues:
    """A project's issues with logged time, and those whose context is recent enough."""

    logged: frozenset[int] = frozenset()
    fresh: frozenset[int] = frozenset()


def wanted_contexts(project: "ProjectTime", known: KnownIssues) -> list[int]:
    """Issues whose context to read: logged ones without recent context, and those updated."""
    logged = set(known.logged)
    logged.update(e["issue_iid"] for e in project.timelogs or [] if e["issue_iid"] is not None)
    updated = {issue["iid"] for issue in project.issues or []}
    wanted = (logged - known.fresh) | (updated & logged)
    return sorted(wanted)[:CONTEXT_LIMIT]


def read_project(
    client: GitLabClient,
    target: ProjectTarget,
    cursor: TimeCursor | None,
    now: datetime,
    settings: TimeTrackingSettings,
    known: KnownIssues | None = None,
) -> ProjectTime:
    """Read one project's logs, estimates and issue context. Raises Unauthorized on 401."""
    since, start, issues_after = _window(cursor, now, settings)
    project = ProjectTime(target, since, start, now, issues_after)
    try:
        logs = read_timelogs(client, target, start, now)
        if logs is None:
            project.timelog_status, project.timelog_error = (
                "not_found",
                "GraphQL returned no project",
            )
        else:
            project.timelogs, project.timelog_status = logs, "ok"
    except Unauthorized:
        raise
    except GitLabError as error:
        project.timelog_status, project.timelog_error = _status(error), str(error)
    try:
        project.issues = read_estimates(client, target, issues_after)
        project.issue_status = "ok"
    except Unauthorized:
        raise
    except GitLabError as error:
        project.issue_status, project.issue_error = _status(error), str(error)
    if project.timelogs is None:
        return project
    project.context_iids = wanted_contexts(project, known or KnownIssues())
    if not project.context_iids:
        return project
    try:
        contexts = read_contexts(client, target, project.context_iids)
        if contexts is None:
            project.context_status, project.context_error = (
                "not_found",
                "GraphQL returned no project",
            )
        else:
            project.contexts, project.context_status = contexts, "ok"
    except Unauthorized:
        raise
    except GitLabError as error:
        project.context_status, project.context_error = _status(error), str(error)
    return project


def _observation(
    instance_id: int,
    project: ProjectTime,
    provenance: Provenance,
    source: str,
    payload: dict[str, Any],
) -> Observation:
    return Observation(
        instance_id=instance_id,
        project_id=project.target.row_id,
        collection_run_id=provenance.collection_run_id,
        collection_job_id=provenance.collection_job_id,
        source=source,
        source_ref=project.target.path,
        collected_at=func.now(),
        collection_status="ok",
        payload=payload,
        payload_digest=payload_digest(payload),
    )


def write(
    db: Session,
    instance_id: int,
    project: ProjectTime,
    provenance: Provenance,
    tally: TimeCollection,
) -> None:
    """Record the project's time entries, estimates and cursor. The caller commits."""
    target = project.target
    failed: list[Observed] = []
    last_spent: datetime | None = None
    if project.timelogs is not None:
        observation = _observation(
            instance_id,
            project,
            provenance,
            TIMELOG_SOURCE,
            {
                "project": target.path,
                "window": {"start": iso(project.start), "end": iso(project.end)},
                "timelogs": project.timelogs,
            },
        )
        db.add(observation)
        db.flush()
        seen: set[str] = set()
        for entry in project.timelogs:
            seen.add(entry["id"])
            spent_at = parse_time(entry["spent_at"])
            last_spent = max(last_spent, spent_at) if last_spent and spent_at else spent_at
            values = {
                "instance_id": instance_id,
                "project_id": target.row_id,
                "timelog_id": entry["id"],
                "username": entry["username"],
                "user_name": entry["user_name"],
                "spent_at": spent_at,
                "seconds": entry["seconds"],
                "issue_iid": entry["issue_iid"],
                "merge_request_iid": entry["merge_request_iid"],
                "labels": entry["labels"],
                "milestone": entry["milestone"],
                "summary_sha256": entry["summary_sha256"],
                "observation_id": observation.id,
            }
            db.execute(
                pg_insert(TimeEntry)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["instance_id", "timelog_id"],
                    set_={
                        **{
                            k: v
                            for k, v in values.items()
                            if k not in ("instance_id", "timelog_id")
                        },
                        "updated_at": func.now(),
                        "removed_at": None,
                    },
                )
            )
        tally.entries["read"] += len(project.timelogs)
        # Older entries of the same people take their current display name too.
        names = {
            e["username"]: e["user_name"]
            for e in project.timelogs
            if e["username"] is not None and e["user_name"] is not None
        }
        for username, user_name in sorted(names.items()):
            db.execute(
                update(TimeEntry)
                .where(
                    TimeEntry.instance_id == instance_id,
                    TimeEntry.username == username,
                    TimeEntry.user_name.is_distinct_from(user_name),
                )
                .values(user_name=user_name)
            )
        # Logs of the window GitLab no longer returns were deleted there.
        removed = db.execute(
            update(TimeEntry)
            .where(
                TimeEntry.project_id == target.row_id,
                TimeEntry.spent_at >= project.start,
                TimeEntry.spent_at <= project.end,
                TimeEntry.removed_at.is_(None),
                TimeEntry.timelog_id.not_in(seen) if seen else TimeEntry.id.is_not(None),
            )
            .values(removed_at=func.now(), updated_at=func.now())
        )
        tally.entries["removed"] += removed.rowcount or 0  # type: ignore[attr-defined]
    else:
        failed.append(
            Observed(
                target.row_id,
                TIMELOG_SOURCE,
                target.path,
                project.timelog_status,
                error=project.timelog_error,
            )
        )
    tally.timelogs[project.timelog_status] += 1
    issues_after: datetime | None = None
    if project.issues is not None:
        observation = _observation(
            instance_id,
            project,
            provenance,
            ESTIMATE_SOURCE,
            {
                "project": target.path,
                "updated_after": iso(project.issues_after),
                "issues": project.issues,
            },
        )
        db.add(observation)
        db.flush()
        for issue in project.issues:
            updated = parse_time(issue["updated_at"])
            if updated is not None:
                issues_after = max(issues_after, updated) if issues_after else updated
            values = {
                "project_id": target.row_id,
                "issue_iid": issue["iid"],
                "instance_id": instance_id,
                "state": issue["state"],
                "estimate_seconds": issue["estimate_seconds"],
                "spent_seconds": issue["spent_seconds"],
                "labels": issue["labels"],
                "milestone": issue["milestone"],
                "issue_updated_at": updated,
                "observation_id": observation.id,
            }
            db.execute(
                pg_insert(IssueEstimate)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["project_id", "issue_iid"],
                    set_={
                        **{k: v for k, v in values.items() if k not in ("project_id", "issue_iid")},
                        "read_at": func.now(),
                    },
                )
            )
    else:
        failed.append(
            Observed(
                target.row_id,
                ESTIMATE_SOURCE,
                target.path,
                project.issue_status,
                error=project.issue_error,
            )
        )
    tally.estimates[project.issue_status] += 1
    _write_contexts(db, instance_id, project, provenance, failed)
    if project.context_iids:
        tally.contexts[project.context_status] += 1
    if failed:
        record(db, instance_id, "project_id", failed, provenance)
    if project.timelogs is not None or project.issues is not None:
        _advance(db, project, last_spent, issues_after)


def _write_contexts(
    db: Session,
    instance_id: int,
    project: ProjectTime,
    provenance: Provenance,
    failed: list[Observed],
) -> None:
    target = project.target
    if not project.context_iids:
        return
    if project.contexts is None:
        failed.append(
            Observed(
                target.row_id,
                CONTEXT_SOURCE,
                target.path,
                project.context_status,
                error=project.context_error,
            )
        )
        return
    observation = _observation(
        instance_id,
        project,
        provenance,
        CONTEXT_SOURCE,
        {"project": target.path, "iids": project.context_iids, "items": project.contexts},
    )
    db.add(observation)
    db.flush()
    for item in project.contexts:
        parent, top = item["parent"] or {}, item["top"] or {}
        values = {
            "project_id": target.row_id,
            "issue_iid": item["iid"],
            "instance_id": instance_id,
            "work_item_type": item["type"],
            "title": item["title"],
            "web_url": item["web_url"],
            "parent_type": parent.get("type"),
            "parent_reference": parent.get("reference"),
            "parent_title": parent.get("title"),
            "top_type": top.get("type"),
            "top_reference": top.get("reference"),
            "top_title": top.get("title"),
            "milestone": item["milestone"],
            "iteration_status": item["iteration_status"],
            "iteration": item["iteration"],
            "estimate_seconds": item["estimate_seconds"],
            "observation_id": observation.id,
        }
        db.execute(
            pg_insert(IssueContext)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["project_id", "issue_iid"],
                set_={
                    **{k: v for k, v in values.items() if k not in ("project_id", "issue_iid")},
                    "read_at": func.now(),
                },
            )
        )


def _advance(
    db: Session, project: ProjectTime, last_spent: datetime | None, issues_after: datetime | None
) -> None:
    values: dict[str, Any] = {
        "project_id": project.target.row_id,
        "history_since": project.history_since,
        "last_spent_at": last_spent,
        "issues_updated_after": issues_after if project.issues is not None else None,
        "updated_at": func.now(),
    }
    later = {
        column: func.greatest(getattr(TimeCursor, column), pg_insert(TimeCursor).excluded[column])
        for column in ("last_spent_at", "issues_updated_after")
    }
    db.execute(
        pg_insert(TimeCursor)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["project_id"], set_={**later, "updated_at": func.now()}
        )
    )


def _known_issues(db: Session, project_id: int, now: datetime) -> KnownIssues:
    logged = db.scalars(
        select(TimeEntry.issue_iid)
        .where(TimeEntry.project_id == project_id, TimeEntry.issue_iid.is_not(None))
        .distinct()
    )
    fresh = db.scalars(
        select(IssueContext.issue_iid).where(
            IssueContext.project_id == project_id,
            IssueContext.read_at > now - CONTEXT_MAX_AGE,
        )
    )
    return KnownIssues(frozenset(i for i in logged if i is not None), frozenset(fresh))


def collect(
    client: GitLabClient,
    engine: Engine,
    instance_id: int,
    settings: TimeTrackingSettings,
    provenance: Provenance | None = None,
) -> TimeCollection:
    """Read and record every monitored project's time logs and estimates."""
    provenance = provenance or Provenance()
    tally = TimeCollection()
    for target in targets(engine, instance_id):
        if target.archived:
            continue
        with Session(engine) as db:
            now: datetime = db.execute(select(func.now())).scalar_one()
            cursor = db.get(TimeCursor, target.row_id)
            known = _known_issues(db, target.row_id, now)
            db.expunge_all()
        if tally.rejected:
            since, start, after = _window(cursor, now, settings)
            project = ProjectTime(
                target,
                since,
                start,
                now,
                after,
                timelog_error=STOPPED_BY_REJECTION,
                issue_error=STOPPED_BY_REJECTION,
            )
        else:
            try:
                project = read_project(client, target, cursor, now, settings, known)
            except Unauthorized as error:
                tally.rejected = True
                since, start, after = _window(cursor, now, settings)
                project = ProjectTime(
                    target,
                    since,
                    start,
                    now,
                    after,
                    timelog_error=str(error),
                    issue_error=str(error),
                )
        with Session(engine) as db:
            write(db, instance_id, project, provenance, tally)
            db.commit()
        tally.projects += 1
    if not tally.rejected:
        _name_people(client, engine, instance_id, tally)
    if tally.failed:
        logger.warning("gitlab: time logs of %s projects could not be read", tally.failed)
    return tally


def _name_people(
    client: GitLabClient, engine: Engine, instance_id: int, tally: TimeCollection
) -> None:
    """Look up the display name of people whose entries have none: ``GET /users?username=``.

    Names usually come with the logs read again; this covers people with no recent time.
    """
    with Session(engine) as db:
        unnamed = sorted(
            username
            for username in db.scalars(
                select(TimeEntry.username)
                .where(TimeEntry.instance_id == instance_id, TimeEntry.username.is_not(None))
                .group_by(TimeEntry.username)
                .having(func.count(TimeEntry.user_name) == 0)
            )
            if username is not None
        )
    for username in unnamed[:NAME_LOOKUPS]:
        try:
            found = client.get_json("/users", {"username": username})
        except Unauthorized:
            tally.rejected = True
            return
        except GitLabError:
            tally.names["failed"] += 1
            continue
        first = found[0] if isinstance(found, list) and found else None
        name = _text(first.get("name")) if isinstance(first, dict) else None
        if name is None:
            tally.names["not_found"] += 1
            continue
        with Session(engine) as db:
            db.execute(
                update(TimeEntry)
                .where(TimeEntry.instance_id == instance_id, TimeEntry.username == username)
                .values(user_name=name)
            )
            db.commit()
        tally.names["ok"] += 1
