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

Each outcome is also recorded as an observation (``baluarte.evidence.observations``) in the
same transaction as the inventory change: the allowlisted fields of a listed or confirmed
resource, or the status and safe error of one that could not be read.
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
from baluarte.evidence.observations import Observed, Provenance, TargetColumn, record
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
    # Observation target column, and the row fields kept as evidence. Nothing else from the
    # GitLab response is stored, so URLs, tokens and unrelated fields never reach evidence.
    target: TargetColumn
    evidence_fields: tuple[str, ...]

    @property
    def list_source(self) -> str:
        return f"GET {self.list_path}"

    @property
    def lookup_source(self) -> str:
        return f"GET {self.list_path}/:id"

    @property
    def sources(self) -> tuple[str, str]:
        """The resource's own observations; a listing and a lookup are compared with each other."""
        return (self.list_source, self.lookup_source)

    def ref(self, gitlab_id: int) -> str:
        return f"{self.list_path}/{gitlab_id}"


@dataclass
class Discovery:
    instance_id: int
    seen: int = 0
    reconciled: dict[str, int] = field(
        default_factory=lambda: {"active": 0, "inaccessible": 0, "missing": 0, "unchanged": 0}
    )
    # Default-branch protection reads (projects only): protected, unprotected, unread.
    protection: dict[str, int] | None = None


def _upsert(
    db: Session,
    kind: ResourceKind,
    rows: list[dict[str, Any]],
    source: str,
    provenance: Provenance | None,
) -> None:
    if not rows:
        return
    model = kind.model
    statement = pg_insert(model).values(rows)
    stored = db.execute(
        statement.on_conflict_do_update(
            index_elements=[model.instance_id, model.gitlab_id],
            set_={
                **{column: statement.excluded[column] for column in kind.refreshed},
                "state": "active",
                "last_seen_at": func.now(),
                "last_error": None,
                "last_error_at": None,
            },
        ).returning(model.id, model.gitlab_id)
    )
    ids = {gitlab_id: row_id for row_id, gitlab_id in stored}
    record(
        db,
        rows[0]["instance_id"],
        kind.target,
        [
            Observed(
                target_id=ids[row["gitlab_id"]],
                source=source,
                source_ref=kind.ref(row["gitlab_id"]),
                collection_status="ok",
                payload={field: row[field] for field in kind.evidence_fields},
            )
            for row in rows
        ],
        provenance,
        compare_with=kind.sources,
    )


def _update(
    engine: Engine,
    kind: ResourceKind,
    instance_id: int,
    row_id: int,
    observed: Observed,
    provenance: Provenance | None,
    **values: Any,
) -> None:
    with Session(engine) as db:
        db.execute(update(kind.model).where(kind.model.id == row_id).values(**values))
        record(db, instance_id, kind.target, [observed], provenance, compare_with=kind.sources)
        db.commit()


def _unread(kind: ResourceKind, row_id: int, gitlab_id: int, status: str, error: str) -> Observed:
    """A resource that could not be read: a status and a safe error, no payload."""
    return Observed(
        target_id=row_id,
        source=kind.lookup_source,
        source_ref=kind.ref(gitlab_id),
        collection_status=status,
        error=error,
    )


def discover(
    client: GitLabClient,
    engine: Engine,
    base_url: str,
    kind: ResourceKind,
    provenance: Provenance | None = None,
) -> Discovery:
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
            _upsert(db, kind, rows, kind.list_source, provenance)
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
            item = client.get_json(kind.ref(gitlab_id), kind.lookup_params)
            row = kind.to_row(instance_id, item if isinstance(item, dict) else {})
        except (Unauthorized, Forbidden) as error:
            _update(
                engine,
                kind,
                instance_id,
                row_id,
                _unread(kind, row_id, gitlab_id, "forbidden", str(error)),
                provenance,
                state="inaccessible",
                last_error=None,
                last_error_at=None,
            )
            result.reconciled["inaccessible"] += 1
        except NotFound as error:
            _update(
                engine,
                kind,
                instance_id,
                row_id,
                _unread(kind, row_id, gitlab_id, "not_found", str(error)),
                provenance,
                state="missing",
                last_error=None,
                last_error_at=None,
            )
            result.reconciled["missing"] += 1
        except GitLabError as error:
            logger.warning("gitlab: %s %s left unchanged (%s)", kind.name, gitlab_id, error.reason)
            message = f"could not confirm: {error}"
            _update(
                engine,
                kind,
                instance_id,
                row_id,
                _unread(kind, row_id, gitlab_id, "failed", message),
                provenance,
                last_error=message,
                last_error_at=func.now(),
            )
            result.reconciled["unchanged"] += 1
        else:
            with Session(engine) as db:
                _upsert(db, kind, [row], kind.lookup_source, provenance)
                db.commit()
            result.reconciled["active"] += 1
    return result
