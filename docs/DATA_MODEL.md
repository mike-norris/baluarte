# Data model

Implemented in `backend/src/baluarte/db/models.py`; migrations `0001` (inventory, policy and evidence), `0002` (users and sessions), `0003` (job lease constraints), `0004` (collection runs and resource errors), `0005` (immutable policy versions), `0006` (policy assignment rules) and `0007` (project namespace kinds) create it. All primary keys are `bigint` identity columns, all timestamps are `timestamptz` (UTC instants), and status-like columns are `text` with CHECK constraints rather than PostgreSQL enum types. SHA-256 digests are stored as 64-character lowercase hex and checked by constraint.

| Table | Key fields | Notes |
|---|---|---|
| gitlab_instance | id, base_url (unique), version, edition, capability_snapshot, last_checked_at | One instance in v0.1 (ADR-0004); the ID is kept on every GitLab resource |
| gitlab_group | id, instance_id, gitlab_id, parent_gitlab_id, full_path, visibility, state, first_seen_at, last_seen_at, last_error, last_error_at | Unique `(instance_id, gitlab_id)`; state `active`, `missing`, `inaccessible` |
| gitlab_project | id, instance_id, gitlab_id, namespace_gitlab_id, namespace_kind, path_with_namespace, visibility, archived, default_branch, state, first_seen_at, last_seen_at, last_error, last_error_at | Unique `(instance_id, gitlab_id)`; current inventory cache |
| collection_run | id, instance_id, kind, collection_job_id, attempt, status, started_at, finished_at, seen, counts, error | One row per job attempt for freshness. Kind `capabilities`, `groups`, `projects`; status `running`, `succeeded`, `partial`, `failed`; finished exactly when not running. A projects run is `partial` when some projects could not be confirmed or some default-branch protection was not read; its `counts` include `protection` (`protected`, `unprotected`, `unread`) |
| collection_job | id, kind, state, idempotency_key (unique), payload, attempt, max_attempts, run_after, lease_owner, lease_until, error | Background jobs (see [Jobs](#jobs)). A lease is held exactly when `running`; `attempt` never exceeds `max_attempts`; indexed for claiming and for finding expired leases |
| policy | id, name, version, source_yaml, yaml_digest, effective_at | Immutable versions (`UPDATE` rejected by trigger), unique `(name, version)`; `name` is the document's `metadata.name`, `source_yaml` the exact submitted text and `yaml_digest` the SHA-256 of its UTF-8 bytes. See [POLICY_ENGINE.md](POLICY_ENGINE.md#versions) |
| policy_assignment | id, instance_id, policy_id, policy_name, target_type, target_id, created_at | Pins one policy version to the instance, a group or a project. `target_id` is the GitLab ID, or the `gitlab_instance` ID for the instance (checked). Unique `(instance_id, target_type, target_id, policy_name)`: one version of each policy per target. A trigger rejects a `policy_name` that differs from the version's name. See [POLICY_ENGINE.md](POLICY_ENGINE.md#assignment-and-inheritance) |
| observation | id, instance_id, project_id or group_id, collection_job_id, collection_run_id, source, source_ref, collected_at, collection_status, payload, payload_digest, error, retention_until | Evidence provenance. A payload requires a digest (SHA-256 of canonical JSON). Neither target means instance-level (capability snapshots). A new row only when status, digest or error changes; see [AUDIT_EVIDENCE.md](AUDIT_EVIDENCE.md#observations) |
| evaluation_run | id, instance_id, collection_job_id, attempt, status, started_at, finished_at, projects, counts, error | One row per `policy.evaluate` attempt, for evaluation freshness; same statuses and interrupted-run handling as `collection_run`. `counts` holds results by outcome and finding transitions |
| evaluation | id, instance_id, project_id, evaluation_run_id, observation_id, policy_id, policy_name, rule_id, rule_type, severity, outcome, reason, unknown_reasons, expected, observed, source, observed_at, capability_name, capability_status, collection_kind, collection_status, collection_last_success_at, ancestry, exception_id, evaluated_at | One rule for one project at one time. Outcome `pass`, `fail`, `unknown`, `unsupported`, `excepted`. Checked: `pass`, `fail` and `excepted` reference an observation; `unknown_reasons` is non-empty exactly when `unknown`; `observed` is null unless `pass`, `fail` or `excepted`; `exception_id` is set exactly when `excepted`. `observation_id` is otherwise the project's latest observation, or null when there is none. Written every run (not deduplicated), so it records when each rule was checked. See [Evaluations and findings](#evaluations-and-findings) |
| finding | id, project_id, policy_id, policy_name, rule_id, latest_evaluation_id, latest_outcome, severity, state, resolution, exception_id, first_seen_at, last_seen_at, resolved_at | Unique `(project_id, policy_name, rule_id)`: followed across policy versions, `policy_id` is the latest evaluation's version. State `open`, `resolved` (with `resolved_at` and `resolution` `passed` or `no_longer_applies`), `excepted` (with `exception_id`) |
| policy_exception | id, instance_id, project_id, policy_name, rule_id, justification, state, requested_by_user_id, requested_at, expires_at, decided_by_user_id, decided_at, decision_note, ended_by_user_id, ended_at, end_note | State `requested`, `approved`, `rejected`, `revoked`, `expired`. A trigger allows only the documented transitions, keeps what was requested unchanged and rejects `DELETE`. Checked: `expires_at` within 365 days of the request; the decider is not the requester; at most one `requested` or `approved` exception per project, policy name and rule (partial unique index). `evaluation.exception_id` and `finding.exception_id` are set exactly when the outcome or state is `excepted` |
| audit_event | id, actor, action, resource_type, resource_id, details, details_digest, occurred_at | Administrative trace |
| export_job | id, requested_by, scope, period_start, period_end, status, manifest_digest | Unused: evidence exports are streamed and never stored here. Each export is audited instead (`evidence.exported`, and `evidence.export_completed` with the response SHA-256 and `manifest_sha256`); see [AUDIT_EVIDENCE.md](AUDIT_EVIDENCE.md). Kept from the initial schema; period must be ordered |
| app_user | id, issuer, subject, email, display_name, created_at, last_login_at | Unique `(issuer, subject)`; created on first sign-in |
| user_session | id, token_digest (unique), user_id, roles, created_at, last_seen_at, expires_at, revoked_at | SHA-256 of the session cookie, never the cookie itself; roles limited to `admin`, `analyst`, `auditor` and non-empty |
| oidc_login | state_digest (primary key), nonce, code_verifier, return_to, expires_at | Sign-ins sent to the identity provider; single use, expire after 10 minutes |

## Immutability

Database triggers reject `UPDATE` on `observation`, `evaluation` and `policy`, and both `UPDATE` and `DELETE` on `audit_event`; `policy_exception` rejects `DELETE` and allows only its state transitions. Deletes of observations and evaluations remain possible so the retention design (BAL-029) can expire them. These triggers stop accidental or application-level changes; they do not stop a database superuser, so integrity still depends on protected digests (see [SECURITY.md](SECURITY.md)).

## Evaluations and findings

The `policy.evaluate` job (`baluarte.policy.evaluation.runner`) evaluates every active project, and every other project that still has an open finding, against its effective policies (BAL-021). It runs after each project discovery that succeeds or is partial, after a policy assignment changes, and on demand. Projects are read 200 at a time; each page is stored in one transaction under a per-instance advisory lock.

- **Evidence:** a `pass` or `fail` is stored only when the project's latest observation of the value's source is `ok` its payload still matches its SHA-256 digest (recomputed at evaluation time), and the payload holds the judged value: the project's own observation (`GET /projects` or `GET /projects/:id`, field `visibility`) for the visibility rule, and the default-branch protection observation (`GET /projects/:id/protected_branches`, whole payload) for the protection rule. Otherwise the result is stored as `unknown` with `evidence_missing`, `evidence_digest_mismatch` or `evidence_mismatch`, so no stored compliance result lacks or rests on altered evidence. Projects discovered before observations existed are `unknown` until their next discovery.
- **Findings** are keyed by policy name, so a new version of a baseline continues the same finding:
  - `fail` opens one, or reopens a resolved one; `last_seen_at` is the latest failing evaluation.
  - `pass` resolves an open one (`resolution = passed`).
  - `unknown` and `unsupported` never open or resolve one; `latest_outcome` shows the last check could not decide.
  - When a rule no longer applies to the project, its open finding resolves as `no_longer_applies`, but only if the project's policy resolution was complete: a gap in the group chain proves nothing.
  - `excepted`: a backed `fail` waived by an exception in force is stored as `excepted` (with `exception_id`), and its finding becomes `excepted`, opened in that state if needed. Once the exception has ended, the next `fail` or `unknown` reopens the finding and a `pass` resolves it. Revoking an exception, and the start of each evaluation run for expired ones, reopens its findings at once. See [POLICY_ENGINE.md](POLICY_ENGINE.md#exceptions).
- **Severity** is the rule's `severity` in the policy (`medium` when omitted).
- `policy_name` on `evaluation` and `finding` must match the referenced version (the trigger from migration 0006).

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

`gitlab_project.namespace_kind` is `group`, `user` (a personal project) or null when unknown. Rows discovered before migration `0007` are null until the next project discovery refreshes them, and a kind GitLab adds later is stored as null rather than guessed. Policy resolution treats null as unconfirmed, never as a complete ancestry ([POLICY_ENGINE.md](POLICY_ENGINE.md#personal-projects)).

`gitlab_instance.capability_snapshot` holds the latest capability detection result: `status` (`ok`, `partial`, `failed`), `checked_at`, `version`, `edition`, `tier`, `error` and one entry per probe with `status`, `endpoint`, `http_status` and `detail` (see [GITLAB_INTEGRATION.md](GITLAB_INTEGRATION.md#capability-detection)).

`audit_event.details_digest` is the SHA-256 of `details` serialised as canonical JSON: keys sorted, no insignificant whitespace, UTF-8 (`baluarte.audit.details_digest`). Details are redacted before they are hashed, and each committed event is mirrored to the `baluarte.audit` log stream (see [SECURITY.md](SECURITY.md#audit-trail)).

## Migrations

Alembic revisions live in `backend/src/baluarte/db/migrations/versions`. Deployments run `baluarte-migrate`, which waits for PostgreSQL, takes an advisory lock so concurrent runners serialise, and upgrades to head. The API's `/api/v1/ready` endpoint reports not ready until the schema is at head. Tests check that the models and migrations do not drift, and that upgrade, downgrade and re-upgrade succeed.
