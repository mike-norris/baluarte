"""``gitlab.labels.consistent``: no label drift from the taxonomy in force reaches the project.

The drift that reaches a project is the drift on the project and on every group above it
(``baluarte.labels.drift``). The rule passes when none of it counts: kinds listed in ``ignore``
do not. A project outside the taxonomy's scope passes with nothing to check. The value is
unknown, never assumed, when no taxonomy is in force or the labels of the project or a group
above it could not be read.

The observed value names the taxonomy, the drift items (at most ``MAX_LISTED``, with ``total``)
and the label observations it was computed from; the stored result is backed by every one of
them (``baluarte.policy.evaluation.store``).
"""

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from baluarte.db.models import GitLabProject
from baluarte.labels import drift
from baluarte.labels.taxonomy import in_force, spec_of
from baluarte.policy.evaluation.engine import LabelFacts, ProjectFacts, RuleEvaluator
from baluarte.policy.schema import LabelsConsistentRule, Rule

RULE_TYPE = "gitlab.labels.consistent"
SOURCE = "label drift"
MAX_LISTED = 50


def facts(db: Session, project: GitLabProject, as_of: datetime | None = None) -> LabelFacts:
    """The drift that reaches ``project`` from the taxonomy in force (at ``as_of``).

    Only the project and the groups above it are read, so evaluating many projects stays cheap.
    """
    taxonomy = in_force(db, project.instance_id, as_of)
    if taxonomy is None:
        return LabelFacts(None, False, (), ())
    ref = {
        "id": taxonomy.id,
        "name": taxonomy.name,
        "version": taxonomy.version,
        "yaml_digest": taxonomy.yaml_digest,
    }
    found = drift.inventory(db, project.instance_id, as_of)
    target = next((p for p in found.projects if p.row_id == project.id), None)
    if target is None:
        return LabelFacts(ref, False, (), ())
    above = {gitlab_id for kind, gitlab_id in drift.chain(found, target) if kind == "group"}
    groups = {gid: group for gid, group in found.groups.items() if gid in above}
    narrowed = drift.Inventory(groups, [target])
    spec = spec_of(taxonomy)
    if not any(tree.projects for tree in drift.hierarchies(spec, narrowed)):
        return LabelFacts(ref, False, (), ())
    owners = {("project", target.row_id)} | {("group", group.row_id) for group in groups.values()}
    labels = drift.observations(db, project.instance_id, as_of, only=owners)
    items = drift.for_project(drift.compute(spec, narrowed, labels), narrowed, target)
    return LabelFacts(
        ref,
        True,
        tuple(item.as_dict() for item in items),
        tuple(sorted((value.observation_id, value.payload_digest) for value in labels.values())),
    )


def _rule(rule: Rule) -> LabelsConsistentRule:
    if not isinstance(rule, LabelsConsistentRule):
        raise TypeError(f"not a LabelsConsistentRule: {rule.type}")
    return rule


def _expected(rule: Rule) -> dict[str, Any]:
    return {"drift": [], "ignore": list(_rule(rule).ignore)}


def _observe(project: ProjectFacts) -> Any:
    labels = project.labels
    if labels is None or labels.taxonomy is None:
        return None
    if any(item["kind"] == "unknown" for item in labels.drift):
        return None
    return {
        "taxonomy": labels.taxonomy,
        "in_scope": labels.in_scope,
        "total": len(labels.drift),
        "drift": [
            {key: item[key] for key in ("id", "kind", "label", "owner")}
            for item in labels.drift[:MAX_LISTED]
        ],
        "kinds": sorted({item["kind"] for item in labels.drift}),
        "observations": [[obs_id, digest] for obs_id, digest in labels.observations],
    }


def _judge(rule: Rule, observed: Any) -> tuple[bool, str]:
    if not observed["in_scope"]:
        return True, "The project is outside the label taxonomy's scope."
    ignore = set(_rule(rule).ignore)
    counted = [kind for kind in observed["kinds"] if kind not in ignore]
    if not counted:
        if observed["total"]:
            return True, f"Only ignored drift ({', '.join(observed['kinds'])}) reaches the project."
        return True, "Every taxonomy label is available and consistent for the project."
    return False, (
        f"{observed['total']} label drift items reach the project ({', '.join(counted)})."
    )


def _value_unknown(project: ProjectFacts) -> str:
    labels = project.labels
    if labels is None or labels.taxonomy is None:
        return "No label taxonomy is in force."
    unread = [item["owner"]["path"] for item in labels.drift if item["kind"] == "unknown"]
    return (
        f"The labels of {', '.join(unread[:5])} could not be read, so drift that reaches "
        f"project {project.path} is not known."
    )


def _observed_at(project: ProjectFacts) -> datetime | None:
    return project.last_seen_at


LABELS_CONSISTENT = RuleEvaluator(
    rule_type=RULE_TYPE,
    collection="labels",
    source=SOURCE,
    capability=None,
    expected=_expected,
    observe=_observe,
    judge=_judge,
    value_unknown=_value_unknown,
    observed_at=_observed_at,
)
