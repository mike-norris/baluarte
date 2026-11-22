"""Per-repository reads after discovery: security features and labels (roadmap M13.3b).

Read in the same ``github.collect`` job as the default-branch settings
(``baluarte.integrations.github.branches``), for every active, unarchived repository with a
default branch. Every request is a GET.

Security features (``SECURITY_SOURCE``), one observation per repository:

- ``GET /repos/{owner}/{repo}``: ``security_and_analysis`` gives the status (``enabled`` or
  ``disabled``) of secret scanning, push protection and Dependabot security updates. GitHub
  includes it only for a token with admin rights on the repository; a feature it does not report
  is recorded as null, never as off.
- ``GET /repos/{owner}/{repo}/code-scanning/analyses?ref=refs/heads/{branch}&per_page=1``: code
  scanning is ``enabled`` when the default branch has an analysis, and ``disabled`` when it has
  none (an empty list, or a 404 "no analysis found"). A 403 naming GitHub Advanced Security is
  ``unsupported``: the repository's licence does not offer it. Any other refusal is null.

Labels (``LABELS_SOURCE``), one observation per repository in monitoring scope, in the shape
GitLab's project labels are recorded in (``baluarte.integrations.gitlab.labels``): id, name,
colour as ``#rrggbb``, description, and no priority. GitHub organizations define no labels that
repositories inherit, so label drift treats each repository as the root of its own hierarchy
(``baluarte.labels.drift``).
"""

from collections.abc import Iterable
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from baluarte.db.models import GitLabInstance, GitLabProject
from baluarte.evidence.observations import Observed, Provenance, record
from baluarte.integrations.github.client import GitHubClient
from baluarte.integrations.gitlab.client import (
    Forbidden,
    GitLabError,
    NotFound,
    RateLimited,
    Unauthorized,
)
from baluarte.integrations.gitlab.labels import label_entry
from baluarte.scope.exclusions import coverage

SECURITY_SOURCE = "GET /repos/:owner/:repo (security_and_analysis, code scanning)"
LABELS_SOURCE = "GET /repos/:owner/:repo/labels"
# The features of ``security_and_analysis`` kept, by the name rules use.
_REPORTED = (
    "advanced_security",
    "secret_scanning",
    "secret_scanning_push_protection",
    "dependabot_security_updates",
)


def _status(error: GitLabError) -> str:
    if isinstance(error, Unauthorized | Forbidden):
        return "forbidden"
    if isinstance(error, NotFound):
        return "not_found"
    return "failed"


def _code_scanning(client: GitHubClient, full_name: str, branch: str) -> tuple[str | None, str]:
    """(status, how it was decided) for code scanning on the default branch."""
    try:
        analyses = client.get_json(
            f"/repos/{full_name}/code-scanning/analyses",
            {"ref": f"refs/heads/{branch}", "per_page": 1},
        )
    except NotFound:
        return "disabled", "no code scanning analysis for the default branch"
    except RateLimited:
        raise
    except Forbidden as error:
        if "advanced security" in str(error).lower():
            return "unsupported", "GitHub Advanced Security is not enabled for the repository"
        return None, f"code scanning could not be read: {error}"
    except GitLabError as error:
        return None, f"code scanning could not be read: {error}"
    if isinstance(analyses, list) and analyses:
        return "enabled", "the default branch has a code scanning analysis"
    return "disabled", "no code scanning analysis for the default branch"


def read_security(client: GitHubClient, row_id: int, full_name: str, branch: str) -> Observed:
    """One repository's security features. Rate limits are raised, not recorded."""
    ref = f"/repos/{full_name}"
    try:
        body = client.get_json(ref)
    except RateLimited:
        raise
    except GitLabError as error:
        return Observed(row_id, SECURITY_SOURCE, ref, _status(error), error=str(error))
    reported = body.get("security_and_analysis") if isinstance(body, dict) else None
    features: dict[str, str | None] = {}
    for name in _REPORTED:
        entry = reported.get(name) if isinstance(reported, dict) else None
        status = entry.get("status") if isinstance(entry, dict) else None
        features[name] = status if status in ("enabled", "disabled") else None
    scanning, decided = _code_scanning(client, full_name, branch)
    features["code_scanning"] = scanning
    return Observed(
        row_id,
        SECURITY_SOURCE,
        ref,
        "ok",
        payload={
            "branch": branch,
            "features": features,
            "code_scanning_basis": decided,
            "security_and_analysis_reported": isinstance(reported, dict),
        },
    )


def read_labels(client: GitHubClient, row_id: int, full_name: str) -> Observed:
    """One repository's labels, in GitLab's project label shape."""
    ref = f"/repos/{full_name}/labels"
    try:
        labels: list[dict[str, Any]] = []
        for page in client.paginate(ref):
            for item in page:
                if not isinstance(item, dict):
                    continue
                color = item.get("color")
                entry = label_entry(
                    {**item, "color": f"#{color}" if isinstance(color, str) else None}, "project"
                )
                if entry is not None:
                    labels.append(entry)
    except RateLimited:
        raise
    except GitLabError as error:
        return Observed(row_id, LABELS_SOURCE, ref, _status(error), error=str(error))
    labels.sort(key=lambda entry: (entry["name"], entry["id"]))
    return Observed(
        row_id, LABELS_SOURCE, ref, "ok", payload={"owner": full_name, "labels": labels}
    )


def targets(engine: Engine, instance_id: int) -> list[tuple[int, str, str, bool]]:
    """(row ID, full name, default branch, in monitoring scope) of every repository to read."""
    with Session(engine) as db:
        scope = coverage(db, instance_id)
        rows = db.scalars(
            select(GitLabProject)
            .where(
                GitLabProject.instance_id == instance_id,
                GitLabProject.state == "active",
                GitLabProject.archived.is_(False),
                GitLabProject.default_branch.is_not(None),
            )
            .order_by(GitLabProject.id)
        ).all()
        return [
            (row.id, row.path_with_namespace, row.default_branch, scope.of(row) is None)
            for row in rows
            if row.default_branch is not None
        ]


def _count(counts: dict[str, int], observed: Observed) -> None:
    counts[observed.collection_status] = counts.get(observed.collection_status, 0) + 1


def read_repositories(
    client: GitHubClient,
    engine: Engine,
    connection: GitLabInstance,
    provenance: Provenance | None = None,
) -> dict[str, dict[str, int]]:
    """Read and record each repository's security features; counts by status."""
    counts: dict[str, int] = {}
    for row_id, full_name, branch, _ in targets(engine, connection.id):
        observed = read_security(client, row_id, full_name, branch)
        with Session(engine) as db:
            record(db, connection.id, "project_id", [observed], provenance)
            db.commit()
        _count(counts, observed)
    return {"security": counts}


def read_all_labels(
    client: GitHubClient,
    engine: Engine,
    connection: GitLabInstance,
    provenance: Provenance | None = None,
) -> dict[str, int]:
    """Read and record each in-scope repository's labels; counts by status."""
    counts: dict[str, int] = {}
    for row_id, full_name, _, in_scope in targets(engine, connection.id):
        if not in_scope:
            continue
        observed = read_labels(client, row_id, full_name)
        with Session(engine) as db:
            record(db, connection.id, "project_id", [observed], provenance)
            db.commit()
        _count(counts, observed)
    return counts


def unread(counts: Iterable[tuple[str, int]]) -> int:
    return sum(count for status, count in counts if status != "ok")
