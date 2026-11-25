"""Verifying applied template changes against collected evidence (roadmap M14.5).

Applying a change already reads the setting back from the provider (``baluarte.templates.execute``).
This is the second, independent check: once discovery has collected the project again, the
evidence the drift view and findings rest on must show the value written. Each applied change
that was not rolled back is, from stored evidence and without calling the provider:

- ``verified``: evidence collected after the write shows the value written;
- ``drift``: evidence collected after the write shows another value; someone or something
  changed it again, or the write did not hold;
- ``awaiting_collection``: no evidence has been collected since the write;
- ``unknown``: the evidence cannot tell (stale or failed collection, missing access).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from baluarte.db.models import GitLabInstance, GitLabProject, TemplateChangeItem
from baluarte.policy.assignments import resolve_project
from baluarte.policy.evaluation.project import capability_statuses, collection_contexts
from baluarte.templates.drift import judge_for
from baluarte.workspaces import settings_of

VerificationStatus = Literal["verified", "drift", "awaiting_collection", "unknown"]


@dataclass(frozen=True)
class Verification:
    item_id: int
    project_path: str
    setting: str
    after: Any
    status: VerificationStatus
    observed: Any
    observed_at: datetime | None
    reason: str


def _same(setting: str, observed: Any, after: Any) -> bool:
    if setting == "security_features":
        return sorted(observed) == sorted(after)
    return bool(observed == after)


def verify(
    db: Session, instance: GitLabInstance, items: Sequence[TemplateChangeItem]
) -> list[Verification]:
    """The evidence check of each applied change that was not rolled back, in item order."""
    applied = [
        item for item in items if item.status == "applied" and item.rollback_status != "rolled_back"
    ]
    if not applied:
        return []
    collections = collection_contexts(db, instance.id, settings_of(instance))
    capabilities = capability_statuses(db, instance.id)
    judges: dict[int, Any] = {}
    found = []
    for item in applied:
        judge = judges.get(item.project_id)
        if judge is None:
            project = db.get(GitLabProject, item.project_id)
            if project is None:
                continue
            ancestry = resolve_project(db, instance, project).ancestry
            judge = judge_for(db, instance, project, collections, capabilities, ancestry)
            judges[item.project_id] = judge
        current = judge.current(item.setting, item.after)
        status: VerificationStatus
        if current.value is None:
            status, reason = "unknown", current.reason
        elif (
            current.observed_at is None
            or item.finished_at is None
            or current.observed_at < item.finished_at
        ):
            status, reason = (
                "awaiting_collection",
                "No evidence has been collected since the write.",
            )
        elif _same(item.setting, current.value, item.after):
            status, reason = (
                "verified",
                "Evidence collected after the write shows the value written.",
            )
        else:
            status, reason = (
                "drift",
                "Evidence collected after the write shows another value: it changed again, or "
                "the write did not hold.",
            )
        found.append(
            Verification(
                item_id=item.id,
                project_path=item.project_path,
                setting=item.setting,
                after=item.after,
                status=status,
                observed=current.value,
                observed_at=current.observed_at,
                reason=reason,
            )
        )
    return found
