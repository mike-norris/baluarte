"""Timecards: each time entry of a period with the developer and the issue's planning context.

A row is one time log as recorded (``logged``). A log GitLab no longer returns (deleted there)
also gets a ``removed`` row with the same date and the negative of its minutes, so the period
still adds up to the time that stands. Time removed in GitLab with a negative log (``/spend -1h``
or resetting time spent) is already a log with negative minutes and shows as such.

Each row carries the issue's milestone, iteration, parent, top-level ancestor (the "project",
usually an epic) and estimate, from the issue context read in GitLab; a merge request has none
of these. Milestone falls back to the one recorded with the log while the issue's context has
not been read yet.

Rows cover only the projects in the reader's manager scope (every project for an
administrator) and in monitoring scope. Context filters match the shown text exactly; ``NONE``
matches an empty value.
"""

import csv
import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from html import escape
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from baluarte.db.models import GitLabProject, IssueContext, IssueEstimate, TimeEntry
from baluarte.manager.scopes import PeopleScope
from baluarte.timetracking.summary import Filters, SummaryRefused, allowed_projects

NONE = "(none)"
MAX_ROWS = 20000
# Column key -> heading, in the order of CSV and HTML downloads.
COLUMNS: dict[str, str] = {
    "developer": "Developer",
    "username": "Username",
    "date": "Date",
    "minutes": "Minutes",
    "entry": "Entry",
    "removed_on": "Removed on",
    "top": "Project (top-level epic)",
    "parent": "Parent",
    "work_item": "Issue",
    "title": "Issue title",
    "milestone": "Milestone",
    "iteration": "Iteration",
    "estimate_minutes": "Estimate (minutes)",
    "repository": "Repository",
}
NUMERIC = frozenset({"minutes", "estimate_minutes"})
DEFAULT_SORT = ("date", "developer")
# Cells a spreadsheet would read as a formula; such text cells are prefixed with an apostrophe.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class ContextFilters:
    people: tuple[str, ...] = ()
    milestone: str | None = None
    iteration: str | None = None
    parent: str | None = None
    top: str | None = None
    removed: bool = True

    def as_details(self) -> dict[str, Any]:
        return {
            "people": list(self.people),
            "milestone": self.milestone,
            "iteration": self.iteration,
            "parent": self.parent,
            "top": self.top,
            "removed": self.removed,
        }


@dataclass
class Row:
    entry_id: int
    timelog_id: str
    kind: str
    username: str | None
    developer: str
    spent_at: datetime
    minutes: float
    removed_on: date | None
    repository: str
    project_gitlab_id: int
    work_item: str | None
    title: str | None
    url: str | None
    milestone: str | None
    iteration: str | None
    parent: str | None
    parent_reference: str | None
    top: str | None
    top_reference: str | None
    estimate_minutes: float | None
    context_read: bool

    @property
    def day(self) -> date:
        return self.spent_at.date()

    def value(self, key: str) -> Any:
        if key == "date":
            return self.day
        if key == "entry":
            return "Removed" if self.kind == "removed" else "Logged"
        return getattr(self, key)

    def text(self, key: str) -> str:
        value = self.value(key)
        if value is None:
            return ""
        if isinstance(value, float):
            return _number(value)
        if isinstance(value, date):
            return value.isoformat()
        return str(value)


@dataclass
class Totals:
    minutes: float = 0
    logged_minutes: float = 0
    removed_minutes: float = 0
    rows: int = 0
    people: int = 0


@dataclass
class Timecard:
    rows: list[Row]
    totals: Totals
    sort: tuple[str, ...]
    per_person: dict[str, float] = field(default_factory=dict)


def _number(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:.2f}".rstrip("0")


def minutes(seconds: int) -> float:
    return round(seconds / 60, 2)


def check_sort(sort: str | None) -> tuple[str, ...]:
    """``date,-minutes``: keys of COLUMNS, each optionally descending with a leading ``-``."""
    if not sort:
        return DEFAULT_SORT
    keys = tuple(part.strip() for part in sort.split(",") if part.strip())
    if not keys or len(keys) > len(COLUMNS) or any(k.lstrip("-") not in COLUMNS for k in keys):
        raise SummaryRefused(
            "invalid_sort", f"sort takes {', '.join(COLUMNS)}, each may start with -"
        )
    return keys


def _names(db: Session, instance_id: int | None, usernames: Iterable[str]) -> dict[str, str]:
    """Each person's display name from their most recently read entry that has one."""
    wanted = sorted(set(usernames))
    if not wanted:
        return {}
    found: dict[str, str] = {}
    statement = (
        select(TimeEntry.username, TimeEntry.user_name)
        .where(
            TimeEntry.instance_id == (instance_id or 0),
            TimeEntry.username.in_(wanted),
            TimeEntry.user_name.is_not(None),
        )
        .order_by(TimeEntry.updated_at.desc(), TimeEntry.id.desc())
    )
    for username, name in db.execute(statement):
        if username is not None and name is not None:
            found.setdefault(username, name)
    return found


def _matches(value: str | None, wanted: str | None) -> bool:
    if wanted is None:
        return True
    return (value or NONE) == wanted


def _sort(rows: list[Row], keys: Sequence[str]) -> list[Row]:
    # Stable sorts from the last key to the first; empty values last in either direction.
    ordered = sorted(rows, key=lambda r: (r.spent_at, r.entry_id, r.kind != "logged"))
    for key in reversed(keys):
        name, descending = key.lstrip("-"), key.startswith("-")
        present = [r for r in ordered if r.value(name) not in (None, "")]
        missing = [r for r in ordered if r.value(name) in (None, "")]
        present.sort(key=lambda r: _key(r.value(name)), reverse=descending)
        ordered = present + missing
    return ordered


def _key(value: Any) -> Any:
    return value.casefold() if isinstance(value, str) else value


def build(
    db: Session,
    instance_id: int | None,
    filters: Filters,
    scope: PeopleScope,
    context: ContextFilters,
    sort: tuple[str, ...] = DEFAULT_SORT,
) -> Timecard:
    """The timecard for the period, filters and scope. Raises SummaryRefused."""
    allowed = allowed_projects(db, instance_id, filters, scope)
    statement = (
        select(TimeEntry, GitLabProject, IssueContext, IssueEstimate.estimate_seconds)
        .join(GitLabProject, GitLabProject.id == TimeEntry.project_id)
        .outerjoin(
            IssueContext,
            and_(
                IssueContext.project_id == TimeEntry.project_id,
                IssueContext.issue_iid == TimeEntry.issue_iid,
            ),
        )
        .outerjoin(
            IssueEstimate,
            and_(
                IssueEstimate.project_id == TimeEntry.project_id,
                IssueEstimate.issue_iid == TimeEntry.issue_iid,
            ),
        )
        .where(
            TimeEntry.instance_id == (instance_id or 0),
            TimeEntry.spent_at >= filters.start,
            TimeEntry.spent_at < filters.end,
            TimeEntry.project_id.in_(allowed),
        )
        .order_by(TimeEntry.spent_at, TimeEntry.id)
        .limit(MAX_ROWS + 1)
    )
    if context.people:
        statement = statement.where(TimeEntry.username.in_(context.people))
    if not context.removed:
        statement = statement.where(TimeEntry.removed_at.is_(None))
    found = db.execute(statement).all()
    if len(found) > MAX_ROWS:
        raise SummaryRefused(
            "too_many_rows", f"More than {MAX_ROWS} entries: choose a shorter period or a person"
        )
    names = _names(db, instance_id, (e.username for e, *_ in found if e.username))
    rows: list[Row] = []
    for entry, project, issue, estimate in found:
        row = _row(entry, project, issue, estimate, names)
        if not all(
            (
                _matches(row.milestone, context.milestone),
                _matches(row.iteration, context.iteration),
                _matches(row.parent, context.parent),
                _matches(row.top, context.top),
            )
        ):
            continue
        rows.append(row)
        if entry.removed_at is not None:
            rows.append(_removal(row, entry.removed_at))
    if len(rows) > MAX_ROWS:
        raise SummaryRefused(
            "too_many_rows", f"More than {MAX_ROWS} rows: choose a shorter period or a person"
        )
    totals = Totals(rows=len(rows))
    per_person: dict[str, float] = {}
    for row in rows:
        totals.minutes += row.minutes
        if row.kind == "removed":
            totals.removed_minutes += row.minutes
        else:
            totals.logged_minutes += row.minutes
        per_person[row.developer] = round(per_person.get(row.developer, 0) + row.minutes, 2)
    totals.minutes = round(totals.minutes, 2)
    totals.logged_minutes = round(totals.logged_minutes, 2)
    totals.removed_minutes = round(totals.removed_minutes, 2)
    totals.people = len(per_person)
    return Timecard(_sort(rows, sort), totals, sort, dict(sorted(per_person.items())))


def _display(title: str | None, reference: str | None) -> str | None:
    return title or reference


def _row(
    entry: TimeEntry,
    project: GitLabProject,
    issue: IssueContext | None,
    estimate: int | None,
    names: dict[str, str],
) -> Row:
    username = entry.username
    developer = names.get(username or "") or entry.user_name or username or "(unknown)"
    if entry.issue_iid is not None:
        work_item: str | None = f"#{entry.issue_iid}"
    elif entry.merge_request_iid is not None:
        work_item = f"!{entry.merge_request_iid}"
    else:
        work_item = None
    seconds = issue.estimate_seconds if issue is not None else None
    if seconds is None:
        seconds = estimate
    return Row(
        entry_id=entry.id,
        timelog_id=entry.timelog_id,
        kind="logged",
        username=username,
        developer=developer,
        spent_at=entry.spent_at,
        minutes=minutes(entry.seconds),
        removed_on=None,
        repository=project.path_with_namespace,
        project_gitlab_id=project.gitlab_id,
        work_item=work_item,
        title=issue.title if issue is not None else None,
        url=issue.web_url if issue is not None else None,
        milestone=issue.milestone if issue is not None else entry.milestone,
        iteration=issue.iteration if issue is not None else None,
        parent=_display(issue.parent_title, issue.parent_reference) if issue else None,
        parent_reference=issue.parent_reference if issue is not None else None,
        top=_display(issue.top_title, issue.top_reference) if issue else None,
        top_reference=issue.top_reference if issue is not None else None,
        estimate_minutes=minutes(seconds) if seconds and entry.issue_iid else None,
        context_read=issue is not None,
    )


def _removal(row: Row, removed_at: datetime) -> Row:
    """The balancing row of a log GitLab no longer returns: same date, negative minutes."""
    return Row(
        **{
            **row.__dict__,
            "kind": "removed",
            "minutes": -row.minutes,
            "removed_on": removed_at.date(),
        }
    )


# --- Downloads ---------------------------------------------------------------------------------


def _cell(row: Row, key: str) -> str:
    text = row.text(key)
    if key not in NUMERIC and text.startswith(_FORMULA_START):
        return "'" + text
    return text


def to_csv(card: Timecard) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(COLUMNS.values())
    for row in card.rows:
        writer.writerow(_cell(row, key) for key in COLUMNS)
    return out.getvalue()


def to_html(card: Timecard, period: dict[str, date], described: dict[str, Any]) -> str:
    """A standalone page: the filters, totals and table, sortable by clicking a heading."""
    last = period["to"].toordinal() - 1
    title = f"Timecard {period['from'].isoformat()} to {date.fromordinal(last).isoformat()}"
    shown = [
        f"<li><strong>{escape(name)}</strong>: {escape(str(value))}</li>"
        for name, value in described.items()
        if value not in (None, "", [], ())
    ]
    totals = card.totals
    heads = "".join(
        f'<th scope="col" data-key="{k}"{" data-num" if k in NUMERIC else ""}>'
        f'<button type="button">{escape(v)}</button></th>'
        for k, v in COLUMNS.items()
    )
    body = "\n".join(
        "<tr{}>{}</tr>".format(
            ' class="removed"' if row.kind == "removed" else "",
            "".join(
                f"<td{' class=num' if key in NUMERIC else ''}>{escape(row.text(key))}</td>"
                for key in COLUMNS
            ),
        )
        for row in card.rows
    )
    people = "".join(
        f"<tr><th scope=row>{escape(name)}</th><td class=num>{_number(value)}</td></tr>"
        for name, value in card.per_person.items()
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>
body{{font:14px/1.4 system-ui,sans-serif;margin:24px;color:#0f172a;background:#fff}}
h1{{font-size:20px;margin:0 0 8px}} table{{border-collapse:collapse;margin:12px 0}}
th,td{{border:1px solid #cbd5e1;padding:4px 8px;text-align:left;vertical-align:top}}
thead th{{background:#f1f5f9;position:sticky;top:0}} th button{{all:unset;cursor:pointer}}
th[aria-sort=ascending] button::after{{content:" \\25B2"}}
th[aria-sort=descending] button::after{{content:" \\25BC"}}
.num{{text-align:right;font-variant-numeric:tabular-nums}} tr.removed td{{color:#b91c1c}}
input{{padding:4px 8px;width:20em}} ul{{margin:4px 0;padding-left:20px}}
@media print{{input,th button::after{{display:none}}}}
</style></head><body>
<h1>{escape(title)}</h1>
<ul>{"".join(shown)}</ul>
<p>Total <strong>{_number(totals.minutes)}</strong> minutes:
{_number(totals.logged_minutes)} logged, {_number(totals.removed_minutes)} removed;
{totals.rows} rows, {totals.people} people.</p>
<table><caption>Minutes per developer</caption><tbody>{people}</tbody></table>
<label>Filter rows <input id="filter" type="search"></label>
<table id="card"><thead><tr>{heads}</tr></thead><tbody>
{body}
</tbody></table>
<script>
(() => {{
  const table = document.getElementById("card");
  const rows = () => Array.from(table.tBodies[0].rows);
  document.getElementById("filter").addEventListener("input", (e) => {{
    const q = e.target.value.toLowerCase();
    for (const r of rows()) r.hidden = q && !r.textContent.toLowerCase().includes(q);
  }});
  table.tHead.querySelectorAll("th").forEach((th, i) => {{
    th.querySelector("button").addEventListener("click", () => {{
      const asc = th.getAttribute("aria-sort") !== "ascending";
      table.tHead.querySelectorAll("th").forEach((h) => h.removeAttribute("aria-sort"));
      th.setAttribute("aria-sort", asc ? "ascending" : "descending");
      const num = th.hasAttribute("data-num");
      const val = (r) => r.cells[i].textContent;
      const sorted = rows().sort((a, b) => {{
        const x = val(a), y = val(b);
        if (!x || !y) return !x - !y;
        const c = num ? Number(x) - Number(y) : x.localeCompare(y);
        return asc ? c : -c;
      }});
      table.tBodies[0].append(...sorted);
    }});
  }});
}})();
</script>
</body></html>
"""
