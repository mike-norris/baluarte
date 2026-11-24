"""Choosing the projects a change set targets (roadmap M14.3, docs/CONFIGURATION_TEMPLATES.md).

A selection names criteria; a project is selected when it meets every non-empty one. Only
active, monitored, unarchived projects of the connection are ever selected: archived projects
are read-only on the provider and excluded projects are out of scope.

- ``groups``: full group paths; a group also covers its subgroups.
- ``projects``: exact project paths.
- ``tags``: ``key=value`` pairs, all of which the project carries, directly or from a group
  above (owner and business unit are tags, docs/TAGS.md).
- ``template``: the name of a template that decides at least one of the project's effective
  settings.
- ``drift``: the project's drift status (``drift``, ``unknown``, ``aligned``).
- ``findings``: ``open`` for projects with open baseline findings, ``none`` for those without;
  with ``policy``, only that baseline's findings count (``policy`` alone means ``open``).
"""

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte.db.models import Finding, GitLabInstance, GitLabProject, Policy
from baluarte.policy.evaluation.model import CapabilityStatus, CollectionContext
from baluarte.policy.schema import NAME_PATTERN
from baluarte.scope.exclusions import coverage
from baluarte.tags import index as tag_index
from baluarte.tags import parse_filter
from baluarte.templates.drift import project_drift
from baluarte.templates.resolve import effective_settings

MAX_CRITERIA = 50
Path = Annotated[str, Field(min_length=1, max_length=1000)]


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    groups: Annotated[list[Path], Field(max_length=MAX_CRITERIA)] = []
    projects: Annotated[list[Path], Field(max_length=MAX_CRITERIA)] = []
    tags: Annotated[list[Annotated[str, Field(max_length=200)]], Field(max_length=10)] = []
    template: Annotated[str, Field(pattern=NAME_PATTERN, max_length=63)] | None = None
    drift: list[Literal["drift", "unknown", "aligned"]] = []
    findings: Literal["open", "none"] | None = None
    policy: Annotated[str, Field(pattern=NAME_PATTERN, max_length=63)] | None = None

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: list[str]) -> list[str]:
        for tag in value:
            if parse_filter(tag) is None:
                raise ValueError("tags are key=value")
        return value

    @field_validator("groups", "projects")
    @classmethod
    def _paths(cls, value: list[str]) -> list[str]:
        return [path.strip("/") for path in value]

    def empty(self) -> bool:
        return not (
            self.groups
            or self.projects
            or self.tags
            or self.template
            or self.drift
            or self.findings
            or self.policy
        )


def _in_groups(path: str, groups: list[str]) -> bool:
    return any(path.startswith(f"{group}/") for group in groups)


def select_projects(
    db: Session,
    instance: GitLabInstance,
    selection: Selection,
    collections: Mapping[str, CollectionContext],
    capabilities: Mapping[str, CapabilityStatus],
) -> list[GitLabProject]:
    """The projects ``selection`` targets, by path."""
    scope = coverage(db, instance.id)
    rows = db.scalars(
        select(GitLabProject)
        .where(
            GitLabProject.instance_id == instance.id,
            GitLabProject.state == "active",
            GitLabProject.archived.is_(False),
        )
        .order_by(GitLabProject.path_with_namespace)
    ).all()
    found = [row for row in rows if scope.of(row) is None]
    if selection.groups or selection.projects:
        exact = set(selection.projects)
        found = [
            row
            for row in found
            if row.path_with_namespace in exact
            or _in_groups(row.path_with_namespace, selection.groups)
        ]
    if selection.tags:
        pairs = [pair for pair in (parse_filter(tag) for tag in selection.tags) if pair]
        tagged = tag_index(db, instance.id).projects_with(pairs)
        found = [row for row in found if row.id in tagged]
    if selection.findings is not None or selection.policy is not None:
        statement = select(Finding.project_id).where(
            Finding.project_id.in_([row.id for row in found]), Finding.state == "open"
        )
        if selection.policy is not None:
            statement = statement.join(Policy, Policy.id == Finding.policy_id).where(
                Policy.name == selection.policy
            )
        with_findings = set(db.scalars(statement))
        wanted = selection.findings != "none"
        found = [row for row in found if (row.id in with_findings) == wanted]
    if selection.template is not None or selection.drift:
        kept = []
        for row in found:
            effective = effective_settings(db, instance, row)
            if selection.template is not None and not any(
                setting.decided_by.template == selection.template for setting in effective.settings
            ):
                continue
            if selection.drift:
                status = project_drift(
                    db, instance, row, collections, capabilities, effective
                ).status
                if status not in selection.drift:
                    continue
            kept.append(row)
        found = kept
    return found
