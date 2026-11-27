"""Change set records (roadmap M14.6, docs/CONFIGURATION_TEMPLATES.md#records).

What an auditor needs about a bulk change, from rows that are never edited: who requested,
approved and executed it, why, which template version and scope, when, the exact before and
after values and each target's result, and the controls each setting supports. The change set
page shows it; evidence packages export it for the sets active in their period.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from baluarte.controls.posture import Effective, effective
from baluarte.db.models import TemplateChangeItem, TemplateChangeRun, TemplateChangeSet
from baluarte.templates.schema import RULE_TYPE


@dataclass(frozen=True)
class ControlLink:
    framework: str
    control_id: str
    # The settings of the set whose rule type supports the control.
    settings: tuple[str, ...]


def settings_of(document: dict[str, Any]) -> list[str]:
    """The settings the set changes, in first-seen order."""
    seen: dict[str, None] = {}
    for target in document.get("targets", []):
        for change in target.get("changes", []):
            seen.setdefault(change["key"], None)
    return list(seen)


def control_links(mapping: Effective, settings: Sequence[str]) -> list[ControlLink]:
    """The controls the settings support through the rule type that judges each, in the order
    first found. Topics and other settings no rule type judges link to none."""
    linked: dict[tuple[str, str], list[str]] = defaultdict(list)
    order: list[tuple[str, str]] = []
    for key in settings:
        rule_type = RULE_TYPE.get(key)
        if rule_type is None:
            continue
        for control in mapping.controls_for(rule_type):
            if control not in linked:
                order.append(control)
            linked[control].append(key)
    return [ControlLink(f, c, tuple(linked[(f, c)])) for f, c in order]


def controls_of(db: Session, row: TemplateChangeSet) -> list[ControlLink]:
    return control_links(effective(db), settings_of(row.document))


def runs_of(db: Session, change_set_ids: Sequence[int]) -> dict[int, list[TemplateChangeRun]]:
    found: dict[int, list[TemplateChangeRun]] = defaultdict(list)
    if not change_set_ids:
        return found
    for run in db.scalars(
        select(TemplateChangeRun)
        .where(TemplateChangeRun.change_set_id.in_(change_set_ids))
        .order_by(TemplateChangeRun.id)
    ):
        found[run.change_set_id].append(run)
    return found


def active_between(db: Session, start: datetime, end: datetime) -> list[TemplateChangeSet]:
    """Sets created, decided, applied, finished, cancelled or rolled back in [start, end)."""
    columns = (
        TemplateChangeSet.created_at,
        TemplateChangeSet.first_approved_at,
        TemplateChangeSet.decided_at,
        TemplateChangeSet.apply_started_at,
        TemplateChangeSet.finished_at,
        TemplateChangeSet.cancelled_at,
        TemplateChangeSet.rollback_started_at,
        TemplateChangeSet.ended_at,
    )
    ran = select(TemplateChangeRun.change_set_id).where(
        TemplateChangeRun.started_at >= start, TemplateChangeRun.started_at < end
    )
    return list(
        db.scalars(
            select(TemplateChangeSet)
            .where(
                or_(
                    *((column >= start) & (column < end) for column in columns),
                    TemplateChangeSet.id.in_(ran),
                )
            )
            .order_by(TemplateChangeSet.id)
        )
    )


def items_of(db: Session, change_set_ids: Sequence[int]) -> list[TemplateChangeItem]:
    if not change_set_ids:
        return []
    return list(
        db.scalars(
            select(TemplateChangeItem)
            .where(TemplateChangeItem.change_set_id.in_(change_set_ids))
            .order_by(TemplateChangeItem.change_set_id, TemplateChangeItem.position)
        )
    )


def template_versions(document: dict[str, Any]) -> list[str]:
    """The template versions the set's values came from: its source template, or each
    change's deciding template when it remediates drift."""
    source = document.get("source", {})
    found: dict[str, None] = {}
    if source.get("template"):
        found[source["template"]] = None
    for target in document.get("targets", []):
        for change in target.get("changes", []):
            decided = change.get("decided_by") or {}
            if decided.get("template"):
                found.setdefault(decided["template"], None)
    return list(found)
