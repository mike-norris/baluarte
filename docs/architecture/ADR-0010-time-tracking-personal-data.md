# ADR-0010: Time tracking, including time per person

- Status: Accepted
- Date: 2026-11-05

## Context

Managers want time spent and estimated across groups, projects, labels and milestones, and per person. GitLab exposes time logs (who, when, how long, on which issue or merge request) on every tier. Time per person is personal data about how individuals work, so it needs tighter handling than configuration evidence.

## Decision

- Baluarte reads time logs with the read-only token and stores each entry with its user, date, duration, and issue or merge request.
- **Totals** by group, project, label and milestone are visible to the manager, administrator and auditor roles.
- **Per-person** figures are visible only to the manager and administrator roles, for the groups they are allowed to see. Every view of per-person data is audited.
- Exports leave per-person data out unless the export asks for it explicitly, which is audited and noted in the manifest.
- Time logs follow the evidence retention period, and can be given a shorter one of their own.
- Time is never mapped to a compliance control and is never a finding.

## Consequences

- Organizations get one view of effort across the GitLab hierarchy without a separate timesheet tool.
- Works councils or privacy officers may need to approve per-person reporting; the per-person view can be switched off for a deployment.

## Implementation

Built as described in [TIME_TRACKING.md](../TIME_TRACKING.md) and [MANAGER_SPACE.md](../MANAGER_SPACE.md), with these choices:

- Time logs are read with a GraphQL query sent as a `GET`, per project in monitoring scope; estimates come from the issues' `time_stats`. The first collection looks back 180 days by default; later ones re-read the last 30 days and mark logs GitLab no longer returns as removed rather than deleting them.
- The text written with a log is never stored, only its SHA-256, so a free-text note cannot leak through Baluarte.
- Totals (`time:read`) are open to every role, analysts included, because they name no one. Per-person figures need `time:read_people` (managers and administrators) and, for a manager, a manager scope: the groups an administrator assigned them. Administrators see every project.
- `BALUARTE_TIME_PER_PERSON=false` turns per-person figures off for everyone, administrators included (`403 per_person_disabled`).
- Each per-person read is audited as `time.people_viewed` with the filters only. Exports add per-person entries only with `include_people: true`, audited as `time.people_exported` and noted in the manifest (`time_people`); audit-period packages never include them.
- Retention deletes time entries before the shorter of the time tracking `retention_days` and the evidence period.

## Revisit when

Per-person data is needed outside the manager space, or a deployment needs per-team rather than per-group access.
