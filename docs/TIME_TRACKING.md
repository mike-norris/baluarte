# Time tracking

Baluarte reads the time people log in GitLab, and the estimates on issues, so managers can see effort across groups, projects, labels, milestones and tags without a separate timesheet tool. Time per person is personal data, so it is handled more tightly than configuration evidence ([ADR-0010](architecture/ADR-0010-time-tracking-personal-data.md)).

Implemented in `backend/src/baluarte/integrations/gitlab/timelogs.py` (collection), `backend/src/baluarte/timetracking/` (settings, summaries and timecards) and `backend/src/baluarte/api/v1/time.py`, with migrations `0023` and `0024`.

Time is effort data. It is never mapped to a control, never judged by a rule and never a finding.

## Collection

The `gitlab.collect_time` job runs after each project discovery, as a collection run of kind `time`, for every active, unarchived project in monitoring scope ([MONITORING_SCOPE.md](MONITORING_SCOPE.md)). Every request is a `GET` with the read-only token.

| What | Request |
|---|---|
| Time logs | GraphQL `project(fullPath) { timelogs(startTime, endTime, first: 100, after) { pageInfo nodes { id spentAt timeSpent summary user { username name } issue { iid labels milestone } mergeRequest { iid labels milestone } } } }`, sent as a `GET` to `/api/graphql` with `query` and `variables` parameters. |
| Estimates | `GET /projects/:id/issues?scope=all&updated_after=...&order_by=updated_at`, reading each issue's `time_stats.time_estimate` and `time_stats.total_time_spent`, state, labels and milestone. |
| Issue context | GraphQL `project(fullPath) { workItems(iids, first: 50) { nodes { iid title webUrl workItemType widgets { ... on WorkItemWidgetHierarchy { parent ancestors } ... on WorkItemWidgetMilestone ... on WorkItemWidgetTimeTracking { timeEstimate } ... on WorkItemWidgetIteration { iteration } } } } }`, sent as a `GET`, for the issues with logged time. |
| Display names | Each log's `user { name }`; for a person with no log read again, `GET /users?username=...`. |

**Windows.** A project's first collection reads time spent in the last `lookback_days` (default 180). Later collections read from `reread_days` ago (default 30) to now, so a log deleted in GitLab within that window is noticed: an entry GitLab no longer returns is marked `removed_at`, not deleted, and a log that comes back is restored. A log backdated further than `reread_days` before the last collection is picked up only if it is later edited into the window. Results are also filtered by `spentAt` in Baluarte, because GitLab's date filters on time logs have been inexact on some versions. Issue estimates are read incrementally by `updated_after`, with a five-minute overlap. Each project's progress is kept in `time_cursor`.

**Summaries are never stored.** The text written with a log can hold anything, so Baluarte keeps only its SHA-256 (`summary_sha256`), in the time entry and in the observation payload. The API never returns it.

**Unknown is not zero.** Each read is an observation (`GET /api/graphql project.timelogs` or `GET /projects/:id/issues (time_stats)`). A read GitLab refuses (`forbidden`), a project it does not return (`not_found`) or a field it does not know (`unsupported`) is recorded with that status, stored entries are left as they were, and the run is `partial`. A `401` stops the collection; projects not yet read are recorded as `failed`. A run that reads no project's time logs fails.

**Issue context.** For timecards, Baluarte reads where each issue with logged time sits in GitLab's planning: its work item type and title, its parent, its top-level ancestor (the first of its ancestors; usually an epic), milestone, iteration and estimate. An issue's context is read when it has none yet, when the issue was updated since the last read, and at least once a day otherwise (`CONTEXT_MAX_AGE`), at most 1,000 issues per project and collection. On a GitLab without iterations (GitLab Free), the query is sent again without the iteration widget and iterations are recorded as `unsupported`, not as none. A GitLab without work items leaves the context unknown (`unsupported`), and timecards show the milestone recorded with each log instead. Each read is an observation (`GET /api/graphql project.workItems`).

**Display names.** Each log's author is read with their display name, and every entry of that person takes the current name. A person with no log in the re-read window is looked up with `GET /users?username=...`, at most 50 per collection.

The capability probe `timelogs.read` runs the smallest timelogs query on the sample project.

### GitLab API notes

- GraphQL queries can be sent with `GET` (`/api/graphql?query=...&variables=...`); Baluarte's client always does, so collection stays read-only.
- `Project.timelogs` takes `startTime`/`endTime` (older documentation shows `startDate`/`endDate`). It is available on every tier.
- A user who is not an administrator must filter time logs by project, group or username; querying through `project(fullPath)` satisfies that.
- GitLab checks permission on each log after paging, so a page can hold fewer nodes than asked for. Baluarte follows `pageInfo.hasNextPage` rather than counting nodes.
- The documentation does not say in which version every Timelog field appeared. A field this GitLab does not know makes the read `unsupported` rather than failing the run.
- `time_stats` on the issues REST API returns seconds. Merge requests carry no estimate in Baluarte's figures.
- A work item's `ancestors` are listed from the top down, so the first is the top-level one. `workItems(iids:)` takes at most 100 IIDs; Baluarte asks for 50 at a time.
- An issue with no estimate reports `timeEstimate: 0`; timecards show it as no estimate.
- Time taken back with `/spend -1h`, or by resetting time spent, is a log with negative `timeSpent`. A log deleted in GitLab is no longer returned at all.

## Data

| Table | Holds |
|---|---|
| `time_entry` | One row per GitLab time log (`timelog_id`, the GitLab global ID): project, `username`, `user_name` (display name), `spent_at`, `seconds`, issue or merge request IID, its labels and milestone at collection, `summary_sha256`, the observation, `removed_at`. |
| `issue_estimate` | Per project and issue IID: state, `estimate_seconds`, `spent_seconds`, labels, milestone, `issue_updated_at`, the observation. |
| `issue_context` | Per project and issue IID with logged time: work item type, title and URL, parent and top-level ancestor (type, reference, title), milestone, `iteration_status` and iteration, `estimate_seconds`, the observation, `read_at`. |
| `time_cursor` | Per project: `history_since`, `last_spent_at`, `issues_updated_after`. |

## Summaries

`GET /time/summary` (`time:read`: administrators, managers, analysts, auditors) sums time spent in `[from, to]` (whole days, by default the last 30, at most three years) and never names a person.

| `by` | Rows |
|---|---|
| `project` | Each project. |
| `group` | Each top-level group; with `group`, each child group of it, plus the group itself for its own projects. |
| `label` | Each label of the issue or merge request. An entry counts under every label it has, so rows can add up to more than the total; entries without labels are `(no label)`. |
| `milestone` | Each milestone; `(no milestone)` otherwise. |
| `week` | Each week, Monday to Sunday (UTC). |
| `tag:<key>` | Each value of the tag key; `(none)` for projects without it ([TAGS.md](TAGS.md)). |

Filters: `group` (full path, subgroups included), `project` (GitLab ID), repeatable `tag=key:value`, `label`, `milestone`. Only projects in monitoring scope count, and removed entries never do. `estimate_seconds` of a row is the estimate of every issue with time logged under that row in the period, each issue once.

```json
{
  "by": "project",
  "period": {"from": "2026-10-08", "to": "2026-11-07"},
  "rows": [
    {"key": "100", "label": "acme/team/web", "spent_seconds": 6000, "estimate_seconds": 10800,
     "issues": 1, "entries": 3}
  ],
  "totals": {"key": "", "label": "Total", "spent_seconds": 7500, "estimate_seconds": 14400,
             "issues": 3, "entries": 5},
  "per_person_enabled": true,
  "collected_at": "2026-11-06T20:00:00Z"
}
```

## Time per person

`GET /time/people` (time per person, with the same filters) and `GET /time/people/{username}` (one person's entries, newest first, `limit` up to 200 and `after` for the next page, removed entries included) need all of:

1. `time:read_people`: administrators and managers only;
2. the deployment switch `BALUARTE_TIME_PER_PERSON` not `false` (default `true`); otherwise `403 per_person_disabled` for everyone, administrators included;
3. a manager scope ([MANAGER_SPACE.md](MANAGER_SPACE.md)): a manager sees only projects under the groups assigned to them (`403 out_of_scope` without any); administrators see every project. A `group` or `project` filter reaching outside the manager's scope is refused with `403 out_of_scope`, not silently narrowed.

Every per-person read is audited as `time.people_viewed` with the filters asked for (`from`, `to`, `group`, `project`, `tags`, `label`, `milestone`, `username`, `after`), never with what was returned.

```json
{
  "username": "dev1",
  "period": {"from": "2026-10-08", "to": "2026-11-07"},
  "items": [
    {"id": "gid://gitlab/Timelog/812", "project": {"gitlab_id": 100, "path": "acme/team/web"},
     "spent_at": "2026-11-03T09:00:00Z", "seconds": 3600, "issue_iid": 7,
     "merge_request_iid": null, "labels": ["bug"], "milestone": "M1", "removed_at": null}
  ],
  "next_after": null
}
```

## Timecards

`GET /time/timecard` lists each time log in `[from, to]` (whole days, by default the last 30, at most three years) with:

| Column | From |
|---|---|
| Developer, Username | The log's author: display name and username. |
| Date | The day the time was spent (UTC). |
| Minutes | The time spent; negative for time taken back in GitLab, and for removed rows. |
| Entry | `Logged`, or `Removed`: see below. |
| Project (top-level epic) | The issue's top-level ancestor, usually an epic. |
| Parent | The issue's parent: an epic, or an issue for a task. |
| Issue, Issue title | `#iid` for an issue or `!iid` for a merge request, with the issue's title and link. |
| Milestone, Iteration | The issue's milestone and iteration now; a cadence and its dates when an iteration has no title. |
| Estimate (minutes) | The issue's time estimate; empty when it has none. |
| Repository | The GitLab project. |

**Removed time balances.** A log deleted in GitLab keeps its `logged` row and gets a `removed` row on the same date with the negative of its minutes, and the date Baluarte noticed (`removed_on`). The period then adds up to the time that stands, and a timecard printed before the removal can be reconciled with one printed after. `totals` gives the net `minutes`, `logged_minutes` and `removed_minutes`; `removed=false` leaves removed logs out altogether.

**Filters and sorting.** `group`, `project` and `tag` as for summaries, repeatable `person` (username), and `milestone`, `iteration`, `parent` and `top` matching the shown text exactly, with `(none)` for an empty value. `sort` lists columns (`date`, `developer`, `username`, `minutes`, `entry`, `removed_on`, `top`, `parent`, `work_item`, `title`, `milestone`, `iteration`, `estimate_minutes`, `repository`), each descending with a leading `-`; empty values sort last either way. The default is `date,developer`. A timecard of more than 20,000 rows is refused with `422 too_many_rows`: choose a shorter period or a person.

**Downloads.** `format=csv` and `format=html` return the same rows as an attachment named `timecard-<from>-<to>.csv` or `.html`. In CSV, text a spreadsheet would read as a formula starts with an apostrophe; minutes stay numbers. The HTML file is a standalone page with the filters, totals, minutes per developer and the table, which can be sorted by clicking a heading and filtered by text.

**Access.** The same as time per person: `time:read_people`, the deployment switch, and the manager scope. A view is audited as `time.timecard_viewed` and a download as `time.timecard_exported` with its format, both with the filters and sort asked for, never the rows.

In the web interface, **Manager → Timecard** shows the timecard with these filters, sortable columns (sorting in the page does not read the timecard again), totals and minutes per developer, and the two downloads.

## Exports

An evidence export (`POST /evidence/exports`) leaves time out unless its scope sets `include_people: true`. That needs `time:read_people` as well as `evidence:export`, the switch on and a manager scope (administrators: every project); the export then adds `time_entries` (no summaries), its manifest gains `time_people: {included, projects, entries}` and a limitation, and the request is audited as `time.people_exported` besides `evidence.exported`. Audit-period evidence packages never include time per person.

## Settings and retention

`GET /settings/time` (`time:read`) and `PUT /settings/time` (`settings:manage`, audited as `settings.time_tracking_updated`):

| Setting | Range | Default |
|---|---|---|
| `lookback_days` | 1 to 730 | 180 |
| `reread_days` | 1 to 90 | 30 |
| `retention_days` | 30 to 3650, or `null` | `null`: as long as evidence |

The response also carries `per_person_enabled`, which comes from the deployment and cannot be changed through the API.

The daily retention job deletes time entries spent, and estimates last read, before the shorter of `retention_days` and the evidence retention period. Legal hold deletes nothing.
