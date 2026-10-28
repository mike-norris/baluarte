"""Detect the GitLab version, edition, tier and which endpoints answer for the configured token.

Detection records what GitLab actually returned instead of inferring support from the version
(docs/GITLAB_INTEGRATION.md). Each probe is one of:

- ``supported``: the endpoint answered 2xx and, where checked, returned the fields rules need.
- ``forbidden``: 401 or 403. The endpoint exists but this token may not use it.
- ``not_found``: 404. GitLab also answers 404 for resources hidden from the caller, so this
  does not by itself prove the edition or tier lacks the feature.
- ``error``: rate limiting, server errors, timeouts or an unexpected response.
- ``skipped``: the probe needs a sample project or group and none was visible.

Rules treat anything other than ``supported`` as "cannot evaluate", never as compliant.
"""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte.db.models import GitLabInstance
from baluarte.integrations.gitlab.client import (
    Forbidden,
    GitLabClient,
    GitLabError,
    NotFound,
    Unauthorized,
)

ProbeStatus = Literal["supported", "forbidden", "not_found", "error", "skipped"]

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


class CapabilitySnapshot(BaseModel):
    status: Literal["ok", "partial", "failed"]
    checked_at: datetime
    version: str | None
    edition: Literal["ee", "ce"] | None
    tier: str | None
    error: str | None
    probes: dict[str, Probe]


def _failed_probe(endpoint: str, error: GitLabError) -> Probe:
    if isinstance(error, Unauthorized | Forbidden):
        status: ProbeStatus = "forbidden"
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
    groups, probes["groups.list"] = _get(
        client, "GET /groups", "/groups", {"per_page": 1, "all_available": "true"}
    )
    probes["groups.visibility"] = _field_probe("GET /groups", _first(groups), "visibility")

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
    ):
        if isinstance(project_id, int):
            _, probes[name] = _get(client, template, f"/projects/{project_id}/{suffix}")
        else:
            probes[name] = Probe(status="skipped", endpoint=template, detail="no visible project")

    license_body, probes["license.read"] = _get(client, "GET /license", "/license")
    plan = license_body.get("plan") if isinstance(license_body, dict) else None

    core_ok = all(probes[name].status == "supported" for name in CORE_PROBES)
    return CapabilitySnapshot(
        status="ok" if core_ok else "partial",
        checked_at=checked_at,
        version=version,
        edition=_edition(version_body, version),
        tier=plan if isinstance(plan, str) else None,
        error=None,
        probes=probes,
    )


def record_snapshot(db: Session, base_url: str, snapshot: CapabilitySnapshot) -> GitLabInstance:
    """Store the snapshot on the instance row, creating the row on first contact.

    A failed detection replaces the previous snapshot, so a stale "ok" is never shown; the
    last detected version and edition are kept because they remain true until seen otherwise.
    """
    instance = db.scalars(select(GitLabInstance).where(GitLabInstance.base_url == base_url)).first()
    if instance is None:
        instance = GitLabInstance(base_url=base_url)
        db.add(instance)
    if snapshot.version is not None:
        instance.version = snapshot.version
        instance.edition = snapshot.edition
    instance.capability_snapshot = snapshot.model_dump(mode="json")
    instance.last_checked_at = snapshot.checked_at
    return instance
