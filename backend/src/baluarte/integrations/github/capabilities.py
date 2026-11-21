"""GitHub capability detection (roadmap M13.2, docs/GITHUB_INTEGRATION.md).

Like GitLab's (``baluarte.integrations.gitlab.capabilities``), the probes record what GitHub
actually answers for the connection's credential instead of inferring it from the plan. The
snapshot has the same shape, so the Connections page shows either provider. Probes on a
repository use the first one listed; with no repository they are skipped.

A 403 whose message asks for a plan upgrade is ``unsupported`` (GitHub Free does not offer
protected branches on private repositories); any other 403 is ``forbidden``.
"""

from datetime import UTC, datetime
from typing import Any

from baluarte.integrations.github.client import API_URL, GitHubClient
from baluarte.integrations.gitlab.capabilities import (
    CapabilitySnapshot,
    Probe,
    ProbeStatus,
    TokenUser,
)
from baluarte.integrations.gitlab.client import Forbidden, GitLabError, NotFound, Unauthorized

CORE_PROBES = ("org.read", "repos.list", "repos.visibility", "repos.default_branch")


def _failed(endpoint: str, error: GitLabError) -> Probe:
    status: ProbeStatus
    text = str(error)
    if isinstance(error, Forbidden) and "upgrade" in text.lower():
        status = "unsupported"
    elif isinstance(error, Unauthorized | Forbidden):
        status = "forbidden"
    elif isinstance(error, NotFound):
        status = "not_found"
    else:
        status = "error"
    return Probe(status=status, endpoint=endpoint, http_status=error.status, detail=text)


def _get(
    client: GitHubClient, endpoint: str, path: str, params: dict[str, Any] | None = None
) -> tuple[Any, Probe]:
    try:
        body = client.get_json(path, params)
    except GitLabError as error:
        return None, _failed(endpoint, error)
    return body, Probe(status="supported", endpoint=endpoint, http_status=200)


def _field(endpoint: str, sample: Any, name: str) -> Probe:
    if not isinstance(sample, dict):
        return Probe(status="skipped", endpoint=endpoint, detail="no visible object to inspect")
    if name in sample:
        return Probe(status="supported", endpoint=endpoint)
    return Probe(status="error", endpoint=endpoint, detail=f"response has no {name!r} field")


def detect(
    client: GitHubClient, organization: str, now: datetime | None = None
) -> CapabilitySnapshot:
    checked_at = now or datetime.now(UTC)
    probes: dict[str, Probe] = {}
    org_path = f"/orgs/{organization}"
    org, probes["org.read"] = _get(client, "GET /orgs/:org", org_path)
    if org is None:
        return CapabilitySnapshot(
            status="failed",
            checked_at=checked_at,
            version="github.com",
            edition=None,
            tier=None,
            error=probes["org.read"].detail,
            probes=probes,
        )
    two_factor = org.get("two_factor_requirement_enabled") if isinstance(org, dict) else None
    probes["org.two_factor"] = (
        Probe(status="supported", endpoint="GET /orgs/:org")
        if isinstance(two_factor, bool)
        else Probe(
            status="forbidden",
            endpoint="GET /orgs/:org",
            detail="two_factor_requirement_enabled is shown only to organization owners",
        )
    )
    plan = org.get("plan") if isinstance(org, dict) else None
    tier = (
        plan.get("name") if isinstance(plan, dict) and isinstance(plan.get("name"), str) else None
    )

    repos, probes["repos.list"] = _get(
        client, "GET /orgs/:org/repos", f"{org_path}/repos", {"per_page": 1, "type": "all"}
    )
    sample = repos[0] if isinstance(repos, list) and repos else None
    probes["repos.visibility"] = _field("GET /orgs/:org/repos", sample, "visibility")
    probes["repos.default_branch"] = _field("GET /orgs/:org/repos", sample, "default_branch")
    _, probes["teams.list"] = _get(
        client, "GET /orgs/:org/teams", f"{org_path}/teams", {"per_page": 1}
    )
    _, probes["members.list"] = _get(
        client, "GET /orgs/:org/members", f"{org_path}/members", {"per_page": 1}
    )
    full_name = sample.get("full_name") if isinstance(sample, dict) else None
    branch = sample.get("default_branch") if isinstance(sample, dict) else None
    for key, endpoint, suffix in (
        (
            "branch_protection.read",
            "GET /repos/:owner/:repo/branches/:branch/protection",
            "branches/{branch}/protection",
        ),
        ("rules.read", "GET /repos/:owner/:repo/rules/branches/:branch", "rules/branches/{branch}"),
    ):
        if not isinstance(full_name, str) or not isinstance(branch, str):
            probes[key] = Probe(status="skipped", endpoint=endpoint, detail="no repository to read")
            continue
        _, probe = _get(client, endpoint, f"/repos/{full_name}/" + suffix.format(branch=branch))
        if probe.status == "not_found" and key == "branch_protection.read":
            # GitHub answers 404 for an unprotected branch: the read itself is allowed.
            probe = Probe(
                status="supported",
                endpoint=endpoint,
                http_status=404,
                detail="the default branch of the sampled repository is not protected",
            )
        probes[key] = probe

    token_user: TokenUser | None = None
    if client.acts_as_user:  # an App installation acts as no user
        user, probes["user.read"] = _get(client, "GET /user", "/user")
        if isinstance(user, dict) and isinstance(user.get("login"), str):
            membership, probes["membership.read"] = _get(
                client,
                "GET /user/memberships/orgs/:org",
                f"/user/memberships/orgs/{organization}",
            )
            role = membership.get("role") if isinstance(membership, dict) else None
            token_user = TokenUser(username=user["login"], is_admin=role == "admin")

    core = all(probes[name].status == "supported" for name in CORE_PROBES)
    return CapabilitySnapshot(
        status="ok" if core else "partial",
        checked_at=checked_at,
        version="github.com",
        edition=None,
        tier=tier,
        error=None,
        probes=probes,
        token_user=token_user,
    )


__all__ = ["API_URL", "CORE_PROBES", "detect"]
