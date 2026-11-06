# ADR-0009: Write access to GitLab for approved label plans only

- Status: Accepted
- Date: 2026-11-05 (accepted 2026-11-06)

## Context

Organizations with many groups want one label taxonomy (names, colours, descriptions, scoped labels) everywhere. Baluarte can report drift with its read-only token, but fixing hundreds of groups by hand is the reason they want the tool. [ADR-0003](ADR-0003-read-only-v0-1.md) keeps GitLab access read-only, and [ADR-0007](ADR-0007-remediation-constraints.md) sets the constraints any write must meet.

## Decision

Baluarte may write labels, and nothing else, under ADR-0007's constraints:

- A **separate** token (`api` scope, ideally a group access token on a bot user) is used only by the label applier. Collection keeps using the read-only token, and the integration layer exposes write methods only to the applier.
- Only **approved** label plans are applied. A plan is a dry run listing each create, rename, recolour or description change per group or project. Approval follows the two-person `governance` review and is bound to the plan's digest.
- No deletes. Renaming or merging labels is never done by deleting; a label in use is renamed in place so issues and merge requests keep it.
- Each request and response is audited, applied in order, stops at the first unexpected result, and is verified by reading the label back.
- Writing is off until an administrator enters the write token; the deployment can forbid it entirely.

## Consequences

- A leaked write token can change labels in the groups it can reach. Its scope should be limited to those groups and it should expire.
- Read-only deployments keep working and still see drift.
- The applier is the first write path, so it sets the pattern for later remediation.

## Implementation

Built as described in [LABELS.md](../LABELS.md), with these choices:

- Writing needs both the deployment switch `BALUARTE_LABEL_WRITES=true` (default `false`) and a stored write token. The token alone is not enough, so a deployment forbids writes by leaving the switch off.
- Plans create, rename and update labels on the top-level group only. Duplicates, misplaced labels and anything below the top-level group are listed for manual work, because fixing them would mean deleting or merging labels.
- A write is sent once and never retried. A label that changed since the plan was built, a refused write, or a read-back that does not match stops the plan.
- Approval goes through the same review as remediation plans: by someone other than the creator with `remediation:approve`, bound to the digest. While review is not required, the creator's submission approves it and is recorded as self-approved.

## Revisit when

The label applier is built, or another write (such as branch protection) is proposed.
