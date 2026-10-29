"""Discover the groups and nested subgroups visible to the token.

``GET /groups?all_available=true`` lists every group the token can see, nested subgroups
included, so one paginated listing covers the hierarchy; ``parent_gitlab_id`` records it. A
subgroup can be visible while its parent is not, so a parent ID may have no row. Reconciliation
is shared with projects (``baluarte.integrations.gitlab.discovery``).
"""

from typing import Any

from sqlalchemy import Engine

from baluarte.db.models import VISIBILITIES, GitLabGroup
from baluarte.integrations.gitlab.client import GitLabClient, InvalidResponse
from baluarte.integrations.gitlab.discovery import Discovery, ResourceKind, discover


def _row(instance_id: int, group: dict[str, Any]) -> dict[str, Any]:
    gitlab_id = group.get("id")
    full_path = group.get("full_path")
    if not isinstance(gitlab_id, int) or not isinstance(full_path, str):
        raise InvalidResponse("GET /groups returned a group without an id or full_path")
    parent = group.get("parent_id")
    visibility = group.get("visibility")
    return {
        "instance_id": instance_id,
        "gitlab_id": gitlab_id,
        "parent_gitlab_id": parent if isinstance(parent, int) else None,
        "full_path": full_path,
        "name": str(group.get("name") or full_path.rsplit("/", 1)[-1]),
        # An unrecognised value is stored as unknown rather than guessed.
        "visibility": visibility if visibility in VISIBILITIES else None,
        "state": "active",
    }


GROUPS = ResourceKind(
    name="group",
    model=GitLabGroup,
    list_path="/groups",
    list_params={"all_available": "true", "order_by": "id", "sort": "asc"},
    lookup_params={"with_projects": "false"},
    to_row=_row,
    refreshed=("parent_gitlab_id", "full_path", "name", "visibility"),
)


def discover_groups(client: GitLabClient, engine: Engine, base_url: str) -> Discovery:
    return discover(client, engine, base_url, GROUPS)
