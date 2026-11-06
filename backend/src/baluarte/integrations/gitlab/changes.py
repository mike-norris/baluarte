"""Read what changed on each project's default branch: change evidence (docs/CHANGE_EVIDENCE.md).

The ``gitlab.collect_changes`` job runs after each project discovery, for every active, unarchived
project in monitoring scope that has a default branch. Every request is a GET. Each project is
read incrementally from its ``change_cursor``; its first collection looks back
``lookback_days`` (``baluarte.changes.settings``), and its change history starts there.

- Deployments: ``GET /projects/:id/deployments`` with ``status=success``, for each production
  environment, finished since the cursor. Each is recorded as a ``deployment`` change.
- Merge requests merged into the default branch since the cursor
  (``GET /projects/:id/merge_requests?state=merged&target_branch=...&updated_after=...``), and
  again for ``redeploy_days`` after they merge, so a later deployment is recorded. For each:
  ``GET .../merge_requests/:iid/approvals`` (every tier), the newest pipeline for its last commit
  (``GET .../merge_requests/:iid/pipelines``, then ``GET /projects/:id/pipelines/:id`` and its
  ``test_report_summary``), and the recorded deployments of its commits.
- Direct pushes: first-parent commits on the default branch since the cursor
  (``GET /projects/:id/repository/commits?ref_name=...&first_parent=true&since=...``) that are
  not a merge, squash or last commit of a merged merge request, and that
  ``GET .../repository/commits/:sha/merge_requests`` ties to no merged merge request into the
  branch (a fast-forward merge of several commits, or one merged before the history started).
  Their pipeline is the newest one for the commit on the branch.

Each change is an observation (``MERGE_REQUEST_SOURCE``, ``DIRECT_PUSH_SOURCE`` or
``DEPLOYMENT_SOURCE``, ``source_ref`` naming the change) and a ``change_record`` row pointing at
the newest one. A change read again with a different payload gets a new observation, so its
history is kept. A part GitLab refused or could not answer is recorded with its status and
judged unknown; a part that failed is read again by the next collection.

Each project also gets a ``CHANGE_READ_SOURCE`` observation: where its history starts, the
environments read, and whether its merge requests, commits and deployments could be listed. The
change rules judge a project's changes only together with it.
"""

import logging
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from baluarte.changes import assess
from baluarte.changes.settings import ChangeEvidenceSettings
from baluarte.db.models import ChangeCursor, ChangeRecord, Observation
from baluarte.evidence.observations import Observed, Provenance, payload_digest, record
from baluarte.integrations.gitlab.client import (
    Forbidden,
    GitLabClient,
    GitLabError,
    InvalidResponse,
    NotFound,
    Unauthorized,
)
from baluarte.integrations.gitlab.project_reads import ProjectTarget, TokenRejected, targets

logger = logging.getLogger(__name__)

CHANGE_READ_SOURCE = "GET /projects/:id/merge_requests (change evidence)"
MERGE_REQUEST_SOURCE = "GET merge request change"
DIRECT_PUSH_SOURCE = "GET default branch commit"
DEPLOYMENT_SOURCE = "GET production deployment"
CHANGE_SOURCES = {
    "merge_request": MERGE_REQUEST_SOURCE,
    "direct_push": DIRECT_PUSH_SOURCE,
    "deployment": DEPLOYMENT_SOURCE,
}
# Capability probes for change evidence (``baluarte.integrations.gitlab.capabilities``).
PROBES = (
    "merge_requests.read",
    "merge_request_approvals_state.read",
    "pipelines.read",
    "test_reports.read",
    "deployments.read",
    "commits.read",
)
# Commits read again before the commit cursor: a rebased or fast-forwarded commit can carry a
# commit date a little before it reached the branch.
COMMIT_OVERLAP = timedelta(days=1)
DEPLOYMENT_OVERLAP = timedelta(hours=1)
# A part stops being read after this many failures in a row; the rest are recorded as failed and
# read again next time.
MAX_CONSECUTIVE_FAILURES = 20
# Incomplete changes read again per project and collection.
MAX_REREAD = 200
FINISHED_PIPELINES = ("success", "failed", "canceled", "skipped", "manual")


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: Any) -> int | None:
    return value if type(value) is int else None


def _username(user: Any) -> str | None:
    return _text(user.get("username")) if isinstance(user, dict) else None


def _status(error: GitLabError) -> str:
    if isinstance(error, Forbidden):
        return "forbidden"
    if isinstance(error, NotFound):
        return "not_found"
    return "failed"


def _all(client: GitLabClient, path: str, params: dict[str, Any] | None = None) -> list[Any]:
    return [item for page in client.paginate(path, params) for item in page]


@dataclass
class Parts:
    """Consecutive failures per part across a collection, to stop reading a part that keeps
    failing (for example, approvals the token may not read anywhere)."""

    failures: Counter[str] = field(default_factory=Counter)

    def read(self, name: str, fetch: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """``fetch``'s part, or ``{status, error}`` when GitLab did not answer.

        Raises TokenRejected when GitLab answers 401.
        """
        if self.failures[name] >= MAX_CONSECUTIVE_FAILURES:
            return {
                "status": "failed",
                "error": f"not read: {MAX_CONSECUTIVE_FAILURES} reads in a row failed earlier",
            }
        try:
            part = fetch()
        except Unauthorized as error:
            raise TokenRejected(
                Observed(None, CHANGE_READ_SOURCE, None, "forbidden", error=str(error))
            ) from error
        except GitLabError as error:
            status = _status(error)
            if status == "failed":
                self.failures[name] += 1
            return {"status": status, "error": str(error)}
        self.failures[name] = 0
        return part


# Parts ------------------------------------------------------------------------------------------


def approvals_part(body: Any, author: str | None) -> dict[str, Any]:
    """The approvals of a merge request: who approved and when, never their names or emails."""
    if not isinstance(body, dict) or not isinstance(body.get("approved_by"), list):
        raise InvalidResponse("GET .../merge_requests/:iid/approvals did not list approved_by")
    approved = sorted(
        (
            {
                "username": _username(entry.get("user")),
                "approved_at": _text(entry.get("approved_at")),
            }
            for entry in body["approved_by"]
            if isinstance(entry, dict)
        ),
        key=lambda entry: (entry["username"] or "", entry["approved_at"] or ""),
    )
    names = [entry["username"] for entry in approved]
    known = author is not None and all(isinstance(name, str) for name in names)
    return {
        "status": "ok",
        "approved_by": approved,
        # Enterprise Edition only; null on Community Edition.
        "approvals_required": _int(body.get("approvals_required")),
        "approvals_left": _int(body.get("approvals_left")),
        "self_approved": (author in names) if known else None,
        # GitLab lists commits with names and emails, never usernames, so whether a committer
        # approved cannot be established.
        "committer_approved": None,
    }


def tests_part(body: Any) -> dict[str, Any] | None:
    """The test report summary, or None when the pipeline produced no report."""
    if not isinstance(body, dict) or not isinstance(body.get("total"), dict):
        raise InvalidResponse("GET .../test_report_summary did not return a total")
    total = body["total"]
    suites = body.get("test_suites")
    if _int(total.get("count")) == 0 and not suites:
        return None
    return {
        "total": _int(total.get("count")),
        "success": _int(total.get("success")),
        "failed": _int(total.get("failed")),
        "skipped": _int(total.get("skipped")),
        "error": _int(total.get("error")),
    }


def _pipeline_detail(client: GitLabClient, path: str) -> dict[str, Any]:
    body = client.get_json(path)
    if not isinstance(body, dict):
        raise InvalidResponse("GET /projects/:id/pipelines/:id did not return an object")
    # ``status`` is the pipeline's own status; keep it apart from the part's read status.
    return {key: value for key, value in body.items() if key != "status"} | {
        "status_value": body.get("status")
    }


def deployments_part(
    shas: Iterable[str], deployments: Iterable[Mapping[str, Any]], readable: dict[str, Any]
) -> dict[str, Any]:
    """The recorded deployments of a change's commits, or why they are not known."""
    if readable.get("status") != "ok":
        return {"status": readable.get("status", "failed"), "error": readable.get("error")}
    wanted = set(shas)
    items = sorted(
        (
            {
                "id": deployment.get("id"),
                "environment": deployment.get("environment"),
                "status": deployment.get("status"),
                "finished_at": deployment.get("finished_at"),
            }
            for deployment in deployments
            if deployment.get("sha") in wanted
        ),
        key=lambda item: (item["finished_at"] or "", item["id"] or 0),
    )
    return {"status": "ok", "items": items}


# Payloads ---------------------------------------------------------------------------------------


def web_path(url: Any) -> str | None:
    """A GitLab URL without its scheme and host, so evidence does not repeat the instance."""
    if not isinstance(url, str):
        return None
    return urlparse(url).path or None


def deployment_payload(body: dict[str, Any]) -> dict[str, Any] | None:
    gitlab_id = _int(body.get("id"))
    sha = _text(body.get("sha"))
    environment = body.get("environment")
    deployable = body.get("deployable")
    finished = (
        _text(deployable.get("finished_at")) if isinstance(deployable, dict) else None
    ) or _text(body.get("updated_at"))
    if gitlab_id is None or sha is None:
        return None
    return {
        "kind": "deployment",
        "id": gitlab_id,
        "iid": _int(body.get("iid")),
        "environment": _text(environment.get("name")) if isinstance(environment, dict) else None,
        "status": _text(body.get("status")),
        "ref": _text(body.get("ref")),
        "sha": sha,
        "created_at": _text(body.get("created_at")),
        "finished_at": finished,
    }


def merge_request_fields(body: dict[str, Any]) -> dict[str, Any]:
    merged_by = body.get("merge_user") or body.get("merged_by")
    return {
        "kind": "merge_request",
        "iid": _int(body.get("iid")),
        "title": _text(body.get("title")),
        "web_path": web_path(body.get("web_url")),
        "author": _username(body.get("author")),
        "merged_by": _username(merged_by),
        "created_at": _text(body.get("created_at")),
        "merged_at": _text(body.get("merged_at")),
        "target_branch": _text(body.get("target_branch")),
        "sha": _text(body.get("sha")),
        "merge_commit_sha": _text(body.get("merge_commit_sha")),
        "squash_commit_sha": _text(body.get("squash_commit_sha")),
    }


# Collection -------------------------------------------------------------------------------------


@dataclass
class Known:
    """What is already recorded for a project."""

    cursor: ChangeCursor | None
    # Merge requests by iid: (merged at, complete).
    merge_requests: dict[int, tuple[datetime, bool]]
    # Every SHA of a recorded merge request.
    merge_request_shas: set[str]
    pushes: dict[str, bool]
    # Recorded deployments' payloads by deployment ID.
    deployments: dict[int, dict[str, Any]]


@dataclass
class ProjectChanges:
    """One project's reads, written in one transaction after GitLab has been called."""

    target: ProjectTarget
    history_since: datetime
    changes: list[dict[str, Any]] = field(default_factory=list)
    listed: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_merged_at: datetime | None = None
    last_commit_at: datetime | None = None
    last_deployed_at: datetime | None = None
    error: str | None = None


@dataclass
class ChangeCollection:
    projects: int = 0
    # Changes by kind and what happened to them: new, updated (a new version), unchanged.
    changes: Counter[str] = field(default_factory=Counter)
    # Parts of reads by status, e.g. {"approvals:forbidden": 3}.
    parts: Counter[str] = field(default_factory=Counter)
    failed_projects: int = 0
    rejected: bool = False

    def as_counts(self) -> dict[str, Any]:
        return {
            "projects": self.projects,
            "failed_projects": self.failed_projects,
            "changes": dict(sorted(self.changes.items())),
            "parts": dict(sorted(self.parts.items())),
        }


def _known(db: Session, target: ProjectTarget) -> Known:
    cursor = db.get(ChangeCursor, target.row_id)
    rows = db.execute(
        select(
            ChangeRecord.kind,
            ChangeRecord.iid,
            ChangeRecord.sha,
            ChangeRecord.shas,
            ChangeRecord.changed_at,
            ChangeRecord.complete,
            Observation.payload,
        )
        .join(Observation, Observation.id == ChangeRecord.observation_id)
        .where(ChangeRecord.project_id == target.row_id)
    ).all()
    known = Known(cursor, {}, set(), {}, {})
    for kind, iid, sha, shas, changed_at, complete, payload in rows:
        if kind == "merge_request" and iid is not None:
            known.merge_requests[iid] = (changed_at, complete)
            known.merge_request_shas.update(shas)
        elif kind == "direct_push":
            known.pushes[sha] = complete
        elif kind == "deployment" and isinstance(payload, dict):
            known.deployments[payload["id"]] = payload
    return known


def _listing(
    client: GitLabClient, parts: Parts, name: str, path: str, params: dict[str, Any]
) -> dict[str, Any]:
    return parts.read(name, lambda: {"status": "ok", "items": _all(client, path, params)})


class ProjectReader:
    """Reads one project's changes from GitLab; no database transaction is open meanwhile."""

    def __init__(
        self,
        client: GitLabClient,
        parts: Parts,
        settings: ChangeEvidenceSettings,
        now: datetime,
    ) -> None:
        self.client = client
        self.parts = parts
        self.settings = settings
        self.now = now

    def read(self, target: ProjectTarget, known: Known) -> ProjectChanges:
        branch = target.default_branch
        if branch is None:  # ``targets`` keeps empty repositories out
            raise ValueError(f"project {target.path} has no default branch")
        cursor = known.cursor
        history_since = _history_since(known, self.now, self.settings)
        result = ProjectChanges(target=target, history_since=history_since)
        base = f"/projects/{target.gitlab_id}"

        # Deployments first, so merge requests can name theirs.
        deployments_since = max(
            history_since,
            (cursor.last_deployed_at - DEPLOYMENT_OVERLAP)
            if cursor is not None and cursor.last_deployed_at is not None
            else history_since,
        )
        deployment_state: dict[str, Any] = {"status": "ok"}
        deployments = dict(known.deployments)
        for environment in self.settings.production_environments:
            listed = _listing(
                self.client,
                self.parts,
                "deployments",
                f"{base}/deployments",
                {
                    "environment": environment,
                    "status": "success",
                    "finished_after": iso(deployments_since),
                    "order_by": "finished_at",
                    "sort": "asc",
                },
            )
            if listed["status"] != "ok":
                deployment_state = listed
                continue
            for body in listed["items"]:
                payload = deployment_payload(body) if isinstance(body, dict) else None
                if payload is None or payload["id"] in deployments:
                    continue
                deployments[payload["id"]] = payload
                result.changes.append(payload)
                finished = parse_time(payload["finished_at"])
                if finished is not None and (
                    result.last_deployed_at is None or finished > result.last_deployed_at
                ):
                    result.last_deployed_at = finished
        result.listed["deployments"] = {
            "status": deployment_state["status"],
            "error": deployment_state.get("error"),
        }

        # Merge requests merged since the cursor, and again while a deployment may follow.
        reread_since = self.now - timedelta(days=self.settings.redeploy_days)
        merged_since = (
            cursor.last_merged_at if cursor is not None and cursor.last_merged_at else history_since
        )
        list_since = max(history_since, min(merged_since, reread_since))
        listed = _listing(
            self.client,
            self.parts,
            "merge_requests",
            f"{base}/merge_requests",
            {
                "state": "merged",
                "target_branch": branch,
                "updated_after": iso(list_since),
                "order_by": "updated_at",
                "sort": "asc",
            },
        )
        result.listed["merge_requests"] = {
            "status": listed["status"],
            "error": listed.get("error"),
        }
        if listed["status"] != "ok":
            result.error = f"merge requests could not be listed: {listed.get('error')}"
            return result
        mr_shas = set(known.merge_request_shas)
        seen: set[int] = set()
        bodies = [body for body in listed["items"] if isinstance(body, dict)]
        listed_iids = {body.get("iid") for body in bodies}
        # Merge requests with a part that failed, merged before this listing: read them again.
        incomplete = [
            iid
            for iid, (_merged, complete) in sorted(known.merge_requests.items())
            if not complete and iid not in listed_iids
        ]
        for number in incomplete[:MAX_REREAD]:
            path = f"{base}/merge_requests/{number}"
            single = self.parts.read("merge_requests", self._single(path))
            if single["status"] == "ok" and isinstance(single["body"], dict):
                bodies.append(single["body"])
        for body in bodies:
            found = _int(body.get("iid"))
            merged_at = parse_time(body.get("merged_at"))
            if found is None or merged_at is None or found in seen:
                continue
            if merged_at < history_since or body.get("target_branch") != branch:
                continue
            seen.add(found)
            recorded = known.merge_requests.get(found)
            if recorded is not None and recorded[1] and merged_at < reread_since:
                continue
            payload = self.merge_request(target, body, deployments, deployment_state)
            mr_shas.update(assess.shas(payload))
            result.changes.append(payload)
            if result.last_merged_at is None or merged_at > result.last_merged_at:
                result.last_merged_at = merged_at

        # Direct pushes.
        commits_since = max(
            history_since,
            (cursor.last_commit_at - COMMIT_OVERLAP)
            if cursor is not None and cursor.last_commit_at is not None
            else history_since,
        )
        listed = _listing(
            self.client,
            self.parts,
            "commits",
            f"{base}/repository/commits",
            {"ref_name": branch, "first_parent": "true", "since": iso(commits_since)},
        )
        commit_state = {"status": listed["status"], "error": listed.get("error")}
        if listed["status"] == "ok":
            latest: datetime | None = None
            undecided = False
            for body in listed["items"]:
                sha = _text(body.get("id")) if isinstance(body, dict) else None
                committed = parse_time(body.get("committed_date")) if sha else None
                if sha is None or committed is None:
                    continue
                if latest is None or committed > latest:
                    latest = committed
                if sha in mr_shas or known.pushes.get(sha) is True:
                    continue
                merged = self.merged_by_request(target, sha, branch)
                if merged is None:
                    undecided = True
                    commit_state = {
                        "status": "failed",
                        "error": "whether a commit came from a merge request could not be read",
                    }
                    continue
                if merged:
                    continue
                result.changes.append(self.direct_push(target, body, branch))
            if not undecided:
                result.last_commit_at = latest
        result.listed["commits"] = commit_state
        return result

    def _single(self, path: str) -> Callable[[], dict[str, Any]]:
        return lambda: {"status": "ok", "body": self.client.get_json(path)}

    def merge_request(
        self,
        target: ProjectTarget,
        body: dict[str, Any],
        deployments: Mapping[int, Mapping[str, Any]],
        deployment_state: dict[str, Any],
    ) -> dict[str, Any]:
        base = f"/projects/{target.gitlab_id}/merge_requests/{body['iid']}"
        payload = merge_request_fields(body)
        payload["approvals"] = self.parts.read(
            "approvals",
            lambda: approvals_part(self.client.get_json(f"{base}/approvals"), payload["author"]),
        )
        sha = payload["sha"]

        def newest_for_sha() -> dict[str, Any]:
            pipelines = _all(self.client, f"{base}/pipelines")
            matching = [p for p in pipelines if isinstance(p, dict) and p.get("sha") == sha]
            matching.sort(key=lambda p: _int(p.get("id")) or 0, reverse=True)
            return {"status": "ok", "chosen": matching[0] if matching else None}

        listed = self.parts.read("pipelines", newest_for_sha)
        payload["pipeline"] = (
            self.pipeline(target, listed["chosen"]) if listed["status"] == "ok" else listed
        )
        payload["deployments"] = deployments_part(
            assess.shas(payload), deployments.values(), deployment_state
        )
        return payload

    def pipeline(self, target: ProjectTarget, chosen: dict[str, Any] | None) -> dict[str, Any]:
        if chosen is None or _int(chosen.get("id")) is None:
            return {"status": "none"}
        path = f"/projects/{target.gitlab_id}/pipelines/{chosen['id']}"
        detail = self.parts.read(
            "pipelines", lambda: {"status": "ok", **_pipeline_detail(self.client, path)}
        )
        if detail["status"] != "ok":
            return detail
        part: dict[str, Any] = {
            "status": "ok",
            "id": chosen["id"],
            "pipeline_status": _text(detail.get("status_value")),
            "source": _text(detail.get("source")),
            "sha": _text(detail.get("sha")),
            "finished_at": _text(detail.get("finished_at")),
            "tests": None,
            "tests_status": "none",
            "tests_error": None,
        }
        if part["pipeline_status"] not in FINISHED_PIPELINES:
            return part
        summary = self.parts.read(
            "test_reports",
            lambda: {
                "status": "ok",
                "tests": tests_part(self.client.get_json(f"{path}/test_report_summary")),
            },
        )
        if summary["status"] == "ok":
            part["tests"] = summary["tests"]
            part["tests_status"] = "ok" if summary["tests"] is not None else "none"
        else:
            part["tests_status"] = summary["status"]
            part["tests_error"] = summary.get("error")
        return part

    def merged_by_request(self, target: ProjectTarget, sha: str, branch: str) -> bool | None:
        """Whether a merged merge request into ``branch`` brought the commit; None if unknown."""
        found = self.parts.read(
            "commit_merge_requests",
            lambda: {
                "status": "ok",
                "items": self.client.get_json(
                    f"/projects/{target.gitlab_id}/repository/commits/{sha}/merge_requests"
                ),
            },
        )
        if found["status"] != "ok" or not isinstance(found["items"], list):
            return None
        return any(
            isinstance(item, dict)
            and item.get("state") == "merged"
            and item.get("target_branch") == branch
            for item in found["items"]
        )

    def direct_push(
        self, target: ProjectTarget, body: dict[str, Any], branch: str
    ) -> dict[str, Any]:
        sha = body["id"]

        def newest() -> dict[str, Any]:
            pipelines = self.client.get_json(
                f"/projects/{target.gitlab_id}/pipelines",
                {"sha": sha, "ref": branch, "order_by": "id", "sort": "desc", "per_page": 1},
            )
            if not isinstance(pipelines, list):
                raise InvalidResponse("GET /projects/:id/pipelines did not return a list")
            first = pipelines[0] if pipelines and isinstance(pipelines[0], dict) else None
            return {"status": "ok", "chosen": first}

        listed = self.parts.read("pipelines", newest)
        return {
            "kind": "direct_push",
            "sha": sha,
            "title": _text(body.get("title")),
            # Commits carry the author's name and email, never a username; the email is not kept.
            "author": None,
            "author_name": _text(body.get("author_name")),
            "committed_at": _text(body.get("committed_date")),
            "branch": branch,
            "pipeline": self.pipeline(target, listed["chosen"])
            if listed["status"] == "ok"
            else listed,
        }


def change_key(
    kind: str, iid: int | None, deployment_id: int | None, sha: str
) -> tuple[str, int | str | None]:
    """What identifies a change within its project (the unique indexes of ``change_record``)."""
    if kind == "merge_request":
        return kind, iid
    if kind == "deployment":
        return kind, deployment_id
    return kind, sha


def payload_key(payload: Mapping[str, Any]) -> tuple[str, int | str | None]:
    kind = payload["kind"]
    return change_key(kind, payload.get("iid"), payload.get("id"), payload.get("sha") or "")


def _changed_at(payload: Mapping[str, Any]) -> datetime | None:
    key = {"merge_request": "merged_at", "direct_push": "committed_at", "deployment": "finished_at"}
    return parse_time(payload.get(key[payload["kind"]]))


def source_ref(gitlab_id: int, payload: Mapping[str, Any]) -> str:
    """Names the change, so its observations form its history."""
    if payload["kind"] == "merge_request":
        return f"/projects/{gitlab_id}/merge_requests/{payload['iid']}"
    if payload["kind"] == "deployment":
        return f"/projects/{gitlab_id}/deployments/{payload['id']}"
    return f"/projects/{gitlab_id}/repository/commits/{payload['sha']}"


def by_sha(payloads: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Merge requests and direct pushes by each of their SHAs."""
    found: dict[str, Mapping[str, Any]] = {}
    for payload in payloads:
        if payload.get("kind") == "deployment":
            continue
        for sha in assess.shas(payload):
            found.setdefault(sha, payload)
    return found


def judge(
    payload: Mapping[str, Any],
    changes: Mapping[str, Mapping[str, Any]],
    default_branch: str | None,
) -> tuple[str, list[str]]:
    """The stored outcome of a change: the default checks, or for a deployment whether it
    shipped a reviewed change."""
    if payload.get("kind") == "deployment":
        _found, (verdict, reason) = assess.deployed_change(payload, changes, default_branch)
        return verdict, [reason]
    return assess.assess(payload)


def review_of(payload: Mapping[str, Any]) -> str | None:
    """The review check alone with the default settings; None for a deployment."""
    if payload.get("kind") == "deployment":
        return None
    return assess.review(payload)[0]


def write(
    db: Session,
    instance_id: int,
    project: ProjectChanges,
    provenance: Provenance,
    tally: ChangeCollection,
) -> None:
    """Record the project's changes, cursor and read observation. The caller commits."""
    target = project.target
    if project.error is None:
        current = {
            change_key(row.kind, row.iid, row.deployment_id, row.sha): row
            for row in db.scalars(
                select(ChangeRecord).where(ChangeRecord.project_id == target.row_id)
            )
        }
        payloads = {
            row.id: payload
            for row, payload in db.execute(
                select(ChangeRecord, Observation.payload)
                .join(Observation, Observation.id == ChangeRecord.observation_id)
                .where(ChangeRecord.project_id == target.row_id)
            ).all()
        }
        # Judge against everything recorded and everything just read.
        everything = [p for p in payloads.values() if isinstance(p, dict)] + project.changes
        shas = by_sha(reversed(everything))
        for payload in project.changes:
            changed_at = _changed_at(payload)
            if changed_at is None:
                tally.changes[f"{payload['kind']}:no_time"] += 1
                continue
            row = current.get(payload_key(payload))
            digest = payload_digest(payload)
            outcome, reasons = judge(payload, shas, target.default_branch)
            reviewed = review_of(payload)
            complete = not assess.incomplete(payload)
            for name in ("approvals", "pipeline", "deployments"):
                part = payload.get(name)
                if isinstance(part, dict):
                    tally.parts[f"{name}:{part.get('status')}"] += 1
            if row is not None:
                previous = db.get(Observation, row.observation_id)
                if previous is not None and previous.payload_digest == digest:
                    tally.changes[f"{payload['kind']}:unchanged"] += 1
                    if (
                        row.complete != complete
                        or row.outcome != outcome
                        or row.review_outcome != reviewed
                    ):
                        row.outcome, row.reasons, row.complete = outcome, reasons, complete
                        row.review_outcome = reviewed
                        row.updated_at = func.now()
                    continue
            observation = Observation(
                instance_id=instance_id,
                project_id=target.row_id,
                collection_run_id=provenance.collection_run_id,
                collection_job_id=provenance.collection_job_id,
                source=CHANGE_SOURCES[payload["kind"]],
                source_ref=source_ref(target.gitlab_id, payload),
                collected_at=func.now(),
                collection_status="ok",
                payload=payload,
                payload_digest=digest,
            )
            db.add(observation)
            db.flush()
            if row is None:
                db.add(
                    ChangeRecord(
                        instance_id=instance_id,
                        project_id=target.row_id,
                        kind=payload["kind"],
                        iid=payload["iid"] if payload["kind"] == "merge_request" else None,
                        deployment_id=payload["id"] if payload["kind"] == "deployment" else None,
                        sha=(
                            assess.landed_sha(payload)
                            if payload["kind"] == "merge_request"
                            else payload["sha"]
                        )
                        or "",
                        shas=assess.shas(payload) or [payload.get("sha") or ""],
                        changed_at=changed_at,
                        observation_id=observation.id,
                        outcome=outcome,
                        review_outcome=reviewed,
                        reasons=reasons,
                        complete=complete,
                    )
                )
                tally.changes[f"{payload['kind']}:new"] += 1
            else:
                row.observation_id = observation.id
                row.outcome, row.reasons, row.complete = outcome, reasons, complete
                row.review_outcome = reviewed
                row.updated_at = func.now()
                tally.changes[f"{payload['kind']}:updated"] += 1
        _advance(db, project)
    record(
        db,
        instance_id,
        "project_id",
        [read_observation(project)],
        provenance,
    )


def _advance(db: Session, project: ProjectChanges) -> None:
    values: dict[str, Any] = {
        "project_id": project.target.row_id,
        "history_since": project.history_since,
        "last_merged_at": project.last_merged_at,
        "last_commit_at": project.last_commit_at,
        "last_deployed_at": project.last_deployed_at,
        "updated_at": func.now(),
    }
    later = {
        column: func.greatest(
            getattr(ChangeCursor, column), pg_insert(ChangeCursor).excluded[column]
        )
        for column in ("last_merged_at", "last_commit_at", "last_deployed_at")
    }
    db.execute(
        pg_insert(ChangeCursor)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["project_id"], set_={**later, "updated_at": func.now()}
        )
    )


def read_observation(project: ProjectChanges) -> Observed:
    """Where the project's change history starts and whether its changes could be listed."""
    target = project.target
    ref = f"/projects/{target.gitlab_id}/merge_requests"
    if project.error is not None:
        return Observed(target.row_id, CHANGE_READ_SOURCE, ref, "failed", error=project.error)
    listed = {
        name: project.listed.get(name, {"status": "failed", "error": "not read"})["status"]
        for name in ("merge_requests", "commits", "deployments")
    }
    return Observed(
        target_id=target.row_id,
        source=CHANGE_READ_SOURCE,
        source_ref=ref,
        collection_status="ok" if all(s == "ok" for s in listed.values()) else "partial",
        payload={
            "default_branch": target.default_branch,
            "history_since": iso(project.history_since),
            "listed": listed,
        },
    )


def change_targets(engine: Engine, instance_id: int) -> list[ProjectTarget]:
    """Active, unarchived projects in monitoring scope with a default branch."""
    return [
        target
        for target in targets(engine, instance_id)
        if not target.archived and target.default_branch is not None
    ]


def _history_since(known: Known, now: datetime, settings: ChangeEvidenceSettings) -> datetime:
    if known.cursor is not None:
        return known.cursor.history_since
    return now - timedelta(days=settings.lookback_days)


def collect(
    client: GitLabClient,
    engine: Engine,
    instance_id: int,
    settings: ChangeEvidenceSettings,
    provenance: Provenance | None = None,
) -> ChangeCollection:
    """Read and record every project's changes."""
    provenance = provenance or Provenance()
    tally = ChangeCollection()
    parts = Parts()
    for target in change_targets(engine, instance_id):
        with Session(engine) as db:
            now: datetime = db.execute(select(func.now())).scalar_one()
            known = _known(db, target)
        if tally.rejected:
            project = ProjectChanges(
                target=target,
                history_since=_history_since(known, now, settings),
                error="not read: GitLab rejected the token earlier in this collection",
            )
        else:
            try:
                project = ProjectReader(client, parts, settings, now).read(target, known)
            except TokenRejected as error:
                tally.rejected = True
                project = ProjectChanges(
                    target=target,
                    history_since=_history_since(known, now, settings),
                    error=error.observed.error,
                )
        with Session(engine) as db:
            write(db, instance_id, project, provenance, tally)
            db.commit()
        tally.projects += 1
        if project.error is not None:
            tally.failed_projects += 1
    if tally.failed_projects:
        logger.warning("gitlab: changes of %s projects could not be read", tally.failed_projects)
    return tally
