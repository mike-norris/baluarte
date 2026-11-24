"""The configuration template format (roadmap M14.1, ADR-0016, docs/CONFIGURATION_TEMPLATES.md).

A template names exact values for settings Baluarte reads, each judged by an existing rule type,
so drift can rest on evidence. ``flatten`` turns settings into one value per setting key
(``merge_requests.min_approvals`` and so on), the unit precedence and overrides work on.
"""

from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from baluarte.policy.schema import Metadata, SecurityFeature, Visibility, _Strict

EXTENDS_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?@[1-9][0-9]{0,8}$"
MAX_CHAIN = 5


class MergeRequests(_Strict):
    min_approvals: Annotated[int, Field(ge=0, le=20)] | None = None
    forbid_author_approval: bool | None = None
    forbid_committer_approval: bool | None = None
    reset_approvals_on_push: bool | None = None
    forbid_override: bool | None = None

    @model_validator(mode="after")
    def _something(self) -> "MergeRequests":
        if all(value is None for value in self.model_dump().values()):
            raise ValueError("set at least one merge request setting, or leave merge_requests out")
        return self


class Settings(_Strict):
    visibility: Visibility | None = None
    default_branch_protected: bool | None = None
    merge_requests: MergeRequests | None = None
    security_features: (
        Annotated[list[SecurityFeature], Field(min_length=1, max_length=4)] | None
    ) = None
    labels_consistent: Literal[True] | None = None

    @model_validator(mode="after")
    def _unique(self) -> "Settings":
        features = self.security_features or []
        if len(set(features)) != len(features):
            raise ValueError("security_features must not repeat")
        return self


class Spec(_Strict):
    extends: (
        Annotated[
            str,
            Field(pattern=EXTENDS_PATTERN, description="name@version of the template it extends"),
        ]
        | None
    ) = None
    settings: Settings = Settings()


class ConfigurationTemplateDocument(_Strict):
    apiVersion: Literal["baluarte.io/v1alpha1"]
    kind: Literal["ConfigurationTemplate"]
    metadata: Metadata
    spec: Spec


# Every setting key, in the order they are shown.
SETTING_KEYS = (
    "visibility",
    "default_branch_protected",
    "merge_requests.min_approvals",
    "merge_requests.forbid_author_approval",
    "merge_requests.forbid_committer_approval",
    "merge_requests.reset_approvals_on_push",
    "merge_requests.forbid_override",
    "security_features",
    "labels_consistent",
)

# The rule type that judges each setting (docs/POLICY_ENGINE.md#rule-types).
RULE_TYPE = {
    "visibility": "gitlab.project.visibility",
    "default_branch_protected": "gitlab.branch.default_protected",
    "merge_requests.min_approvals": "gitlab.project.merge_request_approvals",
    "merge_requests.forbid_author_approval": "gitlab.project.merge_request_approvals",
    "merge_requests.forbid_committer_approval": "gitlab.project.merge_request_approvals",
    "merge_requests.reset_approvals_on_push": "gitlab.project.merge_request_approvals",
    "merge_requests.forbid_override": "gitlab.project.merge_request_approvals",
    "security_features": "github.repository.security_features",
    "labels_consistent": "gitlab.labels.consistent",
}

Support = Literal["supported", "unsupported", "plan"]
Held = tuple[Support, str | None]
_GITLAB_APPROVALS: Held = ("plan", "Approval rules need GitLab Premium or Ultimate.")
_GITHUB_PROTECTION: Held = (
    "plan",
    "Needs branch protection, which GitHub Free does not offer for private repositories.",
)

# Per setting and provider: whether it can be held, and why not or on what it depends.
SUPPORT: dict[str, dict[str, Held]] = {
    "visibility": {
        "gitlab": ("supported", None),
        "github": (
            "supported",
            "internal is only offered to organizations on GitHub Enterprise Cloud.",
        ),
    },
    "default_branch_protected": {
        "gitlab": ("supported", None),
        "github": _GITHUB_PROTECTION,
    },
    "merge_requests.min_approvals": {"gitlab": _GITLAB_APPROVALS, "github": _GITHUB_PROTECTION},
    "merge_requests.forbid_author_approval": {
        "gitlab": _GITLAB_APPROVALS,
        "github": ("supported", "Always holds: GitHub never lets an author approve."),
    },
    "merge_requests.forbid_committer_approval": {
        "gitlab": _GITLAB_APPROVALS,
        "github": ("unsupported", "GitHub has no equivalent setting."),
    },
    "merge_requests.reset_approvals_on_push": {
        "gitlab": _GITLAB_APPROVALS,
        "github": _GITHUB_PROTECTION,
    },
    "merge_requests.forbid_override": {
        "gitlab": _GITLAB_APPROVALS,
        "github": ("unsupported", "GitHub has no equivalent setting."),
    },
    "security_features": {
        "gitlab": ("unsupported", "A GitHub setting; GitLab security scanning is set by policies."),
        "github": ("supported", "Code scanning needs GitHub Advanced Security."),
    },
    "labels_consistent": {
        "gitlab": ("supported", None),
        "github": ("supported", None),
    },
}


def flatten(settings: Settings | dict[str, Any] | None) -> dict[str, Any]:
    """The settings set, one value per setting key."""
    if settings is None:
        return {}
    data = settings if isinstance(settings, dict) else settings.model_dump(exclude_none=True)
    flat: dict[str, Any] = {}
    for key, value in data.items():
        if value is None:
            continue
        if key == "merge_requests" and isinstance(value, dict):
            for field, inner in value.items():
                if inner is not None:
                    flat[f"merge_requests.{field}"] = inner
        else:
            flat[key] = list(value) if isinstance(value, (list, tuple)) else value
    return flat


def split_extends(value: str) -> tuple[str, int]:
    name, version = value.rsplit("@", 1)
    return name, int(version)
