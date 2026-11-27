"""Applying approved template change sets.

Roadmap M14.4; see docs/CONFIGURATION_TEMPLATES.md#execution.

Applying is refused unless ``BALUARTE_TEMPLATE_WRITES`` is true, a template write token is stored,
the set is approved and unexpired, its connection is the primary GitLab connection (the token
belongs to it), no other set of that connection is applying or rolling back, the set is not
stale, and every change that cannot be fully undone is acknowledged. Whoever applies, continues
or rolls back a set is never one of its approvers (M14.6); each such run is a
``template_change_run`` row, and its trigger refuses an approver outside development mode.

**Stages.** Starting names how many targets the first stage covers; the
``gitlab.apply_template_change_set`` job applies their changes and the set becomes ``paused``
until someone continues with more targets, or ``finished`` when none remain.

**Each change** (a ``template_change_item``):

1. **Read** the setting with the read-only token. An archived project, or a value that is
   neither the approved ``before`` nor ``after``, is ``out_of_date``: someone changed it since
   the set was built, and nothing is written (a concurrent change, flagged).
2. **Already as wanted:** ``skipped``.
3. **Write** with the template write token. The value read is kept as ``previous`` for rollback.
   A change GitLab cannot make in one request (a protection by wildcard, several approval rules
   covering the branch) is ``manual``.
4. **Retry** only what may not have happened: on a rate limit (429, honouring ``Retry-After``),
   a 5xx or no answer, the setting is read again and the write is repeated only while it still
   shows the old value, at most three attempts.
5. **Verify** by reading again with the read-only token: ``applied`` only when it shows the
   wanted value, otherwise ``failed``. A failed change does not stop the others.

**Cancelling** stops before the next change; pending changes become ``cancelled``. **Resuming:**
a job that stopped mid-run (the worker restarted) is claimed again and continues; a change left
``writing`` is read first and counted ``applied`` if the write took effect.

**Rolling back** a paused, cancelled or finished set undoes its applied changes in reverse order,
each only while the setting still shows the value written (otherwise ``not_rolled_back``), then
verifies. Removing a protection cannot be fully undone: rollback protects the branch again with
the recorded role levels, but not per-user or per-group access or its other options; such
changes are disclosed before applying.

Every write is audited as ``template_change_set.write``, every change's outcome as
``template_change_set.item`` and every run's end. Project discovery is queued afterwards so
drift and findings follow from evidence.
"""

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.configuration.crypto import SecretUnavailable, load_secret_box
from baluarte.connections import ConnectionRefused, is_primary, settings_for
from baluarte.db.models import (
    GitLabInstance,
    TemplateChangeItem,
    TemplateChangeRun,
    TemplateChangeSet,
)
from baluarte.integrations.gitlab.access import summarise_approvals
from baluarte.integrations.gitlab.branches import matches
from baluarte.integrations.gitlab.client import GitLabClient, GitLabError
from baluarte.integrations.gitlab.settings import GitLabSettings
from baluarte.integrations.gitlab.template_writer import TemplateWriter, WriteResult
from baluarte.jobs import queue
from baluarte.jobs.queue import ClaimedJob
from baluarte.policy.evaluation.access import COUNTED_RULE_TYPES, required_approvals
from baluarte.settings import ConfigurationError, development_mode
from baluarte.templates.change_sets import current_state, stale_reason
from baluarte.templates.write_token import TEMPLATE_WRITES
from baluarte.templates.write_token import TOKEN as WRITE_TOKEN
from baluarte.workspaces import configured_settings

logger = logging.getLogger(__name__)

APPLY_CHANGE_SET = "gitlab.apply_template_change_set"
ROLLBACK_CHANGE_SET = "gitlab.rollback_template_change_set"
SYSTEM_ACTOR = "system:template-change-sets"
MAX_ATTEMPTS = 3
MAX_WAIT_SECONDS = 60.0
RETRYABLE = frozenset({429, 500, 502, 503, 504})

UNPROTECT_IRREVERSIBLE = (
    "Removing the protection drops its exact settings: rolling back protects the branch again "
    "with the recorded role levels, but not per-user or per-group access, force push or code "
    "owner approval."
)

# Template flag -> (GitLab approval setting, whether GitLab's value is the opposite).
FLAGS: dict[str, tuple[str, bool]] = {
    "merge_requests.forbid_author_approval": ("merge_requests_author_approval", True),
    "merge_requests.forbid_committer_approval": (
        "merge_requests_disable_committers_approval",
        False,
    ),
    "merge_requests.reset_approvals_on_push": ("reset_approvals_on_push", False),
    "merge_requests.forbid_override": (
        "disable_overriding_approvers_per_merge_request",
        False,
    ),
}
# Template setting -> GitLab's project setting, which says the opposite.
TRIGGERER = "deployments.forbid_pipeline_triggerer_approval"
APPLICABLE = frozenset(
    {
        "visibility",
        "default_branch_protected",
        "merge_requests.min_approvals",
        "topics",
        TRIGGERER,
        *FLAGS,
    }
)

REFUSALS = {
    "writes_disabled": f"Template writes are off for this deployment ({TEMPLATE_WRITES}).",
    "token_missing": "No template write token is stored.",
    "not_approved": "Only an approved change set can be applied.",
    "expired": "The change set has expired.",
    "not_primary": "Change sets are applied only to the primary GitLab connection, which the "
    "template write token belongs to.",
    "busy": "Another change set of this connection is applying or rolling back.",
    "stale": "Today's evidence no longer gives the same change set.",
    "unacknowledged": "Some changes cannot be fully undone; acknowledge them to apply.",
    "nothing_left": "Every target has already been included.",
    "approver": "Whoever approved a change set cannot apply, continue or roll it back; someone "
    "else with the permission must.",
}


class ApplyRefused(Exception):
    def __init__(self, reasons: Sequence[str], detail: str | None = None) -> None:
        text = " ".join(REFUSALS[reason] for reason in reasons)
        super().__init__(f"{text} {detail}" if detail else text)
        self.reasons = list(reasons)


# --- reading a setting ---------------------------------------------------------------------


@dataclass(frozen=True)
class Live:
    """A setting as GitLab shows it now; ``problem`` when the change no longer applies."""

    value: Any = None
    detail: dict[str, Any] = field(default_factory=dict)
    problem: str | None = None


def _project(client: GitLabClient, gitlab_id: int) -> dict[str, Any]:
    project = client.get_json(f"/projects/{gitlab_id}")
    if not isinstance(project, dict):
        raise GitLabError(f"GET /projects/{gitlab_id} did not return an object")
    return project


def _all(client: GitLabClient, path: str) -> list[dict[str, Any]]:
    return [item for page in client.paginate(path) for item in page if isinstance(item, dict)]


def _covering(rules: list[dict[str, Any]], branch: str) -> list[dict[str, Any]]:
    return [r for r in rules if isinstance(r.get("name"), str) and matches(r["name"], branch)]


def _levels(rule: dict[str, Any]) -> dict[str, int]:
    """The role levels of a protection, for recreating it: the highest role-based entry of each."""
    found = {}
    for kind in ("push_access_levels", "merge_access_levels", "unprotect_access_levels"):
        entries = rule.get(kind)
        levels = [
            e["access_level"]
            for e in entries or []
            if isinstance(e, dict)
            and isinstance(e.get("access_level"), int)
            and e.get("user_id") is None
            and e.get("group_id") is None
        ]
        if levels:
            found[kind.removesuffix("s")] = max(levels)
    return found


def _covers(rule: dict[str, Any], branch: str, protected: bool) -> bool | None:
    """Whether an approval rule covers the default branch; None when it cannot be told."""
    if rule.get("applies_to_all_protected_branches") is True:
        return protected
    branches = rule.get("protected_branches")
    if not isinstance(branches, list):
        return None
    names = [b.get("name") for b in branches if isinstance(b, dict)]
    return not names or any(isinstance(n, str) and matches(n, branch) for n in names)


def read_live(client: GitLabClient, item: TemplateChangeItem) -> Live:
    gitlab_id = item.project_gitlab_id
    project = _project(client, gitlab_id)
    if project.get("archived") is True:
        return Live(problem="The project is now archived.")
    key = item.setting
    if key == "visibility":
        return Live(project.get("visibility"))
    if key == "topics":
        topics = project.get("topics")
        if not isinstance(topics, list):
            return Live(problem="GitLab did not report the project's topics.")
        return Live(sorted({t for t in topics if isinstance(t, str)}))
    if key == TRIGGERER:
        allowed = project.get("allow_pipeline_trigger_approve_deployment")
        if not isinstance(allowed, bool):
            return Live(problem="GitLab did not report whether the pipeline's starter may approve.")
        return Live(not allowed)
    branch = project.get("default_branch")
    if not isinstance(branch, str) or not branch:
        return Live(problem="The project has no default branch.")
    protections = _all(client, f"/projects/{gitlab_id}/protected_branches")
    covering = _covering(protections, branch)
    if key == "default_branch_protected":
        detail = {
            "branch": branch,
            "rules": [
                {
                    "name": r.get("name"),
                    "levels": _levels(r),
                    "allow_force_push": r.get("allow_force_push"),
                    "code_owner_approval_required": r.get("code_owner_approval_required"),
                }
                for r in covering
            ],
        }
        return Live(bool(covering), detail)
    if key in FLAGS:
        setting, inverted = FLAGS[key]
        settings = client.get_json(f"/projects/{gitlab_id}/approvals")
        value = settings.get(setting) if isinstance(settings, dict) else None
        if not isinstance(value, bool):
            return Live(problem=f"GitLab did not report {setting}.")
        return Live(not value if inverted else value)
    if key == "merge_requests.min_approvals":
        rules = _all(client, f"/projects/{gitlab_id}/approval_rules")
        required = required_approvals(summarise_approvals(branch, bool(covering), {}, rules))
        if required is None:
            return Live(problem="Which approval rules cover the default branch cannot be told.")
        counted = [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "approvals_required": r.get("approvals_required"),
            }
            for r in rules
            if r.get("rule_type") in COUNTED_RULE_TYPES and _covers(r, branch, bool(covering))
        ]
        return Live(required, {"branch": branch, "rules": counted})
    return Live(problem=f"{key} is not changed by change sets.")


# --- writing a setting ---------------------------------------------------------------------


@dataclass(frozen=True)
class Manual:
    message: str


Write = Callable[[], WriteResult]


def forward(
    writer: TemplateWriter, item: TemplateChangeItem, live: Live
) -> tuple[Write | Manual, dict[str, Any]]:
    """The write that makes the setting ``after``, and what rollback needs to undo it."""
    gitlab_id, key, after = item.project_gitlab_id, item.setting, item.after
    previous = {"value": live.value, **live.detail}
    if key == "visibility":
        return (lambda: writer.set_visibility(gitlab_id, after)), previous
    if key == "topics":
        return (lambda: writer.set_topics(gitlab_id, list(after))), previous
    if key == TRIGGERER:
        return (lambda: writer.set_pipeline_trigger_approval(gitlab_id, not after)), previous
    if key == "default_branch_protected":
        branch = live.detail["branch"]
        if after:
            return (lambda: writer.protect_branch(gitlab_id, branch)), previous
        names = [r["name"] for r in live.detail["rules"]]
        if names != [branch]:
            return Manual(
                f"{branch} is protected by {', '.join(names)}; only a protection named exactly "
                "after the branch is removed. Change it by hand."
            ), previous
        return (lambda: writer.unprotect_branch(gitlab_id, branch)), previous
    if key in FLAGS:
        setting, inverted = FLAGS[key]
        value = (not after) if inverted else bool(after)
        return (lambda: writer.set_approval_setting(gitlab_id, setting, value)), previous
    if key == "merge_requests.min_approvals":
        rules = live.detail["rules"]
        if not rules:
            return (lambda: writer.create_approval_rule(gitlab_id, int(after))), previous
        if len(rules) == 1 and isinstance(rules[0]["id"], int):
            rule_id = rules[0]["id"]
            return (lambda: writer.update_approval_rule(gitlab_id, rule_id, int(after))), previous
        listed = ", ".join(str(r["name"]) for r in rules)
        return Manual(
            f"Several approval rules cover the default branch ({listed}); change them by hand."
        ), previous
    return Manual(f"{key} is not changed by change sets."), previous


def backward(writer: TemplateWriter, item: TemplateChangeItem, live: Live) -> Write | Manual:
    """The write that puts the setting back to the value it had before it was applied."""
    gitlab_id, key = item.project_gitlab_id, item.setting
    previous = item.previous or {}
    before = previous.get("value", item.before)
    if key == "visibility":
        return lambda: writer.set_visibility(gitlab_id, before)
    if key == "topics":
        return lambda: writer.set_topics(gitlab_id, list(before))
    if key == TRIGGERER:
        return lambda: writer.set_pipeline_trigger_approval(gitlab_id, not before)
    if key == "default_branch_protected":
        branch = live.detail["branch"]
        if item.after:  # it was protected by this set: remove that protection
            names = [r["name"] for r in live.detail["rules"]]
            if names != [branch]:
                return Manual(f"{branch} is now protected by {', '.join(names)}; left as it is.")
            return lambda: writer.unprotect_branch(gitlab_id, branch)
        removed = (previous.get("rules") or [{}])[0]
        return lambda: writer.protect_branch(gitlab_id, branch, removed.get("levels") or None)
    if key in FLAGS:
        setting, inverted = FLAGS[key]
        value = (not before) if inverted else bool(before)
        return lambda: writer.set_approval_setting(gitlab_id, setting, value)
    if key == "merge_requests.min_approvals":
        created = previous.get("created_rule_id")
        if isinstance(created, int):
            return lambda: writer.delete_approval_rule(gitlab_id, created)
        rules = previous.get("rules") or []
        if len(rules) == 1 and isinstance(rules[0].get("id"), int):
            rule_id = rules[0]["id"]
            return lambda: writer.update_approval_rule(gitlab_id, rule_id, int(before))
        return Manual("The approval rule this set changed is not known; change it by hand.")
    return Manual(f"{key} is not changed by change sets.")


# --- one change ----------------------------------------------------------------------------


Status = Literal["applied", "skipped", "out_of_date", "manual", "failed"]


@dataclass
class Outcome:
    status: Status
    message: str
    observed: Any = None
    request_status: int | None = None
    attempts: int = 0
    previous: dict[str, Any] | None = None


AuditWrite = Callable[[WriteResult, int], None]
Sleep = Callable[[float], None]


def _wait(result: WriteResult, attempt: int) -> float:
    wait = result.retry_after if result.retry_after is not None else 2.0**attempt
    return min(max(wait, 0.0), MAX_WAIT_SECONDS)


def _failed(result: WriteResult) -> str:
    reason = result.error or f"GitLab answered {result.status}"
    return f"{result.method} {result.path} failed: {reason}"


def write_verified(
    client: GitLabClient,
    send: Write,
    item: TemplateChangeItem,
    wanted: Any,
    old: Any,
    audit_write: AuditWrite,
    sleep: Sleep,
) -> Outcome:
    """Send a write, retrying only while GitLab still shows the old value, then verify."""
    result: WriteResult | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = send()
        audit_write(result, attempt)
        try:
            live = read_live(client, item)
        except GitLabError as error:
            return Outcome(
                "failed",
                f"The write was sent but could not be read back: {error}",
                request_status=result.status,
                attempts=attempt,
            )
        if live.problem is None and live.value == wanted:
            message = (
                "Written and read back as wanted."
                if result.ok
                else f"{_failed(result)}, but a read shows the wanted value."
            )
            return Outcome("applied", message, live.value, result.status, attempt)
        retryable = result.status is None or result.status in RETRYABLE
        if not retryable or live.problem is not None or live.value != old:
            break
        if attempt < MAX_ATTEMPTS:
            sleep(_wait(result, attempt))
    if result is None:  # MAX_ATTEMPTS is at least one
        raise RuntimeError("no write was sent")
    message = (
        "The write was accepted but does not read back as wanted."
        if result.ok
        else f"{_failed(result)}, and a read does not show the wanted value."
    )
    observed = None
    try:
        observed = read_live(client, item).value
    except GitLabError:
        pass
    return Outcome("failed", message, observed, result.status, attempt)


def apply_item(
    client: GitLabClient,
    writer: TemplateWriter,
    item: TemplateChangeItem,
    audit_write: AuditWrite,
    sleep: Sleep,
    before_write: Callable[[dict[str, Any]], None],
) -> Outcome:
    try:
        live = read_live(client, item)
    except GitLabError as error:
        return Outcome("failed", f"{item.project_path} could not be read: {error}")
    if live.problem is not None:
        return Outcome("out_of_date", live.problem)
    if live.value == item.after:
        return Outcome("skipped", "Already as wanted.", live.value)
    if live.value != item.before:
        return Outcome(
            "out_of_date",
            f"Changed since the set was built: GitLab now shows {live.value!r}, the set "
            f"expected {item.before!r}. Nothing was written.",
            live.value,
        )
    plan, previous = forward(writer, item, live)
    if isinstance(plan, Manual):
        return Outcome("manual", plan.message, live.value, previous=previous)
    before_write(previous)
    created: dict[str, Any] = {}

    def send() -> WriteResult:
        result = plan()
        if (
            item.setting == "merge_requests.min_approvals"
            and result.ok
            and isinstance(result.body, dict)
            and isinstance(result.body.get("id"), int)
            and not previous["rules"]
        ):
            created["created_rule_id"] = result.body["id"]
        return result

    outcome = write_verified(client, send, item, item.after, item.before, audit_write, sleep)
    outcome.previous = {**previous, **created}
    return outcome


def resume_item(client: GitLabClient, item: TemplateChangeItem) -> Outcome | None:
    """A change left ``writing`` by a stopped run: applied if the write took effect, else None
    to run it again."""
    try:
        live = read_live(client, item)
    except GitLabError as error:
        return Outcome("failed", f"{item.project_path} could not be read: {error}")
    if live.problem is None and live.value == item.after:
        return Outcome("applied", "Found applied when the run resumed.", live.value)
    return None


# --- runs ----------------------------------------------------------------------------------


def _now(db: Session) -> Any:
    return db.execute(select(func.now())).scalar_one()


def _set(db: Session, change_set_id: int) -> TemplateChangeSet | None:
    return db.get(TemplateChangeSet, change_set_id, with_for_update=True, populate_existing=True)


def _finish_item(db: Session, item_id: int, outcome: Outcome) -> TemplateChangeItem:
    item = db.get_one(TemplateChangeItem, item_id, with_for_update=True, populate_existing=True)
    now = _now(db)
    item.status = outcome.status
    item.message = outcome.message
    item.observed = outcome.observed
    item.request_status = outcome.request_status
    item.attempts = item.attempts + outcome.attempts
    if outcome.previous is not None:
        item.previous = outcome.previous
    item.finished_at = now
    audit.record(
        db,
        actor=f"template-change-set:{item.change_set_id}",
        action="template_change_set.item",
        resource_type="template_change_set",
        resource_id=str(item.change_set_id),
        details={
            "item_id": item.id,
            "project_gitlab_id": item.project_gitlab_id,
            "setting": item.setting,
            "before": item.before,
            "after": item.after,
            "status": item.status,
            "request_status": item.request_status,
            "observed": item.observed,
        },
    )
    return item


def credentials(db: Session, row: TemplateChangeSet) -> tuple[GitLabSettings, str] | str:
    """The read settings and the write token, or why the set cannot be written now."""
    try:
        if not WRITE_TOKEN.writes_enabled():
            return REFUSALS["writes_disabled"]
        connection = db.get_one(GitLabInstance, row.instance_id)
        if connection.provider != "gitlab" or not is_primary(connection, configured_settings()):
            return REFUSALS["not_primary"]
        box = load_secret_box()
        token = WRITE_TOKEN.load(db, box)
        if token is None:
            return REFUSALS["token_missing"]
        settings = settings_for(db, None, box).settings
    except (ConfigurationError, ConnectionRefused, SecretUnavailable) as exc:
        return str(exc)
    return settings, token


def _audit_writer(
    engine: Engine, change_set_id: int, item: TemplateChangeItem, phase: str
) -> AuditWrite:
    def record(result: WriteResult, attempt: int) -> None:
        with Session(engine) as db:
            audit.record(
                db,
                actor=f"template-change-set:{change_set_id}",
                action="template_change_set.write",
                resource_type="template_change_set",
                resource_id=str(change_set_id),
                details={
                    "item_id": item.id,
                    "phase": phase,
                    "attempt": attempt,
                    "method": result.method,
                    "path": result.path,
                    "status": result.status,
                    "error": result.error,
                },
            )
            db.commit()

    return record


def _discover(db: Session, row: TemplateChangeSet, phase: str) -> None:
    from baluarte.integrations.gitlab.jobs import DISCOVER_PROJECTS

    queue.enqueue(
        db, DISCOVER_PROJECTS, f"{DISCOVER_PROJECTS}:after-change-set:{row.id}:{phase}:{row.runs}"
    )


def _halt(engine: Engine, change_set_id: int, reason: str) -> None:
    with Session(engine) as db:
        row = _set(db, change_set_id)
        if row is None or row.state != "applying":
            return
        row.state = "paused"
        row.halt_reason = reason
        audit.record(
            db,
            actor=SYSTEM_ACTOR,
            action="template_change_set.halted",
            resource_type="template_change_set",
            resource_id=str(row.id),
            details={"change_set_id": row.id, "reason": reason},
        )
        db.commit()


def make_apply_change_set(
    transport: httpx.BaseTransport | None = None, sleep: Sleep = time.sleep
) -> Callable[[ClaimedJob, Engine], None]:
    def apply(job: ClaimedJob, engine: Engine) -> None:
        change_set_id = int(job.payload.get("change_set_id", 0))
        with Session(engine) as db:
            row = _set(db, change_set_id)
            if row is None or row.state != "applying":
                return
            found = credentials(db, row)
            stage = row.stage_targets
            db.commit()
        if isinstance(found, str):
            _halt(engine, change_set_id, found)
            return
        settings, token = found
        with (
            GitLabClient(settings, transport=transport) as client,
            TemplateWriter(settings, token, transport=transport) as writer,
        ):
            while True:
                with Session(engine) as db:
                    row = db.get(TemplateChangeSet, change_set_id, populate_existing=True)
                    if row is None or row.state != "applying":
                        return  # cancelled: the API already marked what is left
                    item = db.scalars(
                        select(TemplateChangeItem)
                        .where(
                            TemplateChangeItem.change_set_id == change_set_id,
                            TemplateChangeItem.status.in_(("pending", "writing")),
                            TemplateChangeItem.target_index < stage,
                        )
                        .order_by(TemplateChangeItem.target_index, TemplateChangeItem.position)
                        .limit(1)
                    ).first()
                    db.expunge_all()
                if item is None:
                    break
                outcome = resume_item(client, item) if item.status == "writing" else None
                if outcome is None:

                    def before_write(previous: dict[str, Any], item_id: int = item.id) -> None:
                        with Session(engine) as db:
                            db.execute(
                                update(TemplateChangeItem)
                                .where(TemplateChangeItem.id == item_id)
                                .values(status="writing", previous=previous)
                            )
                            db.commit()

                    outcome = apply_item(
                        client,
                        writer,
                        item,
                        _audit_writer(engine, change_set_id, item, "apply"),
                        sleep,
                        before_write,
                    )
                with Session(engine) as db:
                    _finish_item(db, item.id, outcome)
                    db.commit()
        with Session(engine) as db:
            row = _set(db, change_set_id)
            if row is None or row.state != "applying":
                return
            remaining = db.scalar(
                select(func.count())
                .select_from(TemplateChangeItem)
                .where(
                    TemplateChangeItem.change_set_id == change_set_id,
                    TemplateChangeItem.status == "pending",
                )
            )
            now = _now(db)
            row.halt_reason = None
            if remaining:
                row.state = "paused"
            else:
                row.state = "finished"
                row.finished_at = now
            counts = dict(
                db.execute(
                    select(TemplateChangeItem.status, func.count())
                    .where(TemplateChangeItem.change_set_id == change_set_id)
                    .group_by(TemplateChangeItem.status)
                ).all()
            )
            audit.record(
                db,
                actor=SYSTEM_ACTOR,
                action=f"template_change_set.{row.state}",
                resource_type="template_change_set",
                resource_id=str(row.id),
                details={"change_set_id": row.id, "stage_targets": stage, "items": counts},
            )
            _discover(db, row, "apply")
            db.commit()
        logger.info(
            "template change set %s: %s", change_set_id, "run done", extra={"job_id": job.id}
        )

    return apply


def rollback_item(
    client: GitLabClient,
    writer: TemplateWriter,
    item: TemplateChangeItem,
    audit_write: AuditWrite,
    sleep: Sleep,
) -> tuple[str, str]:
    try:
        live = read_live(client, item)
    except GitLabError as error:
        return "failed", f"{item.project_path} could not be read: {error}"
    before = (item.previous or {}).get("value", item.before)
    if live.problem is not None:
        return "not_rolled_back", live.problem
    if live.value == before:
        return "rolled_back", "Already back to its previous value."
    if live.value != item.after:
        return (
            "not_rolled_back",
            f"Changed since it was applied: GitLab now shows {live.value!r}; left as it is.",
        )
    plan = backward(writer, item, live)
    if isinstance(plan, Manual):
        return "not_rolled_back", plan.message
    outcome = write_verified(client, plan, item, before, item.after, audit_write, sleep)
    if outcome.status == "applied":
        return "rolled_back", "Put back and read back as before."
    return "failed", outcome.message


def make_rollback_change_set(
    transport: httpx.BaseTransport | None = None, sleep: Sleep = time.sleep
) -> Callable[[ClaimedJob, Engine], None]:
    def rollback(job: ClaimedJob, engine: Engine) -> None:
        change_set_id = int(job.payload.get("change_set_id", 0))
        with Session(engine) as db:
            row = _set(db, change_set_id)
            if row is None or row.state != "rolling_back":
                return
            found = credentials(db, row)
            items = list(
                db.scalars(
                    select(TemplateChangeItem)
                    .where(
                        TemplateChangeItem.change_set_id == change_set_id,
                        TemplateChangeItem.status.in_(("applied", "writing")),
                        TemplateChangeItem.rollback_status.is_(None),
                    )
                    .order_by(TemplateChangeItem.position.desc())
                )
            )
            db.expunge_all()
        if isinstance(found, str):
            with Session(engine) as db:
                row = _set(db, change_set_id)
                if row is not None and row.state == "rolling_back":
                    row.halt_reason = found
                    audit.record(
                        db,
                        actor=SYSTEM_ACTOR,
                        action="template_change_set.halted",
                        resource_type="template_change_set",
                        resource_id=str(row.id),
                        details={"change_set_id": row.id, "reason": found},
                    )
                    db.commit()
            return  # stays rolling_back; rolling back again restarts it
        settings, token = found
        with (
            GitLabClient(settings, transport=transport) as client,
            TemplateWriter(settings, token, transport=transport) as writer,
        ):
            for item in items:
                status, message = rollback_item(
                    client,
                    writer,
                    item,
                    _audit_writer(engine, change_set_id, item, "rollback"),
                    sleep,
                )
                with Session(engine) as db:
                    now = _now(db)
                    stored = db.get_one(
                        TemplateChangeItem, item.id, with_for_update=True, populate_existing=True
                    )
                    stored.rollback_status = status
                    stored.rollback_message = message
                    stored.rolled_back_at = now
                    audit.record(
                        db,
                        actor=f"template-change-set:{change_set_id}",
                        action="template_change_set.item_rolled_back",
                        resource_type="template_change_set",
                        resource_id=str(change_set_id),
                        details={"item_id": item.id, "setting": item.setting, "status": status},
                    )
                    db.commit()
        with Session(engine) as db:
            row = _set(db, change_set_id)
            if row is None or row.state != "rolling_back":
                return
            now = _now(db)
            row.state = "rolled_back"
            row.finished_at = now
            row.halt_reason = None
            audit.record(
                db,
                actor=SYSTEM_ACTOR,
                action="template_change_set.rolled_back",
                resource_type="template_change_set",
                resource_id=str(row.id),
                details={"change_set_id": row.id},
            )
            _discover(db, row, "rollback")
            db.commit()

    return rollback


# --- starting, continuing, cancelling, rolling back -----------------------------------------


def _items(db: Session, row: TemplateChangeSet) -> list[TemplateChangeItem]:
    found = []
    index = 0
    position = 0
    for target in row.document["targets"]:
        if not target["changes"]:
            continue
        for change in target["changes"]:
            key = change["key"]
            irreversible = (
                UNPROTECT_IRREVERSIBLE
                if key == "default_branch_protected" and change["after"] is False
                else None
            )
            found.append(
                TemplateChangeItem(
                    workspace_id=row.workspace_id,
                    change_set_id=row.id,
                    position=position,
                    target_index=index,
                    project_id=target["project"]["id"],
                    project_gitlab_id=target["project"]["gitlab_id"],
                    project_path=target["project"]["path"],
                    setting=key,
                    before=change["before"],
                    after=change["after"],
                    irreversible=irreversible,
                )
            )
            position += 1
        index += 1
    return found


def changing_targets(row: TemplateChangeSet) -> int:
    return sum(1 for target in row.document["targets"] if target["changes"])


def irreversible_changes(row: TemplateChangeSet) -> list[dict[str, str]]:
    return [
        {
            "project": target["project"]["path"],
            "setting": change["key"],
            "why": UNPROTECT_IRREVERSIBLE,
        }
        for target in row.document["targets"]
        for change in target["changes"]
        if change["key"] == "default_branch_protected" and change["after"] is False
    ]


def write_refusals(db: Session, row: TemplateChangeSet) -> list[str]:
    reasons = []
    try:
        if not WRITE_TOKEN.writes_enabled():
            reasons.append("writes_disabled")
    except ConfigurationError:
        reasons.append("writes_disabled")
    if not WRITE_TOKEN.status(db).set:
        reasons.append("token_missing")
    connection = db.get_one(GitLabInstance, row.instance_id)
    if connection.provider != "gitlab" or not is_primary(connection, configured_settings()):
        reasons.append("not_primary")
    busy = db.scalar(
        select(func.count())
        .select_from(TemplateChangeSet)
        .where(
            TemplateChangeSet.instance_id == row.instance_id,
            TemplateChangeSet.id != row.id,
            TemplateChangeSet.state.in_(("applying", "rolling_back")),
        )
    )
    if busy:
        reasons.append("busy")
    return reasons


def approved_by(row: TemplateChangeSet, user_id: int) -> bool:
    """Whether ``user_id`` gave one of the set's approvals."""
    return user_id in (row.first_approved_by_user_id, row.decided_by_user_id)


def run_refusals(row: TemplateChangeSet, user_id: int) -> list[str]:
    """Why ``user_id`` may not start a run of the set (M14.6); development mode allows an
    approver, and the run records it."""
    return ["approver"] if approved_by(row, user_id) and not development_mode() else []


def _record_run(
    db: Session, row: TemplateChangeSet, kind: str, user_id: int, stage: int | None
) -> TemplateChangeRun:
    run = TemplateChangeRun(
        workspace_id=row.workspace_id,
        change_set_id=row.id,
        kind=kind,
        user_id=user_id,
        stage_targets=stage,
        development_execution=approved_by(row, user_id),
    )
    db.add(run)
    return run


def _enqueue(db: Session, row: TemplateChangeSet, kind: str) -> None:
    row.runs = row.runs + 1
    queue.enqueue(
        db,
        kind,
        f"{kind}:{row.id}:{row.runs}",
        {"change_set_id": row.id},
        max_attempts=5,
    )


def start(
    db: Session,
    row: TemplateChangeSet,
    stage_targets: int,
    acknowledge_irreversible: bool,
    user_id: int,
    actor: str,
) -> TemplateChangeSet:
    state = current_state(db, row)
    reasons = write_refusals(db, row) + run_refusals(row, user_id)
    if state == "expired":
        reasons.append("expired")
    elif state != "approved":
        reasons.append("not_approved")
    if irreversible_changes(row) and not acknowledge_irreversible:
        reasons.append("unacknowledged")
    detail = None
    if not reasons:
        detail = stale_reason(db, row)
        if detail is not None:
            reasons.append("stale")
    if reasons:
        raise ApplyRefused(reasons, detail)
    total = changing_targets(row)
    now = _now(db)
    db.add_all(_items(db, row))
    row.state = "applying"
    row.applied_by_user_id = user_id
    row.apply_started_at = now
    row.stage_targets = max(1, min(stage_targets, total))
    run = _record_run(db, row, "apply", user_id, row.stage_targets)
    _enqueue(db, row, APPLY_CHANGE_SET)
    db.flush()
    audit.record(
        db,
        actor=actor,
        action="template_change_set.apply_started",
        resource_type="template_change_set",
        resource_id=str(row.id),
        details={
            "change_set_id": row.id,
            "digest": row.digest,
            "stage_targets": row.stage_targets,
            "targets": total,
            "irreversible": len(irreversible_changes(row)),
            "run_id": run.id,
            "development_execution": run.development_execution,
        },
    )
    return row


def resume(
    db: Session, row: TemplateChangeSet, more_targets: int | None, user_id: int, actor: str
) -> TemplateChangeSet:
    """Continue a paused set: ``more_targets`` more targets, or all that remain."""
    if row.state != "paused":
        raise ApplyRefused(
            ["not_approved"], f"Only a paused set continues; this one is {row.state}."
        )
    reasons = write_refusals(db, row) + run_refusals(row, user_id)
    total = changing_targets(row)
    if row.stage_targets >= total and not db.scalar(
        select(func.count())
        .select_from(TemplateChangeItem)
        .where(
            TemplateChangeItem.change_set_id == row.id,
            TemplateChangeItem.status.in_(("pending", "writing")),
        )
    ):
        reasons.append("nothing_left")
    if reasons:
        raise ApplyRefused(reasons)
    target = total if more_targets is None else min(total, row.stage_targets + more_targets)
    row.stage_targets = max(row.stage_targets, target)
    row.state = "applying"
    row.halt_reason = None
    run = _record_run(db, row, "continue", user_id, row.stage_targets)
    _enqueue(db, row, APPLY_CHANGE_SET)
    db.flush()
    audit.record(
        db,
        actor=actor,
        action="template_change_set.continued",
        resource_type="template_change_set",
        resource_id=str(row.id),
        details={
            "change_set_id": row.id,
            "stage_targets": row.stage_targets,
            "run_id": run.id,
            "development_execution": run.development_execution,
        },
    )
    return row


def cancel(db: Session, row: TemplateChangeSet, user_id: int, actor: str) -> TemplateChangeSet:
    if row.state not in ("applying", "paused"):
        raise ApplyRefused(
            [], f"Only an applying or paused set can be cancelled; this one is {row.state}."
        )
    now = _now(db)
    row.state = "cancelled"
    row.cancelled_by_user_id = user_id
    row.cancelled_at = now
    pending = list(
        db.scalars(
            select(TemplateChangeItem)
            .where(
                TemplateChangeItem.change_set_id == row.id,
                TemplateChangeItem.status == "pending",
            )
            .with_for_update()
        )
    )
    for item in pending:
        item.status = "cancelled"
        item.message = "Cancelled before it was applied."
        item.finished_at = now
    db.flush()
    audit.record(
        db,
        actor=actor,
        action="template_change_set.cancelled",
        resource_type="template_change_set",
        resource_id=str(row.id),
        details={"change_set_id": row.id, "not_applied": len(pending)},
    )
    return row


def start_rollback(
    db: Session, row: TemplateChangeSet, user_id: int, actor: str
) -> TemplateChangeSet:
    restart = row.state == "rolling_back" and row.halt_reason is not None
    if row.state not in ("paused", "cancelled", "finished") and not restart:
        raise ApplyRefused(
            [],
            "Only a paused, cancelled or finished set, or a rollback that stopped, can be "
            f"rolled back; this one is {row.state}.",
        )
    reasons = write_refusals(db, row) + run_refusals(row, user_id)
    if reasons:
        raise ApplyRefused(reasons)
    now = _now(db)
    row.halt_reason = None
    if not restart:
        row.state = "rolling_back"
        row.rollback_by_user_id = user_id
        row.rollback_started_at = now
    for item in db.scalars(
        select(TemplateChangeItem).where(
            TemplateChangeItem.change_set_id == row.id, TemplateChangeItem.status == "pending"
        )
    ):
        item.status = "cancelled"
        item.message = "Not applied: the set was rolled back."
        item.finished_at = now
    run = _record_run(db, row, "rollback", user_id, None)
    _enqueue(db, row, ROLLBACK_CHANGE_SET)
    db.flush()
    audit.record(
        db,
        actor=actor,
        action="template_change_set.rollback_started",
        resource_type="template_change_set",
        resource_id=str(row.id),
        details={
            "change_set_id": row.id,
            "restarted": restart,
            "run_id": run.id,
            "development_execution": run.development_execution,
        },
    )
    return row
