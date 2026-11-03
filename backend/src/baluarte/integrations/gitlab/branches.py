"""Read each project's default-branch protection, as evidence for the protected-branch rule.

Runs after project discovery (``baluarte.integrations.gitlab.project_reads``), for every active
project with a default branch. It reads ``GET /projects/:id/protected_branches`` (read-only,
every page) and matches the default branch against each rule the way GitLab does: an exact name,
or a wildcard where ``*`` matches any run of characters, ``/`` included. Reading the
single-branch endpoint instead could miss a wildcard rule such as ``*`` or ``release/*`` that
covers the branch (docs/REMEDIATION_RESEARCH.md).

Each project gets one observation with source ``PROTECTION_SOURCE``:

- ``ok``: ``protected`` is true when a rule matched, with only the matching rules' access-level
  summary (role, user, group or deploy key, never names or IDs), force push and code owner
  approval. A listing with no matching rule is real data: ``protected`` is false.
- ``forbidden`` (401, 403), ``not_found`` (404) or ``failed`` (anything else): no payload and a
  safe error. A project that could not be read is never treated as protected or unprotected.

Any read that does not succeed counts toward stopping: protection is part of every project
discovery, so a run that could not confirm it is reported as partial.
"""

import re
from collections.abc import Mapping
from typing import Any

from baluarte.evidence.observations import Observed
from baluarte.integrations.gitlab.client import (
    Forbidden,
    GitLabClient,
    GitLabError,
    NotFound,
    Unauthorized,
)
from baluarte.integrations.gitlab.project_reads import ProjectTarget, Reader, TokenRejected

PROTECTION_SOURCE = "GET /projects/:id/protected_branches"
_ACCESS_LISTS = ("push_access_levels", "merge_access_levels", "unprotect_access_levels")
# Every status but "ok" means the protection was not confirmed.
_NOT_OK = frozenset({"partial", "forbidden", "not_found", "unsupported", "failed"})


def matches(rule_name: str, branch: str) -> bool:
    """Whether a protected-branch rule name covers ``branch``, as GitLab matches wildcards."""
    if "*" not in rule_name:
        return rule_name == branch
    pattern = ".*?".join(re.escape(part) for part in rule_name.split("*"))
    return re.fullmatch(pattern, branch, flags=re.DOTALL) is not None


def _level(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return {"kind": "unknown", "access_level": None}
    if entry.get("user_id") is not None:
        kind = "user"
    elif entry.get("group_id") is not None:
        kind = "group"
    elif entry.get("deploy_key_id") is not None:
        kind = "deploy_key"
    else:
        kind = "role"
    level = entry.get("access_level")
    return {"kind": kind, "access_level": level if isinstance(level, int) else None}


def _levels(rule: dict[str, Any], key: str) -> list[dict[str, Any]] | None:
    entries = rule.get(key)
    if not isinstance(entries, list):
        return None
    summary = [_level(entry) for entry in entries]
    return sorted(summary, key=lambda item: (item["kind"], item["access_level"] or -1))


def _flag(rule: dict[str, Any], key: str) -> bool | None:
    value = rule.get(key)
    return value if isinstance(value, bool) else None


def summarise(branch: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
    """The allowlisted payload: the branch, whether it is protected, and the matching rules."""
    matching = sorted(
        (
            rule
            for rule in rules
            if isinstance(rule.get("name"), str) and matches(rule["name"], branch)
        ),
        key=lambda rule: str(rule["name"]),
    )
    return {
        "branch": branch,
        "protected": bool(matching),
        "rules": [
            {
                "name": rule["name"],
                **{key: _levels(rule, key) for key in _ACCESS_LISTS},
                "allow_force_push": _flag(rule, "allow_force_push"),
                "code_owner_approval_required": _flag(rule, "code_owner_approval_required"),
            }
            for rule in matching
        ],
    }


def _ref(gitlab_id: int) -> str:
    return f"/projects/{gitlab_id}/protected_branches"


def _unread(row_id: int, gitlab_id: int, status: str, error: str) -> Observed:
    return Observed(
        target_id=row_id,
        source=PROTECTION_SOURCE,
        source_ref=_ref(gitlab_id),
        collection_status=status,
        error=error,
    )


def read_protection(client: GitLabClient, row_id: int, gitlab_id: int, branch: str) -> Observed:
    """One project's default-branch protection, or why it could not be read.

    Raises TokenRejected, carrying the observation to record, when GitLab answers 401.
    """
    try:
        rules = [rule for page in client.paginate(_ref(gitlab_id)) for rule in page]
    except Unauthorized as error:
        raise TokenRejected(_unread(row_id, gitlab_id, "forbidden", str(error))) from error
    except Forbidden as error:
        return _unread(row_id, gitlab_id, "forbidden", str(error))
    except NotFound as error:
        return _unread(row_id, gitlab_id, "not_found", str(error))
    except GitLabError as error:
        return _unread(row_id, gitlab_id, "failed", str(error))
    return Observed(
        target_id=row_id,
        source=PROTECTION_SOURCE,
        source_ref=_ref(gitlab_id),
        collection_status="ok",
        payload=summarise(branch, rules),
    )


def _read(client: GitLabClient, target: ProjectTarget, _done: Mapping[str, Observed]) -> Observed:
    if target.default_branch is None:  # ``applies`` keeps empty repositories out
        raise ValueError(f"project {target.path} has no default branch")
    return read_protection(client, target.row_id, target.gitlab_id, target.default_branch)


def _tally(observed: Observed) -> str:
    if observed.collection_status != "ok":
        return "unread"
    return "protected" if observed.payload and observed.payload["protected"] else "unprotected"


PROTECTION = Reader(
    key="protection",
    source=PROTECTION_SOURCE,
    noun="protected-branch",
    source_ref=lambda target: _ref(target.gitlab_id),
    read=_read,
    # An empty repository has no branch to read.
    applies=lambda target: target.default_branch is not None,
    stop_on=_NOT_OK,
    tally=_tally,
)


def protection_counts(counts: Mapping[str, int]) -> dict[str, int]:
    """Protected, unprotected and unread, each present even when zero."""
    return {key: counts.get(key, 0) for key in ("protected", "unprotected", "unread")}
