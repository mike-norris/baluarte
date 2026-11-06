"""The manager overview: posture, consistency and effort per child group or tag value.

Rows are the top-level groups; with ``group``, that group's child groups (and a row for the
projects directly in it, when it has any); with ``by_tag``, one row per value of that tag key.
``tag`` filters every row to the projects with that ``key:value``. A row covers the monitored
projects under it; excluded projects are only counted.

Figures and where they come from:

- ``findings_open``: open findings by severity. With ``as_of``, the failing results of the
  evaluation run in force then (the open findings of that moment, apart from excepted ones).
- ``controls_not_met``: per framework, controls with a mapped rule type that failed for one of
  the row's projects in that run, unless the organization marked the control not applicable.
- ``changes``: merge requests and direct pushes in the last 30 and 90 days (before ``as_of``):
  how many passed the review check (an approval by someone other than the author, no direct
  push), how many were pushed directly, and how many could not be judged. ``rate`` is reviewed
  over total, null without changes.
- ``label_drift``: drift items on the row's groups and projects; null without a taxonomy.
- ``time_30d``: time spent and estimated in the last 30 days; null before time was collected.

Time and tags have no history, so they are today's even with ``as_of``; the response lists them
in ``current_only``. Groups are today's tree.
"""

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte import tags as tag_index
from baluarte.changes import assess
from baluarte.compliance.catalogue import FRAMEWORK_IDS
from baluarte.controls.posture import effective
from baluarte.db.models import (
    ChangeRecord,
    Evaluation,
    EvaluationRun,
    Finding,
    Observation,
    TimeEntry,
)
from baluarte.evidence.as_of import run_at
from baluarte.hierarchy import GroupTree
from baluarte.labels import drift as label_drift
from baluarte.scope.exclusions import Coverage, coverage
from baluarte.timetracking import summary as time_summary

SEVERITIES = ("critical", "high", "medium", "low")
CURRENT_ONLY = ["time", "tags", "groups"]


class OverviewRefused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class RowSet:
    """One overview row: what it is and the projects (row IDs) and groups it covers."""

    key: dict[str, Any]
    projects: set[int]
    excluded: int
    groups: set[int] = field(default_factory=set)


def _excluded(scope: Coverage, tree: GroupTree, row_id: int) -> bool:
    node = tree.projects[row_id]
    return (
        scope.for_project(
            node.gitlab_id,
            node.group_gitlab_id or 0,
            None if node.group_gitlab_id is not None else "user",
        )
        is not None
    )


def monitored(tree: GroupTree, scope: Coverage) -> set[int]:
    """Active, unarchived projects in monitoring scope."""
    return {
        row_id
        for row_id, node in tree.projects.items()
        if node.state == "active" and not node.archived and not _excluded(scope, tree, row_id)
    }


def row_sets(
    tree: GroupTree,
    scope: Coverage,
    index: tag_index.TagIndex,
    group: str | None,
    tag: tuple[str, str] | None,
    by_tag: str | None,
) -> tuple[dict[str, Any], list[RowSet]]:
    """The parent the rows belong to, and each row's projects."""
    included = monitored(tree, scope)
    active = {r for r, n in tree.projects.items() if n.state == "active" and not n.archived}
    if tag is not None:
        tagged = index.projects_with([tag])
        included &= tagged
        active &= tagged
    parent: dict[str, Any] = {"type": "instance"}
    group_id: int | None = None
    if group is not None:
        group_id = next((g for g, path in tree.paths.items() if path == group), None)
        if group_id is None:
            raise OverviewRefused("unknown_group", f"No group {group} in the inventory")
        parent = {"type": "group", "gitlab_id": group_id, "path": group}
    if tag is not None:
        parent = {**parent, "tag": {"key": tag[0], "value": tag[1]}}

    def make(key: dict[str, Any], projects: set[int], groups: set[int]) -> RowSet:
        return RowSet(key, projects & included, len((projects & active) - included), groups)

    if by_tag is not None:
        scope_projects = (
            tree.projects_under([group_id]) if group_id is not None else set(tree.projects)
        )
        values: dict[str, set[int]] = defaultdict(set)
        for row_id in scope_projects:
            applied = index.by_project.get(row_id, {}).get(by_tag)
            values[applied.value if applied else ""].add(row_id)
        rows = [
            make({"type": "tag", "key": by_tag, "value": value or None}, projects, set())
            for value, projects in sorted(values.items(), key=lambda item: (not item[0], item[0]))
        ]
        return {**parent, "by_tag": by_tag}, rows
    rows = []
    children = tree.children.get(group_id, []) if group_id is not None else tree.top_level()
    for child in children:
        groups = tree.descendants(child)
        rows.append(
            make(
                {"type": "group", "gitlab_id": child, "path": tree.paths[child]},
                tree.projects_under([child]),
                groups,
            )
        )
    if group_id is not None:
        direct = {r for r, n in tree.projects.items() if n.group_gitlab_id == group_id}
        if direct:
            rows.append(
                make(
                    {"type": "group", "gitlab_id": group_id, "path": group, "direct_only": True},
                    direct,
                    {group_id},
                )
            )
    return parent, rows


def _findings(
    db: Session, run: EvaluationRun | None, live: bool, projects: set[int]
) -> dict[int, Counter[str]]:
    """Open findings by project and severity: live from the findings, else the run's failures."""
    found: dict[int, Counter[str]] = defaultdict(Counter)
    if not projects:
        return found
    if live:
        rows = db.execute(
            select(Finding.project_id, Finding.severity, func.count())
            .where(Finding.state == "open", Finding.project_id.in_(projects))
            .group_by(Finding.project_id, Finding.severity)
        )
    elif run is not None:
        rows = db.execute(
            select(Evaluation.project_id, Evaluation.severity, func.count())
            .where(
                Evaluation.evaluation_run_id == run.id,
                Evaluation.outcome == "fail",
                Evaluation.project_id.in_(projects),
            )
            .group_by(Evaluation.project_id, Evaluation.severity)
        )
    else:
        return found
    for project_id, severity, count in rows:
        found[project_id][severity] += count
    return found


def _failing_types(
    db: Session, run: EvaluationRun | None, projects: set[int]
) -> dict[int, set[str]]:
    found: dict[int, set[str]] = defaultdict(set)
    if run is None or not projects:
        return found
    for project_id, rule_type in db.execute(
        select(Evaluation.project_id, Evaluation.rule_type)
        .where(
            Evaluation.evaluation_run_id == run.id,
            Evaluation.outcome == "fail",
            Evaluation.project_id.in_(projects),
        )
        .distinct()
    ):
        found[project_id].add(rule_type)
    return found


def controls_not_met(
    failing: set[str], controls: Sequence[tuple[str, str, set[str]]]
) -> dict[str, int]:
    """Per framework, controls one of whose rule types is in ``failing``."""
    counts: dict[str, int] = {framework: 0 for framework in FRAMEWORK_IDS}
    for framework, _control, types in controls:
        if types & failing:
            counts[framework] = counts.get(framework, 0) + 1
    return counts


def _mapped_controls(db: Session, as_of: datetime | None) -> list[tuple[str, str, set[str]]]:
    mapping = effective(db, as_of)
    controls = []
    for (framework, control_id), rules in mapping.rules.items():
        change = mapping.status_changes.get((framework, control_id))
        if change is not None and change.kind == "not_applicable":
            continue
        controls.append((framework, control_id, {rule.rule_type for rule in rules}))
    return controls


ReviewState = Literal["pass", "fail", "unknown"]


@dataclass(frozen=True)
class ChangeFact:
    project_id: int
    kind: str
    changed_at: datetime
    review: ReviewState


def change_facts(
    db: Session,
    instance_id: int | None,
    projects: Iterable[int],
    since: datetime,
    until: datetime,
    recorded_by: datetime | None = None,
) -> list[ChangeFact]:
    """Merge requests and direct pushes that landed in [since, until) with their review check.

    A change recorded before review outcomes were kept is judged from its observation here.
    """
    wanted = list(projects)
    if not wanted:
        return []
    statement = select(
        ChangeRecord.project_id,
        ChangeRecord.kind,
        ChangeRecord.changed_at,
        ChangeRecord.review_outcome,
        ChangeRecord.observation_id,
    ).where(
        ChangeRecord.instance_id == (instance_id or 0),
        ChangeRecord.kind.in_(("merge_request", "direct_push")),
        ChangeRecord.changed_at >= since,
        ChangeRecord.changed_at < until,
        ChangeRecord.project_id.in_(wanted),
    )
    if recorded_by is not None:
        statement = statement.where(ChangeRecord.created_at <= recorded_by)
    rows = list(db.execute(statement))
    missing = [row.observation_id for row in rows if row.review_outcome is None]
    payloads: dict[int, Any] = {}
    for start in range(0, len(missing), 1000):
        payloads.update(
            db.execute(
                select(Observation.id, Observation.payload).where(
                    Observation.id.in_(missing[start : start + 1000])
                )
            ).tuples()
        )
    facts = []
    for project_id, kind, changed_at, stored, observation_id in rows:
        if stored is None:
            payload = payloads.get(observation_id)
            stored = assess.review(payload)[0] if isinstance(payload, dict) else "unknown"
        review: ReviewState = (
            "pass" if stored == "pass" else "fail" if stored == "fail" else "unknown"
        )
        facts.append(ChangeFact(project_id, kind, changed_at, review))
    return facts


def change_figures(facts: Iterable[ChangeFact]) -> dict[str, Any]:
    total = reviewed = direct = unknown = 0
    for fact in facts:
        total += 1
        reviewed += fact.review == "pass"
        unknown += fact.review == "unknown"
        direct += fact.kind == "direct_push"
    return {
        "total": total,
        "reviewed": reviewed,
        "direct_pushes": direct,
        "unknown": unknown,
        "rate": round(reviewed / total, 4) if total else None,
    }


def _drift(
    db: Session, instance_id: int | None, as_of: datetime | None
) -> list[label_drift.DriftItem] | None:
    found = label_drift.load(db, instance_id, as_of)
    return None if found.taxonomy is None else found.items


def _time(
    db: Session, instance_id: int | None, projects: set[int], since: datetime, until: datetime
) -> dict[int, int]:
    if not projects:
        return {}
    return dict(
        db.execute(
            select(TimeEntry.project_id, func.sum(TimeEntry.seconds))
            .where(
                TimeEntry.instance_id == (instance_id or 0),
                TimeEntry.removed_at.is_(None),
                TimeEntry.spent_at >= since,
                TimeEntry.spent_at < until,
                TimeEntry.project_id.in_(projects),
            )
            .group_by(TimeEntry.project_id)
        ).tuples()
    )


def overview(
    db: Session,
    instance_id: int | None,
    group: str | None = None,
    tag: tuple[str, str] | None = None,
    by_tag: str | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    tree = GroupTree.load(db, instance_id)
    index = tag_index.index(db, instance_id, tree)
    scope = coverage(db, instance_id, as_of)
    parent, rows = row_sets(tree, scope, index, group, tag, by_tag)
    now: datetime = as_of or db.execute(select(func.now())).scalar_one()
    run = run_at(db, instance_id, as_of)
    every = set().union(*(row.projects for row in rows)) if rows else set()
    findings = _findings(db, run, as_of is None, every)
    failing = _failing_types(db, run, every)
    controls = _mapped_controls(db, as_of)
    facts = change_facts(db, instance_id, every, now - timedelta(days=90), now, as_of)
    drift = _drift(db, instance_id, as_of)
    collected = time_summary.collected_at(db, instance_id)
    spent = _time(db, instance_id, every, now - timedelta(days=30), now) if collected else {}
    estimates = (
        time_summary.estimates_for(db, instance_id, every, now - timedelta(days=30), now)
        if collected
        else {}
    )
    out_rows = []
    for row in rows:
        severities: Counter[str] = Counter()
        failed: set[str] = set()
        for project_id in row.projects:
            severities.update(findings.get(project_id, Counter()))
            failed |= failing.get(project_id, set())
        project_gitlab_ids = {tree.projects[p].gitlab_id for p in row.projects}
        row_facts = [f for f in facts if f.project_id in row.projects]
        drift_count = (
            None
            if drift is None
            else sum(
                1
                for item in drift
                if (item.owner.type == "project" and item.owner.gitlab_id in project_gitlab_ids)
                or (item.owner.type == "group" and item.owner.gitlab_id in row.groups)
            )
        )
        out_rows.append(
            {
                "key": row.key,
                "projects": {"monitored": len(row.projects), "excluded": row.excluded},
                "findings_open": {
                    **{severity: severities.get(severity, 0) for severity in SEVERITIES},
                    "total": sum(severities.get(s, 0) for s in SEVERITIES),
                },
                "controls_not_met": controls_not_met(failed, controls),
                "changes": {
                    "days_30": change_figures(
                        f for f in row_facts if f.changed_at >= now - timedelta(days=30)
                    ),
                    "days_90": change_figures(row_facts),
                },
                "label_drift": drift_count,
                "time_30d": (
                    {
                        "spent_seconds": sum(spent.get(p, 0) for p in row.projects),
                        "estimate_seconds": sum(estimates.get(p, 0) for p in row.projects),
                    }
                    if collected
                    else None
                ),
            }
        )
    return {
        "parent": parent,
        "rows": out_rows,
        "evaluation_run": run,
        "current_only": CURRENT_ONLY,
    }
