# ADR-0013: Workspaces and several provider connections per deployment

- Status: Accepted
- Date: 2026-11-21
- Supersedes: [ADR-0004](ADR-0004-single-gitlab-instance.md)

## Context

[ADR-0004](ADR-0004-single-gitlab-instance.md) limited a deployment to one GitLab instance. Baluarte now serves organizations that use GitLab, GitHub or both, often with more than one deployment of each, and it adds time and finance data that must never cross organizational boundaries. ADR-0004 kept `instance_id` on every GitLab resource so that this change would not re-key evidence.

## Decision

- **Workspace.** A workspace is the isolation boundary. It owns its connections, inventory, baselines, findings, evidence, exports, tags, time and finance data, and its settings. A deployment can host several workspaces.
- **Connection.** A connection is one GitLab or GitHub deployment, hosted or self-managed, with its own credentials, capability detection, collection schedule and health. A workspace can have several connections of either provider. Today's `gitlab_instance` becomes a connection.
- **Upgrade without re-keying.** Existing rows move into a default workspace and its GitLab connection. Observations, evaluations, audit events and their digests are not rewritten, and every existing digest still verifies.
- **Isolation everywhere.** Every API query, background job, export, stored file and notification is scoped to one workspace. Roles are granted per workspace. Isolation is enforced in the application and tested on every router and job; a database constraint backs it where a table holds workspace data.
- **Connection URLs are untrusted input.** They are checked against server-side request forgery: allowed schemes, resolution and redirects to link-local and metadata addresses refused, and private addresses allowed only where an administrator marks a connection as self-managed on a private network.

## Consequences

- Every table, query and export gains a workspace scope. This is the largest structural change since v0.1 and comes before GitHub (roadmap M11) so it is done once.
- Cross-workspace views are not offered. Reports across connections are within one workspace.
- Exports and audit packages name their workspace and connections in the manifest.
- Deployments that ran several Baluarte instances for several GitLab instances can consolidate, but are not required to.

## Revisit when

A hosted multi-tenant offering is considered, which would need tenant isolation at the database level as well.
