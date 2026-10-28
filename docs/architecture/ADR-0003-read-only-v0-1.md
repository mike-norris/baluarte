# ADR-0003: Read-only GitLab access for v0.1

- Status: Accepted
- Date: 2026-10-28

## Context

Baluarte's v0.1 value is visibility and audit evidence. Automated changes to GitLab configuration can break pipelines, lock out users, or weaken controls if a rule or permission is wrong. Write-capable tokens also increase the impact of a credential leak.

## Decision

v0.1 performs no writes to GitLab. It uses a least-privilege, read-only token (for example `read_api`), and the integration layer exposes no write methods. Remediation is deferred to milestone M6 and requires its own ADR covering approvals, dry runs, idempotency, and verification.

## Consequences

- A leaked Baluarte token cannot change GitLab configuration.
- Some settings may not be visible to a read-only token; these surface as `unknown` with a permission reason, never as `pass`.
- Users fix findings manually in GitLab until remediation ships.

## Revisit when

M6 begins, following the remediation capability research in the backlog.
