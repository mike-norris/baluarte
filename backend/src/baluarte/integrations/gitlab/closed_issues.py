"""Read closed issues for the time rules on closed issues (ADR-0011, docs/TIME_TRACKING.md).

Run by the ``time`` collection for each project it reads, after its time logs. Every request is
a GET with the read-only token.

- Issues: a project's first read lists every closed issue,
  ``GET /projects/:id/issues?scope=all&state=closed``; later reads list the issues updated since
  the last one, ``...&updated_after=...`` in any state, so an issue closed since is added and a
  reopened one is removed. Each issue's type, title, URL, labels, ``time_stats``
  (``time_estimate``, ``total_time_spent``), ``merge_requests_count``, ``closed_at`` and
  ``updated_at`` are kept.
- Commits: needed only for a closed issue with no time spent. An issue a merge request closes or
  is linked to (``merge_requests_count`` > 0) has ``merge_request``. Otherwise its system notes,
  ``GET /projects/:id/issues/:iid/notes``, are read for "mentioned in commit" or "mentioned in
  merge request": ``mention``, else ``none``. A note GitLab refuses leaves ``unknown``. The result
  holds until the issue is updated again. Issues with time spent are ``not_checked``.

Each read is an observation (``CLOSED_SOURCE``, ``NOTES_SOURCE``). A read that fails leaves the
stored issues as they were.
"""

import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from baluarte.db.models import ClosedIssue, Observation, TimeCursor
from baluarte.evidence.observations import Observed, Provenance, payload_digest, record
from baluarte.integrations.gitlab.client import GitLabClient, GitLabError, Unauthorized
from baluarte.integrations.gitlab.project_reads import ProjectTarget

logger = logging.getLogger(__name__)

CLOSED_SOURCE = "GET /projects/:id/issues (closed)"
NOTES_SOURCE = "GET /projects/:id/issues/:iid/notes (mentions)"
# Commit checks per project and collection; the rest follow in later collections.
MAX_NOTE_CHECKS = 1000
MENTIONS = ("mentioned in commit", "mentioned in merge request")
# A little overlap, so an issue updated as the last collection read is not missed.
OVERLAP = timedelta(minutes=5)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def issue_entry(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """One issue as recorded; None without an iid."""
    iid = item.get("iid")
    if type(iid) is not int:
        return None
    raw = item.get("time_stats")
    stats: Mapping[str, Any] = raw if isinstance(raw, dict) else {}
    labels = item.get("labels")
    return {
        "iid": iid,
        "state": _text(item.get("state")),
        "type": _text(item.get("issue_type")) or _text(item.get("type")),
        "title": _text(item.get("title")),
        "web_url": _text(item.get("web_url")),
        "labels": sorted({label for label in labels or [] if isinstance(label, str)}),
        "estimate_seconds": _int(stats.get("time_estimate")),
        "spent_seconds": _int(stats.get("total_time_spent")),
        "merge_requests_count": _int(item.get("merge_requests_count")),
        "closed_at": _text(item.get("closed_at")),
        "updated_at": _text(item.get("updated_at")),
    }


def read_issues(
    client: GitLabClient, target: ProjectTarget, updated_after: datetime | None
) -> list[dict[str, Any]]:
    """Every closed issue, or every issue updated since ``updated_after`` in any state."""
    params: dict[str, Any] = {"scope": "all", "order_by": "updated_at", "sort": "asc"}
    if updated_after is None:
        params["state"] = "closed"
    else:
        params["updated_after"] = (updated_after - OVERLAP).isoformat().replace("+00:00", "Z")
    found: dict[int, dict[str, Any]] = {}
    for page in client.paginate(f"/projects/{target.gitlab_id}/issues", params):
        for item in page:
            entry = issue_entry(item)
            if entry is not None:
                found[entry["iid"]] = entry
    return [found[iid] for iid in sorted(found)]


def mentioned(client: GitLabClient, target: ProjectTarget, iid: int) -> bool:
    """Whether a system note says a commit or merge request mentions the issue."""
    path = f"/projects/{target.gitlab_id}/issues/{iid}/notes"
    for page in client.paginate(path, {"sort": "asc", "order_by": "created_at"}):
        for note in page:
            body = note.get("body")
            if note.get("system") is True and isinstance(body, str):
                if body.lower().startswith(MENTIONS):
                    return True
    return False


@dataclass
class ClosedRead:
    target: ProjectTarget
    updated_after: datetime | None
    issues: list[dict[str, Any]] | None = None
    status: str = "failed"
    error: str | None = None
    # iid -> merge_request, mention, none or unknown, for the issues checked in this read.
    commits: dict[int, str] = field(default_factory=dict)
    note_errors: dict[int, str] = field(default_factory=dict)


@dataclass
class ClosedTally:
    reads: Counter[str] = field(default_factory=Counter)
    issues: Counter[str] = field(default_factory=Counter)
    checks: Counter[str] = field(default_factory=Counter)

    def as_counts(self) -> dict[str, Any]:
        return {
            "reads": dict(sorted(self.reads.items())),
            "issues": dict(sorted(self.issues.items())),
            "commit_checks": dict(sorted(self.checks.items())),
        }


def _status(error: GitLabError) -> str:
    from baluarte.integrations.gitlab.timelogs import _status as status_of

    return status_of(error)


def _needs_check(issue: dict[str, Any], stored: Mapping[int, tuple[str, datetime | None]]) -> bool:
    if issue["state"] != "closed" or issue["spent_seconds"] > 0:
        return False
    if issue["merge_requests_count"] > 0:
        return False
    previous = stored.get(issue["iid"])
    return (
        previous is None
        or previous[0] in ("unknown", "not_checked")
        or (previous[1] != _time(issue["updated_at"]))
    )


def read_project(
    client: GitLabClient,
    target: ProjectTarget,
    updated_after: datetime | None,
    stored: Mapping[int, tuple[str, datetime | None]],
) -> ClosedRead:
    """Read a project's closed issues and the commits of those that need it.

    ``stored`` maps each stored issue's iid to its ``commits`` and ``commits_for``. Raises
    Unauthorized on 401.
    """
    read = ClosedRead(target, updated_after)
    try:
        read.issues = read_issues(client, target, updated_after)
        read.status = "ok"
    except Unauthorized:
        raise
    except GitLabError as error:
        read.status, read.error = _status(error), str(error)
        return read
    checks = 0
    for issue in read.issues:
        if issue["state"] != "closed":
            continue
        if issue["spent_seconds"] > 0:
            continue
        if issue["merge_requests_count"] > 0:
            read.commits[issue["iid"]] = "merge_request"
            continue
        if not _needs_check(issue, stored) or checks >= MAX_NOTE_CHECKS:
            continue
        checks += 1
        try:
            read.commits[issue["iid"]] = (
                "mention" if mentioned(client, target, issue["iid"]) else "none"
            )
        except Unauthorized:
            raise
        except GitLabError as error:
            read.commits[issue["iid"]] = "unknown"
            read.note_errors[issue["iid"]] = str(error)
    return read


def stored_commits(db: Session, project_id: int) -> dict[int, tuple[str, datetime | None]]:
    return {
        iid: (commits, commits_for)
        for iid, commits, commits_for in db.execute(
            select(ClosedIssue.issue_iid, ClosedIssue.commits, ClosedIssue.commits_for).where(
                ClosedIssue.project_id == project_id
            )
        )
    }


def cursor_of(db: Session, project_id: int) -> datetime | None:
    return db.execute(
        select(TimeCursor.closed_updated_after).where(TimeCursor.project_id == project_id)
    ).scalar_one_or_none()


def _observation(
    instance_id: int, target: ProjectTarget, provenance: Provenance, source: str, payload: Any
) -> Observation:
    return Observation(
        instance_id=instance_id,
        project_id=target.row_id,
        collection_run_id=provenance.collection_run_id,
        collection_job_id=provenance.collection_job_id,
        source=source,
        source_ref=target.path,
        collected_at=func.now(),
        collection_status="ok",
        payload=payload,
        payload_digest=payload_digest(payload),
    )


def write(
    db: Session, instance_id: int, read: ClosedRead, provenance: Provenance, tally: ClosedTally
) -> None:
    """Record a project's closed issues and advance its cursor. The caller commits."""
    target = read.target
    tally.reads[read.status] += 1
    if read.issues is None:
        record(
            db,
            instance_id,
            "project_id",
            [Observed(target.row_id, CLOSED_SOURCE, target.path, read.status, error=read.error)],
            provenance,
        )
        return
    observation = _observation(
        instance_id,
        target,
        provenance,
        CLOSED_SOURCE,
        {
            "project": target.path,
            "updated_after": read.updated_after.isoformat() if read.updated_after else None,
            "issues": read.issues,
        },
    )
    db.add(observation)
    db.flush()
    if read.commits:
        checked = {str(iid): result for iid, result in sorted(read.commits.items())}
        notes = _observation(
            instance_id,
            target,
            provenance,
            NOTES_SOURCE,
            {"project": target.path, "commits": checked, "errors": read.note_errors or None},
        )
        db.add(notes)
    latest: datetime | None = None
    reopened: list[int] = []
    for issue in read.issues:
        updated = _time(issue["updated_at"])
        if updated is not None:
            latest = max(latest, updated) if latest else updated
        if issue["state"] != "closed":
            reopened.append(issue["iid"])
            continue
        commits = read.commits.get(issue["iid"])
        values: dict[str, Any] = {
            "project_id": target.row_id,
            "issue_iid": issue["iid"],
            "instance_id": instance_id,
            "issue_type": issue["type"],
            "title": issue["title"],
            "web_url": issue["web_url"],
            "labels": issue["labels"],
            "estimate_seconds": issue["estimate_seconds"],
            "spent_seconds": issue["spent_seconds"],
            "merge_requests_count": issue["merge_requests_count"],
            "closed_at": _time(issue["closed_at"]),
            "issue_updated_at": updated,
            "observation_id": observation.id,
        }
        changes = {k: v for k, v in values.items() if k not in ("project_id", "issue_iid")}
        if commits is not None:
            values.update(commits=commits, commits_for=updated)
            changes.update(commits=commits, commits_for=updated)
        elif issue["spent_seconds"] > 0:
            values.update(commits="not_checked", commits_for=None)
            changes.update(commits="not_checked", commits_for=None)
        else:
            # Not checked in this read (over the limit): kept as it was, or unknown when new.
            values.update(commits="unknown", commits_for=None)
        db.execute(
            pg_insert(ClosedIssue)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["project_id", "issue_iid"],
                set_={**changes, "read_at": func.now()},
            )
        )
        tally.issues["closed"] += 1
    if reopened:
        db.execute(
            delete(ClosedIssue).where(
                ClosedIssue.project_id == target.row_id, ClosedIssue.issue_iid.in_(reopened)
            )
        )
        tally.issues["reopened"] += len(reopened)
    for result in read.commits.values():
        tally.checks[result] += 1
    if latest is not None or read.updated_after is None:
        # The first full read sets the cursor even for a project without closed issues.
        moment = latest or db.execute(select(func.now())).scalar_one()
        db.execute(
            update(TimeCursor)
            .where(TimeCursor.project_id == target.row_id)
            .values(
                closed_updated_after=func.greatest(
                    func.coalesce(TimeCursor.closed_updated_after, moment), moment
                )
            )
        )
