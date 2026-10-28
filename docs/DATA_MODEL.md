# Data model

Implemented in `backend/src/baluarte/db/models.py`; migration `0001` creates it. All primary keys are `bigint` identity columns, all timestamps are `timestamptz` (UTC instants), and status-like columns are `text` with CHECK constraints rather than PostgreSQL enum types. SHA-256 digests are stored as 64-character lowercase hex and checked by constraint.

| Table | Key fields | Notes |
|---|---|---|
| gitlab_instance | id, base_url (unique), version, edition, capability_snapshot, last_checked_at | One instance in v0.1 (ADR-0004); the ID is kept on every GitLab resource |
| gitlab_group | id, instance_id, gitlab_id, parent_gitlab_id, full_path, visibility, state, first_seen_at, last_seen_at | Unique `(instance_id, gitlab_id)`; state `active`, `missing`, `inaccessible` |
| gitlab_project | id, instance_id, gitlab_id, namespace_gitlab_id, path_with_namespace, visibility, archived, default_branch, state, first_seen_at, last_seen_at | Unique `(instance_id, gitlab_id)`; current inventory cache |
| collection_job | id, kind, state, idempotency_key (unique), payload, attempt, max_attempts, run_after, lease_owner, lease_until, error | Durable worker coordination; indexed on `(state, run_after)` for claiming |
| policy | id, name, version, source_yaml, yaml_digest, effective_at | Immutable versions, unique `(name, version)` |
| policy_assignment | id, policy_id, target_type, target_id, precedence | Unique per policy and target; inheritance rules pending (BAL-018) |
| observation | id, instance_id, project_id or group_id, collection_job_id, source, source_ref, collected_at, collection_status, payload, payload_digest, error, retention_until | Evidence provenance. A payload requires a digest. Neither target means instance-level |
| evaluation | id, observation_id, policy_id, rule_id, outcome, reason, expected, observed, capability_status, evaluated_at | Outcome `pass`, `fail`, `unknown`, `unsupported`, `excepted` |
| finding | id, project_id, policy_id, rule_id, latest_evaluation_id, severity, state, first_seen_at, last_seen_at, resolved_at | Unique `(project_id, policy_id, rule_id)`; state `open`, `resolved`, `excepted` |
| audit_event | id, actor, action, resource_type, resource_id, details, details_digest, occurred_at | Administrative trace |
| export_job | id, requested_by, scope, period_start, period_end, status, manifest_digest | Auditable exports; period must be ordered |

## Immutability

Database triggers reject `UPDATE` on `observation` and `evaluation`, and both `UPDATE` and `DELETE` on `audit_event`. Deletes of observations and evaluations remain possible so the retention design (BAL-029) can expire them. These triggers stop accidental or application-level changes; they do not stop a database superuser, so integrity still depends on protected digests (see [SECURITY.md](SECURITY.md)).

## Migrations

Alembic revisions live in `backend/src/baluarte/db/migrations/versions`. Deployments run `baluarte-migrate`, which waits for PostgreSQL, takes an advisory lock so concurrent runners serialise, and upgrades to head. The API's `/api/v1/ready` endpoint reports not ready until the schema is at head. Tests check that the models and migrations do not drift, and that upgrade, downgrade and re-upgrade succeed.
