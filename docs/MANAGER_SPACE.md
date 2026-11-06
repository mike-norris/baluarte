# Manager space

The manager space answers the questions engineering managers ask across many groups: how the posture of each team compares, whether changes are reviewed, whether labels are consistent, and where the effort goes. It adds a `manager` role, manager scopes, an overview and trends. Time tracking and tags are described in [TIME_TRACKING.md](TIME_TRACKING.md) and [TAGS.md](TAGS.md).

Implemented in `backend/src/baluarte/manager/` and `backend/src/baluarte/api/v1/manager.py`, with migration `0023`.

## The manager role

`manager` is mapped from the identity provider like the other roles: members of a group listed in `BALUARTE_OIDC_MANAGER_GROUPS` (or `manager_groups` in the stored OIDC settings) get it at sign-in.

| Permission | Administrator | Manager | Analyst | Auditor |
|---|---|---|---|---|
| `manager:read`: overview, trends, scopes | yes | yes | yes | yes |
| `time:read`: time summaries | yes | yes | yes | yes |
| `time:read_people`: time per person | yes | yes | | |
| `tags:write`: create and assign tags | yes | yes | yes | |

A manager also has the basic read permissions (`inventory:read`, `findings:read`, `evidence:read`). Managers cannot change baselines, settings or exceptions, export evidence, or read the audit log; give a person a second role for that.

## Manager scopes

A manager scope assigns GitLab groups to a user. It covers the groups' subgroups and their projects, following the inventory's group tree. Scopes decide only **whose time per person** the user may see; the overview and trends show every group to anyone with `manager:read`, because they hold no per-person data. Administrators see time per person everywhere without a scope.

| Method and path | Permission | Purpose |
|---|---|---|
| `GET /manager/scopes?state=active\|all` | `manager:read` | Open assignments, or all of them. |
| `GET /manager/scopes/me` | `manager:read` | The signed-in user's groups; `all` is true for an administrator. |
| `POST /manager/scopes` | `settings:manage` | `{user_id, group_gitlab_ids}` (up to 100). Groups already assigned are kept. `409 unknown_user`, `409 unknown_group` (reasons list the IDs). |
| `POST /manager/scopes/{id}/end` | `settings:manage` | End one assignment. `409 already_ended`. |
| `GET /manager/users` | `settings:manage` | Users who have signed in, with the roles of their latest session, to choose whom to assign. |

```json
{
  "items": [
    {"id": 5, "user": {"actor": "user:7", "user_id": 7, "display_name": "Mia"},
     "group": {"gitlab_id": 11, "path": "acme/team"},
     "assigned_by": {"actor": "user:1", "user_id": 1, "display_name": "Admin"},
     "assigned_at": "2026-11-06T10:00:00Z", "ended_by": null, "ended_at": null}
  ]
}
```

`manager_scope` is append-only apart from the end: a trigger refuses `DELETE`, freezes every column, and allows only an open row to be ended once. A partial unique index allows one open assignment per user and group. Changes are audited as `manager.scope_assigned` (`groups`) and `manager.scope_ended` (`scope_id`, `group`).

## Overview

`GET /manager/overview` has one row per top-level group; with `group=<full path>`, one per child group of it, plus a row for the projects directly in it (`direct_only: true`); with `by_tag=<key>`, one per value of that tag key (and `value: null` for projects without it). `tag=key:value` limits every row to the projects with that tag. Rows cover the active, unarchived projects in monitoring scope under them; excluded projects are only counted.

| Field | Meaning |
|---|---|
| `projects` | `monitored`, and `excluded` from monitoring. |
| `findings_open` | Open findings by severity (`critical`, `high`, `medium`, `low`) and `total`. With `as_of`, the failing results of the evaluation run in force then. |
| `controls_not_met` | Per framework (`soc2`, `iso27001`, `nist_800_53_moderate`), controls with a mapped rule type that failed for one of the row's projects in that run, unless the organization marked the control not applicable. |
| `changes.days_30`, `changes.days_90` | Merge requests and direct pushes that landed in the period: `total`, `reviewed` (approved by someone other than the author, not pushed directly), `direct_pushes`, `unknown` (could not be judged) and `rate` = reviewed / total, `null` without changes. |
| `label_drift` | Drift items on the row's groups and projects ([LABELS.md](LABELS.md)); `null` while no taxonomy is in force. |
| `time_30d` | `spent_seconds` and `estimate_seconds` in the last 30 days; `null` before time was ever collected. |

With `as_of`, findings, controls, changes, label drift and monitoring scope are as they stood then. Groups, tags and time have no history, so they are today's; the response lists them in `current_only`.

```json
{
  "as_of": null,
  "evaluation_run": {"id": 41, "finished_at": "2026-11-06T06:00:00Z"},
  "parent": {"type": "group", "gitlab_id": 10, "path": "acme"},
  "current_only": ["time", "tags", "groups"],
  "rows": [
    {
      "key": {"type": "group", "gitlab_id": 11, "path": "acme/team"},
      "projects": {"monitored": 2, "excluded": 0},
      "findings_open": {"critical": 1, "high": 1, "medium": 0, "low": 0, "total": 2},
      "controls_not_met": {"soc2": 2, "iso27001": 1, "nist_800_53_moderate": 2},
      "changes": {
        "days_30": {"total": 1, "reviewed": 1, "direct_pushes": 0, "unknown": 0, "rate": 1.0},
        "days_90": {"total": 2, "reviewed": 1, "direct_pushes": 1, "unknown": 0, "rate": 0.5}
      },
      "label_drift": null,
      "time_30d": {"spent_seconds": 3600, "estimate_seconds": 7200}
    }
  ]
}
```

## Trends

`GET /manager/trends?bucket=week|month&from=&to=&group=&tag=` has a point per bucket, from the bucket holding `from` to the one holding `to` (by default the last 12). Weeks start on Monday (UTC). At most 105 weekly or 36 monthly points per request (`422 too_many_buckets`). Each point is the state at the end of its bucket:

| Field | Meaning |
|---|---|
| `evaluation_run_id` | The evaluation run in force at the bucket's end; `null` before the first. |
| `findings_open` | Failing results by severity in that run; `null` without a run. |
| `rule_pass_rate` | Passing over passing and failing results in that run; `null` without either. |
| `change_review_rate` | Merge requests and direct pushes landed in the bucket that passed the review check, over all of them; `null` without changes. |
| `time_spent_seconds` | Time logged in the bucket; `null` before time was ever collected. |

```json
{
  "bucket": "week",
  "from": "2026-08-17",
  "to": "2026-11-09",
  "projects": 2,
  "current_only": ["groups", "tags", "monitoring_scope"],
  "points": [
    {"end": "2026-11-09T00:00:00Z", "evaluation_run_id": 41,
     "findings_open": {"critical": 1, "high": 1, "medium": 0, "low": 0, "total": 2},
     "rule_pass_rate": 0.9412, "change_review_rate": 0.75, "time_spent_seconds": 12600}
  ]
}
```

The projects are those under the group or with the tag today, in monitoring scope today.

### Performance and limits

Trends read each figure with one grouped query, whatever the number of buckets:

- results by run, outcome and severity over the index `ix_evaluation_run_outcome (evaluation_run_id, outcome, severity, project_id)`, for the runs in force at the bucket ends;
- changes over `ix_change_record_project_changed (project_id, changed_at)`, with the review check stored on each change (`change_record.review_outcome`, written at collection; changes recorded before it existed are judged from their observation);
- time over `ix_time_entry_project_spent (project_id, spent_at)`.

A test builds 2,000 projects with 104 weekly runs of five results each (about a million results), 50,000 changes and 100,000 time entries; two years of weekly points take about half a second on a developer laptop. The cost grows with the number of results in the runs used (at most one run per bucket) and the number of projects selected, so at 10,000 projects expect a few seconds. Narrow by `group` or `tag` for faster answers. The overview reads the latest run once and is cheaper than trends.

## Access summary

| Data | Who |
|---|---|
| Overview, trends, scopes list, time summaries | `manager:read` / `time:read`: every role |
| Time per person | Administrators everywhere; managers within their scope; never when `BALUARTE_TIME_PER_PERSON=false` |
| Assigning scopes, listing users | `settings:manage`: administrators |
| Tags | Read with `inventory:read`; change with `tags:write` |
