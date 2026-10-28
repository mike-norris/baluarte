# Logical data model (initial proposal)

| Entity | Key fields | Notes |
|---|---|---|
| gitlab_instance | id, base_url, version, capability_snapshot, last_checked_at | Single instance in v0.1; keep ID for integrity |
| gitlab_group | id, instance_id, gitlab_id, parent_gitlab_id, path, last_seen_at | Stable upstream identity |
| gitlab_project | id, instance_id, gitlab_id, namespace_id, path, visibility, archived, last_seen_at | Current inventory cache |
| collection_job | id, type, state, attempt, lease_until, started_at, finished_at, error | Durable worker coordination |
| policy | id, name, version, yaml_digest, effective_at | Immutable version references |
| policy_assignment | id, policy_id, target_type, target_id, precedence | Inheritance design pending |
| observation | id, project_id, collected_at, source, payload_digest, payload_ref, collection_job_id | Evidence provenance |
| evaluation | id, observation_id, policy_id, rule_id, outcome, reason, evaluated_at | Pass/fail/unknown/unsupported/excepted |
| finding | id, evaluation_id, severity, first_seen_at, last_seen_at, state | Operational workflow |
| audit_event | id, actor, action, resource, occurred_at, metadata_digest | Administrative trace |
| export_job | id, requester, scope, period, status, manifest_digest | Auditable exports |

Add constraints on `(instance_id, gitlab_id)`, foreign keys, and retention strategy. Avoid storing credentials in plaintext. Document schema migrations and indexes before implementation.
