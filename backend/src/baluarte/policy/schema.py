"""The ``GitLabBaseline`` document, ``apiVersion: baluarte.io/v1alpha1``.

These models are the single source of truth for the format. The JSON Schema published at
``docs/schemas/gitlab-baseline.v1alpha1.json`` is generated from them (``published_schema``),
and a test keeps the two identical. Every object forbids unknown fields and every value is
strictly typed: a string is never coerced to a number or a boolean.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

API_VERSION = "baluarte.io/v1alpha1"
KIND = "GitLabBaseline"
SCHEMA_ID = "https://baluarte.io/schemas/gitlab-baseline.v1alpha1.json"

MAX_RULES = 100
MAX_SCOPE_GROUPS = 200

# DNS-label style: also safe in URLs, file names and evidence exports.
NAME_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
# "*" (every group) or a GitLab group full path such as "platform/backend".
_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}"
GROUP_PATTERN = rf"^(\*|{_SEGMENT}(/{_SEGMENT}){{0,19}})$"

Visibility = Literal["private", "internal", "public"]
Severity = Literal["low", "medium", "high", "critical"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Metadata(_Strict):
    name: Annotated[
        str,
        Field(pattern=NAME_PATTERN, description="Lowercase letters, digits and hyphens; 1 to 63."),
    ]
    description: Annotated[str, Field(max_length=1000)] | None = None


class Scope(_Strict):
    groups: Annotated[
        list[Annotated[str, Field(pattern=GROUP_PATTERN, max_length=1000)]],
        Field(
            min_length=1,
            max_length=MAX_SCOPE_GROUPS,
            description='"*" for everything the policy is assigned to, or GitLab group full '
            "paths; a path also covers its subgroups. Narrows where an assigned version applies.",
        ),
    ]

    @model_validator(mode="after")
    def _unique(self) -> "Scope":
        if len(set(self.groups)) != len(self.groups):
            raise ValueError("groups must not repeat")
        return self


class _Rule(_Strict):
    id: Annotated[
        str,
        Field(pattern=NAME_PATTERN, description="Unique within the baseline; named in findings."),
    ]
    severity: Severity = "medium"
    description: Annotated[str, Field(max_length=1000)] | None = None


class ProjectVisibilityRule(_Rule):
    """Passes when the project's visibility is one of ``allowed``."""

    type: Literal["gitlab.project.visibility"]
    allowed: Annotated[list[Visibility], Field(min_length=1, max_length=3)]

    @model_validator(mode="after")
    def _unique(self) -> "ProjectVisibilityRule":
        if len(set(self.allowed)) != len(self.allowed):
            raise ValueError("allowed must not repeat a visibility")
        return self


class DefaultBranchProtectedRule(_Rule):
    """With ``required: true``, passes when the project's default branch is protected."""

    type: Literal["gitlab.branch.default_protected"]
    required: bool


Rule = Annotated[ProjectVisibilityRule | DefaultBranchProtectedRule, Field(discriminator="type")]
RULE_TYPES = ("gitlab.project.visibility", "gitlab.branch.default_protected")


class Spec(_Strict):
    scope: Scope
    rules: Annotated[list[Rule], Field(min_length=1, max_length=MAX_RULES)]

    @model_validator(mode="after")
    def _unique_rule_ids(self) -> "Spec":
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise ValueError(f"rule id {rule.id!r} is used more than once")
            seen.add(rule.id)
        return self


class GitLabBaseline(_Strict):
    apiVersion: Literal["baluarte.io/v1alpha1"]
    kind: Literal["GitLabBaseline"]
    metadata: Metadata
    spec: Spec


def published_schema() -> dict[str, Any]:
    """The JSON Schema (draft 2020-12) published for editors and CI pipelines."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "Baluarte GitLab baseline (v1alpha1)",
        **GitLabBaseline.model_json_schema(),
    }
