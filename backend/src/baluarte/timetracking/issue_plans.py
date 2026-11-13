"""Issue plans: the time rules on closed issues, reviewed, then applied (ADR-0011).

The rules, for each closed issue of a covered type (``rule_issue_types``) in an active,
unarchived project in monitoring scope, as last collected (``closed_issue``):

- ``add_label``: no time spent, commits linked (a merge request, or a commit or merge request
  mentioning it) and without the ``missing_time_label``: add it,
  ``PUT /projects/:id/issues/:iid`` with ``add_labels``;
- ``remove_label``: time spent and the label: remove it, with ``remove_labels``;
- ``set_estimate``: time spent and no estimate: the time spent rounded up to
  ``estimate_round_minutes`` (10 minutes spent becomes 30 with the default),
  ``POST /projects/:id/issues/:iid/time_estimate``. An estimate is never changed or removed.

A label is added only in a project where it is already defined (on the project or a group
above it), read fresh when the plan is built, so GitLab never creates it. Issues whose
project lacks the label, or whose commits could not be read, are listed as manual items.

Lifecycle, approval and expiry are those of label plans (``baluarte.labels.plans``), enforced
again by a trigger. Applying needs ``BALUARTE_ISSUE_WRITES=true`` and the stored write token.
The ``gitlab.apply_issue_plan`` job applies the steps in order. For each it reads the issue with
the read token: a step already in place is ``skipped``; an issue that no longer matches the plan
(reopened, time logged or removed, an estimate set meanwhile, the label no longer defined) is
``stale`` and left alone. Otherwise the write is sent once, never retried, and read back. A write
GitLab refuses, a read that fails or a read-back that does not match stops the plan. Every write
is audited as ``issues.write``; afterwards a time collection is queued.
"""

import logging
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal

import httpx
from sqlalchemy import Engine, and_, case, func, select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.audit import details_digest
from baluarte.configuration.crypto import load_secret_box
from baluarte.db.models import (
    ISSUE_PLAN_HOURS,
    MAX_ISSUE_PLAN_STEPS,
    ClosedIssue,
    GitLabInstance,
    GitLabProject,
    IssuePlan,
)
from baluarte.integrations.gitlab.client import GitLabClient, GitLabError, NotFound
from baluarte.integrations.gitlab.label_writer import LabelWriter, WriteResult
from baluarte.integrations.gitlab.settings import load_gitlab_settings
from baluarte.jobs import queue
from baluarte.jobs.queue import ClaimedJob
from baluarte.labels import write_token
from baluarte.policy.changes import Actor
from baluarte.scope.exclusions import coverage
from baluarte.settings import ConfigurationError, env
from baluarte.timetracking.settings import TimeTrackingSettings

logger = logging.getLogger(__name__)

FORMAT = "baluarte.issue-plan/v1"
APPLY_ISSUE_PLAN = "gitlab.apply_issue_plan"
ISSUE_WRITES = "BALUARTE_ISSUE_WRITES"
SYSTEM_ACTOR = "system:issue-plans"
TTL = timedelta(hours=ISSUE_PLAN_HOURS)
MAX_NOTE = 2000
OPEN_STATES = ("draft", "submitted", "approved")
Action = Literal["add_label", "remove_label", "set_estimate"]
ACTIONS: tuple[Action, ...] = ("remove_label", "add_label", "set_estimate")
LINKED = ("merge_request", "mention")

MANUAL_REASONS = {
    "label_undefined": "The label is not defined for this project or a group above it. Define "
    "it there, then build a new plan; Baluarte does not create labels on issues.",
    "label_unreadable": "The project's labels could not be read, so no label is planned here.",
    "commits_unknown": "Whether commits are linked to these issues could not be read yet. The "
    "next time collection checks again.",
}


def writes_enabled() -> bool:
    """Whether the deployment allows issue plans to be applied (``BALUARTE_ISSUE_WRITES``)."""
    raw = env(ISSUE_WRITES).lower()
    if raw in ("", "false"):
        return False
    if raw == "true":
        return True
    raise ConfigurationError(f"{ISSUE_WRITES} must be true or false")


class PlanConflict(Exception):
    """The plan cannot make this transition; ``code`` names why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class NothingToPlan(Exception):
    pass


class ScopeTooLarge(Exception):
    pass


def rounded_estimate(spent_seconds: int, round_minutes: int) -> int:
    """Time spent rounded up to the interval, in seconds: 10 minutes -> 30 minutes."""
    interval = round_minutes * 60
    return max(1, math.ceil(spent_seconds / interval)) * interval


def in_plan_scope(groups: Sequence[str], path: str) -> bool:
    return not groups or any(path == g or path.startswith(g + "/") for g in groups)


# Building.


@dataclass(frozen=True)
class Candidate:
    """A stored closed issue with its project."""

    project_gitlab_id: int
    project_path: str
    iid: int
    issue_type: str | None
    title: str | None
    web_url: str | None
    labels: tuple[str, ...]
    estimate_seconds: int
    spent_seconds: int
    commits: str


def candidates(db: Session, instance_id: int, groups: Sequence[str]) -> list[Candidate]:
    """Closed issues of active, unarchived projects in monitoring scope and in ``groups``."""
    scope = coverage(db, instance_id)
    rows = db.execute(
        select(
            GitLabProject.gitlab_id,
            GitLabProject.path_with_namespace,
            ClosedIssue.issue_iid,
            ClosedIssue.issue_type,
            ClosedIssue.title,
            ClosedIssue.web_url,
            ClosedIssue.labels,
            ClosedIssue.estimate_seconds,
            ClosedIssue.spent_seconds,
            ClosedIssue.commits,
        )
        .join(GitLabProject, GitLabProject.id == ClosedIssue.project_id)
        .where(
            ClosedIssue.instance_id == instance_id,
            GitLabProject.state == "active",
            GitLabProject.archived.is_(False),
            scope.project_included(),
        )
        .order_by(GitLabProject.path_with_namespace, ClosedIssue.issue_iid)
    ).all()
    return [
        Candidate(
            project_gitlab_id=row[0],
            project_path=row[1],
            iid=row[2],
            issue_type=row[3],
            title=row[4],
            web_url=row[5],
            labels=tuple(row[6] or ()),
            estimate_seconds=row[7],
            spent_seconds=row[8],
            commits=row[9],
        )
        for row in rows
        if in_plan_scope(groups, row[1])
    ]


def wanted(candidate: Candidate, rules: TimeTrackingSettings) -> list[Action]:
    """The rule actions an issue needs, ignoring whether its project defines the label."""
    if candidate.issue_type not in rules.rule_issue_types:
        return []
    found: list[Action] = []
    has_label = rules.missing_time_label in candidate.labels
    if candidate.spent_seconds > 0:
        if has_label:
            found.append("remove_label")
        if candidate.estimate_seconds == 0:
            found.append("set_estimate")
    elif not has_label and candidate.commits in LINKED:
        found.append("add_label")
    return found


def label_projects(items: Sequence[Candidate], rules: TimeTrackingSettings) -> set[int]:
    """The projects whose label availability a plan needs: those with a label to add."""
    return {c.project_gitlab_id for c in items if "add_label" in wanted(c, rules)}


def label_defined(client: GitLabClient, project_gitlab_id: int, name: str) -> bool:
    """Whether the project or a group above it defines ``name`` (GET only)."""
    params = {"include_ancestor_groups": "true", "search": name, "per_page": 100}
    for page in client.paginate(f"/projects/{project_gitlab_id}/labels", params):
        if any(label.get("name") == name for label in page):
            return True
    return False


def read_label_availability(
    client: GitLabClient, projects: set[int], name: str
) -> dict[int, bool | None]:
    """For each project: defined, not defined, or None when its labels could not be read."""
    found: dict[int, bool | None] = {}
    for project in sorted(projects):
        try:
            found[project] = label_defined(client, project, name)
        except GitLabError:
            found[project] = None
    return found


@dataclass
class Built:
    steps: list[dict[str, Any]] = field(default_factory=list)
    manual: list[dict[str, Any]] = field(default_factory=list)
    totals: Counter[str] = field(default_factory=Counter)


def _issue(c: Candidate) -> dict[str, Any]:
    return {"iid": c.iid, "type": c.issue_type, "title": c.title, "web_url": c.web_url}


def build(
    items: Sequence[Candidate],
    rules: TimeTrackingSettings,
    available: Mapping[int, bool | None],
) -> Built:
    """The steps and manual items for ``items``; ``available`` from ``read_label_availability``."""
    built = Built()
    label = rules.missing_time_label
    manual: dict[tuple[str, int], dict[str, Any]] = {}

    def note(reason: str, c: Candidate) -> None:
        entry = manual.setdefault(
            (reason, c.project_gitlab_id),
            {
                "reason": reason,
                "message": MANUAL_REASONS[reason],
                "project": {"gitlab_id": c.project_gitlab_id, "path": c.project_path},
                "issues": [],
            },
        )
        entry["issues"].append(c.iid)

    for c in items:
        actions = wanted(c, rules)
        if (
            not actions
            and c.issue_type in rules.rule_issue_types
            and c.spent_seconds == 0
            and label not in c.labels
            and c.commits == "unknown"
        ):
            note("commits_unknown", c)
        for action in actions:
            if action == "add_label":
                state = available.get(c.project_gitlab_id)
                if state is not True:
                    note("label_undefined" if state is False else "label_unreadable", c)
                    continue
            prior = {
                "has_label": label in c.labels,
                "estimate_seconds": c.estimate_seconds,
                "spent_seconds": c.spent_seconds,
            }
            if action == "set_estimate":
                intended: dict[str, Any] = {
                    "estimate_seconds": rounded_estimate(
                        c.spent_seconds, rules.estimate_round_minutes
                    )
                }
            else:
                intended = {"has_label": action == "add_label"}
            built.steps.append(
                {
                    "step": len(built.steps) + 1,
                    "action": action,
                    "project": {"gitlab_id": c.project_gitlab_id, "path": c.project_path},
                    "issue": _issue(c),
                    "label": label if action != "set_estimate" else None,
                    "prior": prior,
                    "intended": intended,
                    "idempotency_key": f"{c.project_gitlab_id}:{c.iid}:{action}",
                }
            )
            built.totals[action] += 1
    built.manual = sorted(manual.values(), key=lambda m: (m["reason"], m["project"]["path"]))
    for entry in built.manual:
        built.totals[f"manual_{entry['reason']}"] += len(entry["issues"])
    return built


# Lifecycle.


def plan_digest(plan: dict[str, Any]) -> str:
    return details_digest(plan)


def digest_valid(row: IssuePlan) -> bool:
    return row.plan_digest == plan_digest(row.plan)


def effective_state() -> Any:
    """SQL: the state, counting an open plan past its expiry as expired."""
    return case(
        (
            and_(IssuePlan.state.in_(OPEN_STATES), IssuePlan.expires_at <= func.now()),
            "expired",
        ),
        else_=IssuePlan.state,
    )


def current_state(db: Session, row: IssuePlan) -> str:
    if row.state in OPEN_STATES and db.execute(select(func.now() >= row.expires_at)).scalar_one():
        return "expired"
    return row.state


def _details(row: IssuePlan, **extra: Any) -> dict[str, Any]:
    return {
        "plan_digest": row.plan_digest,
        "steps": row.steps,
        "manual": row.manual,
        "totals": row.plan.get("totals", {}),
        **extra,
    }


def create(
    db: Session,
    instance: GitLabInstance,
    built: Built,
    rules: TimeTrackingSettings,
    groups: Sequence[str],
    by: Actor,
    review_required: bool,
) -> IssuePlan:
    """Store a draft plan. The caller commits."""
    if not built.steps and not built.manual:
        raise NothingToPlan("Every closed issue in scope already follows the rules")
    if len(built.steps) > MAX_ISSUE_PLAN_STEPS:
        raise ScopeTooLarge(
            f"The plan would hold {len(built.steps)} steps; at most {MAX_ISSUE_PLAN_STEPS}. "
            "Name fewer groups."
        )
    now = db.execute(select(func.now())).scalar_one()
    expires_at = now + TTL
    plan = {
        "format": FORMAT,
        "dry_run": True,
        "instance": instance.base_url,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "created_by": by.actor,
        "rules": {
            "missing_time_label": rules.missing_time_label,
            "estimate_round_minutes": rules.estimate_round_minutes,
            "issue_types": list(rules.rule_issue_types),
        },
        "scope": {"groups": list(groups)},
        "totals": dict(sorted(built.totals.items())),
        "steps": built.steps,
        "manual": built.manual,
    }
    row = IssuePlan(
        instance_id=instance.id,
        created_by=by.actor,
        created_by_user_id=by.user_id,
        created_at=now,
        expires_at=expires_at,
        plan=plan,
        plan_digest=plan_digest(plan),
        steps=len(built.steps),
        manual=len(built.manual),
        review_required=review_required,
    )
    db.add(row)
    db.flush()
    db.refresh(row)
    audit.record(
        db,
        actor=by.actor,
        action="issues.plan_created",
        resource_type="issue_plan",
        resource_id=str(row.id),
        details=_details(row, scope=list(groups), expires_at=expires_at.isoformat()),
    )
    return row


def locked(db: Session, plan_id: int) -> IssuePlan | None:
    return db.get(IssuePlan, plan_id, with_for_update=True, populate_existing=True)


def submit(db: Session, row: IssuePlan, by: Actor) -> IssuePlan:
    """Send a draft for approval; without review, approve it at once. Only its creator may."""
    state = current_state(db, row)
    if state != "draft":
        raise PlanConflict(
            "plan_expired" if state == "expired" else "not_draft",
            f"Only a draft plan can be submitted; this one is {state}",
        )
    if by.user_id != row.created_by_user_id:
        raise PermissionError("Only the plan's creator can submit it")
    now = db.execute(select(func.now())).scalar_one()
    row.submitted_at = now
    if row.review_required:
        row.state = "submitted"
        action = "issues.plan_submitted"
    else:
        row.state = "approved"
        row.self_approved = True
        row.decided_by = by.actor
        row.decided_by_user_id = by.user_id
        row.decided_at = now
        row.approved_digest = row.plan_digest
        action = "issues.plan_approved"
    db.flush()
    audit.record(
        db,
        actor=by.actor,
        action=action,
        resource_type="issue_plan",
        resource_id=str(row.id),
        details=_details(row, self_approved=row.self_approved),
    )
    return row


def decide(
    db: Session,
    row: IssuePlan,
    decision: Literal["approved", "rejected"],
    by: Actor,
    note: str | None,
    reviewed_digest: str | None = None,
) -> IssuePlan:
    """Approve or reject a submitted plan; never one's own."""
    state = current_state(db, row)
    if state != "submitted":
        raise PlanConflict(
            "plan_expired" if state == "expired" else "not_submitted",
            f"Only a submitted plan can be decided; this one is {state}",
        )
    if by.user_id == row.created_by_user_id:
        raise PermissionError(
            "A plan must be approved or rejected by someone other than its creator"
        )
    if decision == "approved":
        if reviewed_digest != row.plan_digest or not digest_valid(row):
            raise PlanConflict("digest_mismatch", "The reviewed digest does not match the plan's")
    elif not (note or "").strip():
        raise ValueError("A rejection needs a note")
    now = db.execute(select(func.now())).scalar_one()
    row.state = decision
    row.decided_by = by.actor
    row.decided_by_user_id = by.user_id
    row.decided_at = now
    row.decision_note = (note or "").strip() or None
    row.approved_digest = row.plan_digest if decision == "approved" else None
    db.flush()
    audit.record(
        db,
        actor=by.actor,
        action=f"issues.plan_{decision}",
        resource_type="issue_plan",
        resource_id=str(row.id),
        details=_details(row, created_by=row.created_by, note=row.decision_note),
    )
    return row


def apply_refusals(db: Session, row: IssuePlan) -> list[str]:
    """Why the plan cannot be applied now (codes); empty when it can."""
    reasons: list[str] = []
    try:
        enabled = writes_enabled()
    except ConfigurationError:
        enabled = False
    if not enabled:
        reasons.append("issue_writes_disabled")
    if not write_token.status(db).set:
        reasons.append("write_token_missing")
    state = current_state(db, row)
    if state == "expired":
        reasons.append("expired")
    elif state != "approved":
        reasons.append("not_approved")
    if row.steps == 0 and "not_approved" not in reasons:
        reasons.append("nothing_to_apply")
    return reasons


REFUSAL_MESSAGES = {
    "issue_writes_disabled": "Issue writes are off for this deployment (BALUARTE_ISSUE_WRITES).",
    "write_token_missing": "No write token is stored.",
    "expired": "The plan has expired.",
    "not_approved": "Only an approved plan can be applied.",
    "nothing_to_apply": "The plan has no steps, only manual items.",
}


def start_apply(db: Session, row: IssuePlan, by: Actor) -> IssuePlan:
    """Mark an approved plan ``applying`` and queue the applier. The caller commits."""
    reasons = apply_refusals(db, row)
    if reasons:
        code = "plan_expired" if reasons[0] == "expired" else reasons[0]
        raise PlanConflict(code, " ".join(REFUSAL_MESSAGES[r] for r in reasons))
    now = db.execute(select(func.now())).scalar_one()
    row.state = "applying"
    row.applied_by = by.actor
    row.apply_started_at = now
    row.progress = [
        {"step": step["step"], "status": "pending"} for step in row.plan.get("steps", [])
    ]
    db.flush()
    # Applied at most once: a failed attempt is recorded on the plan, never retried.
    queue.enqueue(
        db, APPLY_ISSUE_PLAN, f"{APPLY_ISSUE_PLAN}:{row.id}", {"plan_id": row.id}, max_attempts=1
    )
    audit.record(
        db,
        actor=by.actor,
        action="issues.plan_applying",
        resource_type="issue_plan",
        resource_id=str(row.id),
        details=_details(row),
    )
    return row


# Applying.


@dataclass(frozen=True)
class IssueNow:
    state: str | None
    labels: tuple[str, ...]
    estimate_seconds: int
    spent_seconds: int


def read_issue(client: GitLabClient, project_gitlab_id: int, iid: int) -> IssueNow:
    """The issue as GitLab has it now, with the read token. Raises GitLabError."""
    body = client.get_json(f"/projects/{project_gitlab_id}/issues/{iid}")
    if not isinstance(body, dict):
        raise GitLabError("unexpected issue response")
    stats = body.get("time_stats") if isinstance(body.get("time_stats"), dict) else None
    labels = body.get("labels") if isinstance(body.get("labels"), list) else []

    def seconds(key: str) -> int:
        value = (stats or {}).get(key)
        return value if type(value) is int else 0

    return IssueNow(
        state=body.get("state") if isinstance(body.get("state"), str) else None,
        labels=tuple(label for label in labels or [] if isinstance(label, str)),
        estimate_seconds=seconds("time_estimate"),
        spent_seconds=seconds("total_time_spent"),
    )


StepStatus = Literal["skipped", "stale", "applied", "failed"]


@dataclass(frozen=True)
class StepOutcome:
    status: StepStatus
    message: str
    request_status: int | None = None
    verified: bool | None = None


def _in_place(step: dict[str, Any], now: IssueNow) -> bool:
    if step["action"] == "set_estimate":
        return bool(now.estimate_seconds == step["intended"]["estimate_seconds"])
    return (step["label"] in now.labels) == bool(step["intended"]["has_label"])


def _stale(step: dict[str, Any], now: IssueNow) -> str | None:
    """Why the issue no longer matches the plan, or None."""
    if now.state != "closed":
        return "The issue is no longer closed."
    prior = step["prior"]
    if step["action"] == "add_label" and now.spent_seconds > 0:
        return "Time was logged on the issue since the plan was made."
    if step["action"] == "remove_label" and now.spent_seconds <= 0:
        return "The issue's time was removed since the plan was made."
    if step["action"] == "set_estimate":
        if now.estimate_seconds > 0:
            return "The issue has an estimate now; Baluarte never changes one."
        if now.spent_seconds != prior["spent_seconds"]:
            return "The time spent changed since the plan was made."
    return None


def apply_step(
    client: GitLabClient,
    writer: LabelWriter,
    step: dict[str, Any],
    audit_write: Callable[[WriteResult], None],
    label_defined_in: Callable[[int], bool],
) -> StepOutcome:
    """Apply one step: read, skip or leave a stale issue, or write once and read back."""
    project = int(step["project"]["gitlab_id"])
    iid = int(step["issue"]["iid"])
    reference = f"{step['project']['path']}#{iid}"
    try:
        now = read_issue(client, project, iid)
    except NotFound:
        return StepOutcome("stale", f"{reference} no longer exists or cannot be read.")
    except GitLabError as error:
        return StepOutcome("failed", f"{reference} could not be read: {error}")
    if _in_place(step, now):
        return StepOutcome("skipped", "Already in place.", verified=True)
    reason = _stale(step, now)
    if reason is not None:
        return StepOutcome("stale", reason)
    if step["action"] == "add_label":
        try:
            defined = label_defined_in(project)
        except GitLabError as error:
            return StepOutcome(
                "failed", f"The labels of {step['project']['path']} could not be read: {error}"
            )
        if not defined:
            return StepOutcome("stale", "The label is no longer defined for this project.")
    if step["action"] == "set_estimate":
        result = writer.issue_estimate(project, iid, int(step["intended"]["estimate_seconds"]))
    else:
        result = writer.issue_label(project, iid, step["label"], add=step["action"] == "add_label")
    audit_write(result)
    if not result.ok:
        problem = result.error or f"GitLab answered {result.status}"
        return StepOutcome(
            "failed", f"{result.method} {result.path} failed: {problem}.", result.status
        )
    try:
        after = read_issue(client, project, iid)
    except GitLabError as error:
        return StepOutcome(
            "failed",
            f"The write was sent but could not be read back: {error}",
            result.status,
            False,
        )
    if not _in_place(step, after):
        return StepOutcome(
            "failed", "The issue does not read back as intended.", result.status, False
        )
    return StepOutcome("applied", "Written and read back as intended.", result.status, True)


def _record(db: Session, row: IssuePlan, number: int, outcome: StepOutcome) -> None:
    now = db.execute(select(func.now())).scalar_one()
    progress = [dict(entry) for entry in row.progress]
    for entry in progress:
        if entry["step"] == number:
            entry.update(
                status=outcome.status,
                request_status=outcome.request_status,
                verified=outcome.verified,
                message=outcome.message,
                finished_at=now.isoformat().replace("+00:00", "Z"),
            )
    row.progress = progress


def _finish(db: Session, row: IssuePlan, failure: tuple[int, str] | None) -> None:
    now = db.execute(select(func.now())).scalar_one()
    if failure is None:
        row.state = "applied"
    else:
        row.state = "failed"
        row.failed_step, row.failure = failure
        row.progress = [
            {**entry, "status": "not_run"} if entry["status"] == "pending" else entry
            for entry in row.progress
        ]
    row.finished_at = now
    counts: Counter[str] = Counter(entry["status"] for entry in row.progress)
    audit.record(
        db,
        actor=SYSTEM_ACTOR,
        action=f"issues.plan_{row.state}",
        resource_type="issue_plan",
        resource_id=str(row.id),
        details=_details(
            row,
            steps_by_status=dict(sorted(counts.items())),
            failed_step=row.failed_step,
            failure=row.failure,
        ),
    )
    # Read the issues again so the closed-issue view shows what changed.
    from baluarte.integrations.gitlab.jobs import COLLECT_TIME

    queue.enqueue(db, COLLECT_TIME, f"{COLLECT_TIME}:after-issue-plan:{row.id}", max_attempts=3)


def make_apply_issue_plan(
    transport: httpx.BaseTransport | None = None,
) -> Callable[[ClaimedJob, Engine], None]:
    def apply(job: ClaimedJob, engine: Engine) -> None:
        plan_id = int(job.payload.get("plan_id", 0))
        with Session(engine) as db:
            row = locked(db, plan_id)
            if row is None or row.state != "applying":
                return
            steps = list(row.plan.get("steps", []))
            label = str(row.plan.get("rules", {}).get("missing_time_label", ""))
            done = {
                e["step"]
                for e in row.progress
                if e.get("status") in ("applied", "skipped", "stale")
            }
            problem: str | None = None
            token: str | None = None
            settings = None
            try:
                settings = load_gitlab_settings()
                if not writes_enabled():
                    problem = REFUSAL_MESSAGES["issue_writes_disabled"]
                token = write_token.load(db, load_secret_box())
            except ConfigurationError as exc:
                problem = str(exc)
            if problem is None and (settings is None or token is None):
                problem = "GitLab or the write token is no longer configured."
            if not digest_valid(row):
                problem = "The stored plan no longer matches its digest."
            if problem is not None or settings is None or token is None:
                _finish(db, row, (steps[0]["step"] if steps else 0, problem or "not configured"))
                db.commit()
                return
            db.commit()

        def audit_write(result: WriteResult) -> None:
            with Session(engine) as db:
                audit.record(
                    db,
                    actor=f"issue-plan:{plan_id}",
                    action="issues.write",
                    resource_type="issue_plan",
                    resource_id=str(plan_id),
                    details={"method": result.method, "path": result.path, "status": result.status},
                )
                db.commit()

        failure: tuple[int, str] | None = None
        with (
            GitLabClient(settings, transport=transport) as client,
            LabelWriter(settings, token, transport=transport) as writer,
        ):
            defined: dict[int, bool] = {}

            def label_defined_in(project: int) -> bool:
                if project not in defined:  # read once per project and plan
                    defined[project] = label_defined(client, project, label)
                return defined[project]

            for step in steps:
                if step["step"] in done:
                    continue
                outcome = apply_step(client, writer, step, audit_write, label_defined_in)
                with Session(engine) as db:
                    row = locked(db, plan_id)
                    if row is None:
                        return
                    _record(db, row, step["step"], outcome)
                    db.commit()
                if outcome.status == "failed":
                    failure = (step["step"], outcome.message)
                    break
        with Session(engine) as db:
            row = locked(db, plan_id)
            if row is not None and row.state == "applying":
                _finish(db, row, failure)
                db.commit()
        logger.info(
            "issue plan %s: %s",
            plan_id,
            "applied" if failure is None else f"failed at step {failure[0]}",
            extra={"job_id": job.id},
        )

    return apply
