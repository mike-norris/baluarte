"""Template change sets: exact changes to selected projects, reviewed before anything is applied
(roadmap M14.3, docs/CONFIGURATION_TEMPLATES.md#change-sets).

A change set is built from stored evidence, never from a call to the provider: nothing here
uses an HTTP client. Its source is a template version (its settings with what it inherits),
settings written for the set, or each project's own effective settings, which remediates drift
(roadmap M14.5): every change then names the template and assignment that decided it; or the
project topics its Baluarte tags call for, for chosen tag keys (``topics``, M14.5). For every
selected project (``baluarte.templates.selection``) and setting it records one of:

- a **change**: the value the evidence shows (``before``) and the value wanted (``after``),
  with warnings when the change weakens protection, removes access or disrupts work;
- **unchanged**: the evidence already shows the value;
- **skipped**, with a code and reason: the provider has no such setting (``unsupported``), the
  evidence cannot tell the current value (``unknown``), an approved template exception lets the
  project differ (``excepted``), a baseline in force would fail (``conflicts_with_baseline``), or
  the setting is changed by other means (``label_plans``).

A set that **weakens protection or removes access** is ``sensitive`` and needs two approvals by
two different people, neither of them its creator. Others need one. Approval names the digest
reviewed and is refused when the set is stale: rebuilt from today's evidence it no longer shows
the same targets, values and skips. Lifecycle (enforced again by a trigger, migration 0040):
``proposed`` -> ``approved`` | ``rejected``; ``proposed`` -> ``withdrawn`` by the creator;
``proposed`` | ``approved`` -> ``expired`` at ``expires_at`` (database clock). Applying an
approved set is roadmap M14.4.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import ColumnElement, and_, case, func, select, update
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.audit import details_digest
from baluarte.db.models import (
    MAX_CHANGE_SET_HOURS,
    ConfigurationTemplate,
    GitLabInstance,
    GitLabProject,
    Policy,
    TemplateChangeSet,
)
from baluarte.policy.assignments import resolve_project
from baluarte.policy.evaluation.model import CapabilityStatus, CollectionContext
from baluarte.policy.evaluation.project import capability_statuses, collection_contexts
from baluarte.policy.loader import parse_policy
from baluarte.policy.schema import (
    DefaultBranchProtectedRule,
    DeploymentTriggererApprovalRule,
    MergeRequestApprovalsRule,
    ProjectVisibilityRule,
    Rule,
)
from baluarte.settings import development_mode, own_decision_refused
from baluarte.tags import NAME as TAG_NAME
from baluarte.tags import TagIndex
from baluarte.tags import index as tag_index
from baluarte.templates.drift import judge_for
from baluarte.templates.exceptions import open_for_project
from baluarte.templates.loader import parse_settings
from baluarte.templates.resolve import effective_settings
from baluarte.templates.schema import SUPPORT, flatten, split_extends
from baluarte.templates.selection import Selection, select_projects
from baluarte.templates.store import chain_settings, find_version
from baluarte.workspaces import settings_of, workspace_of

FORMAT = 1
MAX_TARGETS = 500
DEFAULT_HOURS = 72
MAX_NOTE = 2000
SYSTEM_ACTOR = "system:template-change-sets"

WarningCode = Literal["weakens_protection", "removes_access", "disrupts_work"]
SENSITIVE: frozenset[str] = frozenset({"weakens_protection", "removes_access"})
_EXPOSURE = {"private": 0, "internal": 1, "public": 2}


class ChangeSetConflict(Exception):
    """The change set is not in a state that allows the action."""


class ChangeSetStale(Exception):
    """Today's evidence no longer gives the same change set."""


class NothingToChange(Exception):
    """The selection and source give no change."""


class ChangeSetRefused(Exception):
    """The request is invalid: unknown template, empty settings or too many targets."""


# --- the source of the wanted values ---


@dataclass(frozen=True)
class Source:
    settings: dict[str, Any]
    template: str | None
    template_id: int | None
    # Each project's own effective settings instead of one set of values (remediating drift).
    effective: bool = False
    # With ``effective``: only settings these assignments decide (automatic remediation).
    assignments: tuple[int, ...] = ()
    # Synchronize these tag keys to project topics, as ``key:value`` (tags and topics).
    topic_keys: tuple[str, ...] = ()

    def document(self) -> dict[str, Any]:
        found: dict[str, Any] = {
            "template": self.template,
            "template_id": self.template_id,
            "settings": self.settings,
        }
        if self.effective:
            found["effective"] = True
        if self.assignments:
            found["assignments"] = list(self.assignments)
        if self.topic_keys:
            found["topics"] = {"keys": list(self.topic_keys)}
        return found


EFFECTIVE = Source({}, None, None, effective=True)


def source_of(
    db: Session,
    template: str | None,
    settings: Mapping[str, Any] | None,
    effective: bool = False,
    topic_keys: Sequence[str] | None = None,
) -> Source:
    """The wanted values: a template version (``name@version``), settings, each project's
    effective settings, or topics from tags; exactly one."""
    given = (template is not None) + (settings is not None) + effective + (topic_keys is not None)
    if given != 1:
        raise ChangeSetRefused(
            "Give exactly one of a template version, settings, each project's effective "
            "settings or tag keys for topics"
        )
    if effective:
        return EFFECTIVE
    if topic_keys is not None:
        keys = tuple(sorted(set(topic_keys)))
        if not keys or not all(TAG_NAME.fullmatch(key) for key in keys):
            raise ChangeSetRefused("Name at least one tag key, in lowercase tag key form")
        return Source({}, None, None, topic_keys=keys)
    if template is not None:
        try:
            name, version = split_extends(template)
        except ValueError:
            raise ChangeSetRefused("A template is named as name@version") from None
        row = find_version(db, name, version)
        if row is None:
            raise ChangeSetRefused(f"Template {template} does not exist")
        return Source(chain_settings(db, row), f"{row.name}@{row.version}", row.id)
    flat = flatten(parse_settings(dict(settings or {}), "settings"))
    if not flat:
        raise ChangeSetRefused("Name at least one setting")
    return Source(flat, None, None)


def _stored_source(db: Session, document: Mapping[str, Any]) -> Source:
    source = document["source"]
    if source.get("effective"):
        return Source(
            {}, None, None, effective=True, assignments=tuple(source.get("assignments", ()))
        )
    if source.get("topics"):
        return Source({}, None, None, topic_keys=tuple(source["topics"]["keys"]))
    if source["template_id"] is not None:
        row = db.get(ConfigurationTemplate, source["template_id"])
        if row is not None:  # versions are immutable and never deleted
            return Source(chain_settings(db, row), source["template"], row.id)
    return Source(dict(source["settings"]), None, None)


# --- checks ---


def _warnings(key: str, before: Any, after: Any) -> list[dict[str, str]]:
    found: list[tuple[WarningCode, str]] = []
    if key == "visibility":
        if _EXPOSURE.get(after, 0) > _EXPOSURE.get(before, 0):
            found.append(
                (
                    "weakens_protection",
                    f"Widens visibility from {before} to {after}: more people can see the code.",
                )
            )
        else:
            found.append(
                (
                    "removes_access",
                    f"Narrows visibility from {before} to {after}: people who are not members "
                    "lose access.",
                )
            )
    elif key == "merge_requests.min_approvals":
        if after < before:
            found.append(
                ("weakens_protection", f"Lowers required approvals from {before} to {after}.")
            )
        else:
            found.append(
                (
                    "disrupts_work",
                    f"Raises required approvals from {before} to {after}: open merge requests "
                    "need more approvals before they merge.",
                )
            )
    elif key == "security_features":
        if "secret_scanning_push_protection" in set(after) - set(before):
            found.append(("disrupts_work", "Push protection blocks pushes that contain secrets."))
    elif key in _BOOLEAN_WORDING:
        weaker, stricter = _BOOLEAN_WORDING[key]
        found.append(("weakens_protection", weaker) if before else ("disrupts_work", stricter))
    return [{"code": code, "text": text} for code, text in found]


_BOOLEAN_WORDING: dict[str, tuple[str, str]] = {
    "default_branch_protected": (
        "Removes protection from the default branch.",
        "Protecting the default branch stops direct pushes to it.",
    ),
    "merge_requests.forbid_author_approval": (
        "Lets authors approve their own merge requests.",
        "Authors can no longer approve their own merge requests.",
    ),
    "merge_requests.forbid_committer_approval": (
        "Lets people approve merge requests they committed to.",
        "People can no longer approve merge requests they committed to.",
    ),
    "merge_requests.reset_approvals_on_push": (
        "Approvals are no longer reset when commits are pushed.",
        "Pushing to a merge request resets its approvals.",
    ),
    "merge_requests.forbid_override": (
        "Lets merge requests change their approval rules.",
        "Merge requests can no longer change their approval rules.",
    ),
    "deployments.forbid_pipeline_triggerer_approval": (
        "Lets whoever started a pipeline, such as a tag's creator, approve its deployments.",
        "Whoever starts a pipeline, such as a tag's creator, can no longer approve its "
        "deployments: someone else must approve each release.",
    ),
}


@dataclass(frozen=True)
class _Baseline:
    name: str
    rule: Rule


def _baselines(db: Session, instance: GitLabInstance, project: GitLabProject) -> list[_Baseline]:
    found = []
    for resolved in resolve_project(db, instance, project).policies:
        policy = db.get(Policy, resolved.policy_id)
        if policy is None:
            continue
        for rule in parse_policy(policy.source_yaml).document.spec.rules:
            found.append(_Baseline(f"{policy.name}@{policy.version}", rule))
    return found


def _conflict(baselines: Sequence[_Baseline], key: str, after: Any) -> str | None:
    """Why a baseline in force would fail with ``after``, or None."""
    for baseline in baselines:
        rule = baseline.rule
        where = f"Baseline {baseline.name} rule {rule.id}"
        if (
            key == "visibility"
            and isinstance(rule, ProjectVisibilityRule)
            and after not in rule.allowed
        ):
            return f"{where} allows only {', '.join(rule.allowed)} visibility."
        if (
            key == "default_branch_protected"
            and isinstance(rule, DefaultBranchProtectedRule)
            and rule.required
            and after is False
        ):
            return f"{where} requires the default branch to be protected."
        if (
            key == "deployments.forbid_pipeline_triggerer_approval"
            and isinstance(rule, DeploymentTriggererApprovalRule)
            and after is False
        ):
            return f"{where} forbids the pipeline's starter approving its deployments."
        if isinstance(rule, MergeRequestApprovalsRule) and key.startswith("merge_requests."):
            field = key.removeprefix("merge_requests.")
            if field == "min_approvals" and after < rule.min_approvals:
                return f"{where} requires at least {rule.min_approvals} approvals."
            if field != "min_approvals" and getattr(rule, field, False) and after is False:
                return f"{where} requires {field.replace('_', ' ')}."
    return None


# --- building ---


def _skip(key: str, code: str, reason: str) -> dict[str, str]:
    return {"key": key, "code": code, "reason": reason}


def _target(
    db: Session,
    instance: GitLabInstance,
    project: GitLabProject,
    source: Source,
    collections: Mapping[str, CollectionContext],
    capabilities: Mapping[str, CapabilityStatus],
) -> dict[str, Any] | None:
    """One project's changes, skips and unchanged settings; None when the source is the
    effective settings and no template sets anything for the project."""
    wanted_values: Mapping[str, Any] = source.settings
    decided: dict[str, dict[str, Any]] = {}
    if source.effective:
        in_force = [
            setting
            for setting in effective_settings(db, instance, project).settings
            if not source.assignments or setting.decided_by.assignment_id in source.assignments
        ]
        if not in_force:
            return None
        wanted_values = {setting.key: setting.value for setting in in_force}
        decided = {
            setting.key: {
                "template": f"{setting.decided_by.template}@{setting.decided_by.version}",
                "origin": setting.decided_by.origin,
                "assignment_id": setting.decided_by.assignment_id,
                "assigned_to": setting.decided_by.assigned_to.path,
            }
            for setting in in_force
        }
    ancestry = resolve_project(db, instance, project).ancestry
    judge = judge_for(db, instance, project, collections, capabilities, ancestry)
    waivers = open_for_project(db, project.id)
    baselines = _baselines(db, instance, project)
    changes: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    unchanged: list[str] = []
    for key, after in wanted_values.items():
        support, note = SUPPORT[key][instance.provider]
        if key == "labels_consistent":
            skipped.append(
                _skip(
                    key, "label_plans", "Labels are changed through label plans, not change sets."
                )
            )
            continue
        if support == "unsupported":
            skipped.append(_skip(key, "unsupported", note or "The provider has no such setting."))
            continue
        current = judge.current(key, after)
        if current.value is None:
            code = "unsupported" if current.outcome == "unsupported" else "unknown"
            skipped.append(_skip(key, code, current.reason))
            continue
        before = current.value
        wanted = after
        if key == "security_features":
            before, wanted = sorted(before), sorted(after)
        if before == wanted:
            unchanged.append(key)
            continue
        waiver = waivers.get(key)
        if waiver is not None and waiver.state == "approved":
            skipped.append(
                _skip(
                    key,
                    "excepted",
                    "An approved template exception lets this project differ until "
                    f"{waiver.expires_at:%Y-%m-%d}.",
                )
            )
            continue
        conflict = _conflict(baselines, key, wanted)
        if conflict is not None:
            skipped.append(_skip(key, "conflicts_with_baseline", conflict))
            continue
        change: dict[str, Any] = {
            "key": key,
            "before": before,
            "after": wanted,
            "observed_at": current.observed_at.isoformat() if current.observed_at else None,
            "warnings": _warnings(key, before, wanted),
        }
        if key in decided:
            change["decided_by"] = decided[key]
        changes.append(change)
    return {
        "project": {
            "id": project.id,
            "gitlab_id": project.gitlab_id,
            "path": project.path_with_namespace,
        },
        "changes": changes,
        "skipped": skipped,
        "unchanged": unchanged,
    }


TOPICS = "topics"


def topic_of(key: str, value: str) -> str:
    return f"{key}:{value}"


def _topics_target(
    instance: GitLabInstance, project: GitLabProject, keys: Sequence[str], tags: TagIndex
) -> dict[str, Any]:
    """The project's topics with those of ``keys`` made to match its tags, other topics kept."""
    target: dict[str, Any] = {
        "project": {
            "id": project.id,
            "gitlab_id": project.gitlab_id,
            "path": project.path_with_namespace,
        },
        "changes": [],
        "skipped": [],
        "unchanged": [],
    }
    if instance.provider != "gitlab":
        target["skipped"].append(
            _skip(TOPICS, "unsupported", "Tags are synchronized to GitLab topics only so far.")
        )
        return target
    if project.topics is None:
        target["skipped"].append(
            _skip(
                TOPICS,
                "unknown",
                "The project's topics have not been collected yet; run project discovery.",
            )
        )
        return target
    managed = set(keys)
    wanted_tags = {
        topic_of(tag.key, tag.value) for tag in tags.of_project(project.id) if tag.key in managed
    }
    kept = {topic for topic in project.topics if topic.split(":", 1)[0] not in managed}
    before = sorted(project.topics)
    after = sorted(kept | wanted_tags)
    if before == after:
        target["unchanged"].append(TOPICS)
    else:
        target["changes"].append(
            {
                "key": TOPICS,
                "before": before,
                "after": after,
                "observed_at": project.last_seen_at.isoformat(),
                "warnings": [],
            }
        )
    return target


def build(
    db: Session, instance: GitLabInstance, selection: Selection, source: Source
) -> dict[str, Any]:
    """The change set document for today's evidence."""
    if selection.empty():
        raise ChangeSetRefused("Choose the projects: give at least one selection criterion")
    collections = collection_contexts(db, instance.id, settings_of(instance))
    capabilities = capability_statuses(db, instance.id)
    projects = select_projects(db, instance, selection, collections, capabilities)
    if not projects:
        raise NothingToChange("No monitored, active project matches the selection")
    if len(projects) > MAX_TARGETS:
        raise ChangeSetRefused(
            f"The selection matches {len(projects)} projects; a change set holds at most "
            f"{MAX_TARGETS}. Narrow it."
        )
    if source.topic_keys:
        tags = tag_index(db, instance.id)
        targets = [_topics_target(instance, p, source.topic_keys, tags) for p in projects]
    else:
        built = (
            _target(db, instance, project, source, collections, capabilities)
            for project in projects
        )
        targets = [target for target in built if target is not None]
    if not targets:
        raise NothingToChange("No template sets anything for the selected projects")
    return {
        "format": FORMAT,
        "connection": {"id": instance.id, "provider": instance.provider},
        "selection": selection.model_dump(mode="json"),
        "source": source.document(),
        "targets": targets,
    }


@dataclass(frozen=True)
class Totals:
    targets: int
    changes: int
    skipped: int
    sensitive: bool


def totals(document: Mapping[str, Any]) -> Totals:
    targets = document["targets"]
    changes = [change for target in targets for change in target["changes"]]
    return Totals(
        targets=len(targets),
        changes=len(changes),
        skipped=sum(len(target["skipped"]) for target in targets),
        sensitive=any(
            warning["code"] in SENSITIVE for change in changes for warning in change["warnings"]
        ),
    )


def fingerprint(document: Mapping[str, Any]) -> str:
    """What approval relies on: targets, values and skips, without evidence times or wording."""
    shape = [
        [
            target["project"]["id"],
            [[c["key"], c["before"], c["after"]] for c in target["changes"]],
            [[s["key"], s["code"]] for s in target["skipped"]],
            target["unchanged"],
        ]
        for target in document["targets"]
    ]
    return json.dumps(shape, sort_keys=True, separators=(",", ":"))


# --- lifecycle ---


def effective_state() -> ColumnElement[str]:
    """The state, counting a past ``expires_at`` as expired, for queries."""
    return case(
        (
            and_(
                TemplateChangeSet.state.in_(("proposed", "approved")),
                TemplateChangeSet.expires_at <= func.now(),
            ),
            "expired",
        ),
        else_=TemplateChangeSet.state,
    )


def current_state(db: Session, row: TemplateChangeSet) -> str:
    expired = db.execute(select(func.now() >= row.expires_at)).scalar_one()
    if row.state in ("proposed", "approved") and expired:
        return "expired"
    return row.state


def expire_due(db: Session) -> list[int]:
    """Mark proposed and approved sets past ``expires_at`` as expired."""
    ids = list(
        db.scalars(
            update(TemplateChangeSet)
            .where(
                TemplateChangeSet.state.in_(("proposed", "approved")),
                TemplateChangeSet.expires_at <= func.now(),
            )
            .values(state="expired", ended_at=func.now())
            .returning(TemplateChangeSet.id)
            .execution_options(synchronize_session=False)
        )
    )
    for change_set_id in ids:
        audit.record(
            db,
            actor=SYSTEM_ACTOR,
            action="template_change_set.expired",
            resource_type="template_change_set",
            resource_id=str(change_set_id),
            details={"change_set_id": change_set_id},
        )
    return ids


def _details(row: TemplateChangeSet, **extra: Any) -> dict[str, Any]:
    return {
        "change_set_id": row.id,
        "connection_id": row.instance_id,
        "digest": row.digest,
        "state": row.state,
        "targets": row.targets,
        "changes": row.changes,
        "sensitive": row.sensitive,
        **extra,
    }


def create(
    db: Session,
    instance: GitLabInstance,
    title: str,
    selection: Selection,
    source: Source,
    hours: int,
    user_id: int | None,
    actor: str,
) -> TemplateChangeSet:
    """Build and store a proposed set. ``user_id`` is None when Baluarte proposes it itself."""
    if not 1 <= hours <= MAX_CHANGE_SET_HOURS:
        raise ChangeSetRefused(f"A change set lasts 1 to {MAX_CHANGE_SET_HOURS} hours")
    expire_due(db)
    document = build(db, instance, selection, source)
    counted = totals(document)
    if counted.changes == 0:
        raise NothingToChange(
            "Nothing would change: every selected project already has these values, or each "
            "setting is skipped"
        )
    now: datetime = db.execute(select(func.now())).scalar_one()
    row = TemplateChangeSet(
        workspace_id=workspace_of(db, instance.id),
        instance_id=instance.id,
        title=title,
        document=document,
        digest=details_digest(document),
        targets=counted.targets,
        changes=counted.changes,
        skipped=counted.skipped,
        sensitive=counted.sensitive,
        created_by_user_id=user_id,
        created_by_system=user_id is None,
        created_at=now,
        expires_at=now + timedelta(hours=hours),
    )
    db.add(row)
    db.flush()
    audit.record(
        db,
        actor=actor,
        action="template_change_set.created",
        resource_type="template_change_set",
        resource_id=str(row.id),
        details=_details(row, source=source.template, skipped=row.skipped),
    )
    return row


def stale_reason(db: Session, row: TemplateChangeSet) -> str | None:
    """Why the set no longer matches today's evidence, or None."""
    instance = db.get(GitLabInstance, row.instance_id)
    if instance is None or instance.removed_at is not None:
        return "The connection has been removed."
    try:
        today = build(
            db,
            instance,
            Selection.model_validate(row.document["selection"]),
            _stored_source(db, row.document),
        )
    except (NothingToChange, ChangeSetRefused) as exc:
        return f"Built again from today's evidence: {exc}."
    if fingerprint(today) != fingerprint(row.document):
        return (
            "Built again from today's evidence, the targets, values or skips differ. Create a "
            "new change set."
        )
    return None


def approve(
    db: Session, row: TemplateChangeSet, digest: str, user_id: int, actor: str, note: str | None
) -> TemplateChangeSet:
    """Approve the reviewed digest. A sensitive set's first approval leaves it proposed."""
    state = current_state(db, row)
    if state != "proposed":
        raise ChangeSetConflict(f"Only a proposed change set can be approved; this one is {state}")
    if digest != row.digest:
        raise ChangeSetConflict("The digest is not this change set's; review it again")
    if own_decision_refused(row.created_by_user_id, user_id):
        raise PermissionError("A change set must be approved by someone other than its creator")
    # Development mode relaxes only the creator's own decisions, never a repeat approver's.
    creator_in_development = development_mode() and user_id == row.created_by_user_id
    if row.first_approved_by_user_id == user_id and not creator_in_development:
        raise PermissionError("A sensitive change set needs its second approval from someone else")
    reason = stale_reason(db, row)
    if reason is not None:
        raise ChangeSetStale(reason)
    now = db.execute(select(func.now())).scalar_one()
    own = user_id == row.created_by_user_id
    if row.sensitive and row.first_approved_at is None:
        row.first_approved_by_user_id = user_id
        row.first_approved_at = now
        row.first_approval_note = note
        row.development_decision = row.development_decision or own
        action = "template_change_set.first_approval"
    else:
        row.state = "approved"
        row.decided_by_user_id = user_id
        row.decided_at = now
        row.decision_note = note
        row.approved_digest = row.digest
        row.development_decision = row.development_decision or own
        action = "template_change_set.approved"
    db.flush()
    audit.record(
        db,
        actor=actor,
        action=action,
        resource_type="template_change_set",
        resource_id=str(row.id),
        details=_details(row, development_decision=row.development_decision),
    )
    return row


def reject(
    db: Session, row: TemplateChangeSet, user_id: int, actor: str, note: str
) -> TemplateChangeSet:
    state = current_state(db, row)
    if state != "proposed":
        raise ChangeSetConflict(f"Only a proposed change set can be rejected; this one is {state}")
    if own_decision_refused(row.created_by_user_id, user_id):
        raise PermissionError("A change set must be rejected by someone other than its creator")
    now = db.execute(select(func.now())).scalar_one()
    row.state = "rejected"
    row.decided_by_user_id = user_id
    row.decided_at = now
    row.decision_note = note
    row.development_decision = row.development_decision or user_id == row.created_by_user_id
    db.flush()
    audit.record(
        db,
        actor=actor,
        action="template_change_set.rejected",
        resource_type="template_change_set",
        resource_id=str(row.id),
        details=_details(row),
    )
    return row


def withdraw(db: Session, row: TemplateChangeSet, user_id: int, actor: str) -> TemplateChangeSet:
    state = current_state(db, row)
    if state != "proposed":
        raise ChangeSetConflict(f"Only a proposed change set can be withdrawn; this one is {state}")
    if user_id != row.created_by_user_id:
        raise PermissionError("Only its creator can withdraw a change set")
    now = db.execute(select(func.now())).scalar_one()
    row.state = "withdrawn"
    row.ended_at = now
    db.flush()
    audit.record(
        db,
        actor=actor,
        action="template_change_set.withdrawn",
        resource_type="template_change_set",
        resource_id=str(row.id),
        details=_details(row),
    )
    return row


def locked(db: Session, change_set_id: int) -> TemplateChangeSet | None:
    return db.get(TemplateChangeSet, change_set_id, with_for_update=True, populate_existing=True)
