"""Discover the projects visible to the token, including personal projects.

``GET /projects`` uses keyset pagination ordered by ID: GitLab limits offset pagination of this
endpoint on large instances, and keyset pages stay consistent while projects are created.
Archived projects are listed and flagged, because archiving does not remove them from scope.
Reconciliation is shared with groups (``baluarte.integrations.gitlab.discovery``).
"""

from typing import Any

from sqlalchemy import Engine

from baluarte.db.models import NAMESPACE_KINDS, VISIBILITIES, GitLabProject
from baluarte.integrations.gitlab.client import GitLabClient, InvalidResponse
from baluarte.integrations.gitlab.discovery import Discovery, ResourceKind, discover


def _row(instance_id: int, project: dict[str, Any]) -> dict[str, Any]:
    gitlab_id = project.get("id")
    path = project.get("path_with_namespace")
    namespace = project.get("namespace")
    namespace_id = namespace.get("id") if isinstance(namespace, dict) else None
    namespace_kind = namespace.get("kind") if isinstance(namespace, dict) else None
    if (
        not isinstance(gitlab_id, int)
        or not isinstance(path, str)
        or not isinstance(namespace_id, int)
    ):
        raise InvalidResponse(
            "GET /projects returned a project without an id, path_with_namespace or namespace"
        )
    visibility = project.get("visibility")
    default_branch = project.get("default_branch")
    return {
        "instance_id": instance_id,
        "gitlab_id": gitlab_id,
        "namespace_gitlab_id": namespace_id,
        # Unknown kinds are stored as null, which policy resolution treats as unconfirmed.
        "namespace_kind": namespace_kind if namespace_kind in NAMESPACE_KINDS else None,
        "path_with_namespace": path,
        "name": str(project.get("name") or path.rsplit("/", 1)[-1]),
        # An unrecognised value is stored as unknown rather than guessed.
        "visibility": visibility if visibility in VISIBILITIES else None,
        "archived": project.get("archived") is True,
        # Empty repositories have no default branch.
        "default_branch": default_branch if isinstance(default_branch, str) else None,
        "state": "active",
    }


PROJECTS = ResourceKind(
    name="project",
    model=GitLabProject,
    list_path="/projects",
    list_params={"pagination": "keyset", "order_by": "id", "sort": "asc"},
    lookup_params={"license": "false", "statistics": "false"},
    to_row=_row,
    refreshed=(
        "namespace_gitlab_id",
        "namespace_kind",
        "path_with_namespace",
        "name",
        "visibility",
        "archived",
        "default_branch",
    ),
)


def discover_projects(client: GitLabClient, engine: Engine, base_url: str) -> Discovery:
    return discover(client, engine, base_url, PROJECTS)
