"""The result of evaluating one rule against one target, shared by every rule type.

A result carries what the rule expected, what was observed and where it came from, so that it
can be persisted (BAL-021), shown as "expected versus actual" and exported as evidence without
re-reading the inventory. ``unknown`` is never compliant.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from baluarte.integrations.gitlab.freshness import Freshness

Outcome = Literal["pass", "fail", "unknown", "unsupported", "excepted"]

# Why a rule could not be decided. Several can apply at once: all are kept, ordered by
# ``UNKNOWN_PRECEDENCE``, and the first explains the result.
UnknownReason = Literal[
    "target_not_in_inventory",
    "target_missing",
    "target_inaccessible",
    "collection_not_configured",
    "collection_failed",
    "collection_never",
    "collection_stale",
    "target_collection_error",
    "ancestry_incomplete",
    "capability_unknown",
    "value_unknown",
    # Set when a result is stored (BAL-021): a pass or fail must be backed by an observation.
    "evidence_missing",
    "evidence_mismatch",
]
UNKNOWN_PRECEDENCE: tuple[UnknownReason, ...] = (
    "target_not_in_inventory",
    "target_missing",
    "target_inaccessible",
    "collection_not_configured",
    "collection_failed",
    "collection_never",
    "collection_stale",
    "target_collection_error",
    "ancestry_incomplete",
    "capability_unknown",
    "value_unknown",
    "evidence_missing",
    "evidence_mismatch",
)

CapabilityStatus = Literal["not_required", "supported", "unsupported", "unknown"]
CollectionStatus = Freshness | Literal["not_configured"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class PolicyRef(_Frozen):
    """The immutable policy version a rule came from."""

    policy_id: int
    name: str
    version: int
    yaml_digest: str


class TargetRef(_Frozen):
    target_type: Literal["project", "group"]
    gitlab_id: int
    path: str


class CapabilityContext(_Frozen):
    # The capability probe the rule depends on; None when it needs none.
    name: str | None
    status: CapabilityStatus


class CollectionContext(_Frozen):
    """Freshness of the collection the observed value comes from, at evaluation time."""

    kind: str
    status: CollectionStatus
    last_success_at: datetime | None


class RuleResult(_Frozen):
    policy: PolicyRef
    rule_id: str
    rule_type: str
    severity: str
    target: TargetRef
    outcome: Outcome
    # What the rule requires, as written in the policy, e.g. {"allowed": ["private"]}.
    expected: dict[str, Any]
    # The current value; None whenever the outcome is unknown or unsupported.
    observed: Any
    # Where the value is read from, e.g. "gitlab_project.visibility".
    source: str
    # The ``observation.source`` of the evidence for this value, when it is not the project's
    # inventory observation ("GET /projects" or "GET /projects/:id"). None for inventory fields,
    # and then left out of the serialized result.
    observation_source: str | None = Field(default=None, exclude_if=lambda value: value is None)
    # When the value was last confirmed in GitLab; None when ``observed`` is None.
    observed_at: datetime | None
    evaluated_at: datetime
    # Safe, human-readable explanation built from fixed text and inventory paths.
    reason: str
    unknown_reasons: tuple[UnknownReason, ...]
    ancestry: Literal["complete", "incomplete"]
    capability: CapabilityContext
    collection: CollectionContext
