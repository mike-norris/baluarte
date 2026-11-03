"""Detect the GitLab version, edition, tier and which endpoints answer for the configured token.

Detection records what GitLab actually returned instead of inferring support from the version
(docs/GITLAB_INTEGRATION.md). Each probe is one of:

- ``supported``: the endpoint answered 2xx and, where checked, returned the fields rules need.
- ``forbidden``: 401 or 403. The endpoint exists but this token may not use it.
- ``not_found``: 404. GitLab also answers 404 for resources hidden from the caller, so this
  does not by itself prove the edition or tier lacks the feature.
- ``error``: rate limiting, server errors, timeouts or an unexpected response.
- ``skipped``: the probe needs a sample project or group and none was visible.
- ``unsupported``: GraphQL does not have the field on this GitLab version or edition.

Rules treat anything other than ``supported`` as "cannot evaluate", never as compliant.
"""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from baluarte.db.models import GitLabInstance
from baluarte.evidence.observations import Observed, Provenance, record
from baluarte.integrations.gitlab import security_policies
from baluarte.integrations.gitlab.client import (
    GRAPHQL_ENDPOINT,
    Forbidden,
    GitLabClient,
    GitLabError,
    NotFound,
    Unauthorized,
    UnsupportedField,
)
from baluarte.integrations.gitlab.instances import instance_for

ProbeStatus = Literal["supported", "forbidden", "not_found", "error", "skipped", "unsupported"]

# Probes the first two rules (visibility, default-branch protection) and inventory depend on.
# The overall status is "ok" only when all of these are supported.
CORE_PROBES = (
    "groups.list",
    "groups.visibility",
    "projects.list",
    "projects.visibility",
    "projects.default_branch",
    "protected_branches.read",
)


class Probe(BaseModel):
    status: ProbeStatus
    endpoint: str
    http_status: int | None = None
    detail: str | None = None


class TokenUser(BaseModel):
    """Who the token acts as, from ``GET /user``: what it can see bounds the inventory."""

    username: str | None
    # GitLab includes ``is_admin`` only for administrators, so its absence means not one.
    is_admin: bool
    # Whether this token can use that administrator access: an admin-only read succeeded. With
    # Admin Mode on, an administrator's token without the ``admin_mode`` scope cannot, and then
    # sees only what the account sees as a member. None in snapshots from before this was checked.
    admin_access: bool | None = None


class CapabilitySnapshot(BaseModel):
    status: Literal["ok", "partial", "failed"]
    checked_at: datetime
    version: str | None
    edition: Literal["ee", "ce"] | None
    tier: str | None
    error: str | None
    probes: dict[str, Probe]
    # None when ``GET /user`` could not be read (see the ``user.read`` probe).
    token_user: TokenUser | None = None


def _failed_probe(endpoint: str, error: GitLabError) -> Probe:
    if isinstance(error, UnsupportedField):
        status: ProbeStatus = "unsupported"
    elif isinstance(error, Unauthorized | Forbidden):
        status = "forbidden"
    elif isinstance(error, NotFound):
        status = "not_found"
    else:
        status = "error"
    return Probe(status=status, endpoint=endpoint, http_status=error.status, detail=str(error))


def _get(
    client: GitLabClient, endpoint: str, path: str, params: dict[str, Any] | None = None
) -> tuple[Any, Probe]:
    try:
        body = client.get_json(path, params)
    except GitLabError as error:
        return None, _failed_probe(endpoint, error)
    return body, Probe(status="supported", endpoint=endpoint, http_status=200)


def _field_probe(endpoint: str, sample: Any, field: str) -> Probe:
    """Whether a listed object carries ``field`` (feature detection on the response shape)."""
    if not isinstance(sample, dict):
        return Probe(status="skipped", endpoint=endpoint, detail="no visible object to inspect")
    if field in sample:
        return Probe(status="supported", endpoint=endpoint)
    return Probe(status="error", endpoint=endpoint, detail=f"response has no {field!r} field")


def _graphql(client: GitLabClient, query: str, group_path: str) -> Probe:
    try:
        data = client.graphql(query, {"path": group_path})
    except GitLabError as error:
        return _failed_probe(GRAPHQL_ENDPOINT, error)
    if not isinstance(data.get("group"), dict):
        return Probe(
            status="not_found",
            endpoint=GRAPHQL_ENDPOINT,
            http_status=200,
            detail="GraphQL returned no group for the sample group's path",
        )
    return Probe(status="supported", endpoint=GRAPHQL_ENDPOINT, http_status=200)


def security_policy_probes(client: GitLabClient, group: Any) -> dict[str, Probe]:
    """Whether security policy links and each policy type can be read, on a sample group.

    Each type is probed with the same selection discovery uses, so a field this GitLab lacks
    shows up here as ``unsupported`` rather than failing every target later. Security policies
    need GitLab Ultimate, but the probes never infer the tier: a refusal is ``forbidden``.
    """
    names = [security_policies.READ_PROBE] + [
        security_policies.type_probe(policy_type) for policy_type in security_policies.FIELDS
    ]
    path = group.get("full_path") if isinstance(group, dict) else None
    if not isinstance(path, str):
        return {
            name: Probe(status="skipped", endpoint=GRAPHQL_ENDPOINT, detail="no visible group")
            for name in names
        }
    probes = {
        security_policies.READ_PROBE: _graphql(client, security_policies.link_probe_query(), path)
    }
    for policy_type in security_policies.FIELDS:
        probes[security_policies.type_probe(policy_type)] = _graphql(
            client, security_policies.type_probe_query(policy_type), path
        )
    return probes


def _first(body: Any) -> Any:
    return body[0] if isinstance(body, list) and body else None


def _edition(version_body: dict[str, Any], version: str) -> Literal["ee", "ce"] | None:
    enterprise = version_body.get("enterprise")
    if isinstance(enterprise, bool):
        return "ee" if enterprise else "ce"
    if version.endswith("-ee"):
        return "ee"
    # A plain version string is usual for CE but not proof of it.
    return None


def detect(client: GitLabClient, now: datetime | None = None) -> CapabilitySnapshot:
    checked_at = now or datetime.now(UTC)
    probes: dict[str, Probe] = {}

    body, probes["version"] = _get(client, "GET /version", "/version")
    version_body: dict[str, Any] = body if isinstance(body, dict) else {}
    version = version_body.get("version")
    if probes["version"].status != "supported" or not isinstance(version, str):
        error = probes["version"].detail or "GET /version did not report a version"
        return CapabilitySnapshot(
            status="failed",
            checked_at=checked_at,
            version=None,
            edition=None,
            tier=None,
            error=error,
            probes=probes,
        )
    user, probes["user.read"] = _get(client, "GET /user", "/user")
    token_user = (
        TokenUser(
            username=user.get("username") if isinstance(user.get("username"), str) else None,
            is_admin=user.get("is_admin") is True,
        )
        if isinstance(user, dict)
        else None
    )
    if token_user is not None and token_user.is_admin:
        _, probes["admin.read"] = _get(
            client, "GET /application/statistics", "/application/statistics"
        )
        token_user.admin_access = probes["admin.read"].status == "supported"
    elif token_user is not None:
        # Not an administrator: nothing to probe, and no admin access to use.
        token_user.admin_access = False

    groups, probes["groups.list"] = _get(
        client, "GET /groups", "/groups", {"per_page": 1, "all_available": "true"}
    )
    probes["groups.visibility"] = _field_probe("GET /groups", _first(groups), "visibility")
    probes["groups.two_factor"] = _field_probe(
        "GET /groups", _first(groups), "require_two_factor_authentication"
    )

    projects, probes["projects.list"] = _get(
        client, "GET /projects", "/projects", {"per_page": 1, "order_by": "id", "sort": "asc"}
    )
    project = _first(projects)
    probes["projects.visibility"] = _field_probe("GET /projects", project, "visibility")
    probes["projects.default_branch"] = _field_probe("GET /projects", project, "default_branch")

    project_id = project.get("id") if isinstance(project, dict) else None
    for name, template, suffix in (
        ("protected_branches.read", "GET /projects/:id/protected_branches", "protected_branches"),
        ("merge_request_approvals.read", "GET /projects/:id/approvals", "approvals"),
        (
            "merge_request_approval_rules.read",
            "GET /projects/:id/approval_rules",
            "approval_rules",
        ),
        ("project_members.read", "GET /projects/:id/members/all", "members/all"),
        # Needs the Maintainer role on the sample project; other projects may still be readable.
        ("project_access_tokens.read", "GET /projects/:id/access_tokens", "access_tokens"),
    ):
        if isinstance(project_id, int):
            _, probes[name] = _get(client, template, f"/projects/{project_id}/{suffix}")
        else:
            probes[name] = Probe(status="skipped", endpoint=template, detail="no visible project")

    license_body, probes["license.read"] = _get(client, "GET /license", "/license")
    plan = license_body.get("plan") if isinstance(license_body, dict) else None
    probes.update(security_policy_probes(client, _first(groups)))

    core_ok = all(probes[name].status == "supported" for name in CORE_PROBES)
    return CapabilitySnapshot(
        status="ok" if core_ok else "partial",
        checked_at=checked_at,
        version=version,
        edition=_edition(version_body, version),
        tier=plan if isinstance(plan, str) else None,
        error=None,
        probes=probes,
        token_user=token_user,
    )


def record_snapshot(db: Session, base_url: str, snapshot: CapabilitySnapshot) -> GitLabInstance:
    """Store the snapshot on the instance row, creating the row on first contact.

    A failed detection replaces the previous snapshot, so a stale "ok" is never shown; the
    last detected version and edition, and the token's user, are kept because they remain true
    until seen otherwise.
    """
    instance = instance_for(db, base_url)
    if snapshot.version is not None:
        instance.version = snapshot.version
        instance.edition = snapshot.edition
    previous = instance.capability_snapshot or {}
    if snapshot.token_user is None and isinstance(previous.get("token_user"), dict):
        snapshot = snapshot.model_copy(
            update={"token_user": TokenUser.model_validate(previous["token_user"])}
        )
    instance.capability_snapshot = snapshot.model_dump(mode="json")
    instance.last_checked_at = snapshot.checked_at
    return instance


CAPABILITY_SOURCE = "capabilities"


def observe_snapshot(
    db: Session, instance_id: int, snapshot: CapabilitySnapshot, provenance: Provenance
) -> None:
    """Record the snapshot as instance-level evidence; a failed detection has no payload.

    ``checked_at`` is left out of the payload (the observation's ``collected_at`` records the
    time), so an unchanged snapshot has an unchanged digest and is not stored again.
    """
    payload = (
        None
        if snapshot.status == "failed"
        else snapshot.model_dump(
            mode="json", include={"version", "edition", "tier", "probes", "token_user"}
        )
    )
    record(
        db,
        instance_id,
        None,
        [
            Observed(
                target_id=None,
                source=CAPABILITY_SOURCE,
                source_ref=None,
                collection_status=snapshot.status,
                payload=payload,
                error=snapshot.error,
            )
        ],
        provenance,
    )
