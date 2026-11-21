"""Each repository's default-branch settings, as evidence for the branch and review rules (M13.3).

Read after discovery in the same ``github.collect`` job, for every active, unarchived repository
with a default branch. Two reads, both GETs:

- ``GET /repos/{owner}/{repo}/branches/{branch}/protection``: classic branch protection. A 404
  means the branch has no classic protection. A 403 asking for a plan upgrade means the plan does
  not offer protection for this repository (private repositories on GitHub Free).
- ``GET /repos/{owner}/{repo}/rules/branches/{branch}``: every active ruleset rule that applies
  to the branch, from repository and organization rulesets.

Each repository gets one observation with source ``BRANCH_SOURCE``:

- ``ok``: ``protected`` is true when classic protection exists or any ruleset rule applies,
  ``rules`` names what protects it, ``classic`` holds the allowlisted review settings of classic
  protection (or null), and ``ruleset_rules`` each rule's type and allowlisted parameters.
- ``unsupported``: the plan does not offer branch protection for the repository; no payload.
- ``forbidden``, ``not_found`` or ``failed``: no payload and a safe error. A repository that
  could not be read is never treated as protected or unprotected.
"""

from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

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

BRANCH_SOURCE = "GET /repos/:owner/:repo/branches/:branch (protection and rules)"
# Classic protection settings kept as evidence; everything else (people, teams, apps) is left out.
_REVIEW_FIELDS = (
    "required_approving_review_count",
    "dismiss_stale_reviews",
    "require_code_owner_reviews",
    "require_last_push_approval",
)
# Ruleset rule parameters kept as evidence, by rule type.
_RULE_PARAMETERS = {
    "pull_request": (
        "required_approving_review_count",
        "dismiss_stale_reviews_on_push",
        "require_code_owner_review",
        "require_last_push_approval",
        "required_review_thread_resolution",
    ),
    "required_status_checks": ("strict_required_status_checks_policy",),
}


def _plan_refused(error: GitLabError) -> bool:
    return isinstance(error, Forbidden) and "upgrade" in str(error).lower()


def _status(error: GitLabError) -> str:
    if _plan_refused(error):
        return "unsupported"
    if isinstance(error, Unauthorized | Forbidden):
        return "forbidden"
    if isinstance(error, NotFound):
        return "not_found"
    return "failed"


def _classic(body: Any) -> dict[str, Any]:
    reviews = body.get("required_pull_request_reviews") if isinstance(body, dict) else None
    enforce = body.get("enforce_admins") if isinstance(body, dict) else None
    return {
        "reviews": (
            {key: reviews.get(key) for key in _REVIEW_FIELDS} if isinstance(reviews, dict) else None
        ),
        "enforce_admins": enforce.get("enabled") if isinstance(enforce, dict) else None,
        "required_status_checks": isinstance(body, dict)
        and isinstance(body.get("required_status_checks"), dict),
    }


def _rule(entry: dict[str, Any]) -> dict[str, Any]:
    kind = str(entry.get("type"))
    parameters = entry.get("parameters")
    kept = _RULE_PARAMETERS.get(kind, ())
    return {
        "type": kind,
        "ruleset_id": entry.get("ruleset_id") if isinstance(entry.get("ruleset_id"), int) else None,
        "source_type": entry.get("ruleset_source_type")
        if isinstance(entry.get("ruleset_source_type"), str)
        else None,
        "parameters": {key: parameters.get(key) for key in kept}
        if isinstance(parameters, dict)
        else {},
    }


def read_branch(client: GitHubClient, row_id: int, full_name: str, branch: str) -> Observed:
    """One repository's default-branch observation. Rate limits are raised, not recorded."""
    ref = f"/repos/{full_name}/branches/{quote(branch, safe='/')}"

    def unread(status: str, error: str) -> Observed:
        return Observed(row_id, BRANCH_SOURCE, ref, status, error=error)

    try:
        classic: dict[str, Any] | None = _classic(client.get_json(f"{ref}/protection"))
    except NotFound:
        classic = None  # GitHub answers 404 for a branch without classic protection.
    except RateLimited:
        raise
    except GitLabError as error:
        return unread(_status(error), f"branch protection: {error}")
    try:
        rules: list[dict[str, Any]] = [
            _rule(entry)
            for page in client.paginate(
                f"/repos/{full_name}/rules/branches/{quote(branch, safe='/')}"
            )
            for entry in page
            if isinstance(entry, dict) and isinstance(entry.get("type"), str)
        ]
    except RateLimited:
        raise
    except GitLabError as error:
        return unread(_status(error), f"branch rules: {error}")
    names = (["classic branch protection"] if classic is not None else []) + sorted(
        {f"ruleset rule {rule['type']}" for rule in rules}
    )
    return Observed(
        row_id,
        BRANCH_SOURCE,
        ref,
        "ok",
        payload={
            "branch": branch,
            "protected": bool(names),
            "rules": [{"name": name} for name in names],
            "classic": classic,
            "ruleset_rules": rules,
        },
    )


def _targets(engine: Engine, instance_id: int) -> list[tuple[int, str, str]]:
    with Session(engine) as db:
        return [
            (row.id, row.path_with_namespace, row.default_branch)
            for row in db.scalars(
                select(GitLabProject)
                .where(
                    GitLabProject.instance_id == instance_id,
                    GitLabProject.state == "active",
                    GitLabProject.archived.is_(False),
                    GitLabProject.default_branch.is_not(None),
                )
                .order_by(GitLabProject.id)
            )
            if row.default_branch is not None
        ]


def read_branches(
    client: GitHubClient,
    engine: Engine,
    connection: GitLabInstance,
    provenance: Provenance | None = None,
) -> dict[str, int]:
    """Read and record every eligible repository's default branch; counts by status."""
    counts: dict[str, int] = {}
    for row_id, full_name, branch in _targets(engine, connection.id):
        observed = read_branch(client, row_id, full_name, branch)
        with Session(engine) as db:
            record(db, connection.id, "project_id", [observed], provenance)
            db.commit()
        counts[observed.collection_status] = counts.get(observed.collection_status, 0) + 1
    return counts


def unconfirmed(counts: Iterable[tuple[str, int]]) -> int:
    """Repositories whose branch settings were not read (plan refusals are real answers)."""
    return sum(count for status, count in counts if status not in ("ok", "unsupported"))
