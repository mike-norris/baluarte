# ADR-0012: Applying approved remediation plans with a separate write token

- Status: Accepted
- Date: 2026-11-20

## Context

[ADR-0003](ADR-0003-read-only-v0-1.md) kept v0.1 read-only and deferred remediation to M6. [ADR-0007](ADR-0007-remediation-constraints.md) set the constraints any remediation write must meet. BAL-031 built the planning half: dry-run plans from stored evidence, approved by a second person and bound to the plan's digest. Until now, an approved plan was applied by hand.

[ADR-0009](ADR-0009-label-writes.md) and [ADR-0011](ADR-0011-issue-time-writes.md) set the pattern for a write path: an allowlist of requests, a sealed token entered in the web UI, a deployment switch, a write sent once, and a read-back with the read-only token.

The [remediation research](../REMEDIATION_RESEARCH.md) left questions that need a live GitLab of each version and tier. Among them: which role protected-branch writes need, what a repeated protection POST answers, and whether an access token can hold the Owner role.

## Decision

An approved remediation plan may be applied from Baluarte, under ADR-0007's constraints, for the two allowlisted actions only: restricting a project's visibility, and protecting an unprotected default branch.

- **A third token.** Remediation has its own write token, separate from the read token and from the label write token. Restricting visibility needs the Owner role, which label writes never should. It is a sealed secret (ADR-0008), like the label write token. Only the worker's applier decrypts it; the API only stores it and checks it with `GET /personal_access_tokens/self`.
- **Two switches.** Applying needs both `BALUARTE_REMEDIATION_WRITES=true` in the deployment and a stored token. The label switches and token do not allow remediation.
- **A narrow writer.** `RemediationWriter` can send exactly two requests, with values fixed in code:
  - `PUT /projects/:id` with only `visibility`, `private` or `internal`;
  - `POST /projects/:id/protected_branches` with the branch name and push, merge and unprotect access at Maintainer (40).

  It has no delete, no unprotect and no way to make a project more visible. The read-only `GitLabClient` keeps only `GET`.
- **Applied only from an approved, current plan.** Applying is refused unless the plan is approved, unexpired, not stale, and the projects collection is current. It is also refused while another plan is applying a step with the same idempotency key. Anyone with `remediation:plan` may apply; the creator and the approver are still different people.
- **Each step is read, written once, and verified.**
  - A fresh read with the read token comes first. A step already as intended is skipped, which makes it idempotent.
  - A step whose preconditions no longer hold is left alone as `out_of_date`, and the next step runs. Examples: the project changed, it is archived, or a rule now covers the branch.
  - Otherwise the write is sent once and never retried.
  - The step counts as applied only when a read with the read token shows the intended value. A refused write, such as a 409, is resolved by that read, never by deleting and recreating.
  - The plan stops at the first failed step.
- **Audited on both sides of every write.** Baluarte records the start (`remediation_plan.applying`), each request's status (`remediation_plan.write`), each step's outcome and verification (`remediation_plan.step`), and the end (`.applied` or `.failed`). Afterwards project discovery is queued, so findings resolve through the normal read path, not because a write succeeded.

## Consequences

- **More operator setup.** Remediation needs a write identity with Owner on the projects it fixes, and the `api` scope on versions without fine-grained tokens. A leaked token could change any project that identity reaches, so it should be limited to those groups and expire.
- **The open questions don't block safety.** The design avoids depending on them, but they stay open until tested against real versions and tiers:
  - Protections are read from the list endpoint, with wildcards matched in Baluarte (question 4).
  - A write is never repeated when the value already holds (questions 3 and 5).
  - An unexpected answer is resolved by reading back (questions 3 and 6).
  - A missing role shows as a refused write on the step (questions 2 and 7).
- **The Free-tier audit gap remains.** On Free, Baluarte's audit log is the only record of a change.
- **Approved plans can still be applied by hand,** and a deployment that leaves the switch off stays read-only.
- **No rollback.** Each step records its prior value, so a reverse plan can be built later and approved like any other.

## Revisit when

The open questions are answered by tests against real versions and tiers, fine-grained tokens can be scoped to these two actions, or another remediation action is proposed.
