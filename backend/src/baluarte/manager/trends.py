"""Manager trends: weekly or monthly points for a group, a tag or the whole instance.

Each point is the state at the end of its bucket:

- ``findings_open``: failing results by severity in the evaluation run in force at the bucket's
  end (the latest successful run finished by then), for the selected projects.
- ``rule_pass_rate``: passing over passing and failing results in that run; null without either.
- ``change_review_rate``: merge requests and direct pushes that landed in the bucket and passed
  the review check, over all of them; null without changes.
- ``time_spent_seconds``: time logged in the bucket (not removed); null before time was collected.

The projects are the ones in monitoring scope and under the group or with the tag today: the
inventory keeps no history of the tree or of tags. Every figure is read with one grouped query
over an index (``ix_evaluation_run_outcome``, ``ix_change_record_project_changed``,
``ix_time_entry_instance_spent``), so the cost grows with the number of buckets and runs, not
with history. At most ``MAX_BUCKETS`` buckets per request.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte import tags as tag_index
from baluarte.db.models import Evaluation, EvaluationRun, TimeEntry
from baluarte.hierarchy import GroupTree
from baluarte.manager import overview
from baluarte.scope.exclusions import coverage
from baluarte.timetracking import summary as time_summary

Bucket = Literal["week", "month"]
MAX_BUCKETS: dict[str, int] = {"week": 105, "month": 36}
DEFAULT_BUCKETS = 12


class TrendsRefused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _start_of(bucket: Bucket, day: date) -> date:
    if bucket == "week":
        return day - timedelta(days=day.weekday())
    return day.replace(day=1)


def _next(bucket: Bucket, day: date) -> date:
    if bucket == "week":
        return day + timedelta(days=7)
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def _previous(bucket: Bucket, day: date, count: int) -> date:
    for _ in range(count):
        if bucket == "week":
            day -= timedelta(days=7)
        else:
            day = date(day.year - (day.month == 1), (day.month - 2) % 12 + 1, 1)
    return day


def buckets(
    bucket: Bucket, start: date | None, end: date | None, today: date
) -> list[tuple[date, date]]:
    """[start, end) of each bucket: from the bucket holding ``start`` to the one holding ``end``."""
    last = _start_of(bucket, end or today)
    first = (
        _start_of(bucket, start)
        if start is not None
        else _previous(bucket, last, DEFAULT_BUCKETS - 1)
    )
    if first > last:
        raise TrendsRefused("invalid_range", "from is after to")
    found: list[tuple[date, date]] = []
    current = first
    while current <= last:
        following = _next(bucket, current)
        found.append((current, following))
        if len(found) > MAX_BUCKETS[bucket]:
            raise TrendsRefused(
                "too_many_buckets", f"At most {MAX_BUCKETS[bucket]} {bucket}s per request"
            )
        current = following
    return found


def _moment(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


@dataclass(frozen=True)
class RunPoint:
    run_id: int
    finished_at: datetime


def _runs(db: Session, instance_id: int | None, ends: list[datetime]) -> list[RunPoint | None]:
    """The run in force at each end: one query over the runs that finished in the range."""
    if not ends:
        return []
    rows = [
        RunPoint(run_id, finished)
        for run_id, finished in db.execute(
            select(EvaluationRun.id, EvaluationRun.finished_at)
            .where(
                EvaluationRun.instance_id == (instance_id or 0),
                EvaluationRun.status == "succeeded",
                EvaluationRun.finished_at <= ends[-1],
            )
            .order_by(EvaluationRun.finished_at, EvaluationRun.id)
        )
        if finished is not None
    ]
    found: list[RunPoint | None] = []
    index, current = 0, None
    for end in ends:
        while index < len(rows) and rows[index].finished_at <= end:
            current = rows[index]
            index += 1
        found.append(current)
    return found


def trends(
    db: Session,
    instance_id: int | None,
    bucket: Bucket,
    start: date | None = None,
    end: date | None = None,
    group: str | None = None,
    tag: tuple[str, str] | None = None,
) -> dict[str, Any]:
    today: date = db.execute(select(func.current_date())).scalar_one()
    spans = buckets(bucket, start, end, today)
    tree = GroupTree.load(db, instance_id)
    index = tag_index.index(db, instance_id, tree)
    projects = overview.monitored(tree, coverage(db, instance_id))
    if group is not None:
        group_id = next((g for g, path in tree.paths.items() if path == group), None)
        if group_id is None:
            raise TrendsRefused("unknown_group", f"No group {group} in the inventory")
        projects &= tree.projects_under([group_id])
    if tag is not None:
        projects &= index.projects_with([tag])
    ends = [_moment(following) for _, following in spans]
    runs = _runs(db, instance_id, ends)
    run_ids = sorted({run.run_id for run in runs if run is not None})
    results: dict[int, Counter[tuple[str, str]]] = defaultdict(Counter)
    if run_ids and projects:
        for run_id, outcome, severity, count in db.execute(
            select(
                Evaluation.evaluation_run_id,
                Evaluation.outcome,
                Evaluation.severity,
                func.count(),
            )
            .where(
                Evaluation.evaluation_run_id.in_(run_ids),
                Evaluation.outcome.in_(("pass", "fail")),
                Evaluation.project_id.in_(projects),
            )
            .group_by(Evaluation.evaluation_run_id, Evaluation.outcome, Evaluation.severity)
        ):
            results[run_id or 0][(outcome, severity)] += count
    first, last = _moment(spans[0][0]), ends[-1]
    facts = overview.change_facts(db, instance_id, projects, first, last)
    changes: dict[int, list[overview.ChangeFact]] = defaultdict(list)
    starts = [_moment(s) for s, _ in spans]
    for fact in facts:
        changes[_bucket_of(starts, fact.changed_at)].append(fact)
    collected = time_summary.collected_at(db, instance_id)
    spent: dict[int, int] = defaultdict(int)
    if collected is not None and projects:
        moment = func.date_trunc(bucket, TimeEntry.spent_at, "UTC")
        for when, seconds in db.execute(
            select(moment, func.sum(TimeEntry.seconds))
            .where(
                TimeEntry.instance_id == (instance_id or 0),
                TimeEntry.removed_at.is_(None),
                TimeEntry.spent_at >= first,
                TimeEntry.spent_at < last,
                TimeEntry.project_id.in_(projects),
            )
            .group_by(moment)
        ):
            spent[_bucket_of(starts, when)] += seconds
    points = []
    for position, ((_, following), run) in enumerate(zip(spans, runs, strict=True)):
        counts = results.get(run.run_id, Counter()) if run is not None else Counter()
        fails = {severity: counts.get(("fail", severity), 0) for severity in overview.SEVERITIES}
        passed = sum(c for (outcome, _), c in counts.items() if outcome == "pass")
        failed = sum(fails.values())
        review = overview.change_figures(changes.get(position, []))
        points.append(
            {
                "end": _moment(following),
                "evaluation_run_id": run.run_id if run is not None else None,
                "findings_open": {**fails, "total": failed} if run is not None else None,
                "rule_pass_rate": (
                    round(passed / (passed + failed), 4) if passed + failed else None
                ),
                "change_review_rate": review["rate"],
                "time_spent_seconds": spent.get(position, 0) if collected is not None else None,
            }
        )
    return {
        "bucket": bucket,
        "from": spans[0][0],
        "to": spans[-1][1],
        "projects": len(projects),
        "points": points,
        "current_only": ["groups", "tags", "monitoring_scope"],
    }


def _bucket_of(starts: list[datetime], moment: datetime) -> int:
    """The index of the bucket holding ``moment`` (starts are sorted)."""
    low, high = 0, len(starts) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if starts[middle] <= moment:
            low = middle
        else:
            high = middle - 1
    return low
