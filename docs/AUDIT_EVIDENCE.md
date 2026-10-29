# Audit evidence and export

## Evidence lifecycle

Collect -> normalize -> timestamp -> hash -> persist -> evaluate -> export. Preserve source provenance, collection errors, and policy version. Capture evidence when it exists; GitLab artifacts and audit events may be subject to retention and edition limits.

## Observations

Every discovery and capability detection records what it read as an `observation` row, in the same transaction as the inventory change it describes (BAL-026):

- **Payload:** only an allowlist of normalized fields, never the raw GitLab response, so URLs, tokens and unrelated fields are not stored.

  | Target | Source | Payload fields |
  |---|---|---|
  | Project | `GET /projects` (listing) or `GET /projects/:id` (reconciliation) | `gitlab_id`, `path_with_namespace`, `name`, `namespace_gitlab_id`, `namespace_kind`, `visibility`, `archived`, `default_branch` |
  | Group | `GET /groups` or `GET /groups/:id` | `gitlab_id`, `parent_gitlab_id`, `full_path`, `name`, `visibility` |
  | Instance | `capabilities` | `version`, `edition`, `tier`, `probes` (status, endpoint, HTTP status, detail of each probe) |

  Values are stored as Baluarte normalized them: an unrecognised visibility or namespace kind is null, exactly as in the inventory.
- **Provenance:** `source` (endpoint template), `source_ref` (API-relative path such as `/projects/42`), `collected_at` (database clock), `collection_run_id` and `collection_job_id`.
- **Digest:** `payload_digest` is the SHA-256 of the payload's canonical JSON (sorted keys, no insignificant whitespace, UTF-8), the same algorithm as `audit_event.details_digest`. The observation API returns `digest_valid` by recomputing it.
- **Could not observe:** a resource the reconciliation lookup could not read gets an observation with no payload, `collection_status` `forbidden` (401/403), `not_found` (404) or `failed` (any other error), and the safe client error. A failed capability detection is recorded the same way.
- **Growth:** a new row is written only when a target's `collection_status`, `payload_digest` or `error` differs from its latest observation. Unchanged data is not copied each run: the latest observation remains the evidence, and the resource's `last_seen_at` and the collection runs show it was confirmed since. Evaluations reference the observation they judged, which never changes (a trigger rejects `UPDATE`).
- **Not yet:** `retention_until` is not set and nothing expires observations ([OPERATIONS.md](OPERATIONS.md#retention)). A listing that fails part way records no per-resource observation; the failed collection run is the evidence.

Read them with `GET /api/v1/gitlab/projects/{gitlab_id}/observations`, `GET /api/v1/gitlab/groups/{gitlab_id}/observations` and `GET /api/v1/gitlab/instance/observations` (`evidence:read`).

## Export package

ZIP containing `manifest.json`, `inventory.csv`, `evaluations.csv`, `observations.jsonl` (redacted), `collection_errors.csv`, and `README.txt` describing scope and limitations. Include per-file SHA-256 hashes and package generation metadata. PDF reports are a later enhancement.

## Audit language

A rule evaluation is supporting evidence for an organization-defined control objective; it is **not** a SOC 2 certification, ISO 27001 certification, or NIST authorization. Mapping must be reviewed by the organization's compliance owner.

## Retention

Nothing is deleted automatically yet; see [OPERATIONS.md](OPERATIONS.md#retention) for what accumulates and what is not implemented. A configurable policy must be designed before production GA: retention period, deletion and legal-hold semantics, storage encryption, backup retention and independent integrity anchors. Never export credentials or sensitive token values.

## Audit limitations

What the audit trail and evidence do and do not establish today:

- **Database administrators can rewrite history.** The `audit_event` triggers reject `UPDATE` and `DELETE`, but they are row-level: a PostgreSQL superuser or the table owner (the application's own database user, which in the Compose stack is also a superuser) can disable them, and `TRUNCATE` does not fire them at all.
- **Digests are not anchored.** `details_digest` (and `observation.payload_digest`) detects a row whose details changed without its digest; it does not detect a rewrite that recomputes the digest, a deleted row, or a forged one. Nothing chains rows or publishes digests outside the database yet.
- **The log stream is the independent copy.** Each committed event is also written to the `baluarte.audit` log. Shipping that stream to a collector the database administrators cannot alter is the main mitigation today; without it the database is the only record.
- **Restores lose recent history.** Restoring a backup discards every event, observation and run recorded after the backup point. Record the restore itself (time, backup used, operator) outside Baluarte.
- **Time comes from the database clock.** `occurred_at` and run times are PostgreSQL's `now()`; keep the database host on NTP.
- **Collection is at least once.** A job can run more than once after a crash or expired lease. Inventory upserts are idempotent, but run history can show an interrupted attempt next to the retry.
- **GitLab limits what can be shown.** Results depend on the token's visibility, the GitLab edition and tier, and GitLab's own retention of audit events and artifacts; `missing` means deleted or hidden, and capability probes record what was refused rather than inferring it.
- **Evidence is not certification.** See [Audit language](#audit-language): Baluarte records configuration evidence; it does not by itself establish compliance with any framework.

`deploy/compose/backup-restore-test.sh` demonstrates both the digest check and its limit: it rewrites a row with the trigger disabled and shows that the check catches it only because the digest was left unchanged.
