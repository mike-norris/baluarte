"""Read project issues for the auditable issue register (roadmap M12.1, ADR-0015).

Run as the ``issues`` collection, after each project discovery, for connections whose workspace
has a register: at least one active selection rule or open decision. Others are skipped, so a
deployment without a register never lists every issue.

A project's first read lists every issue in any state,
``GET /projects/:id/issues?scope=all&order_by=updated_at&sort=asc``; later reads add
``updated_after`` (the latest ``updated_at`` seen, less a few minutes), so only changed issues are
read again. Each issue's IDs, type, title, state, labels, author, assignees, milestone, URL,
confidentiality and GitLab's timestamps are kept. Descriptions, comments and history are read for
auditable issues only, from M12.2.

Each project read is an observation (``SOURCE``) holding what was read, so every stored issue
traces to the read that recorded it. A read that fails is recorded with its status and leaves the
stored issues and the cursor as they were. Every request is a GET with the read-only token.
"""

import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from baluarte.db.models import (
    GitLabInstance,
    Observation,
    RegisterCursor,
    RegisterDecision,
    RegisterIssue,
    RegisterRule,
)
from baluarte.evidence.observations import Observed, Provenance, payload_digest, record
from baluarte.integrations.gitlab.client import GitLabClient, GitLabError, Unauthorized
from baluarte.integrations.gitlab.project_reads import STOPPED_BY_REJECTION, ProjectTarget, targets

logger = logging.getLogger(__name__)

SOURCE = "GET /projects/:id/issues (register)"
# A little overlap, so an issue updated as the last collection read is not missed.
OVERLAP = timedelta(minutes=5)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _username(value: Any) -> str | None:
    return _text(value.get("username")) if isinstance(value, dict) else None


def parse_time(value: Any) -> datetime | None:
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
    gitlab_id = item.get("id")
    milestone = item.get("milestone")
    assignees = item.get("assignees")
    labels = item.get("labels")
    return {
        "iid": iid,
        "id": gitlab_id if type(gitlab_id) is int else None,
        "type": _text(item.get("issue_type")) or _text(item.get("type")),
        "title": _text(item.get("title")),
        "state": _text(item.get("state")),
        "labels": sorted({label for label in labels or [] if isinstance(label, str) and label}),
        "author": _username(item.get("author")),
        "assignees": sorted(
            {name for name in (_username(a) for a in assignees or []) if name is not None}
        ),
        "milestone": _text(milestone.get("title")) if isinstance(milestone, dict) else None,
        "web_url": _text(item.get("web_url")),
        "confidential": item.get("confidential") is True,
        "created_at": _text(item.get("created_at")),
        "updated_at": _text(item.get("updated_at")),
        "closed_at": _text(item.get("closed_at")),
    }


def read_issues(
    client: GitLabClient, target: ProjectTarget, updated_after: datetime | None
) -> list[dict[str, Any]]:
    """Every issue of the project, or those updated since ``updated_after``."""
    params: dict[str, Any] = {"scope": "all", "order_by": "updated_at", "sort": "asc"}
    if updated_after is not None:
        params["updated_after"] = (updated_after - OVERLAP).isoformat().replace("+00:00", "Z")
    found: dict[int, dict[str, Any]] = {}
    for page in client.paginate(f"/projects/{target.gitlab_id}/issues", params):
        for item in page:
            entry = issue_entry(item)
            if entry is not None:
                found[entry["iid"]] = entry
    return [found[iid] for iid in sorted(found)]


def _status(error: GitLabError) -> str:
    from baluarte.integrations.gitlab.timelogs import _status as status_of

    return status_of(error)


@dataclass
class IssueRead:
    target: ProjectTarget
    updated_after: datetime | None
    issues: list[dict[str, Any]] | None = None
    status: str = "failed"
    error: str | None = None


@dataclass
class RegisterCollection:
    """What one collection read."""

    # False when the connection's workspace has no register, so nothing was read.
    active: bool = True
    projects: int = 0
    reads: Counter[str] = field(default_factory=Counter)
    issues: int = 0
    rejected: bool = False

    def as_counts(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "projects": self.projects,
            "reads": dict(sorted(self.reads.items())),
            "issues": self.issues,
        }


def has_register(db: Session, instance_id: int) -> bool:
    """Whether the connection's workspace has an active selection rule or open decision."""
    workspace = select(GitLabInstance.workspace_id).where(GitLabInstance.id == instance_id)
    rules = exists().where(
        RegisterRule.workspace_id.in_(workspace), RegisterRule.ended_at.is_(None)
    )
    decisions = exists().where(
        RegisterDecision.workspace_id.in_(workspace), RegisterDecision.ended_at.is_(None)
    )
    return bool(db.execute(select(or_(rules, decisions))).scalar_one())


def read_project(
    client: GitLabClient, target: ProjectTarget, updated_after: datetime | None
) -> IssueRead:
    """Read one project's issues. Raises Unauthorized on 401."""
    read = IssueRead(target, updated_after)
    try:
        read.issues = read_issues(client, target, updated_after)
        read.status = "ok"
    except Unauthorized:
        raise
    except GitLabError as error:
        read.status, read.error = _status(error), str(error)
    return read


def _row(
    instance_id: int, project_id: int, issue: Mapping[str, Any], observation_id: int
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "issue_iid": issue["iid"],
        "instance_id": instance_id,
        "gitlab_id": issue["id"],
        "issue_type": issue["type"],
        "title": issue["title"],
        "state": issue["state"],
        "labels": issue["labels"],
        "author": issue["author"],
        "assignees": issue["assignees"],
        "milestone": issue["milestone"],
        "web_url": issue["web_url"],
        "confidential": issue["confidential"],
        "opened_at": parse_time(issue["created_at"]),
        "source_updated_at": parse_time(issue["updated_at"]),
        "closed_at": parse_time(issue["closed_at"]),
        "observation_id": observation_id,
    }


UPDATED = (
    "instance_id",
    "gitlab_id",
    "issue_type",
    "title",
    "state",
    "labels",
    "author",
    "assignees",
    "milestone",
    "web_url",
    "confidential",
    "opened_at",
    "source_updated_at",
    "closed_at",
    "observation_id",
)


def write(
    db: Session,
    instance_id: int,
    read: IssueRead,
    provenance: Provenance,
    tally: RegisterCollection,
) -> None:
    """Record a project's issues and advance its cursor. The caller commits."""
    target = read.target
    tally.reads[read.status] += 1
    if read.issues is None:
        record(
            db,
            instance_id,
            "project_id",
            [Observed(target.row_id, SOURCE, target.path, read.status, error=read.error)],
            provenance,
        )
        return
    since = read.updated_after.isoformat() if read.updated_after else None
    payload = {"updated_after": since, "issues": read.issues}
    observation = Observation(
        instance_id=instance_id,
        project_id=target.row_id,
        collection_run_id=provenance.collection_run_id,
        collection_job_id=provenance.collection_job_id,
        source=SOURCE,
        source_ref=target.path,
        collected_at=func.now(),
        collection_status="ok",
        payload=payload,
        payload_digest=payload_digest(payload),
    )
    db.add(observation)
    db.flush()
    rows = [_row(instance_id, target.row_id, issue, observation.id) for issue in read.issues]
    for start in range(0, len(rows), 500):
        statement = pg_insert(RegisterIssue).values(rows[start : start + 500])
        db.execute(
            statement.on_conflict_do_update(
                index_elements=[RegisterIssue.project_id, RegisterIssue.issue_iid],
                set_={
                    **{column: statement.excluded[column] for column in UPDATED},
                    "read_at": func.now(),
                },
            )
        )
    tally.issues += len(rows)
    latest = max(
        (moment for moment in (parse_time(i["updated_at"]) for i in read.issues) if moment),
        default=read.updated_after,
    )
    cursor = pg_insert(RegisterCursor).values(project_id=target.row_id, read_through=latest)
    db.execute(
        cursor.on_conflict_do_update(
            index_elements=[RegisterCursor.project_id],
            set_={"read_through": cursor.excluded.read_through, "read_at": func.now()},
        )
    )


def collect(
    client: GitLabClient,
    engine: Engine,
    instance_id: int,
    provenance: Provenance | None = None,
    projects: Sequence[ProjectTarget] | None = None,
) -> RegisterCollection:
    """Read and record the issues of every monitored project, if the workspace has a register."""
    provenance = provenance or Provenance()
    tally = RegisterCollection()
    with Session(engine) as db:
        tally.active = has_register(db, instance_id)
    if not tally.active:
        return tally
    for target in projects if projects is not None else targets(engine, instance_id):
        with Session(engine) as db:
            since = db.execute(
                select(RegisterCursor.read_through).where(
                    RegisterCursor.project_id == target.row_id
                )
            ).scalar_one_or_none()
        if tally.rejected:
            read = IssueRead(target, since, status="failed", error=STOPPED_BY_REJECTION)
        else:
            try:
                read = read_project(client, target, since)
            except Unauthorized as error:
                tally.rejected = True
                read = IssueRead(target, since, status="failed", error=str(error))
        with Session(engine) as db:
            write(db, instance_id, read, provenance, tally)
            db.commit()
        tally.projects += 1
    if tally.reads.get("failed") or tally.rejected:
        logger.warning("gitlab: issues of %s projects could not be read", tally.reads["failed"])
    return tally
