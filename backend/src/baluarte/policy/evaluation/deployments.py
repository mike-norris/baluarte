"""``gitlab.deployment.triggerer_cannot_approve``: whoever started a pipeline may not approve its
deployments (release governance RG.5, docs/RELEASE_GOVERNANCE.md).

The value is the project setting ``allow_pipeline_trigger_approve_deployment``, read with the
project's release protections in the ``changes`` collection
(``baluarte.integrations.gitlab.release_protections``). For a tag pipeline whoever started it is
the tag's creator, so the setting off keeps a tag creator from approving their own release.

The value is unknown when the release protections were never read or this part of them could not
be read. When GitLab answered without the setting, the rule is ``unsupported``: deployment
approvals need GitLab Premium or Ultimate.
"""

from datetime import datetime
from typing import Any

from baluarte.integrations.gitlab.release_protections import RELEASE_PROTECTION_SOURCE
from baluarte.policy.evaluation.engine import ProjectFacts, RuleEvaluator
from baluarte.policy.schema import Rule

RULE_TYPE = "gitlab.deployment.triggerer_cannot_approve"
PART = "pipeline_trigger_approval"


def _part(project: ProjectFacts) -> dict[str, Any] | None:
    release = project.release
    if release is None or release.protections is None:
        return None
    payload = release.protections.payload or {}
    part = payload.get(PART)
    return part if isinstance(part, dict) else None


def value_of(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """The judged value from a release protections payload; None when it is not known."""
    part = (payload or {}).get(PART)
    if not isinstance(part, dict) or part.get("status") != "ok":
        return None
    allowed = part.get("allowed")
    return {"allowed": allowed} if isinstance(allowed, bool) else None


def _observe(project: ProjectFacts) -> dict[str, Any] | None:
    release = project.release
    if release is None or release.protections is None:
        return None
    return value_of(release.protections.payload)


def _judge(_rule: Rule, observed: Any) -> tuple[bool, str]:
    if observed["allowed"]:
        return (
            False,
            "Whoever starts a pipeline may approve its deployments "
            "(allow_pipeline_trigger_approve_deployment is on), so a tag's creator can approve "
            "their own release.",
        )
    return True, "Whoever starts a pipeline may not approve its deployments."


def _value_unknown(project: ProjectFacts) -> str:
    part = _part(project)
    if project.release is None or project.release.protections is None:
        return (
            f"The release protections of project {project.path} have not been read yet; they are "
            "read with change evidence."
        )
    if part is None:
        return f"The release protections of project {project.path} do not include the setting."
    detail = f": {part['error']}" if part.get("error") else ""
    return (
        f"The deployment approval setting of project {project.path} could not be read "
        f"({part.get('status')}){detail}."
    )


def _unsupported(_rule: Rule, project: ProjectFacts) -> str | None:
    part = _part(project)
    if part is not None and part.get("status") == "ok" and part.get("allowed") is None:
        return (
            "GitLab did not report whether the pipeline's starter may approve deployments; "
            "deployment approvals need GitLab Premium or Ultimate."
        )
    return None


def _observed_at(project: ProjectFacts) -> datetime | None:
    release = project.release
    if release is None or release.protections is None:
        return None
    return release.protections.collected_at


TRIGGERER_CANNOT_APPROVE = RuleEvaluator(
    rule_type=RULE_TYPE,
    collection="changes",
    source=RELEASE_PROTECTION_SOURCE,
    capability=None,
    expected=lambda _rule: {},
    observe=_observe,
    judge=_judge,
    value_unknown=_value_unknown,
    observed_at=_observed_at,
    unsupported=_unsupported,
)
