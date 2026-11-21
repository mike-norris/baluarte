"""Capture the evidence of auditable issues (roadmap M12.2, ADR-0015, docs/ISSUE_REGISTER.md).

Run by the ``issues`` collection after it reads the issue index, for the connection's auditable
issues only (selected or included, not excluded). An issue is captured when it has no capture,
when GitLab changed it since, when its last capture is older than ``REFRESH_AFTER`` (linked
merge requests and pipelines change without touching the issue), or an hour after a capture that
missed something. At most ``MAX_CAPTURES`` issues per collection; the rest follow.

Each capture reads, with GETs and the read-only token:

- the issue, with its description (``GET /projects/:id/issues/:iid``);
- comments and system notes (``.../notes``), internal notes flagged;
- its status history: state, label and milestone events (``.../resource_state_events``,
  ``.../resource_label_events``, ``.../resource_milestone_events``);
- linked issues (``.../links``);
- merge requests that close it or are related to it (``.../closed_by``,
  ``.../related_merge_requests``), each with its approvals (``.../approvals``) and latest
  pipelines (``.../pipelines``), up to ``MAX_MERGE_REQUESTS``;
- the project's releases for the issue's milestone (``GET /projects/:id/releases``);
- files uploaded to the description or comments (``/uploads/<secret>/<name>``), downloaded with
  ``GET /projects/:id/uploads/:secret/:filename`` up to the size limit, never following a
  redirect. Files are stored once by SHA-256 (``captured_file``).

The capture is one observation (``SOURCE``) holding all of it and a status per source; a source
that could not be read is a gap, listed on ``issue_evidence`` and shown with the issue, never
silently left out. A capture identical to the previous one is not stored again.
"""

import hashlib
import logging
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import Any
from urllib.parse import quote, unquote

from sqlalchemy import Engine, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from baluarte.db.models import (
    CapturedFile,
    GitLabInstance,
    GitLabProject,
    IssueEvidence,
    Observation,
    RegisterIssue,
)
from baluarte.evidence.observations import Provenance, payload_digest
from baluarte.integrations.gitlab.client import GitLabClient, GitLabError, Unauthorized
from baluarte.register import selection
from baluarte.settings import ConfigurationError, env

logger = logging.getLogger(__name__)

SOURCE = "issue evidence (register)"
MAX_CAPTURES = 200
MAX_MERGE_REQUESTS = 20
MAX_PIPELINES = 5
MAX_ATTACHMENTS = 50
REFRESH_AFTER = timedelta(days=7)
RETRY_AFTER = timedelta(hours=1)
ATTACHMENT_LIMIT = "BALUARTE_ISSUE_ATTACHMENT_MAX_BYTES"
DEFAULT_ATTACHMENT_BYTES = 20 * 1024 * 1024
UPLOAD = re.compile(r"/uploads/([0-9a-f]{32})/([^\s)\"'<>\]]+)")


def attachment_limit() -> int:
    """The largest attachment captured, in bytes (``BALUARTE_ISSUE_ATTACHMENT_MAX_BYTES``)."""
    raw = env(ATTACHMENT_LIMIT)
    if not raw:
        return DEFAULT_ATTACHMENT_BYTES
    if not raw.isdigit() or not 0 <= int(raw) <= 1024 * 1024 * 1024:
        raise ConfigurationError(f"{ATTACHMENT_LIMIT} must be a number of bytes up to 1 GiB")
    return int(raw)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _user(value: Any) -> str | None:
    return _text(value.get("username")) if isinstance(value, dict) else None


def _int(value: Any) -> int | None:
    return value if type(value) is int else None


def _status(error: GitLabError) -> str:
    from baluarte.integrations.gitlab.timelogs import _status as status_of

    return status_of(error)


def issue_record(item: Mapping[str, Any]) -> dict[str, Any]:
    milestone = item.get("milestone")
    return {
        "id": _int(item.get("id")),
        "iid": _int(item.get("iid")),
        "type": _text(item.get("issue_type")) or _text(item.get("type")),
        "title": _text(item.get("title")),
        "description": item.get("description")
        if isinstance(item.get("description"), str)
        else None,
        "state": _text(item.get("state")),
        "labels": sorted({x for x in item.get("labels") or [] if isinstance(x, str)}),
        "author": _user(item.get("author")),
        "assignees": sorted({u for u in map(_user, item.get("assignees") or []) if u}),
        "milestone": _text(milestone.get("title")) if isinstance(milestone, dict) else None,
        "confidential": item.get("confidential") is True,
        "web_url": _text(item.get("web_url")),
        "created_at": _text(item.get("created_at")),
        "updated_at": _text(item.get("updated_at")),
        "closed_at": _text(item.get("closed_at")),
        "closed_by": _user(item.get("closed_by")),
    }


def note_record(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _int(item.get("id")),
        "author": _user(item.get("author")),
        "body": item.get("body") if isinstance(item.get("body"), str) else None,
        "system": item.get("system") is True,
        "internal": item.get("internal") is True or item.get("confidential") is True,
        "created_at": _text(item.get("created_at")),
        "updated_at": _text(item.get("updated_at")),
    }


def event_record(kind: str, item: Mapping[str, Any]) -> dict[str, Any]:
    label = item.get("label")
    milestone = item.get("milestone")
    merge_request = item.get("source_merge_request")
    return {
        "kind": kind,
        "id": _int(item.get("id")),
        "user": _user(item.get("user")),
        "created_at": _text(item.get("created_at")),
        "action": _text(item.get("action")),
        "state": _text(item.get("state")),
        "label": _text(label.get("name")) if isinstance(label, dict) else None,
        "milestone": _text(milestone.get("title")) if isinstance(milestone, dict) else None,
        "merge_request": (
            _text(merge_request.get("web_url")) if isinstance(merge_request, dict) else None
        ),
    }


def link_record(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "project_id": _int(item.get("project_id")),
        "iid": _int(item.get("iid")),
        "title": _text(item.get("title")),
        "state": _text(item.get("state")),
        "web_url": _text(item.get("web_url")),
        "link_type": _text(item.get("link_type")),
    }


def merge_request_record(item: Mapping[str, Any], relation: str) -> dict[str, Any]:
    return {
        "project_id": _int(item.get("project_id")),
        "iid": _int(item.get("iid")),
        "relation": relation,
        "title": _text(item.get("title")),
        "state": _text(item.get("state")),
        "web_url": _text(item.get("web_url")),
        "author": _user(item.get("author")),
        "reviewers": sorted({u for u in map(_user, item.get("reviewers") or []) if u}),
        "source_branch": _text(item.get("source_branch")),
        "target_branch": _text(item.get("target_branch")),
        "created_at": _text(item.get("created_at")),
        "merged_at": _text(item.get("merged_at")),
        "merged_by": _user(item.get("merged_by") or item.get("merge_user")),
        "merge_commit_sha": _text(item.get("merge_commit_sha")),
        "approvals": None,
        "pipelines": None,
    }


def release_record(item: Mapping[str, Any]) -> dict[str, Any]:
    links = item.get("_links")
    return {
        "tag_name": _text(item.get("tag_name")),
        "name": _text(item.get("name")),
        "created_at": _text(item.get("created_at")),
        "released_at": _text(item.get("released_at")),
        "web_url": _text(links.get("self")) if isinstance(links, dict) else None,
        "milestones": sorted(
            {
                title
                for title in (
                    _text(m.get("title"))
                    for m in item.get("milestones") or []
                    if isinstance(m, dict)
                )
                if title
            }
        ),
    }


def uploads(texts: Mapping[str, str | None]) -> dict[tuple[str, str], list[str]]:
    """Upload references in the given texts: (secret, filename) -> where each was found."""
    found: dict[tuple[str, str], list[str]] = {}
    for where, body in texts.items():
        for secret, name in UPLOAD.findall(body or ""):
            found.setdefault((secret, unquote(name)), []).append(where)
    return found


@dataclass
class Capture:
    """One issue's evidence as read."""

    payload: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    files: list[tuple[str, bytes, str | None]] = field(default_factory=list)

    def note(self, source: str, error: GitLabError | str) -> None:
        if isinstance(error, GitLabError):
            self.sources[source] = _status(error)
        else:
            self.sources[source] = "partial"
        self.gaps.append(f"{source}: {error}")

    @property
    def status(self) -> str:
        if self.sources.get("issue") != "ok":
            return "failed"
        return "complete" if not self.gaps else "partial"


@dataclass(frozen=True)
class IssueTarget:
    project_id: int
    gitlab_id: int
    path: str
    iid: int
    updated_at: datetime | None


def _read(capture: Capture, source: str, read: Callable[[], Any]) -> Any:
    """Run ``read``; on failure record a gap and return None. Unauthorized propagates."""
    try:
        value = read()
    except Unauthorized:
        raise
    except GitLabError as error:
        capture.note(source, error)
        return None
    capture.sources.setdefault(source, "ok")
    return value


def _all(
    client: GitLabClient, path: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    return [item for page in client.paginate(path, params) for item in page]


def capture_issue(
    client: GitLabClient,
    target: IssueTarget,
    previous_files: Mapping[tuple[str, str], Mapping[str, Any]],
    releases_of: Callable[[int], list[dict[str, Any]]],
    max_bytes: int,
) -> Capture:
    """Read everything Baluarte keeps about one issue. Raises Unauthorized on 401."""
    capture = Capture()
    base = f"/projects/{target.gitlab_id}/issues/{target.iid}"
    item = _read(capture, "issue", lambda: client.get_json(base))
    if not isinstance(item, dict):
        if "issue" not in capture.sources:
            capture.note("issue", "GitLab did not return the issue")
        capture.payload = {"sources": capture.sources}
        return capture
    issue = issue_record(item)
    notes = _read(
        capture,
        "notes",
        lambda: _all(client, f"{base}/notes", {"sort": "asc", "order_by": "created_at"}),
    )
    history: list[dict[str, Any]] = []
    for kind, suffix in (
        ("state", "resource_state_events"),
        ("label", "resource_label_events"),
        ("milestone", "resource_milestone_events"),
    ):
        events = _read(capture, f"{kind}_events", partial(_all, client, f"{base}/{suffix}"))
        history += [event_record(kind, e) for e in events or []]
    history.sort(key=lambda e: (e["created_at"] or "", e["kind"], e["id"] or 0))
    links = _read(capture, "links", lambda: client.get_json(f"{base}/links"))
    closing = _read(capture, "closed_by", lambda: client.get_json(f"{base}/closed_by"))
    related = _read(
        capture, "related_merge_requests", lambda: _all(client, f"{base}/related_merge_requests")
    )
    merge_requests: dict[tuple[int | None, int | None], dict[str, Any]] = {}
    for relation, items in (("closes", closing), ("related", related)):
        for mr in items or []:
            if isinstance(mr, dict):
                key = (_int(mr.get("project_id")), _int(mr.get("iid")))
                merge_requests.setdefault(key, merge_request_record(mr, relation))
    listed = sorted(merge_requests.values(), key=lambda m: (m["project_id"] or 0, m["iid"] or 0))
    if len(listed) > MAX_MERGE_REQUESTS:
        capture.note(
            "merge_requests", f"{len(listed) - MAX_MERGE_REQUESTS} more merge requests not read"
        )
        listed = listed[:MAX_MERGE_REQUESTS]
    for mr in listed:
        if mr["project_id"] is None or mr["iid"] is None:
            continue
        mr_path = f"/projects/{mr['project_id']}/merge_requests/{mr['iid']}"
        approvals = _read(capture, "approvals", partial(client.get_json, f"{mr_path}/approvals"))
        if isinstance(approvals, dict):
            mr["approvals"] = {
                "approved": approvals.get("approved") is True,
                "approved_by": sorted(
                    {
                        u
                        for u in (
                            _user(a.get("user"))
                            for a in approvals.get("approved_by") or []
                            if isinstance(a, dict)
                        )
                        if u
                    }
                ),
                "approvals_required": _int(approvals.get("approvals_required")),
            }
        pipelines = _read(
            capture,
            "pipelines",
            partial(client.get_json, f"{mr_path}/pipelines", {"per_page": MAX_PIPELINES}),
        )
        if isinstance(pipelines, list):
            mr["pipelines"] = [
                {
                    "id": _int(p.get("id")),
                    "status": _text(p.get("status")),
                    "ref": _text(p.get("ref")),
                    "sha": _text(p.get("sha")),
                    "web_url": _text(p.get("web_url")),
                    "created_at": _text(p.get("created_at")),
                }
                for p in pipelines[:MAX_PIPELINES]
                if isinstance(p, dict)
            ]
    releases: list[dict[str, Any]] = []
    if issue["milestone"]:
        found = _read(capture, "releases", lambda: releases_of(target.gitlab_id))
        releases = [r for r in found or [] if issue["milestone"] in r["milestones"]]
    else:
        capture.sources["releases"] = "not_applicable"
    texts: dict[str, str | None] = {"description": issue["description"]}
    for note in notes or []:
        if isinstance(note, dict):
            texts[f"note:{note.get('id')}"] = (
                note.get("body") if isinstance(note.get("body"), str) else None
            )
    attachments = _attachments(capture, client, target, uploads(texts), previous_files, max_bytes)
    capture.payload = {
        "issue": issue,
        "notes": [note_record(n) for n in notes or [] if isinstance(n, dict)],
        "history": history,
        "links": [link_record(link) for link in links or [] if isinstance(link, dict)],
        "merge_requests": listed,
        "releases": releases,
        "attachments": attachments,
        "sources": dict(sorted(capture.sources.items())),
    }
    return capture


def _attachments(
    capture: Capture,
    client: GitLabClient,
    target: IssueTarget,
    found: Mapping[tuple[str, str], list[str]],
    previous: Mapping[tuple[str, str], Mapping[str, Any]],
    max_bytes: int,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    problems = Counter[str]()
    for index, ((secret, name), where) in enumerate(sorted(found.items())):
        record: dict[str, Any] = {
            "secret": secret,
            "filename": name,
            "referenced_in": where,
            "status": "ok",
            "sha256": None,
            "size_bytes": None,
            "content_type": None,
            "error": None,
        }
        known = previous.get((secret, name))
        if known is not None and known.get("status") == "ok" and known.get("sha256"):
            # The same upload was captured before; its secret makes it immutable in GitLab.
            for key in ("sha256", "size_bytes", "content_type"):
                record[key] = known.get(key)
        elif index >= MAX_ATTACHMENTS:
            record["status"], record["error"] = "not_read", "over the limit per issue"
            problems["not_read"] += 1
        else:
            path = f"/projects/{target.gitlab_id}/uploads/{secret}/{quote(name, safe='')}"
            try:
                download = client.download(path, max_bytes)
            except Unauthorized:
                raise
            except GitLabError as error:
                record["status"], record["error"] = error.reason, str(error)
                problems[error.reason] += 1
            else:
                digest = hashlib.sha256(download.content).hexdigest()
                record["sha256"] = digest
                record["size_bytes"] = len(download.content)
                record["content_type"] = download.content_type
                capture.files.append((digest, download.content, download.content_type))
        records.append(record)
    capture.sources["attachments"] = "ok" if not problems else "partial"
    if problems:
        summary = ", ".join(
            f"{count} {reason.replace('_', ' ')}" for reason, count in sorted(problems.items())
        )
        capture.gaps.append(f"attachments: {summary}")
    return records


@dataclass
class EvidenceTally:
    captured: Counter[str] = field(default_factory=Counter)
    unchanged: int = 0
    files: int = 0
    remaining: int = 0
    rejected: bool = False

    def as_counts(self) -> dict[str, Any]:
        return {
            "captured": dict(sorted(self.captured.items())),
            "unchanged": self.unchanged,
            "files": self.files,
            "remaining": self.remaining,
        }


def due(db: Session, instance_id: int, limit: int) -> tuple[list[IssueTarget], int]:
    """Auditable issues of the connection that need a capture, and how many more wait."""
    workspace_id = db.execute(
        select(GitLabInstance.workspace_id).where(GitLabInstance.id == instance_id)
    ).scalar_one()
    rules = selection.active_rules(db, workspace_id)
    now: datetime = db.execute(select(func.now())).scalar_one()
    statement = (
        selection.register_query(workspace_id, rules, selection.Filters(instance_id=instance_id))
        .add_columns(GitLabProject.gitlab_id)
        .outerjoin(
            IssueEvidence,
            (IssueEvidence.project_id == RegisterIssue.project_id)
            & (IssueEvidence.issue_iid == RegisterIssue.issue_iid),
        )
        .where(
            or_(
                IssueEvidence.project_id.is_(None),
                IssueEvidence.captured_for.is_distinct_from(RegisterIssue.source_updated_at),
                IssueEvidence.captured_at < now - REFRESH_AFTER,
                (IssueEvidence.status != "complete")
                & (IssueEvidence.captured_at < now - RETRY_AFTER),
            )
        )
        .order_by(
            IssueEvidence.captured_at.asc().nulls_first(),
            RegisterIssue.source_updated_at.desc().nulls_last(),
        )
    )
    total = db.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
    rows = db.execute(statement.limit(limit)).all()
    targets = [
        IssueTarget(issue.project_id, gitlab_id, path, issue.issue_iid, issue.source_updated_at)
        for issue, path, _category, _excluded, gitlab_id in rows
    ]
    return targets, max(total - len(targets), 0)


def _previous(db: Session, target: IssueTarget) -> tuple[IssueEvidence | None, Observation | None]:
    evidence = db.get(IssueEvidence, (target.project_id, target.iid))
    if evidence is None:
        return None, None
    return evidence, db.get(Observation, evidence.observation_id)


def _previous_files(observation: Observation | None) -> dict[tuple[str, str], Mapping[str, Any]]:
    if observation is None or not isinstance(observation.payload, dict):
        return {}
    return {
        (a["secret"], a["filename"]): a
        for a in observation.payload.get("attachments") or []
        if isinstance(a, dict) and "secret" in a and "filename" in a
    }


def write(
    db: Session,
    instance_id: int,
    target: IssueTarget,
    capture: Capture,
    previous: tuple[IssueEvidence | None, Observation | None],
    provenance: Provenance,
    tally: EvidenceTally,
) -> None:
    """Store a capture, its files and the issue's evidence row. The caller commits."""
    for digest, content, content_type in capture.files:
        inserted = db.execute(
            pg_insert(CapturedFile)
            .values(
                sha256=digest, size_bytes=len(content), content_type=content_type, content=content
            )
            .on_conflict_do_nothing(index_elements=[CapturedFile.sha256])
            .returning(CapturedFile.sha256)
        ).first()
        tally.files += inserted is not None
    _evidence, observation = previous
    digest = payload_digest(capture.payload)
    if observation is not None and observation.payload_digest == digest:
        tally.unchanged += 1
        observation_id = observation.id
    else:
        row = Observation(
            instance_id=instance_id,
            project_id=target.project_id,
            collection_run_id=provenance.collection_run_id,
            collection_job_id=provenance.collection_job_id,
            source=SOURCE,
            source_ref=f"{target.path}#{target.iid}",
            collected_at=func.now(),
            collection_status="ok"
            if capture.status == "complete"
            else ("partial" if capture.status == "partial" else "failed"),
            payload=capture.payload,
            payload_digest=digest,
        )
        db.add(row)
        db.flush()
        observation_id = row.id
    tally.captured[capture.status] += 1
    values = {
        "instance_id": instance_id,
        "observation_id": observation_id,
        "status": capture.status,
        "gaps": capture.gaps,
        "captured_for": target.updated_at,
        "captured_at": func.now(),
    }
    statement = pg_insert(IssueEvidence).values(
        project_id=target.project_id, issue_iid=target.iid, **values
    )
    db.execute(
        statement.on_conflict_do_update(
            index_elements=[IssueEvidence.project_id, IssueEvidence.issue_iid], set_=values
        )
    )


def collect(
    client: GitLabClient,
    engine: Engine,
    instance_id: int,
    provenance: Provenance | None = None,
    limit: int = MAX_CAPTURES,
) -> EvidenceTally:
    """Capture the evidence of the connection's auditable issues that need it."""
    provenance = provenance or Provenance()
    tally = EvidenceTally()
    max_bytes = attachment_limit()
    with Session(engine) as db:
        targets, tally.remaining = due(db, instance_id, limit)
    releases: dict[int, list[dict[str, Any]]] = {}

    def releases_of(gitlab_id: int) -> list[dict[str, Any]]:
        if gitlab_id not in releases:
            releases[gitlab_id] = [
                release_record(r) for r in _all(client, f"/projects/{gitlab_id}/releases")
            ]
        return releases[gitlab_id]

    for target in targets:
        with Session(engine) as db:
            evidence, observation = _previous(db, target)
            files = _previous_files(observation)
            db.expunge_all()
        try:
            capture = capture_issue(client, target, files, releases_of, max_bytes)
        except Unauthorized:
            tally.rejected = True
            break
        with Session(engine) as db:
            write(
                db,
                instance_id,
                target,
                capture,
                (
                    db.merge(evidence) if evidence else None,
                    db.merge(observation) if observation else None,
                ),
                provenance,
                tally,
            )
            db.commit()
    if tally.captured.get("failed") or tally.rejected:
        logger.warning(
            "gitlab: evidence of %s issues could not be captured", tally.captured.get("failed", 0)
        )
    return tally
