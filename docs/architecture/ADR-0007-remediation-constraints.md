# ADR-0007: Constraints for GitLab remediation (M6)

- Status: Proposed. The plan and approval constraints are implemented (BAL-031); execution is pending (BAL-032)
- Date: 2026-10-29

## Context

[ADR-0003](ADR-0003-read-only-v0-1.md) keeps v0.1 read-only. It defers remediation to M6 and requires an ADR covering approvals, dry runs, idempotency and verification.

The [remediation research](../REMEDIATION_RESEARCH.md) found three things:
- No classic token scope narrower than `api` allows the needed writes.
- Several write endpoints are not repeat-safe:
  - protected-branch POST on an existing branch (409 per issue reports)
  - approval-rule POST, which creates duplicates
  - push-rule DELETE, which removes all rules
- GitLab records audit events for these changes only on Premium and Ultimate.

The research was not tested against a live instance.

## Decision

Proposed for BAL-031 and BAL-032. It does not change v0.1.

- **Humans approve every change.** Remediation is never automatic: changes run only from an approved, unexpired dry-run plan computed from a fresh read. Approval is bound to the plan digest, and requester and approver are different people.
- **Write credentials are separate.** The write credential is separate from the read credential: its own secret file and setting, mounted only into the executing worker, and absent unless remediation is enabled. The read path keeps `read_api`.
- **Only allowlisted actions run.** The first allowlist is: restrict project visibility, and protect or tighten default-branch protection. No action may widen access. Group visibility, DELETE-based changes, approval-rule replacement and push-rule deletion are excluded.
- **Execution is idempotent and verified.**
  - Each step has an idempotency key and is skipped when the read-back already matches.
  - Writes are not replayed: a 409 or a failed write is resolved by reading back and re-planning.
  - Delete-then-create is never used.
  - A step succeeds only when a read with the read token shows the intended value.
- **Both sides are audited.** Baluarte audits the plan, approval, each request status and each verification. GitLab's audit events are referenced where the tier provides them.
- **Rollback is a new plan.** Each step records its prior value, so a reverse plan can be generated and approved like any other. Nothing reverts automatically.

## Consequences

- **More operator setup:** operators who enable remediation must provision a second, write-capable identity. On versions without fine-grained tokens, that identity holds a broad `api` scope bounded only by its role.
- **Free-tier audit gap:** on Free, Baluarte's audit trail is the only record of a change.
- **Open questions first:** the open questions in the research must be answered by tests against real versions and tiers before this ADR is accepted.

## Implementation status

BAL-031 implements the planning half ([REMEDIATION.md](../REMEDIATION.md)):
- **Plans:** dry-run plans built only from open, failing, evidence-backed findings, with expiry.
- **Approval:** bound to the plan digest, with requester and approver kept separate.
- **Allowlist:** restricting project visibility and protecting the default branch. Changes that widen access, and the excluded actions, are refused.
- **Per-step records:** idempotency keys, preconditions, effects and prior values.
- **Audit:** of every transition.

Not yet implemented:
- the write credential
- execution
- verification after a write
- rollback plans

Baluarte remains read-only, so this ADR stays Proposed until the open questions are answered and BAL-032 is designed against them.

## Revisit when

BAL-031 starts, or when the open questions in the research are answered.
