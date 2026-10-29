"""Read each project's default-branch protection, as evidence for the protected-branch rule.

Runs at the end of project discovery, for every active project with a default branch. It reads
``GET /projects/:id/protected_branches`` (read-only, every page) and matches the default branch
against each rule the way GitLab does: an exact name, or a wildcard where ``*`` matches any run
of characters, ``/`` included. Reading the single-branch endpoint instead could miss a wildcard
rule such as ``*`` or ``release/*`` that covers the branch (docs/REMEDIATION_RESEARCH.md).

Each project gets one observation with source ``PROTECTION_SOURCE``:

- ``ok``: ``protected`` is true when a rule matched, with only the matching rules' access-level
  summary (role, user, group or deploy key, never names or IDs), force push and code owner
  approval. A listing with no matching rule is real data: ``protected`` is false.
- ``forbidden`` (401, 403), ``not_found`` (404) or ``failed`` (anything else): no payload and a
  safe error. A project that could not be read is never treated as protected or unprotected.

Projects are read one at a time and results are written in small batches, so no database
transaction is open while GitLab is called. If GitLab rejects the token, or reads keep failing,
the remaining projects are recorded as not read without calling GitLab, so a stale protection
observation never outlives a collection that did not confirm it.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from baluarte.db.models import GitLabProject
from baluarte.evidence.observations import Observed, Provenance, record
from baluarte.integrations.gitlab.client import (
    Forbidden,
    GitLabClient,
    GitLabError,
    NotFound,
    Unauthorized,
)

logger = logging.getLogger(__name__)

PROTECTION_SOURCE = "GET /projects/:id/protected_branches"
# Stop calling GitLab after this many reads in a row fail; the rest are recorded as not read.
MAX_CONSECUTIVE_FAILURES = 20
BATCH = 100
_ACCESS_LISTS = ("push_access_levels", "merge_access_levels", "unprotect_access_levels")


@dataclass
class ProtectionReads:
    protected: int = 0
    unprotected: int = 0
    unread: int = 0

    def as_counts(self) -> dict[str, int]:
        return {"protected": self.protected, "unprotected": self.unprotected, "unread": self.unread}


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


class TokenRejected(Exception):
    """GitLab answered 401: the token no longer works, so reading other projects is pointless."""

    def __init__(self, observed: Observed) -> None:
        super().__init__(observed.error)
        self.observed = observed


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


def _targets(engine: Engine, instance_id: int) -> list[tuple[int, int, str]]:
    with Session(engine) as db:
        rows = db.execute(
            select(GitLabProject.id, GitLabProject.gitlab_id, GitLabProject.default_branch)
            .where(
                GitLabProject.instance_id == instance_id,
                GitLabProject.state == "active",
                GitLabProject.default_branch.is_not(None),
            )
            .order_by(GitLabProject.id)
        ).all()
    return [(row_id, gitlab_id, branch) for row_id, gitlab_id, branch in rows if branch]


def collect_protection(
    client: GitLabClient,
    engine: Engine,
    instance_id: int,
    provenance: Provenance | None = None,
) -> ProtectionReads:
    """Read and record the default-branch protection of every active project."""
    reads = ProtectionReads()
    pending: list[Observed] = []
    stop_reason: str | None = None
    failures = 0

    def flush() -> None:
        with Session(engine) as db:
            record(db, instance_id, "project_id", pending, provenance)
            db.commit()
        pending.clear()

    for row_id, gitlab_id, branch in _targets(engine, instance_id):
        if stop_reason is not None:
            observed = _unread(row_id, gitlab_id, "failed", stop_reason)
        else:
            try:
                observed = read_protection(client, row_id, gitlab_id, branch)
            except TokenRejected as rejected:
                observed = rejected.observed
                stop_reason = "not read: GitLab rejected the token earlier in this collection"
        if observed.collection_status == "ok":
            failures = 0
            if observed.payload and observed.payload["protected"]:
                reads.protected += 1
            else:
                reads.unprotected += 1
        else:
            reads.unread += 1
            failures += 1
            if stop_reason is None and failures >= MAX_CONSECUTIVE_FAILURES:
                stop_reason = (
                    f"not read: {MAX_CONSECUTIVE_FAILURES} protected-branch reads in a row "
                    "failed earlier in this collection"
                )
        pending.append(observed)
        if len(pending) >= BATCH:
            flush()
    if pending:
        flush()
    if reads.unread:
        logger.warning("gitlab: default-branch protection not read for %s projects", reads.unread)
    return reads
