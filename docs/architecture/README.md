# Architecture decision records

Record a decision here when it changes or establishes a foundational choice: runtime topology, data stores, external integrations, security boundaries, or deployment contracts. One decision per file, named `ADR-XXXX-short-title.md`, numbered sequentially. Do not rewrite an accepted ADR; supersede it with a new one and update the status of the old one.

| ADR | Decision | Status |
|---|---|---|
| [ADR-0001](ADR-0001-modular-monolith.md) | Modular monolith with shared API/worker image | Accepted |
| [ADR-0002](ADR-0002-postgresql-job-queue.md) | PostgreSQL as the background job queue | Accepted |
| [ADR-0003](ADR-0003-read-only-v0-1.md) | Read-only GitLab access for v0.1 | Accepted |
| [ADR-0004](ADR-0004-single-gitlab-instance.md) | One GitLab instance per deployment for v0.1 | Accepted |
| [ADR-0005](ADR-0005-no-cloud-dependency.md) | No cloud or internet dependency at runtime | Accepted |
| [ADR-0006](ADR-0006-server-side-oidc-sessions.md) | API is the OIDC client; sessions in PostgreSQL | Accepted |

## Template

```markdown
# ADR-XXXX: Title

- Status: Proposed | Accepted | Superseded by ADR-YYYY
- Date: YYYY-MM-DD

## Context

## Decision

## Consequences

## Revisit when
```
