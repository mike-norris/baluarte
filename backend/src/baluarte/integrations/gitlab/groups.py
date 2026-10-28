"""Discover the groups and nested subgroups visible to the token, and reconcile the inventory.

``GET /groups?all_available=true`` lists every group the token can see, nested subgroups
included, so one paginated listing covers the hierarchy; ``parent_gitlab_id`` records it. A
subgroup can be visible while its parent is not, so a parent ID may have no row.

Reconciliation runs only after the listing completed. A previously active group that was not
listed is looked up directly, because GitLab does not distinguish "deleted" from "hidden from
you" in a listing:

- 200: still visible (listed between pages, for example); stays ``active``.
- 401 or 403: ``inaccessible``. It exists, but this token may not read it.
- 404: ``missing``. Deleted, or hidden from the token; GitLab answers 404 for both.

Any other failure leaves the row unchanged. A group seen again becomes ``active``.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from baluarte.db.models import VISIBILITIES, GitLabGroup
from baluarte.integrations.gitlab.client import (
    Forbidden,
    GitLabClient,
    GitLabError,
    InvalidResponse,
    NotFound,
    Unauthorized,
)
from baluarte.integrations.gitlab.instances import instance_for

logger = logging.getLogger(__name__)

LIST_PARAMS = {"all_available": "true", "order_by": "id", "sort": "asc"}


@dataclass
class GroupDiscovery:
    instance_id: int
    seen: int = 0
    reconciled: dict[str, int] = field(
        default_factory=lambda: {"active": 0, "inaccessible": 0, "missing": 0, "unchanged": 0}
    )


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


def _upsert(db: Session, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    statement = pg_insert(GitLabGroup).values(rows)
    db.execute(
        statement.on_conflict_do_update(
            index_elements=[GitLabGroup.instance_id, GitLabGroup.gitlab_id],
            set_={
                "parent_gitlab_id": statement.excluded.parent_gitlab_id,
                "full_path": statement.excluded.full_path,
                "name": statement.excluded.name,
                "visibility": statement.excluded.visibility,
                "state": "active",
                "last_seen_at": func.now(),
            },
        )
    )


def _set_state(engine: Engine, row_id: int, state: str) -> None:
    with Session(engine) as db:
        db.execute(update(GitLabGroup).where(GitLabGroup.id == row_id).values(state=state))
        db.commit()


def discover_groups(client: GitLabClient, engine: Engine, base_url: str) -> GroupDiscovery:
    """List, upsert and reconcile. Raises GitLabError if the listing does not complete."""
    with Session(engine) as db:
        instance_id = instance_for(db, base_url).id
        started_at: datetime = db.execute(select(func.now())).scalar_one()
        db.commit()
    result = GroupDiscovery(instance_id=instance_id)

    # Each page is committed on its own: no transaction stays open across GitLab calls.
    for page in client.paginate("/groups", LIST_PARAMS):
        rows = [_row(instance_id, group) for group in page]
        with Session(engine) as db:
            _upsert(db, rows)
            db.commit()
        result.seen += len(rows)

    with Session(engine) as db:
        unseen = db.execute(
            select(GitLabGroup.id, GitLabGroup.gitlab_id).where(
                GitLabGroup.instance_id == instance_id,
                GitLabGroup.state == "active",
                GitLabGroup.last_seen_at < started_at,
            )
        ).all()
    for row_id, gitlab_id in unseen:
        try:
            group = client.get_json(f"/groups/{gitlab_id}", {"with_projects": "false"})
            row = _row(instance_id, group if isinstance(group, dict) else {})
        except (Unauthorized, Forbidden):
            _set_state(engine, row_id, "inaccessible")
            result.reconciled["inaccessible"] += 1
        except NotFound:
            _set_state(engine, row_id, "missing")
            result.reconciled["missing"] += 1
        except GitLabError as error:
            logger.warning("gitlab: group %s left unchanged (%s)", gitlab_id, error.reason)
            result.reconciled["unchanged"] += 1
        else:
            with Session(engine) as db:
                _upsert(db, [row])
                db.commit()
            result.reconciled["active"] += 1
    return result
