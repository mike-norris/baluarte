# Data model

Implemented in `backend/src/baluarte/db/models.py`; migrations `0001` (inventory, policy and evidence), `0002` (users and sessions), `0003` (job lease constraints) and `0004` (collection runs and resource errors) create it. All primary keys are `bigint` identity columns, all timestamps are `timestamptz` (UTC instants), and status-like columns are `text` with CHECK constraints rather than PostgreSQL enum types. SHA-256 digests are stored as 64-character lowercase hex and checked by constraint.

| Table | Key fields | Notes |
|---|---|---|
| gitlab_instance | id, base_url (unique), version, edition, capability_snapshot, last_checked_at | One instance in v0.1 (ADR-0004); the ID is kept on every GitLab resource |
| gitlab_group | id, instance_id, gitlab_id, parent_gitlab_id, full_path, visibility, state, first_seen_at, last_seen_at, last_error, last_error_at | Unique `(instance_id, gitlab_id)`; state `active`, `missing`, `inaccessible` |
| gitlab_project | id, instance_id, gitlab_id, namespace_gitlab_id, path_with_namespace, visibility, archived, default_branch, state, first_seen_at, last_seen_at, last_error, last_error_at | Unique `(instance_id, gitlab_id)`; current inventory cache |
| collection_run | id, instance_id, kind, collection_job_id, attempt, status, started_at, finished_at, seen, counts, error | One row per job attempt for freshness. Kind `capabilities`, `groups`, `projects`; status `running`, `succeeded`, `partial`, `failed`; finished exactly when not running |
| collection_job | id, kind, state, idempotency_key (unique), payload, attempt, max_attempts, run_after, lease_owner, lease_until, error | Background jobs (see [Jobs](#jobs)). A lease is held exactly when `running`; `attempt` never exceeds `max_attempts`; indexed for claiming and for finding expired leases |
| policy | id, name, version, source_yaml, yaml_digest, effective_at | Immutable versions, unique `(name, version)` |
| policy_assignment | id, policy_id, target_type, target_id, precedence | Unique per policy and target; inheritance rules pending (BAL-018) |
| observation | id, instance_id, project_id or group_id, collection_job_id, source, source_ref, collected_at, collection_status, payload, payload_digest, error, retention_until | Evidence provenance. A payload requires a digest. Neither target means instance-level |
| evaluation | id, observation_id, policy_id, rule_id, outcome, reason, expected, observed, capability_status, evaluated_at | Outcome `pass`, `fail`, `unknown`, `unsupported`, `excepted` |
| finding | id, project_id, policy_id, rule_id, latest_evaluation_id, severity, state, first_seen_at, last_seen_at, resolved_at | Unique `(project_id, policy_id, rule_id)`; state `open`, `resolved`, `excepted` |
| audit_event | id, actor, action, resource_type, resource_id, details, details_digest, occurred_at | Administrative trace |
| export_job | id, requested_by, scope, period_start, period_end, status, manifest_digest | Auditable exports; period must be ordered |
| app_user | id, issuer, subject, email, display_name, created_at, last_login_at | Unique `(issuer, subject)`; created on first sign-in |
| user_session | id, token_digest (unique), user_id, roles, created_at, last_seen_at, expires_at, revoked_at | SHA-256 of the session cookie, never the cookie itself; roles limited to `admin`, `analyst`, `auditor` and non-empty |
| oidc_login | state_digest (primary key), nonce, code_verifier, return_to, expires_at | Sign-ins sent to the identity provider; single use, expire after 10 minutes |

## Immutability

Database triggers reject `UPDATE` on `observation` and `evaluation`, and both `UPDATE` and `DELETE` on `audit_event`. Deletes of observations and evaluations remain possible so the retention design (BAL-029) can expire them. These triggers stop accidental or application-level changes; they do not stop a database superuser, so integrity still depends on protected digests (see [SECURITY.md](SECURITY.md)).

## Jobs

Implements [ADR-0002](architecture/ADR-0002-postgresql-job-queue.md) in `baluarte.jobs`:

- `enqueue` inserts a job unless its `idempotency_key` exists. The key names the work, so repeating an enqueue is a no-op even after the job finished.
- Workers claim due `queued` jobs of the kinds they handle with `FOR UPDATE SKIP LOCKED`, set `running`, increment `attempt` and take a lease (`lease_owner`, `lease_until`). All times come from the database clock.
- While a handler runs the worker renews the lease. Completing, failing and renewing are fenced on `lease_owner` and `attempt`, so a worker whose lease expired cannot overwrite a later attempt.
- A failed attempt is requeued after 30s, 60s, 120s ... (capped at one hour, +/-20% jitter) until `max_attempts` (default 5), then the job is `failed`. Handlers raise `PermanentJobError` to fail at once.
- A lease that expires (worker crashed or was killed) returns the job to the queue with backoff, or fails it if no attempts remain. `error` records what happened.
- `error` holds only messages written for storage. Unexpected exceptions are recorded by type name, because their text may contain URLs, tokens or response bodies.

Delivery is at least once: handlers must be idempotent. Jobs are never deleted by the queue; retention is part of BAL-029.

## Audit digests

`gitlab_group.state` and `gitlab_project.state` are `active` (returned by the last complete listing, or confirmed by a direct lookup), `inaccessible` (lookup answered 401 or 403) or `missing` (lookup answered 404: deleted, or hidden from the token). See [GITLAB_INTEGRATION.md](GITLAB_INTEGRATION.md#inventory-discovery).

`gitlab_instance.capability_snapshot` holds the latest capability detection result: `status` (`ok`, `partial`, `failed`), `checked_at`, `version`, `edition`, `tier`, `error` and one entry per probe with `status`, `endpoint`, `http_status` and `detail` (see [GITLAB_INTEGRATION.md](GITLAB_INTEGRATION.md#capability-detection)).

`audit_event.details_digest` is the SHA-256 of `details` serialised as canonical JSON: keys sorted, no insignificant whitespace, UTF-8 (`baluarte.audit.details_digest`). Details are redacted before they are hashed, and each committed event is mirrored to the `baluarte.audit` log stream (see [SECURITY.md](SECURITY.md#audit-trail)).

## Migrations

Alembic revisions live in `backend/src/baluarte/db/migrations/versions`. Deployments run `baluarte-migrate`, which waits for PostgreSQL, takes an advisory lock so concurrent runners serialise, and upgrades to head. The API's `/api/v1/ready` endpoint reports not ready until the schema is at head. Tests check that the models and migrations do not drift, and that upgrade, downgrade and re-upgrade succeed.
