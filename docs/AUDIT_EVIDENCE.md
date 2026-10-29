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

Each stored evaluation names the observation it judged (`evaluation.observation_id`). A `pass` or `fail` is never stored without an `ok` observation whose payload holds the judged value; otherwise the result is `unknown` (see [DATA_MODEL.md](DATA_MODEL.md#evaluations-and-findings)).

Read them with `GET /api/v1/gitlab/projects/{gitlab_id}/observations`, `GET /api/v1/gitlab/groups/{gitlab_id}/observations` and `GET /api/v1/gitlab/instance/observations` (`evidence:read`).

## Export

`POST /api/v1/evidence/exports` (BAL-027) exports scoped evidence as one JSON document (`"format": "json"`) or a ZIP of CSV files (`"format": "csv"`). It needs `evidence:export`, which admins and auditors have and analysts do not: an export is a bulk copy of inventory and evidence.

**Scope.** Every field narrows the export, and an empty scope is refused: `all: true` must be set to export everything, and cannot be combined with filters.

| Field | Selects |
|---|---|
| `group` | Projects in that group and all its subgroups (whole path segments, case-insensitive), and the collection errors of those groups. Must be in the inventory. |
| `projects` | Project GitLab IDs, at most 500. Each must be in the inventory. |
| `policy`, `rule` | Evaluations and findings of that policy name and rule ID. |
| `outcome` | Evaluations with that outcome, and findings whose latest outcome it is. |
| `finding_state` | Findings in that state, and the evaluations of their project, policy and rule. |
| `since`, `until` | Evaluations evaluated in `[since, until)`, findings open at some point in it, and runs started in it. Without a window, the latest evaluation of each project, policy and rule, and the latest finished run of each kind. Timestamps need a time zone. |

The inventory section always lists every project in the group or project scope, whatever the other filters.

**Content.** Read in one `REPEATABLE READ`, read-only transaction, so every section describes the same moment. Each row carries its provenance.

| Section (CSV file) | Holds |
|---|---|
| `inventory` (`inventory.csv`) | Projects in scope as the inventory holds them at export time. |
| `evaluations` (`evaluations.csv`) | Stored results, with policy version, reason codes, expected and observed values, capability and collection status, `observation_id` and `evaluation_run_id`. |
| `findings` (`findings.csv`) | Findings with their latest evaluation and the observation it judged. |
| `observations` (`observations.jsonl`) | Every observation those reference: source, `source_ref`, `collected_at`, `collection_run_id`, `collection_job_id`, payload, `payload_digest`, and `digest_valid` recomputed at export. A `false` is exported as it is, never hidden. |
| `policies` (`policies.csv`) | The exact policy versions referenced: name, version, `yaml_digest`, `effective_at` and the source YAML. |
| `collection_errors` (`collection_errors.csv`) | Projects and groups in scope that are missing, inaccessible or carry a collection error. |
| `collection_runs` (`collection_runs.csv`) | Collection and evaluation runs, with status, error and counts. |

The JSON document also holds the scope, the export ID, `generated_at` (database clock) and the collection and evaluation freshness at export time, and ends with row counts. The ZIP holds the same metadata in `export.json` and a `README.txt` describing the scope, the files and their limitations. `unknown` and `unsupported` results, failed runs and unreachable resources are exported, never dropped.

**No credentials.** Observation payloads are already an allowlist. Every other text value passes through the log redactor (registered secrets, GitLab tokens, bearer and basic credentials, JWTs, credentials in URLs and `token=`-style parameters). A payload that would need redacting is withheld (`payload_withheld: true`, payload null) instead of altered, so its digest still describes the stored row. Policy YAML that needed redacting is marked `source_yaml_redacted`, and its `yaml_digest` then no longer matches the exported text.

**CSV safety.** A cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading apostrophe, so spreadsheets do not run it as a formula. Lists and objects are written as canonical JSON.

**Limits.** At most 100,000 rows (inventory, evaluations, findings and collection errors together). A larger scope is a 422 before anything is sent; narrow it. The response is streamed from paged reads, so memory stays bounded, with `Cache-Control: no-store`.

**Reproducible.** The same evidence and scope give the same rows in the same order. ZIP entries have a fixed timestamp. Only the export ID, `generated_at` and the freshness `checked_at` differ.

**Audit.** `evidence.exported` is recorded when the export is accepted: actor, format, scope, planned row counts and instance. `evidence.export_completed` is recorded once every byte has been produced: the response's size and SHA-256, row counts, and each ZIP file's size, rows and SHA-256. Neither holds exported data. An export interrupted part way has no completion event.

Not yet: `manifest.json` inside the package with per-file hashes and the collection errors summary (BAL-028; the completion audit event already records the per-file hashes), and PDF reports.

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
