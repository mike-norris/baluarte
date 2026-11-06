"""The inventory's group tree, for anything that applies to a group's subgroups and projects.

Manager scopes, tags and the manager overview all treat a group as covering its subgroups and
their projects. :class:`GroupTree` loads the tree once per request and answers those questions
without further queries. Groups are today's: the inventory keeps no history of the tree.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte.db.models import GitLabGroup, GitLabProject


@dataclass(frozen=True)
class ProjectNode:
    row_id: int
    gitlab_id: int
    path: str
    # The namespace group's GitLab ID; None for a project in a personal namespace.
    group_gitlab_id: int | None
    state: str
    archived: bool


@dataclass
class GroupTree:
    parents: dict[int, int | None] = field(default_factory=dict)
    paths: dict[int, str] = field(default_factory=dict)
    states: dict[int, str] = field(default_factory=dict)
    children: dict[int | None, list[int]] = field(default_factory=dict)
    projects: dict[int, ProjectNode] = field(default_factory=dict)

    @classmethod
    def load(cls, db: Session, instance_id: int | None) -> "GroupTree":
        tree = cls()
        if instance_id is None:
            return tree
        for gitlab_id, parent, path, state in db.execute(
            select(
                GitLabGroup.gitlab_id,
                GitLabGroup.parent_gitlab_id,
                GitLabGroup.full_path,
                GitLabGroup.state,
            ).where(GitLabGroup.instance_id == instance_id)
        ):
            tree.parents[gitlab_id] = parent
            tree.paths[gitlab_id] = path
            tree.states[gitlab_id] = state
        for gitlab_id, parent in tree.parents.items():
            # A parent missing from the inventory makes the group a root of what is known.
            tree.children.setdefault(parent if parent in tree.parents else None, []).append(
                gitlab_id
            )
        for children in tree.children.values():
            children.sort(key=lambda child: tree.paths[child])
        for row_id, gitlab_id, path, namespace, kind, state, archived in db.execute(
            select(
                GitLabProject.id,
                GitLabProject.gitlab_id,
                GitLabProject.path_with_namespace,
                GitLabProject.namespace_gitlab_id,
                GitLabProject.namespace_kind,
                GitLabProject.state,
                GitLabProject.archived,
            ).where(GitLabProject.instance_id == instance_id)
        ):
            group = None if kind == "user" else namespace
            tree.projects[row_id] = ProjectNode(row_id, gitlab_id, path, group, state, archived)
        return tree

    def ancestors(self, group_gitlab_id: int | None) -> list[int]:
        """The group and its parents, nearest first, guarding against a cycle in stale data."""
        found: list[int] = []
        current = group_gitlab_id
        while current is not None and current in self.parents and current not in found:
            found.append(current)
            current = self.parents[current]
        return found

    def descendants(self, group_gitlab_id: int) -> set[int]:
        """The group and all its subgroups."""
        found: set[int] = set()
        pending = [group_gitlab_id]
        while pending:
            current = pending.pop()
            if current in found:
                continue
            found.add(current)
            pending.extend(self.children.get(current, []))
        return found

    def projects_under(self, group_gitlab_ids: Iterable[int]) -> set[int]:
        """Row IDs of the projects in the groups or their subgroups."""
        groups: set[int] = set()
        for group in group_gitlab_ids:
            groups |= self.descendants(group)
        return {node.row_id for node in self.projects.values() if node.group_gitlab_id in groups}

    def top_level(self) -> list[int]:
        return list(self.children.get(None, []))

    def project_by_gitlab_id(self, gitlab_id: int) -> ProjectNode | None:
        for node in self.projects.values():
            if node.gitlab_id == gitlab_id:
                return node
        return None
