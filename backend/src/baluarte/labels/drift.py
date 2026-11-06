"""Label drift: where the groups and projects in a taxonomy's scope differ from it.

Drift is computed, never stored: from the taxonomy in force, today's group tree and the latest
label observation of each group and project (at a past moment with ``as_of``).

The scope is cut into hierarchies. The root of each is a group in scope whose parent is not (with
``placement: top_level_group`` and a scope of ``"*"``, every top-level group); it is where every
taxonomy label should be defined, and its subgroups and projects inherit them. Per hierarchy and
taxonomy label:

- ``missing``: the root does not define it, so nothing in the hierarchy can rely on it. Reported
  once, on the root.
- ``misnamed``: a label whose name differs only in case or spacing, or is one of the label's
  aliases, is defined instead of (or besides) the canonical name.
- ``misplaced``: the root does not define it but a subgroup or project does.
- ``duplicate``: the root defines it and a subgroup or project defines it again, shadowing it.
- ``color`` and ``description``: the root's definition differs from the taxonomy.
- ``unknown_label``: with ``strict: true``, a label the taxonomy does not name.
- ``unknown``: a group or project whose labels could not be read. A hierarchy whose root could
  not be read reports only that; nothing is claimed missing on evidence that does not exist.

Projects in a personal namespace belong to no group and so to no hierarchy; they are left out.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from baluarte.db.models import GitLabGroup, GitLabProject, LabelTaxonomy, Observation
from baluarte.evidence.observations import verify
from baluarte.integrations.gitlab.labels import GROUP_SOURCE, PROJECT_SOURCE
from baluarte.labels.taxonomy import (
    LabelSpec,
    TaxonomySpec,
    covers,
    in_force,
    normalized,
    same_color,
    spec_of,
)
from baluarte.scope.exclusions import coverage

DriftKind = Literal[
    "missing",
    "misplaced",
    "misnamed",
    "color",
    "description",
    "duplicate",
    "unknown_label",
    "unknown",
]
DRIFT_KINDS: tuple[DriftKind, ...] = (
    "missing",
    "misplaced",
    "misnamed",
    "color",
    "description",
    "duplicate",
    "unknown_label",
    "unknown",
)


@dataclass(frozen=True)
class OwnerRef:
    type: Literal["group", "project"]
    gitlab_id: int
    path: str

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type, "gitlab_id": self.gitlab_id, "path": self.path}


@dataclass(frozen=True)
class OwnerLabels:
    """The latest label observation of one group or project."""

    observation_id: int
    status: str
    collected_at: datetime
    payload_digest: str | None
    payload: dict[str, Any] | None
    error: str | None
    digest_valid: bool = True

    @property
    def readable(self) -> bool:
        return self.status == "ok" and self.payload is not None and self.digest_valid

    @property
    def labels(self) -> list[dict[str, Any]]:
        return list(self.payload.get("labels", [])) if self.payload else []

    def ref(self) -> dict[str, Any]:
        return {
            "id": self.observation_id,
            "payload_digest": self.payload_digest,
            "collected_at": self.collected_at.isoformat().replace("+00:00", "Z"),
        }


@dataclass(frozen=True)
class DriftItem:
    owner: OwnerRef
    root: OwnerRef
    kind: DriftKind
    label: str | None
    observed: dict[str, Any] | None
    expected: dict[str, Any] | None
    reason: str | None
    observations: tuple[OwnerLabels, ...]
    # The GitLab label ID observed, for a plan step that edits it.
    label_id: int | None = None

    @property
    def id(self) -> str:
        return f"{self.owner.type}:{self.owner.gitlab_id}:{self.kind}:{self.label or '-'}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner": self.owner.as_dict(),
            "top_level_group": self.root.path,
            "kind": self.kind,
            "label": self.label,
            "observed": self.observed,
            "expected": self.expected,
            "reason": self.reason,
            "observations": [o.ref() for o in self.observations],
        }


@dataclass(frozen=True)
class Group:
    row_id: int
    gitlab_id: int
    parent_gitlab_id: int | None
    path: str


@dataclass(frozen=True)
class Project:
    row_id: int
    gitlab_id: int
    path: str
    namespace_gitlab_id: int


@dataclass
class Hierarchy:
    root: Group
    groups: list[Group] = field(default_factory=list)
    projects: list[Project] = field(default_factory=list)


@dataclass(frozen=True)
class Inventory:
    groups: Mapping[int, Group]
    projects: Sequence[Project]


def _ref(owner: Group | Project) -> OwnerRef:
    if isinstance(owner, Group):
        return OwnerRef("group", owner.gitlab_id, owner.path)
    return OwnerRef("project", owner.gitlab_id, owner.path)


def hierarchies(spec: TaxonomySpec, inventory: Inventory) -> list[Hierarchy]:
    """The hierarchies in scope, by root path. A group whose parent is not recorded is a root."""
    in_scope = {
        gid: group for gid, group in inventory.groups.items() if covers(spec.scope, group.path)
    }

    def root_of(group: Group) -> Group:
        current = group
        while current.parent_gitlab_id in in_scope:
            current = in_scope[current.parent_gitlab_id]
        return current

    found: dict[int, Hierarchy] = {}
    for group in sorted(in_scope.values(), key=lambda g: g.path):
        root = root_of(group)
        tree = found.setdefault(root.gitlab_id, Hierarchy(root))
        if group is not root:
            tree.groups.append(group)
    for project in sorted(inventory.projects, key=lambda p: p.path):
        namespace = in_scope.get(project.namespace_gitlab_id)
        if namespace is not None:
            found[root_of(namespace).gitlab_id].projects.append(project)
    return sorted(found.values(), key=lambda tree: tree.root.path)


def _definition(owner: OwnerRef, label: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": label.get("name"),
        "color": label.get("color"),
        "description": label.get("description"),
        "defined_at": owner.as_dict(),
    }


def _expected(spec: LabelSpec) -> dict[str, Any]:
    return {"name": spec.name, "color": spec.color, "description": spec.description}


def hierarchy_drift(
    spec: TaxonomySpec,
    tree: Hierarchy,
    labels: Mapping[tuple[str, int], OwnerLabels],
) -> list[DriftItem]:
    """Every drift item of one hierarchy. ``labels`` holds observations by (type, row ID)."""
    root = _ref(tree.root)
    items: list[DriftItem] = []
    members: list[tuple[OwnerRef, OwnerLabels | None]] = [
        (_ref(group), labels.get(("group", group.row_id))) for group in tree.groups
    ] + [(_ref(project), labels.get(("project", project.row_id))) for project in tree.projects]
    root_labels = labels.get(("group", tree.root.row_id))

    def unknown(owner: OwnerRef, observed: OwnerLabels | None) -> DriftItem:
        if observed is None:
            reason = "Its labels have not been read yet."
        elif not observed.digest_valid:
            reason = "The stored observation of its labels fails its integrity check."
        else:
            reason = observed.error or f"Its labels could not be read ({observed.status})."
        return DriftItem(
            owner, root, "unknown", None, None, None, reason, (observed,) if observed else ()
        )

    if root_labels is None or not root_labels.readable:
        return [unknown(root, root_labels)]
    readable: list[tuple[OwnerRef, OwnerLabels]] = []
    for owner, observed in members:
        if observed is None or not observed.readable:
            items.append(unknown(owner, observed))
        else:
            readable.append((owner, observed))

    names = {normalized(label.name): label for label in spec.labels}
    aliases = {normalized(alias): label for label in spec.labels for alias in label.aliases}

    def canonical(name: str) -> LabelSpec | None:
        key = normalized(name)
        return names.get(key) or aliases.get(key)

    everyone = [(root, root_labels), *readable]
    for wanted in spec.labels:
        exact = [
            (owner, observed, label)
            for owner, observed in everyone
            for label in observed.labels
            if label.get("name") == wanted.name
        ]
        variants = [
            (owner, observed, label)
            for owner, observed in everyone
            for label in observed.labels
            if label.get("name") != wanted.name and canonical(str(label.get("name"))) is wanted
        ]
        at_root = [entry for entry in exact if entry[0] == root]
        if at_root:
            _, observed, label = at_root[0]
            match = same_color(label.get("color"), wanted.color)
            if match is False or match is None:
                items.append(
                    DriftItem(
                        root,
                        root,
                        "color",
                        wanted.name,
                        {"color": label.get("color")},
                        {"color": wanted.color},
                        None if match is False else "GitLab reported a colour that is not #RRGGBB.",
                        (observed,),
                        label_id=label.get("id"),
                    )
                )
            if (label.get("description") or None) != wanted.description:
                items.append(
                    DriftItem(
                        root,
                        root,
                        "description",
                        wanted.name,
                        {"description": label.get("description")},
                        {"description": wanted.description},
                        None,
                        (observed,),
                        label_id=label.get("id"),
                    )
                )
        elif not any(owner == root for owner, _, _ in variants):
            items.append(
                DriftItem(
                    root,
                    root,
                    "missing",
                    wanted.name,
                    None,
                    {**_expected(wanted), "placement": root.as_dict()},
                    None,
                    (root_labels,),
                )
            )
        for owner, observed, label in exact:
            if owner == root:
                continue
            items.append(
                DriftItem(
                    owner,
                    root,
                    "duplicate" if at_root else "misplaced",
                    wanted.name,
                    _definition(owner, label),
                    {"placement": root.as_dict()},
                    f"Also defined on {root.path}." if at_root else None,
                    (observed,),
                    label_id=label.get("id"),
                )
            )
        for owner, observed, label in variants:
            items.append(
                DriftItem(
                    owner,
                    root,
                    "misnamed",
                    wanted.name,
                    _definition(owner, label),
                    {**_expected(wanted), "placement": root.as_dict()},
                    None,
                    (observed,),
                    label_id=label.get("id"),
                )
            )
    if spec.strict:
        for owner, observed in everyone:
            for label in observed.labels:
                name = str(label.get("name"))
                if canonical(name) is None:
                    items.append(
                        DriftItem(
                            owner,
                            root,
                            "unknown_label",
                            name,
                            _definition(owner, label),
                            None,
                            None,
                            (observed,),
                            label_id=label.get("id"),
                        )
                    )
    return items


def compute(
    spec: TaxonomySpec,
    inventory: Inventory,
    labels: Mapping[tuple[str, int], OwnerLabels],
) -> list[DriftItem]:
    """Every drift item in the taxonomy's scope, ordered by owner path, kind and label."""
    items = [
        item
        for tree in hierarchies(spec, inventory)
        for item in hierarchy_drift(spec, tree, labels)
    ]
    order = {kind: index for index, kind in enumerate(DRIFT_KINDS)}
    return sorted(
        items, key=lambda i: (i.owner.path, i.owner.type, order[i.kind], i.label or "", i.id)
    )


def chain(inventory: Inventory, project: Project) -> list[tuple[str, int]]:
    """The (type, GitLab ID) of a project and every recorded group above it."""
    found: list[tuple[str, int]] = [("project", project.gitlab_id)]
    current = inventory.groups.get(project.namespace_gitlab_id)
    while current is not None:
        found.append(("group", current.gitlab_id))
        if current.parent_gitlab_id is None:
            break
        current = inventory.groups.get(current.parent_gitlab_id)
    return found


def for_project(
    items: Iterable[DriftItem], inventory: Inventory, project: Project
) -> list[DriftItem]:
    """The drift that reaches a project: on the project, or on a group above it."""
    owners = set(chain(inventory, project))
    return [item for item in items if (item.owner.type, item.owner.gitlab_id) in owners]


# Loading from the database.


def inventory(db: Session, instance_id: int, as_of: datetime | None = None) -> Inventory:
    """Active groups, and active, unarchived group projects in monitoring scope (at ``as_of``).

    The group tree and project fields are today's: the inventory keeps no history.
    """
    groups = {
        row.gitlab_id: Group(row.id, row.gitlab_id, row.parent_gitlab_id, row.full_path)
        for row in db.scalars(
            select(GitLabGroup).where(
                GitLabGroup.instance_id == instance_id, GitLabGroup.state == "active"
            )
        )
    }
    scope = coverage(db, instance_id, as_of)
    projects = [
        Project(row.id, row.gitlab_id, row.path_with_namespace, row.namespace_gitlab_id)
        for row in db.scalars(
            select(GitLabProject).where(
                GitLabProject.instance_id == instance_id,
                GitLabProject.state == "active",
                GitLabProject.archived.is_(False),
                scope.project_included(),
            )
        )
        if row.namespace_kind != "user"
    ]
    return Inventory(groups, projects)


def observations(
    db: Session, instance_id: int, as_of: datetime | None = None
) -> dict[tuple[str, int], OwnerLabels]:
    """The latest label observation of every group and project (collected by ``as_of``)."""
    found: dict[tuple[str, int], OwnerLabels] = {}
    for kind, column, source in (
        ("group", Observation.group_id, GROUP_SOURCE),
        ("project", Observation.project_id, PROJECT_SOURCE),
    ):
        statement = select(Observation).where(
            Observation.instance_id == instance_id,
            Observation.source == source,
            column.is_not(None),
        )
        if as_of is not None:
            statement = statement.where(Observation.collected_at <= as_of)
        rows = db.scalars(
            statement.ext(distinct_on(column)).order_by(
                column, Observation.collected_at.desc(), Observation.id.desc()
            )
        )
        for row in rows:
            target = row.group_id if kind == "group" else row.project_id
            if target is not None:
                found[(kind, target)] = owner_labels(row)
    return found


def owner_labels(row: Observation) -> OwnerLabels:
    return OwnerLabels(
        observation_id=row.id,
        status=row.collection_status,
        collected_at=row.collected_at,
        payload_digest=row.payload_digest,
        payload=row.payload,
        error=row.error,
        digest_valid=verify(row),
    )


@dataclass(frozen=True)
class Drift:
    taxonomy: LabelTaxonomy | None
    inventory: Inventory
    items: list[DriftItem]
    labels: Mapping[tuple[str, int], OwnerLabels]

    @property
    def collected_at(self) -> datetime | None:
        moments = [observed.collected_at for observed in self.labels.values()]
        return max(moments) if moments else None


def load(db: Session, instance_id: int | None, as_of: datetime | None = None) -> Drift:
    """The drift from the taxonomy in force (at ``as_of``); empty when none is in force."""
    taxonomy = in_force(db, instance_id, as_of)
    if instance_id is None or taxonomy is None:
        return Drift(taxonomy, Inventory({}, []), [], {})
    found = inventory(db, instance_id, as_of)
    labels = observations(db, instance_id, as_of)
    return Drift(taxonomy, found, compute(spec_of(taxonomy), found, labels), labels)


def totals(items: Sequence[DriftItem]) -> dict[str, Any]:
    by_kind: dict[str, int] = defaultdict(int)
    by_group: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for item in items:
        by_kind[item.kind] += 1
        by_group[item.root.path][item.kind] += 1
    return {
        "total": len(items),
        "by_kind": {kind: by_kind[kind] for kind in DRIFT_KINDS if by_kind[kind]},
        "by_group": [
            {"group": group, "total": sum(kinds.values()), "by_kind": dict(kinds)}
            for group, kinds in sorted(by_group.items())
        ],
    }
