"""Applying an approved remediation plan (BAL-032, ADR-0012).

The ``gitlab.apply_remediation_plan`` job applies a plan's steps in order. For each step:

1. **Read** the project with the read-only token (``GET /projects/:id``, and for a protection
   ``GET /projects/:id/protected_branches``, wildcards included).
2. **Already as intended:** the step is ``skipped`` and nothing is written. This is what makes a
   step idempotent: applying the same change twice writes it once.
3. **Out of date:** a precondition no longer holds (the visibility is no longer the planned
   ``current`` value, the default branch changed, some rule now covers the branch, or the
   project is archived). The step is ``out_of_date``, nothing is written, and the next step
   runs: someone changed the project since the plan was built, and a new plan from fresh
   evidence decides what, if anything, is still needed.
4. **Write once** with the remediation write token, never retried, whatever the answer.
5. **Verify** by reading again with the read-only token. The step is ``applied`` only when
   that read shows the intended value. A refused write (a 409, say) is resolved by this read:
   if it shows the intended value, the step is applied; otherwise it failed.

The plan stops at the first ``failed`` step (an unreadable project, a write that GitLab refused
and that did not take effect, or a read-back that does not match); later steps are
``not_run``. Every write is audited as ``remediation_plan.write`` and every step's outcome as
``remediation_plan.step``. Afterwards project discovery is queued, so the findings resolve
through the normal read path.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.configuration.crypto import load_secret_box
from baluarte.db.models import RemediationPlan
from baluarte.integrations.gitlab.branches import matches
from baluarte.integrations.gitlab.client import GitLabClient, GitLabError
from baluarte.integrations.gitlab.remediation_writer import (
    MAINTAINER,
    RemediationWriter,
    WriteResult,
)
from baluarte.integrations.gitlab.settings import GitLabSettings, load_gitlab_settings
from baluarte.jobs.queue import ClaimedJob
from baluarte.remediation.plans import APPLY_REFUSALS, digest_valid, locked
from baluarte.remediation.write_token import TOKEN as WRITE_TOKEN
from baluarte.settings import ConfigurationError

logger = logging.getLogger(__name__)

SYSTEM_ACTOR = "system:remediation-plans"
StepStatus = Literal["skipped", "out_of_date", "applied", "failed"]
_LEVELS = ("push_access_levels", "merge_access_levels", "unprotect_access_levels")


@dataclass(frozen=True)
class StepOutcome:
    status: StepStatus
    message: str
    # The write's HTTP status; None when nothing was written or GitLab could not be reached.
    request_status: int | None = None
    # Whether a read with the read token showed the intended value; None when not read back.
    verified: bool | None = None
    # What that read showed, in the step's terms.
    observed: dict[str, Any] = field(default_factory=dict)


Audit = Callable[[WriteResult], None]


def _maintainers_only(entries: Any) -> bool:
    return (
        isinstance(entries, list)
        and bool(entries)
        and all(
            isinstance(entry, dict)
            and entry.get("user_id") is None
            and entry.get("group_id") is None
            and entry.get("deploy_key_id") is None
            and entry.get("access_level") == MAINTAINER
            for entry in entries
        )
    )


def protected_as_intended(rules: list[dict[str, Any]], branch: str) -> bool:
    """Whether a rule named exactly ``branch`` limits push and merge to Maintainers.

    Unprotect access is checked too when GitLab reports it; some versions and tiers do not.
    """
    for rule in rules:
        if rule.get("name") != branch:
            continue
        unprotect = rule.get(_LEVELS[2])
        if (
            _maintainers_only(rule.get(_LEVELS[0]))
            and _maintainers_only(rule.get(_LEVELS[1]))
            and (unprotect is None or _maintainers_only(unprotect))
        ):
            return True
    return False


def _covering(rules: list[dict[str, Any]], branch: str) -> list[str]:
    """The names of the rules that cover ``branch``, wildcards included."""
    return sorted(
        rule["name"]
        for rule in rules
        if isinstance(rule.get("name"), str) and matches(rule["name"], branch)
    )


def _rules(client: GitLabClient, project_id: int) -> list[dict[str, Any]]:
    path = f"/projects/{project_id}/protected_branches"
    return [rule for page in client.paginate(path) for rule in page]


def _project(client: GitLabClient, project_id: int) -> dict[str, Any]:
    project = client.get_json(f"/projects/{project_id}")
    if not isinstance(project, dict):
        raise GitLabError(f"GET /projects/{project_id} did not return an object")
    return project


def _write_failed(result: WriteResult) -> str:
    reason = result.error or f"GitLab answered {result.status}"
    return f"{result.method} {result.path} failed: {reason}"


def _verdict(result: WriteResult, verified: bool, observed: dict[str, Any]) -> StepOutcome:
    if verified:
        message = (
            "Written and read back as intended."
            if result.ok
            else f"{_write_failed(result)}, but a read shows the intended value."
        )
        return StepOutcome("applied", message, result.status, True, observed)
    message = (
        f"{_write_failed(result)}, and a read does not show the intended value."
        if not result.ok
        else "The write was accepted but does not read back as intended."
    )
    return StepOutcome("failed", message, result.status, False, observed)


def _visibility(
    client: GitLabClient, writer: RemediationWriter, step: dict[str, Any], audit_write: Audit
) -> StepOutcome:
    project_id = int(step["target"]["project_gitlab_id"])
    intended = step["intended"]["visibility"]
    planned = step["current"]["visibility"]
    project = _project(client, project_id)
    found = project.get("visibility")
    observed = {"visibility": found}
    if found == intended:
        return StepOutcome("skipped", "Already as intended.", verified=True, observed=observed)
    if project.get("archived") is True:
        return StepOutcome("out_of_date", "The project is now archived.", observed=observed)
    if found != planned:
        return StepOutcome(
            "out_of_date",
            f"The visibility is now {found}, not {planned} as planned.",
            observed=observed,
        )
    result = writer.restrict_visibility(project_id, intended)
    audit_write(result)
    try:
        after = _project(client, project_id).get("visibility")
    except GitLabError as error:
        return StepOutcome(
            "failed", f"The write was sent but could not be read back: {error}", result.status
        )
    return _verdict(result, after == intended, {"visibility": after})


def _protection(
    client: GitLabClient, writer: RemediationWriter, step: dict[str, Any], audit_write: Audit
) -> StepOutcome:
    project_id = int(step["target"]["project_gitlab_id"])
    branch = step["intended"]["branch"]
    project = _project(client, project_id)
    rules = _rules(client, project_id)
    covering = sorted(
        str(r["name"])
        for r in rules
        if isinstance(r.get("name"), str) and matches(r["name"], branch)
    )
    observed = {"default_branch": project.get("default_branch"), "rules": covering}
    if project.get("default_branch") == branch and protected_as_intended(rules, branch):
        return StepOutcome("skipped", "Already as intended.", verified=True, observed=observed)
    if project.get("archived") is True:
        return StepOutcome("out_of_date", "The project is now archived.", observed=observed)
    if project.get("default_branch") != branch:
        return StepOutcome(
            "out_of_date",
            f"The default branch is now {project.get('default_branch')}, not {branch}.",
            observed=observed,
        )
    if covering:
        return StepOutcome(
            "out_of_date",
            f"{branch} is now covered by {', '.join(covering)}; protections are never replaced.",
            observed=observed,
        )
    result = writer.protect_branch(project_id, branch)
    audit_write(result)
    try:
        after = _rules(client, project_id)
    except GitLabError as error:
        return StepOutcome(
            "failed", f"The write was sent but could not be read back: {error}", result.status
        )
    observed = {"default_branch": branch, "rules": _covering(after, branch)}
    return _verdict(result, protected_as_intended(after, branch), observed)


ACTIONS = {
    "restrict_project_visibility": _visibility,
    "protect_default_branch": _protection,
}


def apply_step(
    client: GitLabClient, writer: RemediationWriter, step: dict[str, Any], audit_write: Audit
) -> StepOutcome:
    """Apply one step: read, skip or write once, then read back with the read token."""
    action = ACTIONS.get(step.get("action", ""))
    if action is None:
        return StepOutcome("failed", f"{step.get('action')!r} is not an allowlisted action.")
    try:
        return action(client, writer, step, audit_write)
    except GitLabError as error:
        path = step["target"]["project_path"]
        return StepOutcome("failed", f"{path} could not be read: {error}")


def _now(db: Session) -> str:
    now = db.execute(select(func.now())).scalar_one()
    return str(now.isoformat()).replace("+00:00", "Z")


def _record(db: Session, row: RemediationPlan, step: dict[str, Any], outcome: StepOutcome) -> None:
    finished_at = _now(db)
    row.progress = [
        {
            **entry,
            "status": outcome.status,
            "request_status": outcome.request_status,
            "verified": outcome.verified,
            "observed": outcome.observed,
            "message": outcome.message,
            "finished_at": finished_at,
        }
        if entry["step"] == step["step"]
        else dict(entry)
        for entry in row.progress
    ]
    audit.record(
        db,
        actor=f"remediation-plan:{row.id}",
        action="remediation_plan.step",
        resource_type="remediation_plan",
        resource_id=str(row.id),
        details={
            "step": step["step"],
            "action": step["action"],
            "project_gitlab_id": step["target"]["project_gitlab_id"],
            "idempotency_key": step["idempotency_key"],
            "status": outcome.status,
            "request_status": outcome.request_status,
            "verified": outcome.verified,
            "observed": outcome.observed,
        },
    )


def _finish(db: Session, row: RemediationPlan, failure: tuple[int, str] | None) -> None:
    row.finished_at = db.execute(select(func.now())).scalar_one()
    if failure is None:
        row.state = "applied"
    else:
        row.state = "failed"
        row.failed_step, row.failure = failure
        row.progress = [
            {**entry, "status": "not_run"} if entry["status"] == "pending" else dict(entry)
            for entry in row.progress
        ]
    counts: dict[str, int] = {}
    for entry in row.progress:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    audit.record(
        db,
        actor=SYSTEM_ACTOR,
        action=f"remediation_plan.{row.state}",
        resource_type="remediation_plan",
        resource_id=str(row.id),
        details={
            "plan_digest": row.plan_digest,
            "steps_by_status": counts,
            "failed_step": row.failed_step,
            "failure": row.failure,
        },
    )
    # Read again so findings resolve through evidence; imported here to avoid an import cycle.
    from baluarte.integrations.gitlab.jobs import DISCOVER_PROJECTS
    from baluarte.jobs import queue

    queue.enqueue(db, DISCOVER_PROJECTS, f"{DISCOVER_PROJECTS}:after-remediation-plan:{row.id}")


def _credentials(db: Session, row: RemediationPlan) -> tuple[GitLabSettings, str] | str:
    """The GitLab settings and the write token, or why the plan cannot be applied now."""
    try:
        settings = load_gitlab_settings()
        if not WRITE_TOKEN.writes_enabled():
            return APPLY_REFUSALS["remediation_writes_disabled"]
        token = WRITE_TOKEN.load(db, load_secret_box())
    except ConfigurationError as exc:
        return str(exc)
    if settings is None or token is None:
        return "GitLab or the remediation write token is no longer configured."
    if not digest_valid(row):
        return "The stored plan no longer matches its digest."
    return settings, token


def make_apply_remediation_plan(
    transport: httpx.BaseTransport | None = None,
) -> Callable[[ClaimedJob, Engine], None]:
    def apply(job: ClaimedJob, engine: Engine) -> None:
        plan_id = int(job.payload.get("plan_id", 0))
        with Session(engine) as db:
            row = locked(db, plan_id)
            if row is None or row.state != "applying":
                return
            steps = list(row.plan.get("steps", []))
            done = {e["step"] for e in row.progress if e.get("status") != "pending"}
            credentials = _credentials(db, row)
            if isinstance(credentials, str):
                _finish(db, row, (steps[0]["step"] if steps else 0, credentials))
                db.commit()
                return
            settings, token = credentials
            db.commit()

        def audit_write(step: dict[str, Any]) -> Audit:
            def record(result: WriteResult) -> None:
                with Session(engine) as db:
                    audit.record(
                        db,
                        actor=f"remediation-plan:{plan_id}",
                        action="remediation_plan.write",
                        resource_type="remediation_plan",
                        resource_id=str(plan_id),
                        details={
                            "step": step["step"],
                            "idempotency_key": step["idempotency_key"],
                            "method": result.method,
                            "path": result.path,
                            "status": result.status,
                            "error": result.error,
                        },
                    )
                    db.commit()

            return record

        failure: tuple[int, str] | None = None
        with (
            GitLabClient(settings, transport=transport) as client,
            RemediationWriter(settings, token, transport=transport) as writer,
        ):
            for step in steps:
                if step["step"] in done:
                    continue
                outcome = apply_step(client, writer, step, audit_write(step))
                with Session(engine) as db:
                    row = locked(db, plan_id)
                    if row is None:
                        return
                    _record(db, row, step, outcome)
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
            "remediation plan %s: %s",
            plan_id,
            "applied" if failure is None else f"failed at step {failure[0]}",
            extra={"job_id": job.id},
        )

    return apply
