"""Which issues are auditable, why, and in which category (ADR-0015).

- A **rule** matches an issue that meets all of its non-empty criteria: any of its labels, one of
  its projects, one of its issue types.
- A **decision** on one issue either includes it (whatever the rules say, optionally with a
  category) or excludes one the rules select. Excluded issues are left out of the register by
  default and listed, with the reason, on request.
- The **category** is the inclusion's, when it names one; otherwise that of the oldest matching
  rule; otherwise ``engineering``.

Everything here is computed in SQL, so the register filters, sorts and pages in the database.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    case,
    exists,
    false,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.orm import Session

from baluarte.db.models import (
    REGISTER_CATEGORIES,
    GitLabProject,
    RegisterDecision,
    RegisterIssue,
    RegisterRule,
)

DEFAULT_CATEGORY = "engineering"
# An issue, its project's path, its category and whether it is excluded.
RegisterRows = Select[RegisterIssue, str, str, bool]
CATEGORIES = REGISTER_CATEGORIES


def active_rules(db: Session, workspace_id: int) -> list[RegisterRule]:
    """The workspace's active selection rules, oldest first."""
    return list(
        db.scalars(
            select(RegisterRule)
            .where(RegisterRule.workspace_id == workspace_id, RegisterRule.ended_at.is_(None))
            .order_by(RegisterRule.id)
        )
    )


def rule_clause(rule: RegisterRule) -> ColumnElement[bool]:
    """Whether an issue meets every non-empty criterion of ``rule``."""
    parts: list[ColumnElement[bool]] = []
    if rule.labels:
        parts.append(RegisterIssue.labels.overlap(list(rule.labels)))
    if rule.project_ids:
        parts.append(RegisterIssue.project_id.in_(list(rule.project_ids)))
    if rule.issue_types:
        parts.append(RegisterIssue.issue_type.in_(list(rule.issue_types)))
    return and_(*parts) if parts else false()


def matches(rule: RegisterRule, issue: RegisterIssue) -> bool:
    """``rule_clause`` in Python, for one issue already read."""
    if rule.labels and not set(rule.labels) & set(issue.labels):
        return False
    if rule.project_ids and issue.project_id not in rule.project_ids:
        return False
    return not (rule.issue_types and issue.issue_type not in rule.issue_types)


def _decision(workspace_id: int, kind: str) -> Any:
    return (
        select(RegisterDecision)
        .where(
            RegisterDecision.workspace_id == workspace_id,
            RegisterDecision.ended_at.is_(None),
            RegisterDecision.decision == kind,
            RegisterDecision.project_id == RegisterIssue.project_id,
            RegisterDecision.issue_iid == RegisterIssue.issue_iid,
        )
        .correlate(RegisterIssue)
    )


def included(workspace_id: int) -> ColumnElement[bool]:
    return exists(_decision(workspace_id, "include").with_only_columns(literal(1)))


def excluded(workspace_id: int) -> ColumnElement[bool]:
    return exists(_decision(workspace_id, "exclude").with_only_columns(literal(1)))


def selected(rules: Sequence[RegisterRule]) -> ColumnElement[bool]:
    return or_(*(rule_clause(rule) for rule in rules)) if rules else false()


def category(workspace_id: int, rules: Sequence[RegisterRule]) -> ColumnElement[str]:
    """The issue's category, as an SQL expression."""
    chosen = (
        _decision(workspace_id, "include")
        .where(RegisterDecision.category.is_not(None))
        .with_only_columns(RegisterDecision.category)
        .scalar_subquery()
    )
    whens: list[tuple[ColumnElement[bool], Any]] = [(chosen.is_not(None), chosen)]
    whens += [(rule_clause(rule), literal(rule.category)) for rule in rules]
    return case(*whens, else_=literal(DEFAULT_CATEGORY))


@dataclass(frozen=True)
class Filters:
    instance_id: int | None = None
    project_id: int | None = None
    category: str | None = None
    rule_id: int | None = None
    state: str | None = None
    issue_type: str | None = None
    label: str | None = None
    assignee: str | None = None
    text: str | None = None
    # Issues open at any time in the period: opened by its end, not closed before its start.
    period_start: datetime | None = None
    period_end: datetime | None = None
    include_excluded: bool = False


def register_query(
    workspace_id: int, rules: Sequence[RegisterRule], filters: Filters
) -> RegisterRows:
    """Auditable issues with their project path, category and whether excluded."""
    is_excluded = excluded(workspace_id)
    is_included = included(workspace_id)
    category_of = category(workspace_id, rules)
    statement: RegisterRows = (
        select(
            RegisterIssue,
            GitLabProject.path_with_namespace,
            category_of.label("category"),
            is_excluded.label("excluded"),
        )
        .join(GitLabProject, GitLabProject.id == RegisterIssue.project_id)
        .where(or_(selected(rules), is_included))
    )
    if not filters.include_excluded:
        statement = statement.where(~is_excluded)
    if filters.instance_id is not None:
        statement = statement.where(RegisterIssue.instance_id == filters.instance_id)
    if filters.project_id is not None:
        statement = statement.where(RegisterIssue.project_id == filters.project_id)
    if filters.category is not None:
        statement = statement.where(category_of == filters.category)
    if filters.rule_id is not None:
        rule = next((r for r in rules if r.id == filters.rule_id), None)
        statement = statement.where(rule_clause(rule) if rule is not None else false())
    if filters.state is not None:
        statement = statement.where(RegisterIssue.state == filters.state)
    if filters.issue_type is not None:
        statement = statement.where(RegisterIssue.issue_type == filters.issue_type)
    if filters.label is not None:
        statement = statement.where(RegisterIssue.labels.contains([filters.label]))
    if filters.assignee is not None:
        statement = statement.where(RegisterIssue.assignees.contains([filters.assignee]))
    if filters.text:
        pattern = "%" + filters.text.replace("\\", "\\\\").replace("%", "\\%") + "%"
        statement = statement.where(RegisterIssue.title.ilike(pattern, escape="\\"))
    if filters.period_end is not None:
        statement = statement.where(
            or_(RegisterIssue.opened_at.is_(None), RegisterIssue.opened_at <= filters.period_end)
        )
    if filters.period_start is not None:
        statement = statement.where(
            or_(RegisterIssue.closed_at.is_(None), RegisterIssue.closed_at >= filters.period_start)
        )
    return statement


def count(db: Session, statement: RegisterRows) -> int:
    return db.execute(select(func.count()).select_from(statement.subquery())).scalar_one()


@dataclass(frozen=True)
class Reason:
    """One reason an issue is in the register, or out of it."""

    kind: str  # "rule", "included" or "excluded"
    rule_id: int | None = None
    rule_name: str | None = None
    decision_id: int | None = None
    reason: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None


def open_decisions(
    db: Session, workspace_id: int, keys: Sequence[tuple[int, int]]
) -> dict[tuple[int, int], RegisterDecision]:
    """Open decisions on the given (project ID, iid) pairs."""
    if not keys:
        return {}
    rows = db.scalars(
        select(RegisterDecision).where(
            RegisterDecision.workspace_id == workspace_id,
            RegisterDecision.ended_at.is_(None),
            or_(
                *(
                    and_(RegisterDecision.project_id == p, RegisterDecision.issue_iid == i)
                    for p, i in keys
                )
            ),
        )
    )
    return {(row.project_id, row.issue_iid): row for row in rows}


def reasons(
    issue: RegisterIssue, rules: Sequence[RegisterRule], decision: RegisterDecision | None
) -> list[Reason]:
    """Why ``issue`` is in the register (matching rules, an inclusion) or out of it."""
    found = [
        Reason("rule", rule_id=rule.id, rule_name=rule.name)
        for rule in rules
        if matches(rule, issue)
    ]
    if decision is not None:
        kind = "included" if decision.decision == "include" else "excluded"
        found.append(
            Reason(
                kind,
                decision_id=decision.id,
                reason=decision.reason,
                decided_by=decision.decided_by,
                decided_at=decision.decided_at,
            )
        )
    return found
