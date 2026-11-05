# ADR-0010: Time tracking, including time per person

- Status: Proposed
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

## Revisit when

Per-person data is needed outside the manager space, or a deployment needs per-team rather than per-group access.
