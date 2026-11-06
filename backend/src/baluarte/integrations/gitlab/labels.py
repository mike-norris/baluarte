"""Read the labels each group and project defines: the evidence for label drift (docs/LABELS.md).

The ``gitlab.collect_labels`` job runs after each project discovery, as a collection run of kind
``labels``. Every request is a GET with the read-only token:

- every active group: ``GET /groups/:id/labels?only_group_labels=true&include_ancestor_groups=false
  &include_descendant_groups=false``, so each label is recorded on the group that defines it;
- every active, unarchived project in monitoring scope:
  ``GET /projects/:id/labels?include_ancestor_groups=false``, its own labels only (labels it
  inherits are read from its groups).

Each group or project gets one observation of its label list (``GROUP_SOURCE`` or
``PROJECT_SOURCE``), sorted by name: id, name, color, description, and for a project its
priority. A 403 or 404 is recorded as ``forbidden`` or ``not_found`` and judged unknown, never
as "no labels". A 401 stops the collection: the remaining owners are recorded as not read.

The label plan builder reads the owners in its scope the same way just before it builds, so a
plan always starts from a fresh read.
"""

import logging
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from baluarte.db.models import GitLabGroup
from baluarte.evidence.observations import Observed, Provenance, record
from baluarte.integrations.gitlab.client import (
    Forbidden,
    GitLabClient,
    GitLabError,
    NotFound,
    Unauthorized,
)
from baluarte.integrations.gitlab.project_reads import targets

logger = logging.getLogger(__name__)

GROUP_SOURCE = "GET /groups/:id/labels"
PROJECT_SOURCE = "GET /projects/:id/labels"
SOURCES = (GROUP_SOURCE, PROJECT_SOURCE)
PROBE = "labels.read"
GROUP_PARAMS = {
    "only_group_labels": "true",
    "include_ancestor_groups": "false",
    "include_descendant_groups": "false",
}
PROJECT_PARAMS = {"include_ancestor_groups": "false"}
BATCH = 100
STOPPED_BY_REJECTION = "not read: GitLab rejected the token earlier in this collection"

OwnerType = Literal["group", "project"]


@dataclass(frozen=True)
class Owner:
    """A group or project whose labels are read, as the inventory records it."""

    type: OwnerType
    row_id: int
    gitlab_id: int
    path: str

    @property
    def column(self) -> Literal["group_id", "project_id"]:
        return "group_id" if self.type == "group" else "project_id"

    @property
    def source(self) -> str:
        return GROUP_SOURCE if self.type == "group" else PROJECT_SOURCE

    @property
    def api_path(self) -> str:
        return f"/{self.type}s/{self.gitlab_id}/labels"


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def label_entry(item: dict[str, Any], owner_type: OwnerType) -> dict[str, Any] | None:
    """One label as recorded; None for an item without an ID or name."""
    label_id, name = item.get("id"), item.get("name")
    if type(label_id) is not int or not isinstance(name, str):
        return None
    entry: dict[str, Any] = {
        "id": label_id,
        "name": name,
        "color": _text(item.get("color")),
        "description": _text(item.get("description")) or None,
    }
    if owner_type == "project":
        priority = item.get("priority")
        entry["priority"] = priority if type(priority) is int else None
    if item.get("archived") is True:
        entry["archived"] = True
    return entry


def read_labels(client: GitLabClient, owner: Owner) -> list[dict[str, Any]]:
    """The labels ``owner`` defines itself, sorted by name. Raises GitLabError."""
    params = GROUP_PARAMS if owner.type == "group" else PROJECT_PARAMS
    found: list[dict[str, Any]] = []
    for page in client.paginate(owner.api_path, params):
        for item in page:
            # Inherited labels are never the project's own, whatever the parameters did.
            if owner.type == "project" and item.get("is_project_label") is False:
                continue
            entry = label_entry(item, owner.type)
            if entry is not None:
                found.append(entry)
    return sorted(found, key=lambda entry: (entry["name"], entry["id"]))


def _status(error: GitLabError) -> str:
    if isinstance(error, Forbidden):
        return "forbidden"
    if isinstance(error, NotFound):
        return "not_found"
    return "failed"


def observe(client: GitLabClient, owner: Owner) -> Observed:
    """Read ``owner``'s labels as an observation. Raises Unauthorized on 401."""
    try:
        labels = read_labels(client, owner)
    except Unauthorized:
        raise
    except GitLabError as error:
        return Observed(
            target_id=owner.row_id,
            source=owner.source,
            source_ref=owner.path,
            collection_status=_status(error),
            error=str(error),
        )
    return Observed(
        target_id=owner.row_id,
        source=owner.source,
        source_ref=owner.path,
        collection_status="ok",
        payload={"owner": owner.path, "labels": labels},
    )


def unread(owner: Owner, error: str) -> Observed:
    return Observed(
        target_id=owner.row_id,
        source=owner.source,
        source_ref=owner.path,
        collection_status="failed",
        error=error,
    )


def groups(db: Session, instance_id: int) -> list[Owner]:
    """Every active group of the inventory."""
    rows = db.execute(
        select(GitLabGroup.id, GitLabGroup.gitlab_id, GitLabGroup.full_path)
        .where(GitLabGroup.instance_id == instance_id, GitLabGroup.state == "active")
        .order_by(GitLabGroup.full_path)
    ).all()
    return [Owner("group", *row) for row in rows]


def owners(engine: Engine, instance_id: int) -> list[Owner]:
    """Active groups, then active, unarchived projects in monitoring scope."""
    with Session(engine) as db:
        found = groups(db, instance_id)
    projects = [
        Owner("project", target.row_id, target.gitlab_id, target.path)
        for target in targets(engine, instance_id)
        if not target.archived
    ]
    return found + sorted(projects, key=lambda owner: owner.path)


@dataclass
class LabelCollection:
    """What a label collection read."""

    groups: Counter[str] = field(default_factory=Counter)
    projects: Counter[str] = field(default_factory=Counter)
    labels: int = 0
    rejected: bool = False
    # The observations recorded or confirmed, by owner, for a caller that builds on them.
    observed: dict[tuple[OwnerType, int], Observed] = field(default_factory=dict)

    @property
    def read(self) -> int:
        return sum(self.groups.values()) + sum(self.projects.values())

    @property
    def failed(self) -> int:
        return sum(
            count
            for counts in (self.groups, self.projects)
            for status, count in counts.items()
            if status != "ok"
        )

    def as_counts(self) -> dict[str, Any]:
        return {
            "groups": dict(sorted(self.groups.items())),
            "projects": dict(sorted(self.projects.items())),
            "labels": self.labels,
        }


def collect(
    client: GitLabClient,
    engine: Engine,
    instance_id: int,
    provenance: Provenance | None = None,
    only: Iterable[Owner] | None = None,
) -> LabelCollection:
    """Read every owner's labels (or only those in ``only``) and record what each defines."""
    result = LabelCollection()
    pending: dict[str, list[Observed]] = {GROUP_SOURCE: [], PROJECT_SOURCE: []}

    def flush() -> None:
        with Session(engine) as db:
            record(db, instance_id, "group_id", pending[GROUP_SOURCE], provenance)
            record(db, instance_id, "project_id", pending[PROJECT_SOURCE], provenance)
            db.commit()
        for observed in pending.values():
            observed.clear()

    chosen: Sequence[Owner] = list(only) if only is not None else owners(engine, instance_id)
    for owner in chosen:
        if result.rejected:
            observed = unread(owner, STOPPED_BY_REJECTION)
        else:
            try:
                observed = observe(client, owner)
            except Unauthorized as error:
                result.rejected = True
                observed = unread(owner, str(error))
        (result.groups if owner.type == "group" else result.projects)[
            observed.collection_status
        ] += 1
        if observed.payload is not None:
            result.labels += len(observed.payload["labels"])
        result.observed[(owner.type, owner.row_id)] = observed
        pending[owner.source].append(observed)
        if sum(len(items) for items in pending.values()) >= BATCH:
            flush()
    flush()
    if result.failed:
        logger.warning("gitlab: labels of %s groups or projects could not be read", result.failed)
    return result
