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


Scan = Literal[
    "secret_detection", "sast", "sast_iac", "dependency_scanning", "container_scanning", "dast"
]


class SecurityPolicyScanExecutionRule(_Rule):
    """Passes when the enabled GitLab scan execution policies that reach the project, taken
    together, run every scan in ``scans`` on its default branch."""

    type: Literal["gitlab.security_policy.scan_execution"]
    scans: Annotated[list[Scan], Field(min_length=1, max_length=6)]
    allow_skip_ci: Annotated[
        bool,
        Field(
            description="When false, a policy that lets pipelines skip it with [skip ci] "
            "(skip_ci.allowed: true, GitLab's default) does not count."
        ),
    ] = True

    @model_validator(mode="after")
    def _unique(self) -> "SecurityPolicyScanExecutionRule":
        if len(set(self.scans)) != len(self.scans):
            raise ValueError("scans must not repeat a scan")
        return self


class SecurityPolicyApprovalRule(_Rule):
    """Passes when an enabled GitLab approval policy that reaches the project requires at least
    ``min_approvals`` approvals on every merge request to its protected branches."""

    type: Literal["gitlab.security_policy.approval"]
    min_approvals: Annotated[int, Field(ge=1, le=20)]
    forbid_author_approval: Annotated[
        bool,
        Field(description="Require approval_settings.prevent_approval_by_author: true."),
    ] = False
    forbid_committer_approval: Annotated[
        bool,
        Field(description="Require approval_settings.prevent_approval_by_commit_author: true."),
    ] = False


class MergeRequestApprovalsRule(_Rule):
    """Passes when the project's own approval rules require at least ``min_approvals`` approvals
    on merge requests to its default branch, and its approval settings meet every flag set."""

    type: Literal["gitlab.project.merge_request_approvals"]
    min_approvals: Annotated[
        int,
        Field(
            ge=0,
            le=20,
            description="The largest approvals_required among the regular and any-approver "
            "rules that cover the default branch must be at least this.",
        ),
    ]
    forbid_author_approval: Annotated[
        bool, Field(description="Require merge_requests_author_approval: false.")
    ] = False
    forbid_committer_approval: Annotated[
        bool, Field(description="Require merge_requests_disable_committers_approval: true.")
    ] = False
    reset_approvals_on_push: Annotated[
        bool, Field(description="Require reset_approvals_on_push: true.")
    ] = False
    forbid_override: Annotated[
        bool,
        Field(description="Require disable_overriding_approvers_per_merge_request: true."),
    ] = False


class PrivilegedMembersRule(_Rule):
    """Passes when at most ``max_privileged`` members, direct or inherited, hold the Maintainer
    or Owner role on the project."""

    type: Literal["gitlab.project.privileged_members"]
    max_privileged: Annotated[int, Field(ge=1, le=1000)]
    forbid_inactive_accounts: Annotated[
        bool,
        Field(
            description="Also fail when a member's account is not active (blocked, deactivated "
            "or awaiting approval) but still holds access."
        ),
    ] = False


class TwoFactorRequiredRule(_Rule):
    """Passes when a group above the project, or the instance, requires two-factor
    authentication of its members."""

    type: Literal["gitlab.group.two_factor_required"]
    max_grace_hours: Annotated[
        int | None,
        Field(
            ge=0,
            le=720,
            description="When set, the shortest grace period among the groups (and instance) "
            "that require it must be at most this many hours.",
        ),
    ] = None


class AccessTokensRule(_Rule):
    """Passes when every active project access token expires within ``max_lifetime_days`` of its
    creation and, when ``max_unused_days`` is set, was used within that many days."""

    type: Literal["gitlab.project.access_tokens"]
    max_lifetime_days: Annotated[int, Field(ge=1, le=365)]
    max_unused_days: Annotated[int | None, Field(ge=1, le=365)] = None


WindowDays = Annotated[
    int,
    Field(
        ge=1,
        le=365,
        description="Judge the changes of the last this many days, up to the evaluation.",
    ),
]


class ChangeReviewedRule(_Rule):
    """Passes when every change to the project's default branch in the window was approved by at
    least ``min_approvals`` people other than its author, and none was pushed directly unless
    ``allow_direct_push``."""

    type: Literal["gitlab.change.reviewed"]
    window_days: WindowDays = 90
    min_approvals: Annotated[int, Field(ge=1, le=20)] = 1
    forbid_self_approval: Annotated[
        bool, Field(description="Fail a merge request its author approved.")
    ] = True
    allow_direct_push: Annotated[
        bool, Field(description="Accept commits pushed to the default branch without review.")
    ] = False


class ChangePipelinePassedRule(_Rule):
    """Passes when the pipeline for the last commit of every change in the window succeeded."""

    type: Literal["gitlab.change.pipeline_passed"]
    window_days: WindowDays = 90
    require_tests: Annotated[
        bool,
        Field(description="Also require a test report with no failed or erroring tests."),
    ] = False


class ChangeDeployedAfterReviewRule(_Rule):
    """Passes when every successful production deployment in the window deployed a commit from a
    merge request that was reviewed by someone other than its author."""

    type: Literal["gitlab.change.deployed_after_review"]
    window_days: WindowDays = 90


IgnorableDrift = Literal[
    "missing", "misplaced", "misnamed", "color", "description", "duplicate", "unknown_label"
]


class LabelsConsistentRule(_Rule):
    """Passes when no label drift from the label taxonomy in force reaches the project: on the
    project itself or on a group above it (docs/LABELS.md). Drift kinds in ``ignore`` do not
    count; a group or project whose labels could not be read is never ignored."""

    type: Literal["gitlab.labels.consistent"]
    ignore: Annotated[
        list[IgnorableDrift],
        Field(max_length=7, description="Drift kinds that do not make the rule fail."),
    ] = []

    @model_validator(mode="after")
    def _unique(self) -> "LabelsConsistentRule":
        if len(set(self.ignore)) != len(self.ignore):
            raise ValueError("ignore must not repeat a drift kind")
        return self


Rule = Annotated[
    ProjectVisibilityRule
    | DefaultBranchProtectedRule
    | SecurityPolicyScanExecutionRule
    | SecurityPolicyApprovalRule
    | MergeRequestApprovalsRule
    | PrivilegedMembersRule
    | TwoFactorRequiredRule
    | AccessTokensRule
    | ChangeReviewedRule
    | ChangePipelinePassedRule
    | ChangeDeployedAfterReviewRule
    | LabelsConsistentRule,
    Field(discriminator="type"),
]
RULE_TYPES = (
    "gitlab.project.visibility",
    "gitlab.branch.default_protected",
    "gitlab.security_policy.scan_execution",
    "gitlab.security_policy.approval",
    "gitlab.project.merge_request_approvals",
    "gitlab.project.privileged_members",
    "gitlab.group.two_factor_required",
    "gitlab.project.access_tokens",
    "gitlab.change.reviewed",
    "gitlab.change.pipeline_passed",
    "gitlab.change.deployed_after_review",
    "gitlab.labels.consistent",
)


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
