"""GitHub issues for the auditable issue register (roadmap M13.5; docs/ISSUE_REGISTER.md#github).

The register's collectors (``baluarte.integrations.gitlab.register_issues`` and
``issue_evidence``) are shared; this module reads GitHub and returns what they record, in the
shape GitLab issues are recorded in, so selection, mappings and exports work unchanged.

**The index.** ``GET /repos/{owner}/{repo}/issues?state=all&sort=updated&direction=asc`` lists a
repository's issues, with ``since`` (last update) after the first read. GitHub lists pull
requests there too, marked by a ``pull_request`` key: they are left out. ``open`` is recorded as
GitLab's ``opened``. GitHub's issue types are defined per organization: ``Task`` is recorded as
``task``, every other issue as ``issue``. GitHub has no confidential issues.

**Evidence** of an auditable issue, all with GETs:

- the issue with its description (``GET /repos/{owner}/{repo}/issues/{number}``);
- its comments (``…/comments``);
- its status history from the timeline (``…/timeline``): closed and reopened, labels and
  milestones added and removed;
- issues and pull requests that mention it (``cross-referenced`` timeline events), the issues
  as linked issues, the pull requests (up to ``MAX_MERGE_REQUESTS``) with who approved them
  (``GET /repos/{owner}/{repo}/pulls/{number}`` and ``…/reviews``, each reviewer's latest
  verdict) and the latest run of each workflow for their head commit (``…/actions/runs``). A
  pull request whose merge commit closed the issue is ``closes``; the others are ``related``.

Not captured, and said so on the capture: releases (``unsupported``: GitHub releases are not tied
to milestones) and files attached on GitHub (listed as a gap: GitHub serves them only through
redirects to signed storage, which Baluarte never follows).
"""

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from functools import partial
from typing import Any
from urllib.parse import quote

from baluarte.integrations.github.changes import reviews_part
from baluarte.integrations.github.client import GitHubClient
from baluarte.integrations.gitlab.issue_evidence import (
    MAX_MERGE_REQUESTS,
    MAX_PIPELINES,
    Capture,
    IssueTarget,
    _read,
)
from baluarte.integrations.gitlab.project_reads import ProjectTarget
from baluarte.integrations.gitlab.register_issues import OVERLAP

PROVIDER = "github"
ATTACHMENT = re.compile(
    r"https://(?:github\.com/user-attachments/(?:assets|files)/|"
    r"user-images\.githubusercontent\.com/|private-user-images\.githubusercontent\.com/)"
    r"[^\s)\"'<>\]]+"
)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int | None:
    return value if type(value) is int else None


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _login(user: Any) -> str | None:
    return _text(user.get("login")) if isinstance(user, dict) else None


def _state(value: Any) -> str | None:
    return "opened" if value == "open" else _text(value)


def _type(item: Mapping[str, Any]) -> str:
    name = _object(item.get("type")).get("name")
    return "task" if isinstance(name, str) and name.lower() == "task" else "issue"


def _labels(item: Mapping[str, Any]) -> list[str]:
    names = (
        label.get("name") if isinstance(label, dict) else label
        for label in item.get("labels") or []
    )
    return sorted({name for name in names if isinstance(name, str) and name})


def _repository(full_name: str) -> str:
    return "/repos/" + quote(full_name, safe="/")


def issue_entry(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """One issue as the register's index records it; None for a pull request or no number."""
    number = _int(item.get("number"))
    if number is None or "pull_request" in item:
        return None
    return {
        "iid": number,
        "id": _int(item.get("id")),
        "type": _type(item),
        "title": _text(item.get("title")),
        "state": _state(item.get("state")),
        "labels": _labels(item),
        "author": _login(item.get("user")),
        "assignees": sorted({a for a in map(_login, item.get("assignees") or []) if a}),
        "milestone": _text(_object(item.get("milestone")).get("title")),
        "web_url": _text(item.get("html_url")),
        "confidential": False,
        "created_at": _text(item.get("created_at")),
        "updated_at": _text(item.get("updated_at")),
        "closed_at": _text(item.get("closed_at")),
    }


def read_issues(
    client: GitHubClient, target: ProjectTarget, updated_after: datetime | None
) -> list[dict[str, Any]]:
    """Every issue of the repository, or those updated since ``updated_after``."""
    params: dict[str, Any] = {
        "state": "all",
        "sort": "updated",
        "direction": "asc",
        "per_page": 100,
    }
    if updated_after is not None:
        params["since"] = (updated_after - OVERLAP).isoformat().replace("+00:00", "Z")
    found: dict[int, dict[str, Any]] = {}
    for page in client.paginate(f"{_repository(target.path)}/issues", params):
        for item in page:
            entry = issue_entry(item) if isinstance(item, dict) else None
            if entry is not None:
                found[entry["iid"]] = entry
    return [found[iid] for iid in sorted(found)]


def index_reader(
    client: GitHubClient,
) -> Callable[[ProjectTarget, datetime | None], list[dict[str, Any]]]:
    return partial(read_issues, client)


# Evidence -------------------------------------------------------------------------------------


def issue_record(item: Mapping[str, Any]) -> dict[str, Any]:
    entry = issue_entry(item) or {}
    return {
        "id": entry.get("id"),
        "iid": entry.get("iid"),
        "type": entry.get("type"),
        "title": entry.get("title"),
        "description": item.get("body") if isinstance(item.get("body"), str) else None,
        "state": entry.get("state"),
        "labels": entry.get("labels", []),
        "author": entry.get("author"),
        "assignees": entry.get("assignees", []),
        "milestone": entry.get("milestone"),
        "confidential": False,
        "web_url": entry.get("web_url"),
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
        "closed_at": entry.get("closed_at"),
        "closed_by": _login(item.get("closed_by")),
    }


def note_record(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _int(item.get("id")),
        "author": _login(item.get("user")),
        "body": item.get("body") if isinstance(item.get("body"), str) else None,
        "system": False,
        "internal": False,
        "created_at": _text(item.get("created_at")),
        "updated_at": _text(item.get("updated_at")),
    }


# Timeline events recorded as status history: (kind, action, state).
HISTORY: dict[str, tuple[str, str | None, str | None]] = {
    "closed": ("state", None, "closed"),
    "reopened": ("state", None, "reopened"),
    "labeled": ("label", "add", None),
    "unlabeled": ("label", "remove", None),
    "milestoned": ("milestone", "add", None),
    "demilestoned": ("milestone", "remove", None),
}


def event_record(item: Mapping[str, Any]) -> dict[str, Any] | None:
    found = HISTORY.get(str(item.get("event")))
    if found is None:
        return None
    kind, action, state = found
    return {
        "kind": kind,
        "id": _int(item.get("id")),
        "user": _login(item.get("actor")),
        "created_at": _text(item.get("created_at")),
        "action": action,
        "state": state,
        "label": _text(_object(item.get("label")).get("name")),
        "milestone": _text(_object(item.get("milestone")).get("title")),
        "merge_request": None,
    }


def mentions(timeline: Iterable[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Issues and pull requests that mention the issue, from ``cross-referenced`` events."""
    issues: dict[tuple[Any, Any], dict[str, Any]] = {}
    pulls: dict[tuple[Any, Any], dict[str, Any]] = {}
    for event in timeline:
        if not isinstance(event, dict) or event.get("event") != "cross-referenced":
            continue
        source = _object(_object(event.get("source")).get("issue"))
        repository = _object(source.get("repository"))
        number = _int(source.get("number"))
        full_name = _text(repository.get("full_name"))
        if number is None or full_name is None:
            continue
        key = (full_name, number)
        entry = {
            "project_id": _int(repository.get("id")),
            "full_name": full_name,
            "iid": number,
            "title": _text(source.get("title")),
            "state": _state(source.get("state")),
            "web_url": _text(source.get("html_url")),
        }
        (pulls if "pull_request" in source else issues).setdefault(key, entry)
    return list(issues.values()), list(pulls.values())


def pull_request_record(
    pull: Mapping[str, Any], project_id: int | None, relation: str
) -> dict[str, Any]:
    head = _object(pull.get("head"))
    base = _object(pull.get("base"))
    state = "merged" if pull.get("merged_at") else _state(pull.get("state"))
    return {
        "project_id": project_id,
        "iid": _int(pull.get("number")),
        "relation": relation,
        "title": _text(pull.get("title")),
        "state": state,
        "web_url": _text(pull.get("html_url")),
        "author": _login(pull.get("user")),
        "reviewers": sorted({r for r in map(_login, pull.get("requested_reviewers") or []) if r}),
        "source_branch": _text(head.get("ref")),
        "target_branch": _text(base.get("ref")),
        "created_at": _text(pull.get("created_at")),
        "merged_at": _text(pull.get("merged_at")),
        "merged_by": _login(pull.get("merged_by")),
        "merge_commit_sha": _text(pull.get("merge_commit_sha")),
        "approvals": None,
        "pipelines": None,
    }


def pipeline_records(runs: Iterable[Any]) -> list[dict[str, Any]]:
    """The latest run of each workflow, newest first, as the register records pipelines."""
    newest: dict[Any, dict[str, Any]] = {}
    for run in runs:
        if not isinstance(run, dict) or _int(run.get("id")) is None:
            continue
        workflow = run.get("workflow_id", run.get("name"))
        current = newest.get(workflow)
        if current is None or (run.get("run_attempt") or 0, run["id"]) > (
            current.get("run_attempt") or 0,
            current["id"],
        ):
            newest[workflow] = run
    chosen = sorted(newest.values(), key=lambda run: run["id"], reverse=True)[:MAX_PIPELINES]
    return [
        {
            "id": run["id"],
            # A finished run's conclusion; otherwise where it is (queued, in_progress).
            "status": _text(run.get("conclusion")) or _text(run.get("status")),
            "ref": _text(run.get("head_branch")),
            "sha": _text(run.get("head_sha")),
            "web_url": _text(run.get("html_url")),
            "created_at": _text(run.get("created_at")),
            "workflow": _text(run.get("name")),
        }
        for run in chosen
    ]


def _all(client: GitHubClient, path: str, params: dict[str, Any] | None = None) -> list[Any]:
    return [
        item for page in client.paginate(path, {"per_page": 100, **(params or {})}) for item in page
    ]


def capture_issue(client: GitHubClient, target: IssueTarget) -> Capture:
    """Read everything Baluarte keeps about one GitHub issue. Raises Unauthorized on 401."""
    capture = Capture()
    base = f"{_repository(target.path)}/issues/{target.iid}"
    item = _read(capture, "issue", partial(client.get_json, base))
    if not isinstance(item, dict):
        if "issue" not in capture.sources:
            capture.note("issue", "GitHub did not return the issue")
        capture.payload = {"provider": PROVIDER, "sources": capture.sources}
        return capture
    issue = issue_record(item)
    comments = _read(capture, "notes", partial(_all, client, f"{base}/comments"))
    timeline = _read(capture, "timeline", partial(_all, client, f"{base}/timeline")) or []
    history = [e for e in map(event_record, (t for t in timeline if isinstance(t, dict))) if e]
    history.sort(key=lambda e: (e["created_at"] or "", e["kind"], e["id"] or 0))
    closing_commits = {
        sha
        for sha in (
            _text(t.get("commit_id"))
            for t in timeline
            if isinstance(t, dict) and t.get("event") == "closed"
        )
        if sha
    }
    linked, pulls = mentions(timeline)
    pulls.sort(key=lambda p: (p["full_name"], p["iid"]))
    if len(pulls) > MAX_MERGE_REQUESTS:
        capture.note(
            "merge_requests", f"{len(pulls) - MAX_MERGE_REQUESTS} more pull requests not read"
        )
        pulls = pulls[:MAX_MERGE_REQUESTS]
    merge_requests: list[dict[str, Any]] = []
    for mention in pulls:
        path = f"{_repository(mention['full_name'])}/pulls/{mention['iid']}"
        pull = _read(capture, "pull_requests", partial(client.get_json, path))
        if not isinstance(pull, dict):
            continue
        relation = "closes" if pull.get("merge_commit_sha") in closing_commits else "related"
        record = pull_request_record(pull, mention["project_id"], relation)
        reviews = _read(capture, "approvals", partial(_all, client, f"{path}/reviews"))
        if isinstance(reviews, list):
            approved = reviews_part(reviews, record["author"])["approved_by"]
            record["approvals"] = {
                "approved": bool(approved),
                "approved_by": sorted({a["username"] for a in approved if a["username"]}),
                "approvals_required": None,
            }
        sha = _text(_object(pull.get("head")).get("sha"))
        if sha is not None:
            runs = _read(
                capture,
                "pipelines",
                partial(
                    client.get_json,
                    f"{_repository(mention['full_name'])}/actions/runs",
                    {"head_sha": sha, "per_page": 100},
                ),
            )
            if isinstance(runs, dict):
                record["pipelines"] = pipeline_records(runs.get("workflow_runs") or [])
        merge_requests.append(record)
    capture.sources["releases"] = "not_applicable" if not issue["milestone"] else "unsupported"
    texts = [issue["description"] or ""] + [
        c.get("body")
        for c in comments or []
        if isinstance(c, dict) and isinstance(c.get("body"), str)
    ]
    attached = sorted({url for text in texts for url in ATTACHMENT.findall(text or "")})
    if attached:
        capture.note(
            "attachments",
            f"{len(attached)} files attached on GitHub not downloaded: GitHub serves them through "
            "redirects to signed storage, which Baluarte does not follow",
        )
        capture.sources["attachments"] = "unsupported"
    else:
        capture.sources["attachments"] = "ok"
    capture.payload = {
        "provider": PROVIDER,
        "issue": issue,
        "notes": [note_record(c) for c in comments or [] if isinstance(c, dict)],
        "history": history,
        "links": [
            {
                "project_id": link["project_id"],
                "iid": link["iid"],
                "title": link["title"],
                "state": link["state"],
                "web_url": link["web_url"],
                "link_type": "mentioned",
            }
            for link in linked
        ],
        "merge_requests": merge_requests,
        "releases": [],
        "attachments": [],
        "unfetched_attachments": attached,
        "sources": dict(sorted(capture.sources.items())),
    }
    return capture


def capturer(
    client: GitHubClient,
) -> Callable[[IssueTarget, Mapping[tuple[str, str], Mapping[str, Any]]], Capture]:
    """The register's capture function for a GitHub connection (no attachments to reuse)."""
    return lambda target, _previous_files: capture_issue(client, target)
