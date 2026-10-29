"""Shared discovery and reconciliation for inventory resources (groups, projects).

A discovery lists every resource the token can see, upserting page by page on
``(instance_id, gitlab_id)`` so no transaction stays open across GitLab calls.

Reconciliation runs only after the listing completed. A previously active resource that was
not listed is looked up directly, because GitLab does not distinguish "deleted" from "hidden
from you" in a listing:

- 200: still visible (listed between pages, for example); stays ``active``.
- 401 or 403: ``inaccessible``. It exists, but this token may not read it.
- 404: ``missing``. Deleted, or hidden from the token; GitLab answers 404 for both.

Any other failure leaves the state unchanged and records ``last_error``. A resource seen again
becomes ``active`` and its error is cleared.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from baluarte.db.models import GitLabGroup, GitLabProject
from baluarte.integrations.gitlab.client import (
    Forbidden,
    GitLabClient,
    GitLabError,
    NotFound,
    Unauthorized,
)
from baluarte.integrations.gitlab.instances import instance_for

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResourceKind:
    name: str
    model: type[GitLabGroup] | type[GitLabProject]
    list_path: str
    list_params: dict[str, str]
    lookup_params: dict[str, str]
    # Builds the row from an API object; raises InvalidResponse if required fields are absent.
    to_row: Callable[[int, dict[str, Any]], dict[str, Any]]
    # Columns refreshed when a known resource is seen again.
    refreshed: tuple[str, ...]


@dataclass
class Discovery:
    instance_id: int
    seen: int = 0
    reconciled: dict[str, int] = field(
        default_factory=lambda: {"active": 0, "inaccessible": 0, "missing": 0, "unchanged": 0}
    )


def _upsert(db: Session, kind: ResourceKind, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    model = kind.model
    statement = pg_insert(model).values(rows)
    db.execute(
        statement.on_conflict_do_update(
            index_elements=[model.instance_id, model.gitlab_id],
            set_={
                **{column: statement.excluded[column] for column in kind.refreshed},
                "state": "active",
                "last_seen_at": func.now(),
                "last_error": None,
                "last_error_at": None,
            },
        )
    )


def _update(engine: Engine, kind: ResourceKind, row_id: int, **values: Any) -> None:
    with Session(engine) as db:
        db.execute(update(kind.model).where(kind.model.id == row_id).values(**values))
        db.commit()


def discover(client: GitLabClient, engine: Engine, base_url: str, kind: ResourceKind) -> Discovery:
    """List, upsert and reconcile. Raises GitLabError if the listing does not complete."""
    model = kind.model
    with Session(engine) as db:
        instance_id = instance_for(db, base_url).id
        started_at: datetime = db.execute(select(func.now())).scalar_one()
        db.commit()
    result = Discovery(instance_id=instance_id)

    for page in client.paginate(kind.list_path, kind.list_params):
        rows = [kind.to_row(instance_id, item) for item in page]
        with Session(engine) as db:
            _upsert(db, kind, rows)
            db.commit()
        result.seen += len(rows)

    with Session(engine) as db:
        unseen = db.execute(
            select(model.id, model.gitlab_id).where(
                model.instance_id == instance_id,
                model.state == "active",
                model.last_seen_at < started_at,
            )
        ).all()
    for row_id, gitlab_id in unseen:
        try:
            item = client.get_json(f"{kind.list_path}/{gitlab_id}", kind.lookup_params)
            row = kind.to_row(instance_id, item if isinstance(item, dict) else {})
        except (Unauthorized, Forbidden):
            _update(engine, kind, row_id, state="inaccessible", last_error=None, last_error_at=None)
            result.reconciled["inaccessible"] += 1
        except NotFound:
            _update(engine, kind, row_id, state="missing", last_error=None, last_error_at=None)
            result.reconciled["missing"] += 1
        except GitLabError as error:
            logger.warning("gitlab: %s %s left unchanged (%s)", kind.name, gitlab_id, error.reason)
            _update(
                engine,
                kind,
                row_id,
                last_error=f"could not confirm: {error}",
                last_error_at=func.now(),
            )
            result.reconciled["unchanged"] += 1
        else:
            with Session(engine) as db:
                _upsert(db, kind, [row])
                db.commit()
            result.reconciled["active"] += 1
    return result
