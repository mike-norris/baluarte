"""Dry-run remediation plans: the GitLab changes that would fix open findings (BAL-031).

A plan is built from stored evidence, never from a call to GitLab: nothing here uses an HTTP
client, so planning cannot change GitLab. Execution, with a separate write credential, is
BAL-032 (docs/architecture/ADR-0007-remediation-constraints.md).

Only findings that are ``open`` with a latest outcome of ``fail`` are planned, and only when
the failure is backed by the latest observation of its kind with a valid digest. ``unknown``,
``unsupported`` and ``excepted`` results are never planned: a plan fixes a known failure, it
does not guess at missing evidence or override an approved waiver.

The allowlist of actions (docs/REMEDIATION_RESEARCH.md):

- ``restrict_project_visibility``: ``PUT /projects/:id`` with the least exposed level the rule
  allows. A change that would widen access is skipped.
- ``protect_default_branch``: ``POST /projects/:id/protected_branches`` for the default branch,
  with push, merge and unprotect limited to Maintainers (40), stated explicitly.

Everything else is skipped with a reason, including group visibility, unprotecting or
recreating a protection, approval rules and push rules.

Lifecycle (enforced again by a database trigger): ``draft`` -> ``submitted`` by the creator ->
``approved`` or ``rejected`` by a user holding ``remediation:approve`` who is not the creator.
``draft``, ``submitted`` and ``approved`` plans become ``expired`` at ``expires_at`` (database
clock); reads count a plan past its expiry as expired whether or not its row says so yet.
Approval names the plan digest the approver reviewed and is refused when the plan is stale:
a finding no longer open and failing, or evidence that is no longer the latest.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import ColumnElement, and_, case, func, select, update
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.audit import details_digest
from baluarte.db.models import (
    MAX_PLAN_HOURS,
    MAX_PLAN_STEPS,
    Evaluation,
    Finding,
    GitLabInstance,
    GitLabProject,
    Observation,
    Policy,
    RemediationPlan,
)
from baluarte.evidence.observations import latest, verify
from baluarte.integrations.gitlab.projects import PROJECT_SOURCES
from baluarte.policy.loader import parse_policy
from baluarte.policy.schema import DefaultBranchProtectedRule, ProjectVisibilityRule, Rule

PlanState = Literal["draft", "submitted", "approved", "rejected", "expired"]
FORMAT = "baluarte.remediation-plan/v1"
SYSTEM_ACTOR = "system:remediation-plans"
MAX_TTL = timedelta(hours=MAX_PLAN_HOURS)
MAX_NOTE = 2000
# Least exposed first.
EXPOSURE = ("private", "internal", "public")
# GitLab access level for Maintainer, GitLab's own default for a new protection.
MAINTAINER = 40
# Projects collection statuses that allow planning or approving: the evidence is current.
CURRENT = ("fresh", "partial")

EXCLUDED_ACTIONS = {
    "group visibility": "lowering it needs every subgroup and project changed first, bottom up; "
    "left as a manual change",
    "unprotect or recreate a protection": "the branch would be unprotected between the calls",
    "approval rules": "a repeat POST creates duplicates and PUT drops unlisted approvers",
    "push rules": "DELETE removes every push rule of the project",
}

VISIBILITY_EFFECTS = (
    "Non-members lose access at once: an internal project becomes unreachable to signed-in "
    "non-members, a public one to anonymous users. Clones, pipelines and integrations that "
    "rely on that access stop working.",
    "GitLab requires a project to be at least as restrictive as its parent group; restricting "
    "a project always satisfies that.",
    "GitLab records project_visibility_level_updated only on Premium and Ultimate; on Free, "
    "Baluarte's audit log is the only record of the change.",
)
PROTECTION_EFFECTS = (
    "Pushes, merges and unprotecting the branch become limited to Maintainers (access level "
    "40), GitLab's default for a new protection; Developers can no longer push directly.",
    "Force pushes stay disabled and code owner approval is not changed.",
    "A repeat POST for an already protected branch is reported to fail with 409; it must be "
    "resolved by reading the protection back, never by deleting and recreating it.",
    "GitLab records protected_branch_created only on Premium and Ultimate.",
)


class PlanConflict(Exception):
    """The transition is not allowed in the plan's current state."""


class NothingToPlan(Exception):
    """No finding in scope can be remediated; ``skipped`` says why for each."""

    def __init__(self, skipped: list[dict[str, Any]]) -> None:
        super().__init__("No finding in scope can be remediated")
        self.skipped = skipped


class PlanStale(Exception):
    """The plan no longer matches the findings or evidence it was built from."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass(frozen=True)
class Scope:
    """Which findings to plan: explicit IDs, or every unresolved finding matching the filters."""

    finding_ids: tuple[int, ...] = ()
    policy: str | None = None
    rule: str | None = None
    project_gitlab_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        if self.finding_ids:
            return {"finding_ids": list(self.finding_ids)}
        return {
            key: value
            for key, value in (
                ("policy", self.policy),
                ("rule", self.rule),
                ("project_gitlab_id", self.project_gitlab_id),
            )
            if value is not None
        }


class ScopeTooLarge(Exception):
    pass


@dataclass
class _Built:
    steps: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)


def effective_state() -> ColumnElement[str]:
    """SQL: the state, counting a draft, submitted or approved plan past its expiry as expired."""
    return case(
        (
            and_(
                RemediationPlan.state.in_(("draft", "submitted", "approved")),
                RemediationPlan.expires_at <= func.now(),
            ),
            "expired",
        ),
        else_=RemediationPlan.state,
    )


def plan_digest(plan: dict[str, Any]) -> str:
    """SHA-256 of the plan's canonical JSON, the same algorithm as observations and audit."""
    return details_digest(plan)


def digest_valid(row: RemediationPlan) -> bool:
    return row.plan_digest == plan_digest(row.plan)


def _findings(db: Session, instance_id: int, scope: Scope) -> list[Finding]:
    statement = (
        select(Finding)
        .join(GitLabProject, GitLabProject.id == Finding.project_id)
        .where(GitLabProject.instance_id == instance_id)
    )
    if scope.finding_ids:
        statement = statement.where(Finding.id.in_(scope.finding_ids))
    else:
        statement = statement.where(Finding.state != "resolved")
        if scope.policy is not None:
            statement = statement.where(Finding.policy_name == scope.policy)
        if scope.rule is not None:
            statement = statement.where(Finding.rule_id == scope.rule)
        if scope.project_gitlab_id is not None:
            statement = statement.where(GitLabProject.gitlab_id == scope.project_gitlab_id)
    rows = list(db.scalars(statement.order_by(Finding.id).limit(MAX_PLAN_STEPS + 1)))
    if len(rows) > MAX_PLAN_STEPS:
        raise ScopeTooLarge(
            f"More than {MAX_PLAN_STEPS} findings are in scope; narrow it to at most "
            f"{MAX_PLAN_STEPS}"
        )
    return rows


def _rule(db: Session, policy_id: int, rule_id: str) -> tuple[Policy, Rule | None]:
    policy = db.get_one(Policy, policy_id)
    rules = parse_policy(policy.source_yaml).document.spec.rules
    return policy, next((rule for rule in rules if rule.id == rule_id), None)


def _family(source: str) -> Sequence[str]:
    return PROJECT_SOURCES if source in PROJECT_SOURCES else (source,)


def _evidence_is_latest(db: Session, observation: Observation) -> bool:
    newest = latest(
        db,
        observation.instance_id,
        "project_id",
        observation.project_id,
        sources=_family(observation.source),
    )
    return newest is not None and newest.id == observation.id


def _key(action: str, instance_id: int, gitlab_id: int, request: dict[str, Any]) -> str:
    """The same intended change always gets the same key, so execution can deduplicate it."""
    return f"{action}:{instance_id}:{gitlab_id}:{details_digest(request)[:16]}"


def _skip(finding: Finding, path: str, code: str, reason: str) -> dict[str, Any]:
    return {
        "finding_id": finding.id,
        "project_path": path,
        "policy_name": finding.policy_name,
        "rule_id": finding.rule_id,
        "reason_code": code,
        "reason": reason,
    }


def _visibility_step(
    project: GitLabProject, rule: ProjectVisibilityRule, current: Any
) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
    if current not in EXPOSURE:
        return None, ("value_unknown", f"The observed visibility {current!r} is not recognised.")
    intended = min(rule.allowed, key=EXPOSURE.index)
    if EXPOSURE.index(intended) >= EXPOSURE.index(current):
        return None, (
            "would_widen_access",
            f"The policy allows only {', '.join(rule.allowed)}; changing {current} to "
            f"{intended} would widen access, which remediation never does.",
        )
    path = f"/projects/{project.gitlab_id}"
    return {
        "action": "restrict_project_visibility",
        "current": {"visibility": current},
        "intended": {"visibility": intended},
        "request": {"method": "PUT", "path": path, "body": {"visibility": intended}},
        "verify": {"method": "GET", "path": path, "expect": {"visibility": intended}},
        "preconditions": [
            f"GET {path} still reports visibility {current}",
            f"GET {path} reports the project is not archived",
        ],
        "effects": list(VISIBILITY_EFFECTS),
        "requires": {"role": "Owner", "tier": "all"},
    }, None


def _protection_step(
    project: GitLabProject, rule: DefaultBranchProtectedRule, observed: Any
) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
    if not rule.required:
        return None, ("not_required", "The rule does not require protection.")
    if not isinstance(observed, dict) or observed.get("protected") is not False:
        return None, ("value_unknown", "The observed protection is not a clear 'unprotected'.")
    branch = observed.get("branch")
    if not isinstance(branch, str) or branch != project.default_branch:
        return None, (
            "branch_changed",
            f"Protection was read for branch {branch}, but the default branch is now "
            f"{project.default_branch}; it is read again at the next project discovery.",
        )
    path = f"/projects/{project.gitlab_id}/protected_branches"
    body = {
        "name": branch,
        "push_access_level": MAINTAINER,
        "merge_access_level": MAINTAINER,
        "unprotect_access_level": MAINTAINER,
    }
    return {
        "action": "protect_default_branch",
        "current": {"branch": branch, "protected": False},
        "intended": {
            "branch": branch,
            "protected": True,
            "push_access_level": MAINTAINER,
            "merge_access_level": MAINTAINER,
            "unprotect_access_level": MAINTAINER,
        },
        "request": {"method": "POST", "path": path, "body": body},
        "verify": {"method": "GET", "path": path, "expect": {"rule_matching": branch}},
        "preconditions": [
            f"GET /projects/{project.gitlab_id} still reports default branch {branch}",
            f"GET {path} still has no rule matching {branch}, wildcards included",
        ],
        "effects": list(PROTECTION_EFFECTS),
        "requires": {"role": "Maintainer (UNVERIFIED; see REMEDIATION_RESEARCH.md)", "tier": "all"},
    }, None


def _plan_one(db: Session, instance_id: int, finding: Finding, built: _Built) -> None:
    project = db.get_one(GitLabProject, finding.project_id)
    path = project.path_with_namespace

    def skip(code: str, reason: str) -> None:
        built.skipped.append(_skip(finding, path, code, reason))

    if finding.state != "open":
        skip(finding.state, f"The finding is {finding.state}; only open findings are planned.")
        return
    if finding.latest_outcome != "fail":
        skip(
            f"latest_{finding.latest_outcome}",
            f"The latest result is {finding.latest_outcome}, not fail; only a known failure "
            "is planned.",
        )
        return
    if project.state != "active":
        skip("project_" + project.state, f"The project is {project.state} in the inventory.")
        return
    if project.archived:
        skip(
            "project_archived",
            "The project is archived; whether GitLab accepts setting changes on archived "
            "projects is unverified, so it is left for a manual change.",
        )
        return
    evaluation = db.get_one(Evaluation, finding.latest_evaluation_id)
    observation = (
        db.get(Observation, evaluation.observation_id) if evaluation.observation_id else None
    )
    if observation is None or observation.collection_status != "ok":
        skip("evidence_missing", "The failure has no readable evidence.")
        return
    if not verify(observation):
        skip("evidence_digest_mismatch", "The evidence no longer matches its digest.")
        return
    if not _evidence_is_latest(db, observation):
        skip(
            "evidence_not_latest",
            "Newer evidence exists than the failure was judged on; plan after the next evaluation.",
        )
        return
    policy, rule = _rule(db, evaluation.policy_id, finding.rule_id)
    if rule is None:
        skip("rule_missing", "The rule is not in the policy version that was evaluated.")
        return
    if isinstance(rule, ProjectVisibilityRule):
        step, skipped = _visibility_step(project, rule, evaluation.observed)
    elif isinstance(rule, DefaultBranchProtectedRule):
        step, skipped = _protection_step(project, rule, evaluation.observed)
    else:  # pragma: no cover - every rule type is handled above
        step, skipped = None, ("no_action", f"No remediation action exists for {rule.type}.")
    if step is None:
        skip(*(skipped or ("no_action", f"No remediation action exists for {rule.type}.")))
        return
    step.update(
        {
            "step": len(built.steps) + 1,
            "target": {"project_gitlab_id": project.gitlab_id, "project_path": path},
            "finding_id": finding.id,
            "evaluation_id": evaluation.id,
            "observation_id": observation.id,
            "observation_source": observation.source,
            "observation_digest": observation.payload_digest,
            "observed_at": observation.collected_at.isoformat(),
            "policy": {"name": policy.name, "version": policy.version, "policy_id": policy.id},
            "rule": {"id": rule.id, "type": rule.type, "severity": rule.severity},
            "expected": evaluation.expected,
            "idempotency_key": _key(
                step["action"], instance_id, project.gitlab_id, step["request"]
            ),
        }
    )
    built.steps.append(step)


def build(db: Session, instance_id: int, scope: Scope) -> _Built:
    """Steps and skips for the findings in scope, from stored evidence only."""
    built = _Built()
    found = _findings(db, instance_id, scope)
    missing = sorted(set(scope.finding_ids) - {finding.id for finding in found})
    for finding_id in missing:
        built.skipped.append(
            {
                "finding_id": finding_id,
                "project_path": None,
                "policy_name": None,
                "rule_id": None,
                "reason_code": "not_found",
                "reason": "No such finding for this GitLab instance.",
            }
        )
    for finding in found:
        _plan_one(db, instance_id, finding, built)
    return built


def expire_due(db: Session) -> list[int]:
    """Mark plans past ``expires_at`` expired and audit it. Caller commits."""
    expired = list(
        db.scalars(
            update(RemediationPlan)
            .where(
                RemediationPlan.state.in_(("draft", "submitted", "approved")),
                RemediationPlan.expires_at <= func.now(),
            )
            .values(state="expired", ended_at=func.now())
            .returning(RemediationPlan.id)
        )
    )
    for plan_id in expired:
        audit.record(
            db,
            actor=SYSTEM_ACTOR,
            action="remediation_plan.expired",
            resource_type="remediation_plan",
            resource_id=str(plan_id),
        )
    return expired


def create(
    db: Session,
    instance: GitLabInstance,
    scope: Scope,
    ttl: timedelta,
    user_id: int,
    actor: str,
) -> RemediationPlan:
    """Build and store a plan. Raises NothingToPlan when no step results, ScopeTooLarge."""
    if not timedelta(0) < ttl <= MAX_TTL:
        raise ValueError(f"A plan lasts more than 0 and at most {MAX_PLAN_HOURS} hours")
    expire_due(db)
    built = build(db, instance.id, scope)
    if not built.steps:
        raise NothingToPlan(built.skipped)
    now: datetime = db.execute(select(func.now())).scalar_one()
    expires_at = now + ttl
    plan = {
        "format": FORMAT,
        "dry_run": True,
        "instance": instance.base_url,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "created_by": actor,
        "scope": scope.as_dict(),
        "steps": built.steps,
        "skipped": built.skipped,
        "excluded_actions": EXCLUDED_ACTIONS,
        "execution": "Not available: executing a plan needs a separate write credential and "
        "is BAL-032. Approving this plan changes nothing in GitLab.",
    }
    digest = plan_digest(plan)
    row = RemediationPlan(
        instance_id=instance.id,
        created_by_user_id=user_id,
        created_at=now,
        expires_at=expires_at,
        plan=plan,
        plan_digest=digest,
        steps=len(built.steps),
        skipped=len(built.skipped),
    )
    db.add(row)
    db.flush()
    db.refresh(row)
    audit.record(
        db,
        actor=actor,
        action="remediation_plan.created",
        resource_type="remediation_plan",
        resource_id=str(row.id),
        details={
            "plan_digest": digest,
            "steps": row.steps,
            "skipped": row.skipped,
            "finding_ids": [step["finding_id"] for step in built.steps],
            "expires_at": expires_at.isoformat(),
        },
    )
    return row


def stale_reasons(db: Session, row: RemediationPlan) -> list[str]:
    """Why the plan no longer matches its findings and evidence; empty when it still does."""
    reasons: list[str] = []
    if not digest_valid(row):
        reasons.append("The stored plan no longer matches its digest.")
    for step in row.plan.get("steps", []):
        number = step.get("step")
        finding = db.get(Finding, step.get("finding_id"))
        if finding is None or finding.state != "open" or finding.latest_outcome != "fail":
            reasons.append(f"Step {number}: the finding is no longer open and failing.")
            continue
        evaluation = db.get_one(Evaluation, finding.latest_evaluation_id)
        if evaluation.observation_id != step.get("observation_id"):
            reasons.append(f"Step {number}: the finding was judged again on newer evidence.")
            continue
        observation = db.get(Observation, step.get("observation_id"))
        if observation is None or not _evidence_is_latest(db, observation):
            reasons.append(f"Step {number}: newer evidence exists for the project.")
    return reasons


def _current(db: Session, row: RemediationPlan) -> str:
    expired = db.execute(select(func.now() >= row.expires_at)).scalar_one()
    if row.state in ("draft", "submitted", "approved") and expired:
        return "expired"
    return row.state


def _details(row: RemediationPlan, **extra: Any) -> dict[str, Any]:
    return {"plan_digest": row.plan_digest, "steps": row.steps, **extra}


def submit(db: Session, row: RemediationPlan, user_id: int, actor: str) -> RemediationPlan:
    """Send a draft for approval. Only its creator may."""
    state = _current(db, row)
    if state != "draft":
        raise PlanConflict(f"Only a draft plan can be submitted; this one is {state}")
    if user_id != row.created_by_user_id:
        raise PermissionError("Only the plan's creator can submit it")
    now = db.execute(select(func.now())).scalar_one()
    row.state = "submitted"
    row.submitted_at = now
    db.flush()
    audit.record(
        db,
        actor=actor,
        action="remediation_plan.submitted",
        resource_type="remediation_plan",
        resource_id=str(row.id),
        details=_details(row),
    )
    return row


def decide(
    db: Session,
    row: RemediationPlan,
    decision: Literal["approved", "rejected"],
    user_id: int,
    actor: str,
    note: str | None,
    reviewed_digest: str | None = None,
    *,
    projects_freshness: str | None = None,
) -> RemediationPlan:
    """Approve or reject a submitted plan. The decider must not be its creator.

    Approval needs the digest the approver reviewed, equal to the plan's, a current projects
    collection, and a plan that is not stale. Raises PlanStale with the reasons.
    """
    state = _current(db, row)
    if state != "submitted":
        raise PlanConflict(f"Only a submitted plan can be decided; this one is {state}")
    if user_id == row.created_by_user_id:
        raise PermissionError(
            "A plan must be approved or rejected by someone other than its creator"
        )
    if decision == "approved":
        if reviewed_digest != row.plan_digest:
            raise PlanStale(["The reviewed digest does not match the plan's digest."])
        reasons = stale_reasons(db, row)
        if projects_freshness not in CURRENT:
            reasons.insert(
                0,
                f"The projects collection is {projects_freshness or 'unknown'}; plans are "
                "approved only on current evidence.",
            )
        if reasons:
            raise PlanStale(reasons)
    now = db.execute(select(func.now())).scalar_one()
    row.state = decision
    row.decided_by_user_id = user_id
    row.decided_at = now
    row.decision_note = note
    row.approved_digest = row.plan_digest if decision == "approved" else None
    db.flush()
    audit.record(
        db,
        actor=actor,
        action=f"remediation_plan.{decision}",
        resource_type="remediation_plan",
        resource_id=str(row.id),
        details=_details(row, created_by_user_id=row.created_by_user_id),
    )
    return row


def locked(db: Session, plan_id: int) -> RemediationPlan | None:
    return db.get(RemediationPlan, plan_id, with_for_update=True, populate_existing=True)


def summary(plan: dict[str, Any]) -> list[str]:
    """A diff-style line per step and skip, for people reviewing the plan."""
    lines = []
    for step in plan.get("steps", []):
        target = step["target"]["project_path"]
        request = step["request"]
        if step["action"] == "restrict_project_visibility":
            change = (
                f"visibility {step['current']['visibility']} -> {step['intended']['visibility']}"
            )
        else:
            change = f"default branch {step['current']['branch']}: unprotected -> protected"
        lines.append(
            f"~ {target}: {change} ({request['method']} {request['path']}; "
            f"finding {step['finding_id']})"
        )
    for skipped in plan.get("skipped", []):
        lines.append(
            f"= {skipped.get('project_path') or 'finding ' + str(skipped['finding_id'])}: "
            f"skipped ({skipped['reason_code']}): {skipped['reason']}"
        )
    return lines
