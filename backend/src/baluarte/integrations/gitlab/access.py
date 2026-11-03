"""Read each project's access controls, as evidence for the access-control and approval rules.

Runs after project discovery, next to branch protection
(``baluarte.integrations.gitlab.project_reads``). Every read is a GET. Each project gets one
observation per source:

- ``APPROVALS_SOURCE``: ``GET /projects/:id/approvals`` and ``GET /projects/:id/approval_rules``
  for a project with a default branch. The approval settings, and each approval rule's type,
  required approvals and the protected branches it covers, never its approvers. The default
  branch and whether it is protected (from this pass's protection read) are kept with them, so
  the observation alone shows which rules cover the branch.
- ``MEMBERS_SOURCE``: ``GET /projects/:id/members/all``, direct and inherited members, one entry
  per user at their highest access level. Counts by access level and by account state; usernames
  only of members with Maintainer or Owner access, and of members whose account is not active.
- ``ACCESS_TOKENS_SOURCE``: ``GET /projects/:id/access_tokens``, the project's active access
  tokens: name, scopes, role, creation and expiry dates and the day each was last used. Never a
  token value. Reading them needs the Maintainer role, so a 403 is common and is not a failure.
- ``TWO_FACTOR_SOURCE``: no GitLab call per project. The two-factor settings that group discovery
  stored for each group above the project, with each group's latest observation, and the
  instance's own setting when the token has administrator access (``GET /application/settings``,
  read once per collection).

A read that GitLab refuses or cannot answer is recorded without a payload, like branch protection,
and is never treated as compliant or not.
"""

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from baluarte.db.models import GitLabGroup, GitLabInstance, Observation
from baluarte.evidence.observations import Observed
from baluarte.integrations.gitlab.branches import PROTECTION_SOURCE
from baluarte.integrations.gitlab.client import (
    Forbidden,
    GitLabClient,
    GitLabError,
    InvalidResponse,
    NotFound,
    Unauthorized,
)
from baluarte.integrations.gitlab.groups import GROUPS
from baluarte.integrations.gitlab.project_reads import ProjectTarget, Reader, TokenRejected

APPROVALS_SOURCE = "GET /projects/:id/approvals"
MEMBERS_SOURCE = "GET /projects/:id/members/all"
ACCESS_TOKENS_SOURCE = "GET /projects/:id/access_tokens"
TWO_FACTOR_SOURCE = "GET /groups (two-factor settings of the project's groups)"

# Approval settings kept as evidence, as GitLab names them.
APPROVAL_SETTINGS = (
    "merge_requests_author_approval",
    "merge_requests_disable_committers_approval",
    "reset_approvals_on_push",
    "disable_overriding_approvers_per_merge_request",
    "require_reauthentication_to_approve",
)
# Maintainer and Owner.
PRIVILEGED_ACCESS = 40


def _today() -> date:
    return datetime.now(UTC).date()


def _read(
    target: ProjectTarget, source: str, ref: str, fetch: Callable[[], dict[str, Any]]
) -> Observed:
    """Run ``fetch`` for one project and record its payload, or why GitLab did not answer.

    Raises TokenRejected, carrying the observation to record, when GitLab answers 401.
    """

    def unread(status: str, error: GitLabError) -> Observed:
        return Observed(
            target_id=target.row_id,
            source=source,
            source_ref=ref,
            collection_status=status,
            error=str(error),
        )

    try:
        payload = fetch()
    except Unauthorized as error:
        raise TokenRejected(unread("forbidden", error)) from error
    except Forbidden as error:
        return unread("forbidden", error)
    except NotFound as error:
        return unread("not_found", error)
    except GitLabError as error:
        return unread("failed", error)
    return Observed(
        target_id=target.row_id,
        source=source,
        source_ref=ref,
        collection_status="ok",
        payload=payload,
    )


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _int(value: Any) -> int | None:
    return value if type(value) is int else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _day(value: Any) -> str | None:
    """The date part of a GitLab date or timestamp, or None when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _all(client: GitLabClient, path: str) -> list[dict[str, Any]]:
    return [item for page in client.paginate(path) for item in page]


# Merge request approvals ---------------------------------------------------------------------


def summarise_approvals(
    branch: str,
    branch_protected: bool | None,
    settings: dict[str, Any],
    rules: list[dict[str, Any]],
) -> dict[str, Any]:
    """The allowlisted payload: settings, and rules without their approvers."""
    if "require_reauthentication_to_approve" not in settings:
        # Renamed in GitLab 17.1; the older name means the same.
        settings = settings | {
            "require_reauthentication_to_approve": settings.get("require_password_to_approve")
        }
    summary = []
    for rule in rules:
        branches = rule.get("protected_branches")
        names = (
            sorted(
                entry["name"]
                for entry in branches
                if isinstance(entry, dict) and isinstance(entry.get("name"), str)
            )
            if isinstance(branches, list)
            else None
        )
        summary.append(
            {
                "name": _text(rule.get("name")),
                "rule_type": _text(rule.get("rule_type")),
                "approvals_required": _int(rule.get("approvals_required")),
                "applies_to_all_protected_branches": _bool(
                    rule.get("applies_to_all_protected_branches")
                ),
                "protected_branches": names,
            }
        )
    summary.sort(key=lambda item: (item["name"] or "", item["rule_type"] or ""))
    return {
        "branch": branch,
        "branch_protected": branch_protected,
        "settings": {key: _bool(settings.get(key)) for key in APPROVAL_SETTINGS},
        "rules": summary,
    }


def _read_approvals(
    client: GitLabClient, target: ProjectTarget, done: Mapping[str, Observed]
) -> Observed:
    branch = target.default_branch
    if branch is None:  # ``applies`` keeps empty repositories out
        raise ValueError(f"project {target.path} has no default branch")
    protection = done.get(PROTECTION_SOURCE)
    protected = (
        _bool(protection.payload.get("protected"))
        if protection is not None
        and protection.collection_status == "ok"
        and protection.payload is not None
        else None
    )

    def fetch() -> dict[str, Any]:
        settings = client.get_json(f"/projects/{target.gitlab_id}/approvals")
        if not isinstance(settings, dict):
            raise InvalidResponse("GET /projects/:id/approvals did not return an object")
        rules = _all(client, f"/projects/{target.gitlab_id}/approval_rules")
        return summarise_approvals(branch, protected, settings, rules)

    return _read(target, APPROVALS_SOURCE, f"/projects/{target.gitlab_id}/approvals", fetch)


APPROVALS = Reader(
    key="approvals",
    source=APPROVALS_SOURCE,
    noun="merge request approval",
    source_ref=lambda target: f"/projects/{target.gitlab_id}/approvals",
    read=_read_approvals,
    # Which rules cover the default branch is the point; an empty repository has none.
    applies=lambda target: target.default_branch is not None,
)


# Members ----------------------------------------------------------------------------------------


def summarise_members(members: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts for everyone; names only for privileged and inactive accounts."""
    by_user: dict[Any, dict[str, Any]] = {}
    for member in members:
        key = member.get("id") if member.get("id") is not None else id(member)
        level = _int(member.get("access_level"))
        known = by_user.get(key)
        # GitLab lists each user once at their highest level; keep the highest if it repeats.
        if known is None or (level or -1) > (known["access_level"] or -1):
            by_user[key] = {
                "username": _text(member.get("username")),
                "access_level": level,
                "state": _text(member.get("state")),
                "expires_at": _day(member.get("expires_at")),
            }
    entries = sorted(
        by_user.values(), key=lambda item: (item["username"] or "", item["access_level"] or -1)
    )
    by_level: dict[str, int] = {}
    by_state: dict[str, int] = {}
    for entry in entries:
        level_key = str(entry["access_level"]) if entry["access_level"] is not None else "unknown"
        by_level[level_key] = by_level.get(level_key, 0) + 1
        state_key = entry["state"] or "unknown"
        by_state[state_key] = by_state.get(state_key, 0) + 1
    return {
        "total": len(entries),
        "by_access_level": dict(sorted(by_level.items())),
        "by_state": dict(sorted(by_state.items())),
        "privileged": [
            entry
            for entry in entries
            if entry["access_level"] is not None and entry["access_level"] >= PRIVILEGED_ACCESS
        ],
        "inactive": [entry for entry in entries if entry["state"] != "active"],
    }


def _read_members(
    client: GitLabClient, target: ProjectTarget, _done: Mapping[str, Observed]
) -> Observed:
    ref = f"/projects/{target.gitlab_id}/members/all"
    return _read(target, MEMBERS_SOURCE, ref, lambda: summarise_members(_all(client, ref)))


MEMBERS = Reader(
    key="members",
    source=MEMBERS_SOURCE,
    noun="project member",
    source_ref=lambda target: f"/projects/{target.gitlab_id}/members/all",
    read=_read_members,
)


# Project access tokens --------------------------------------------------------------------------


def summarise_tokens(tokens: list[dict[str, Any]], today: date) -> dict[str, Any]:
    """Active tokens only. ``as_of`` dates the read when there are any, so how long a token has
    gone unused can be judged from the observation alone."""
    active = []
    for token in tokens:
        if token.get("active") is False or token.get("revoked") is True:
            continue
        scopes = token.get("scopes")
        active.append(
            {
                "id": _int(token.get("id")),
                "name": _text(token.get("name")),
                "scopes": sorted(s for s in scopes if isinstance(s, str))
                if isinstance(scopes, list)
                else None,
                "access_level": _int(token.get("access_level")),
                "created_on": _day(token.get("created_at")),
                "expires_at": _day(token.get("expires_at")),
                "last_used_on": _day(token.get("last_used_at")),
            }
        )
    active.sort(key=lambda item: (item["id"] or 0, item["name"] or ""))
    payload: dict[str, Any] = {"tokens": active}
    if active:
        payload["as_of"] = today.isoformat()
    return payload


def _read_tokens(
    client: GitLabClient, target: ProjectTarget, _done: Mapping[str, Observed]
) -> Observed:
    ref = f"/projects/{target.gitlab_id}/access_tokens"
    return _read(
        target, ACCESS_TOKENS_SOURCE, ref, lambda: summarise_tokens(_all(client, ref), _today())
    )


ACCESS_TOKENS = Reader(
    key="access_tokens",
    source=ACCESS_TOKENS_SOURCE,
    noun="project access token",
    source_ref=lambda target: f"/projects/{target.gitlab_id}/access_tokens",
    read=_read_tokens,
)


# Two-factor authentication ----------------------------------------------------------------------


def _admin_access(engine: Engine, instance_id: int) -> bool:
    with Session(engine) as db:
        instance = db.get(GitLabInstance, instance_id)
        snapshot = instance.capability_snapshot if instance is not None else None
    user = snapshot.get("token_user") if isinstance(snapshot, dict) else None
    return isinstance(user, dict) and user.get("admin_access") is True


def instance_two_factor(client: GitLabClient) -> dict[str, Any] | None:
    """The instance's two-factor setting, or None when it cannot be read."""
    try:
        settings = client.get_json("/application/settings")
    except GitLabError:
        return None
    if not isinstance(settings, dict):
        return None
    return {
        "require_two_factor_authentication": _bool(
            settings.get("require_two_factor_authentication")
        ),
        "two_factor_grace_period": _int(settings.get("two_factor_grace_period")),
    }


def _groups(engine: Engine, instance_id: int) -> dict[int, dict[str, Any]]:
    """Every group of the instance by GitLab ID, with its latest inventory observation."""
    with Session(engine) as db:
        groups = db.scalars(select(GitLabGroup).where(GitLabGroup.instance_id == instance_id))
        found: dict[int, dict[str, Any]] = {
            group.gitlab_id: {
                "row_id": group.id,
                "path": group.full_path,
                "parent": group.parent_gitlab_id,
                "state": group.state,
                "require_two_factor_authentication": group.require_two_factor_authentication,
                "two_factor_grace_period": group.two_factor_grace_period,
                "observation_id": None,
            }
            for group in groups
        }
        rows = db.execute(
            select(Observation.group_id, Observation.id)
            .where(
                Observation.instance_id == instance_id,
                Observation.group_id.is_not(None),
                Observation.source.in_(GROUPS.sources),
            )
            .ext(distinct_on(Observation.group_id))
            .order_by(Observation.group_id, Observation.collected_at.desc(), Observation.id.desc())
        ).all()
    latest = {group_id: observation_id for group_id, observation_id in rows}
    for group in found.values():
        group["observation_id"] = latest.get(group["row_id"])
    return found


def two_factor_payload(
    target: ProjectTarget,
    groups: Mapping[int, dict[str, Any]],
    instance: dict[str, Any] | None,
) -> dict[str, Any]:
    """The groups above the project, top-down, and whether that chain is complete."""
    chain: list[dict[str, Any]] = []
    complete = True
    if target.namespace_kind == "group":
        gitlab_id: int | None = target.namespace_gitlab_id
        seen: set[int] = set()
        while gitlab_id is not None:
            group = groups.get(gitlab_id)
            if group is None or group["state"] != "active" or gitlab_id in seen:
                # A parent the token cannot see, or one no longer confirmed, may enforce it.
                complete = False
                break
            seen.add(gitlab_id)
            chain.append(
                {
                    "path": group["path"],
                    "require_two_factor_authentication": group["require_two_factor_authentication"],
                    "two_factor_grace_period": group["two_factor_grace_period"],
                    "observation_id": group["observation_id"],
                }
            )
            gitlab_id = group["parent"]
    elif target.namespace_kind != "user":
        # A personal project has no groups above it; an unknown namespace may have some.
        complete = False
    return {
        "groups": list(reversed(chain)),
        "ancestry": "complete" if complete else "incomplete",
        "instance": instance,
    }


def two_factor_reader(client: GitLabClient, engine: Engine, instance_id: int) -> Reader:
    """A reader over this pass's group inventory and, with admin access, the instance setting."""
    groups = _groups(engine, instance_id)
    instance = instance_two_factor(client) if _admin_access(engine, instance_id) else None

    def read(
        _client: GitLabClient, target: ProjectTarget, _done: Mapping[str, Observed]
    ) -> Observed:
        return Observed(
            target_id=target.row_id,
            source=TWO_FACTOR_SOURCE,
            source_ref=_two_factor_ref(target),
            collection_status="ok",
            payload=two_factor_payload(target, groups, instance),
        )

    return Reader(
        key="two_factor",
        source=TWO_FACTOR_SOURCE,
        noun="two-factor",
        source_ref=_two_factor_ref,
        read=read,
    )


def _two_factor_ref(target: ProjectTarget) -> str | None:
    if target.namespace_kind == "group":
        return f"/groups/{target.namespace_gitlab_id}"
    return None
