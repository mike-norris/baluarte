# ADR-0011: Approved issue plans for time rules on closed issues

- Status: Accepted
- Date: 2026-11-13

## Context

Organizations want every closed issue to account for its time. An issue closed with work behind it (a merge request or a commit) but no time logged should be flagged, and an issue closed without an estimate should get one that matches the time spent, so estimates can be compared with actuals later. Doing this by hand across many groups is error-prone. [ADR-0009](ADR-0009-label-writes.md) allows Baluarte to write label definitions only, through approved plans.

## Decision

Baluarte may make two more kinds of write, on issues only, under the same constraints as label plans:

- **Add or remove one label on an issue**: the configured "missing time" label (default `missing-time`), with `PUT /projects/:id/issues/:iid` and `add_labels` or `remove_labels`. It is added to a closed issue with commits and no time spent, and removed from an issue once time is logged on it. Only a label already defined for the issue's project or one of its groups is added, so GitLab never creates a new label as a side effect.
- **Set a time estimate on an issue that has none**: `POST /projects/:id/issues/:iid/time_estimate`, with the time spent rounded up to the configured interval (default 30 minutes). An existing estimate is never changed or removed.

These writes are made only by applying an **approved issue plan**: a dry run listing each change with the issue's state when it was read, approved by someone other than its creator (or recorded as self-approved while review is off), bound to the plan's digest, and expiring after 24 hours. The applier reads each issue with the read-only token before writing and skips a change already in place. An issue that no longer matches the plan (reopened, time logged or removed, an estimate set meanwhile) is marked stale and left alone, since thousands of issues keep changing while a plan waits. Each write is sent once and verified by reading the issue back; a refused write, a failed read or a read-back that does not match stops the plan. Every write is audited.

Writing needs the deployment switch `BALUARTE_ISSUE_WRITES=true` (default `false`) and the stored write token (the same token as label plans, with `api` scope). Label plans keep their own switch.

## Consequences

- A leaked write token can now also change issue labels and estimates in the projects it reaches. The token should still be limited to the groups Baluarte manages and expire.
- Adding a label or estimate records a system note on the issue and changes its `updated_at`; Baluarte's own next collection reads it like any other change.
- Read-only deployments still see which closed issues miss time or an estimate.

## Revisit when

Another write on issues or merge requests is proposed, or organizations want these rules applied without a per-plan approval.
