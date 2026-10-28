# ADR-0004: One GitLab instance per deployment for v0.1

- Status: Accepted
- Date: 2026-10-28

## Context

Supporting several GitLab instances requires tenant isolation in queries, authorization, exports, and evidence, which multiplies the test surface. Most target organizations run one self-managed instance.

## Decision

A v0.1 deployment connects to exactly one self-managed GitLab instance. The data model still carries `instance_id` on GitLab resources, and uniqueness is `(instance_id, gitlab_id)`, so multi-instance support can be added without re-keying evidence.

## Consequences

- Configuration, UI, and RBAC assume one instance; no cross-instance views.
- Organizations with several instances run several deployments until multi-instance support ships.
- Code must not assume `instance_id` is constant when building queries or digests.

## Revisit when

Multi-instance inventory (FR-016) is scheduled, which requires separate-tenant isolation tests.
