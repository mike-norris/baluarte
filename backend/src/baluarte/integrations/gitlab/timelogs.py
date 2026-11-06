"""Read time logged in GitLab and issue estimates (docs/TIME_TRACKING.md, ADR-0010).

The ``gitlab.collect_time`` job runs after each project discovery, as a collection run of kind
``time``, for every active, unarchived project in monitoring scope. Every request is a GET with
the read-only token; GraphQL queries are sent with GET as well.

- Time logs: GraphQL ``project(fullPath) { timelogs(startTime, endTime, first, after) }`` with
  each log's ``id``, ``spentAt``, ``timeSpent``, ``summary``, ``user { username }`` and the
  ``issue`` or ``mergeRequest`` it was logged on (iid, labels, milestone). A project's first
  collection reads back ``lookback_days``; later ones read from ``reread_days`` ago, so a log
  deleted in GitLab is noticed and marked removed. A log backdated further than that is not
  picked up until it falls in a later window. The results are also filtered by ``spentAt`` here,
  since GitLab's date filters have been inexact on some versions.
- Estimates: ``GET /projects/:id/issues?scope=all&updated_after=...`` with each issue's
  ``time_stats`` (``time_estimate``, ``total_time_spent``), state, labels and milestone.

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

from baluarte.db.models import IssueEstimate, Observation, TimeCursor, TimeEntry
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
PROBE = "timelogs.read"
PAGE_SIZE = 100
MAX_PAGES = 200
STOPPED_BY_REJECTION = "not read: GitLab rejected the token earlier in this collection"

_WORK = "iid labels { nodes { title } } milestone { title }"
QUERY = (
    "query($path: ID!, $start: Time, $end: Time, $after: String) {"
    " project(fullPath: $path) {"
    f" timelogs(startTime: $start, endTime: $end, first: {PAGE_SIZE}, after: $after) {{"
    " pageInfo { hasNextPage endCursor }"
    " nodes { id spentAt timeSpent summary user { username }"
    f" issue {{ {_WORK} }} mergeRequest {{ {_WORK} }} }} }} }} }}"
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
    issue, merge_request = node.get("issue"), node.get("mergeRequest")
    work = issue if isinstance(issue, dict) else merge_request
    return {
        "id": log_id,
        "spent_at": iso(spent_at),
        "seconds": seconds,
        "username": username if isinstance(username, str) else None,
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


@dataclass
class TimeCollection:
    projects: int = 0
    timelogs: Counter[str] = field(default_factory=Counter)
    estimates: Counter[str] = field(default_factory=Counter)
    entries: Counter[str] = field(default_factory=Counter)
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


def read_project(
    client: GitLabClient,
    target: ProjectTarget,
    cursor: TimeCursor | None,
    now: datetime,
    settings: TimeTrackingSettings,
) -> ProjectTime:
    """Read one project's logs and estimates. Raises Unauthorized on 401."""
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
    if failed:
        record(db, instance_id, "project_id", failed, provenance)
    if project.timelogs is not None or project.issues is not None:
        _advance(db, project, last_spent, issues_after)


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
                project = read_project(client, target, cursor, now, settings)
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
    if tally.failed:
        logger.warning("gitlab: time logs of %s projects could not be read", tally.failed)
    return tally
