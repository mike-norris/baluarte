"""Change evidence from GitHub (roadmap M13.4a; docs/CHANGE_EVIDENCE.md).

Each repository's changes are read in the shape GitLab's are recorded in
(``baluarte.integrations.gitlab.changes``), so the change rules, cursors, history and evidence
checks are shared. Pull requests are recorded as ``merge_request`` changes (``iid`` is the pull
request number, ``provider: github``), Actions workflow runs as the change's ``pipeline``, and
deployments as ``deployment`` changes. Every request is a GET.

- Deployments: for each production environment, ``GET /repos/:owner/:repo/deployments`` (newest
  first, read back to the cursor and at least ``redeploy_days``) and, for each not yet recorded,
  its newest status (``GET .../deployments/:id/statuses``). Only a deployment whose newest status
  is ``success`` is recorded, finished when that status was created; one still in progress is
  recorded at a later collection once it succeeds.
- Pull requests merged into the default branch: ``GET /repos/:owner/:repo/pulls?state=closed
  &base=:branch&sort=updated&direction=desc``, read back to the cursor (and again for
  ``redeploy_days``). For each: its reviews (``GET .../pulls/:number/reviews``; a reviewer's
  latest approving or change-requesting review counts, and a dismissed review does not), and the
  workflow runs for its head commit.
- Direct pushes: the first-parent commits of the default branch since the cursor, worked out
  from ``GET /repos/:owner/:repo/commits?sha=:branch&since=...`` by following each commit's first
  parent from the branch head, that are not a commit of a recorded pull request and that
  ``GET .../commits/:sha/pulls`` ties to no pull request merged into the branch.

Workflow runs (``GET /repos/:owner/:repo/actions/runs?head_sha=:sha``) are combined into one
pipeline: the latest run of each workflow counts; any still running makes it ``running``, any
that failed, timed out or was cancelled makes it ``failed`` or ``canceled``, and otherwise it
succeeded. No runs is ``none``, judged as no pipeline. GitHub has no test report API, so
``tests_status`` is ``none``.
"""

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from functools import partial
from typing import Any
from urllib.parse import urlparse

from baluarte.changes import assess
from baluarte.changes.settings import ChangeEvidenceSettings
from baluarte.integrations.github.client import GitHubClient
from baluarte.integrations.gitlab.changes import (
    COMMIT_OVERLAP,
    DEPLOYMENT_OVERLAP,
    MAX_REREAD,
    Known,
    Parts,
    ProjectChanges,
    _history_since,
    deployments_part,
    iso,
    parse_time,
)
from baluarte.integrations.gitlab.client import InvalidResponse
from baluarte.integrations.gitlab.project_reads import ProjectTarget

PROVIDER = "github"
# A run's conclusion, as GitLab's pipeline status the change rules judge.
_FAILED = {"failure", "timed_out", "startup_failure", "action_required"}
_CANCELED = {"cancelled", "stale"}
_PASSED = {"success", "neutral", "skipped"}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: Any) -> int | None:
    return value if type(value) is int else None


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _login(user: Any) -> str | None:
    return _text(user.get("login")) if isinstance(user, dict) else None


def _web_path(url: Any) -> str | None:
    return (urlparse(url).path or None) if isinstance(url, str) else None


def reviews_part(reviews: Iterable[Any], author: str | None) -> dict[str, Any]:
    """Who approved a pull request: each reviewer's latest approving or change-requesting review.

    A comment does not change a reviewer's verdict; a dismissed review withdraws it.
    """
    latest: dict[str, dict[str, Any]] = {}
    unnamed = False
    for review in reviews:
        if not isinstance(review, dict):
            continue
        state = review.get("state")
        if state not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            continue
        login = _login(review.get("user"))
        if login is None:
            unnamed = unnamed or state == "APPROVED"
            continue
        submitted = _text(review.get("submitted_at")) or ""
        current = latest.get(login)
        if current is None or submitted >= current["submitted_at"]:
            latest[login] = {"state": state, "submitted_at": submitted}
    approved = sorted(
        (
            {"username": login, "approved_at": entry["submitted_at"] or None}
            for login, entry in latest.items()
            if entry["state"] == "APPROVED"
        ),
        key=lambda entry: (entry["username"], entry["approved_at"] or ""),
    )
    if unnamed:
        # An approval by a deleted account: who approved cannot be established.
        approved.append({"username": None, "approved_at": None})
    names = [entry["username"] for entry in approved]
    known = author is not None and all(isinstance(name, str) for name in names)
    return {
        "status": "ok",
        "approved_by": approved,
        # Required reviews are a branch setting on GitHub, judged by
        # ``gitlab.project.merge_request_approvals``.
        "approvals_required": None,
        "approvals_left": None,
        "self_approved": (author in names) if known else None,
        "committer_approved": None,
    }


def runs_part(runs: Iterable[Any]) -> dict[str, Any]:
    """The workflow runs for one commit, combined into one pipeline."""
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
    if not newest:
        return {"status": "none"}
    chosen = sorted(newest.values(), key=lambda run: run["id"])
    conclusions = [(run.get("status"), run.get("conclusion")) for run in chosen]
    if any(status != "completed" for status, _ in conclusions):
        result = "running"
    elif any(conclusion in _FAILED for _, conclusion in conclusions):
        result = "failed"
    elif any(conclusion in _CANCELED for _, conclusion in conclusions):
        result = "canceled"
    elif all(conclusion == "skipped" for _, conclusion in conclusions):
        result = "skipped"
    elif all(conclusion in _PASSED for _, conclusion in conclusions):
        result = "success"
    else:
        result = "failed"
    finished = [_text(run.get("updated_at")) for run in chosen]
    return {
        "status": "ok",
        "id": chosen[-1]["id"],
        "pipeline_status": result,
        "source": _text(chosen[-1].get("event")),
        "sha": _text(chosen[-1].get("head_sha")),
        "finished_at": max((moment for moment in finished if moment), default=None),
        "runs": [
            {
                "id": run["id"],
                "workflow": _text(run.get("name")),
                "event": _text(run.get("event")),
                "status": _text(run.get("status")),
                "conclusion": _text(run.get("conclusion")),
            }
            for run in chosen
        ],
        "tests": None,
        # GitHub has no test report API.
        "tests_status": "none",
        "tests_error": None,
    }


def pull_request_fields(body: dict[str, Any]) -> dict[str, Any]:
    head = _object(body.get("head"))
    base = _object(body.get("base"))
    return {
        "kind": "merge_request",
        "provider": PROVIDER,
        "iid": _int(body.get("number")),
        "title": _text(body.get("title")),
        "web_path": _web_path(body.get("html_url")),
        "author": _login(body.get("user")),
        "merged_by": _login(body.get("merged_by")),
        "created_at": _text(body.get("created_at")),
        "merged_at": _text(body.get("merged_at")),
        "target_branch": _text(base.get("ref")),
        "sha": _text(head.get("sha")),
        # The merge commit, the squash commit or the last rebased commit on the base branch.
        "merge_commit_sha": _text(body.get("merge_commit_sha")),
        "squash_commit_sha": None,
    }


def deployment_payload(body: dict[str, Any], status: dict[str, Any]) -> dict[str, Any] | None:
    deployment_id = _int(body.get("id"))
    sha = _text(body.get("sha"))
    if deployment_id is None or sha is None:
        return None
    return {
        "kind": "deployment",
        "provider": PROVIDER,
        "id": deployment_id,
        "iid": None,
        "environment": _text(body.get("environment")),
        "status": "success",
        "ref": _text(body.get("ref")),
        "sha": sha,
        "created_at": _text(body.get("created_at")),
        "finished_at": _text(status.get("created_at")),
    }


class GitHubReader:
    """Reads one repository's changes from GitHub; no database transaction is open meanwhile."""

    def __init__(
        self,
        client: GitHubClient,
        parts: Parts,
        settings: ChangeEvidenceSettings,
        now: datetime,
    ) -> None:
        self.client = client
        self.parts = parts
        self.settings = settings
        self.now = now

    def _newest_first(
        self, path: str, params: dict[str, Any], key: str, since: datetime
    ) -> list[dict[str, Any]]:
        """Items of a newest-first listing, until one older than ``since`` (by ``key``)."""
        found: list[dict[str, Any]] = []
        for page in self.client.paginate(path, params):
            for item in page:
                if not isinstance(item, dict):
                    continue
                moment = parse_time(item.get(key))
                if moment is not None and moment < since:
                    return found
                found.append(item)
        return found

    def _items(self, path: str, per_page: int) -> dict[str, Any]:
        return {"status": "ok", "items": self.client.get_json(path, {"per_page": per_page})}

    def _body(self, path: str) -> dict[str, Any]:
        return {"status": "ok", "body": self.client.get_json(path)}

    def _reviews(self, path: str, author: str | None) -> dict[str, Any]:
        return reviews_part([item for page in self.client.paginate(path) for item in page], author)

    def _listing(self, name: str, fetch: Callable[[], list[dict[str, Any]]]) -> dict[str, Any]:
        return self.parts.read(name, lambda: {"status": "ok", "items": fetch()})

    def read(self, target: ProjectTarget, known: Known) -> ProjectChanges:
        branch = target.default_branch
        if branch is None:
            raise ValueError(f"repository {target.path} has no default branch")
        cursor = known.cursor
        history_since = _history_since(known, self.now, self.settings)
        result = ProjectChanges(target=target, history_since=history_since)
        base = f"/repos/{target.path}"

        # Deployments first, so pull requests can name theirs.
        # GitHub lists deployments by creation, not by when they succeeded, so a deployment
        # still in progress at one collection is listed again for ``redeploy_days``: it is
        # recorded at the first collection after its newest status is ``success``.
        since_recorded = (
            (cursor.last_deployed_at - DEPLOYMENT_OVERLAP)
            if cursor is not None and cursor.last_deployed_at is not None
            else history_since
        )
        deployments_since = max(
            history_since,
            min(since_recorded, self.now - timedelta(days=self.settings.redeploy_days)),
        )
        deployment_state: dict[str, Any] = {"status": "ok"}
        deployments = dict(known.deployments)
        for environment in self.settings.production_environments:
            listed = self._listing(
                "deployments",
                partial(
                    self._newest_first,
                    f"{base}/deployments",
                    {"environment": environment},
                    "created_at",
                    deployments_since,
                ),
            )
            if listed["status"] != "ok":
                deployment_state = listed
                continue
            for body in reversed(listed["items"]):
                deployment_id = _int(body.get("id"))
                if deployment_id is None or deployment_id in deployments:
                    continue
                status = self.parts.read(
                    "deployment_statuses",
                    partial(self._items, f"{base}/deployments/{deployment_id}/statuses", 1),
                )
                if status["status"] != "ok":
                    deployment_state = status
                    continue
                statuses = status["items"]
                if not isinstance(statuses, list):
                    raise InvalidResponse("GET .../deployments/:id/statuses did not return a list")
                newest = statuses[0] if statuses and isinstance(statuses[0], dict) else None
                if newest is None or newest.get("state") != "success":
                    continue
                payload = deployment_payload(body, newest)
                if payload is None:
                    continue
                deployments[deployment_id] = payload
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

        # Pull requests merged since the cursor, and again while a deployment may follow.
        reread_since = self.now - timedelta(days=self.settings.redeploy_days)
        merged_since = (
            cursor.last_merged_at if cursor is not None and cursor.last_merged_at else history_since
        )
        list_since = max(history_since, min(merged_since, reread_since))
        listed = self._listing(
            "merge_requests",
            lambda: self._newest_first(
                f"{base}/pulls",
                {"state": "closed", "base": branch, "sort": "updated", "direction": "desc"},
                "updated_at",
                list_since,
            ),
        )
        result.listed["merge_requests"] = {"status": listed["status"], "error": listed.get("error")}
        if listed["status"] != "ok":
            result.error = f"pull requests could not be listed: {listed.get('error')}"
            return result
        mr_shas = set(known.merge_request_shas)
        seen: set[int] = set()
        bodies = list(listed["items"])
        numbers = {body.get("number") for body in bodies}
        incomplete = [
            number
            for number, (_merged, complete) in sorted(known.merge_requests.items())
            if not complete and number not in numbers
        ]
        for number in incomplete[:MAX_REREAD]:
            single = self.parts.read(
                "merge_requests",
                partial(self._body, f"{base}/pulls/{number}"),
            )
            if single["status"] == "ok" and isinstance(single["body"], dict):
                bodies.append(single["body"])
        for body in bodies:
            payload = pull_request_fields(body)
            number = payload["iid"]
            merged_at = parse_time(payload["merged_at"])
            if number is None or merged_at is None or number in seen:
                continue
            if merged_at < history_since or payload["target_branch"] != branch:
                continue
            seen.add(number)
            recorded = known.merge_requests.get(number)
            if recorded is not None and recorded[1] and merged_at < reread_since:
                continue
            payload["approvals"] = self.parts.read(
                "approvals",
                partial(self._reviews, f"{base}/pulls/{number}/reviews", payload["author"]),
            )
            payload["pipeline"] = self.pipeline(base, payload["sha"], None)
            payload["deployments"] = deployments_part(
                assess.shas(payload), deployments.values(), deployment_state
            )
            mr_shas.update(assess.shas(payload))
            result.changes.append(payload)
            if result.last_merged_at is None or merged_at > result.last_merged_at:
                result.last_merged_at = merged_at

        # Direct pushes: first-parent commits not brought by a pull request.
        commits_since = max(
            history_since,
            (cursor.last_commit_at - COMMIT_OVERLAP)
            if cursor is not None and cursor.last_commit_at is not None
            else history_since,
        )
        listed = self._listing(
            "commits",
            lambda: [
                item
                for page in self.client.paginate(
                    f"{base}/commits", {"sha": branch, "since": iso(commits_since)}
                )
                for item in page
                if isinstance(item, dict)
            ],
        )
        commit_state = {"status": listed["status"], "error": listed.get("error")}
        if listed["status"] == "ok":
            latest: datetime | None = None
            undecided = False
            for body in first_parents(listed["items"]):
                sha = body["sha"]
                committed = parse_time(_committed(body))
                if committed is None:
                    continue
                if latest is None or committed > latest:
                    latest = committed
                if sha in mr_shas or known.pushes.get(sha) is True:
                    continue
                merged = self.merged_by_pull_request(base, sha, branch)
                if merged is None:
                    undecided = True
                    commit_state = {
                        "status": "failed",
                        "error": "whether a commit came from a pull request could not be read",
                    }
                    continue
                if merged:
                    continue
                result.changes.append(self.direct_push(base, body, branch))
            if not undecided:
                result.last_commit_at = latest
        result.listed["commits"] = commit_state
        return result

    def pipeline(self, base: str, sha: str | None, branch: str | None) -> dict[str, Any]:
        if sha is None:
            return {"status": "none"}
        params: dict[str, Any] = {"head_sha": sha}
        if branch is not None:
            params["branch"] = branch

        def runs() -> dict[str, Any]:
            body = self.client.get_json(f"{base}/actions/runs", {**params, "per_page": 100})
            if not isinstance(body, dict) or not isinstance(body.get("workflow_runs"), list):
                raise InvalidResponse("GET .../actions/runs did not list workflow_runs")
            return runs_part(body["workflow_runs"])

        return self.parts.read("pipelines", runs)

    def merged_by_pull_request(self, base: str, sha: str, branch: str) -> bool | None:
        """Whether a pull request merged into ``branch`` brought the commit; None if unknown."""
        found = self.parts.read(
            "commit_merge_requests",
            lambda: {"status": "ok", "items": self.client.get_json(f"{base}/commits/{sha}/pulls")},
        )
        if found["status"] != "ok" or not isinstance(found["items"], list):
            return None
        return any(
            isinstance(item, dict)
            and item.get("merged_at") is not None
            and isinstance(item.get("base"), dict)
            and item["base"].get("ref") == branch
            for item in found["items"]
        )

    def direct_push(self, base: str, body: dict[str, Any], branch: str) -> dict[str, Any]:
        sha = body["sha"]
        commit = _object(body.get("commit"))
        message = _text(commit.get("message")) or ""
        author = _object(commit.get("author"))
        return {
            "kind": "direct_push",
            "provider": PROVIDER,
            "sha": sha,
            "title": message.splitlines()[0] if message else None,
            # The account that pushed is not reported; the commit's author name is kept, never
            # its email.
            "author": None,
            "author_name": _text(author.get("name")),
            "committed_at": _committed(body),
            "branch": branch,
            "pipeline": self.pipeline(base, sha, branch),
        }


def _committed(body: Mapping[str, Any]) -> str | None:
    commit = body.get("commit")
    committer = commit.get("committer") if isinstance(commit, dict) else None
    return _text(committer.get("date")) if isinstance(committer, dict) else None


def first_parents(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The branch's first-parent history among ``items`` (newest first, as GitHub lists them):
    from the newest commit, follow each commit's first parent while it is listed."""
    listed = [item for item in items if isinstance(item.get("sha"), str)]
    if not listed:
        return []
    by_sha = {item["sha"]: item for item in listed}
    chain: list[dict[str, Any]] = []
    current: dict[str, Any] | None = listed[0]
    while current is not None and len(chain) < len(listed):
        chain.append(current)
        parents = current.get("parents")
        first = parents[0] if isinstance(parents, list) and parents else None
        parent_sha = first.get("sha") if isinstance(first, dict) else None
        current = by_sha.get(parent_sha) if isinstance(parent_sha, str) else None
    return chain


def reader(
    client: GitHubClient,
) -> Callable[[Parts, ChangeEvidenceSettings, datetime], GitHubReader]:
    return lambda parts, settings, now: GitHubReader(client, parts, settings, now)
