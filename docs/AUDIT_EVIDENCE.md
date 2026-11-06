# Audit evidence and export

## Evidence lifecycle

Collect -> normalize -> timestamp -> hash -> persist -> evaluate -> export. Preserve source provenance, collection errors, and policy version. Capture evidence when it exists; GitLab artifacts and audit events may be subject to retention and edition limits.

## Observations

Every discovery and capability detection records what it read as an `observation` row, in the same transaction as the inventory change it describes (BAL-026):

- **Payload:** only an allowlist of normalized fields, never the raw GitLab response, so URLs, tokens and unrelated fields are not stored.

  | Target | Source | Payload fields |
  |---|---|---|
  | Project | `GET /projects` (listing) or `GET /projects/:id` (reconciliation) | `gitlab_id`, `path_with_namespace`, `name`, `namespace_gitlab_id`, `namespace_kind`, `visibility`, `archived`, `default_branch` |
  | Group | `GET /groups` or `GET /groups/:id` | `gitlab_id`, `parent_gitlab_id`, `full_path`, `name`, `visibility`, `require_two_factor_authentication`, `two_factor_grace_period` |
  | Project | `GET /projects/:id/protected_branches`, `GET /projects/:id/approvals`, `GET /projects/:id/members/all`, `GET /projects/:id/access_tokens`, `GET /groups (two-factor settings of the project's groups)` | One observation per source, written by project discovery; fields in [GITLAB_INTEGRATION.md](GITLAB_INTEGRATION.md#access-controls) |
  | Instance | `capabilities` | `version`, `edition`, `tier`, `probes` (status, endpoint, HTTP status, detail of each probe), `token_user` (`username`, `is_admin`, `admin_access`) |

  Values are stored as Baluarte normalized them: an unrecognised visibility or namespace kind is null, exactly as in the inventory.
- **Provenance:** `source` (endpoint template), `source_ref` (API-relative path such as `/projects/42`), `collected_at` (database clock), `collection_run_id` and `collection_job_id`.
- **Digest:** `payload_digest` is the SHA-256 of the payload's canonical JSON (sorted keys, no insignificant whitespace, UTF-8), the same algorithm as `audit_event.details_digest`. The observation API returns `digest_valid` by recomputing it.
- **Could not observe:** a resource the reconciliation lookup could not read gets an observation with no payload, `collection_status` `forbidden` (401/403), `not_found` (404) or `failed` (any other error), and the safe client error. A failed capability detection is recorded the same way.
- **Growth:** a new row is written only when a target's `collection_status`, `payload_digest` or `error` differs from its latest observation. Unchanged data is not copied each run: the latest observation remains the evidence, and the resource's `last_seen_at` and the collection runs show it was confirmed since. Evaluations reference the observation they judged, which never changes (a trigger rejects `UPDATE`).
- **Retention:** observations older than the evidence period are deleted daily, except the newest of each target and source and any an evaluation (`observation_id` or `observation_ids`) or a recorded change still refers to ([OPERATIONS.md](OPERATIONS.md#retention)). `retention_until` is not used. A listing that fails part way records no per-resource observation; the failed collection run is the evidence.

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
| `framework`, `control_id` | Together: evaluations, findings and exceptions of the rule types mapped to that control under the mapping in force ([CONTROL_MAPPING.md](CONTROL_MAPPING.md)). The control must be in the catalogue. |
| `since`, `until` | Evaluations evaluated in `[since, until)`, findings open at some point in it, and runs started in it. Without a window, the latest evaluation of each project, policy and rule, and the latest finished run of each kind. Timestamps need a time zone. |

The inventory section always lists every project in the group or project scope, whatever the other filters.

**Content.** Read in one `REPEATABLE READ`, read-only transaction, so every section describes the same moment. Each row carries its provenance.

| Section (CSV file) | Holds |
|---|---|
| `inventory` (`inventory.csv`) | Projects in scope as the inventory holds them at export time. |
| `evaluations` (`evaluations.csv`) | Stored results, with policy version, reason codes, expected and observed values, capability and collection status, `observation_id`, `evaluation_run_id` and, for an `excepted` result, `exception_id`. |
| `findings` (`findings.csv`) | Findings with their latest evaluation, the observation it judged and, while waived, `exception_id`. |
| `observations` (`observations.jsonl`) | Every observation those reference: source, `source_ref`, `collected_at`, `collection_run_id`, `collection_job_id`, payload, `payload_digest`, and `digest_valid` recomputed at export. A `false` is exported as it is, never hidden. |
| `policies` (`policies.csv`) | The exact policy versions referenced: name, version, `yaml_digest`, `effective_at` and the source YAML. |
| `exceptions` (`exceptions.csv`) | Policy exceptions in scope, and every exception an exported evaluation or finding references: project, policy name, rule, `state`, `effective_state`, justification, decision and end notes, and the request, decision, expiry and end times. `effective_state` is the state at export time (database clock), counting a requested or approved exception past `expires_at` as `expired`, as the API does. People appear only as `user:<id>`. Exceptions are narrowed by project, group, policy, rule and window (one requested before `until` that had not ended, been rejected or expired before `since`); `outcome` and `finding_state` do not narrow them. |
| `collection_errors` (`collection_errors.csv`) | Projects and groups in scope that are missing, inaccessible or carry a collection error. |
| `collection_runs` (`collection_runs.csv`) | Collection and evaluation runs, with status, error and counts. |

The JSON document also holds the scope, the export ID, `generated_at` (database clock) and the collection and evaluation freshness at export time, and ends with row counts and the [manifest](#manifest-and-verification). The ZIP holds the same metadata in `export.json`, a `README.txt` describing the scope, the files and their limitations, and ends with `manifest.json` and `SHA256SUMS`. `unknown` and `unsupported` results, failed runs and unreachable resources are exported, never dropped.

**No credentials.** Observation payloads are already an allowlist. Every other text value passes through the log redactor (registered secrets, GitLab tokens, bearer and basic credentials, JWTs, credentials in URLs and `token=`-style parameters). A payload that would need redacting is withheld (`payload_withheld: true`, payload null) instead of altered, so its digest still describes the stored row. Policy YAML that needed redacting is marked `source_yaml_redacted`, and its `yaml_digest` then no longer matches the exported text.

**CSV safety.** A cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading apostrophe, so spreadsheets do not run it as a formula. Lists and objects are written as canonical JSON.

**Limits.** At most 100,000 rows (inventory, evaluations, findings, exceptions and collection errors together). A larger scope is a 422 before anything is sent; narrow it. The response is streamed from paged reads, so memory stays bounded, with `Cache-Control: no-store`.

**Reproducible.** The same evidence and scope give the same rows in the same order. ZIP entries have a fixed timestamp. Only the export ID, `generated_at` and the freshness `checked_at` differ, and with them the files and hashes that hold them (`README.txt`, `export.json`, the manifest and `SHA256SUMS`, and the JSON export's `metadata_sha256`).

**Audit.** `evidence.exported` is recorded when the export is accepted: actor, format, scope, planned row counts and instance. `evidence.export_completed` is recorded once every byte has been produced: the response's size and SHA-256, `manifest_sha256`, row counts, and each ZIP file's (or JSON section's) rows and SHA-256. Neither holds exported data. An export interrupted part way has no completion event.

Not yet: PDF reports.

### Manifest and verification

Every export carries a manifest (BAL-028): `manifest.json` in a ZIP, written after every file it lists, or the `manifest` member at the end of a JSON export.

| Field | Holds |
|---|---|
| `manifest_format`, `export_format`, `package` | `baluarte.evidence-manifest/v7`, `baluarte.evidence-export/v5` (or `baluarte.evidence-package/v1`), and `zip` or `json`. Version 2 added the exceptions section, `exception_id` on evaluations and findings, and the manifest's `exceptions` summary. Manifest version 3 added `population` and version 4 `control_mapping`; the export format did not change. Export version 3 and manifest version 5 add monitoring scope: `monitoring_excluded`, `exclusion_id` and `excluded_via` in the inventory, and `monitoring_scope` in the manifest. Export version 4 and manifest version 6 add `observation_ids` and `evidence_digest` to evaluations, and `evidence_package` to packages. Export version 5 and manifest version 7 add `tags` to the inventory and, only with `include_people`, the `time_entries` section and the manifest's `time_people` ([TIME_TRACKING.md](TIME_TRACKING.md#exports)). |
| `monitoring_scope` | The exclusions from monitoring in force (`id`, `target_type`, `target_id`, `target_path`, `since`, `justification_sha256`) and `excluded_projects`, the inventory projects in the export's scope they cover. Excluded projects appear only in the inventory, flagged; every other section leaves them out ([MONITORING_SCOPE.md](MONITORING_SCOPE.md#evidence)). |
| `control_mapping` | The framework control mapping in force: `mapping_version`, every effective rule type and control pair with its source, and every active organization change with the SHA-256 of its justification ([CONTROL_MAPPING.md](CONTROL_MAPPING.md#evidence)). |
| `export_id`, `generated_at`, `baluarte_version`, `instance`, `scope` | What was exported, when (database clock), by which version, from which GitLab. |
| `requested_by` | The requester's internal user ID (`user:<id>`), as in the audit log. Names and email addresses are not copied into the package; the audit log and user table resolve the ID. |
| `files` (ZIP) | Every other file except `SHA256SUMS`: name, rows (CSV records without the header, or JSON lines), bytes and SHA-256 of its bytes. |
| `sections`, `metadata_sha256` (JSON) | Each section's rows and SHA-256 over the canonical JSON of each row followed by a newline, in document order; and the SHA-256 of the canonical JSON of the metadata members (`format`, `export_id`, `generated_at`, `instance`, `scope`, `freshness`). |
| `counts` | Rows per section. |
| `policies` | Each policy version referenced: ID, name, version, `yaml_digest`, and whether its exported YAML was redacted. |
| `evaluations` | Exported results by outcome, and how often each unknown reason occurs. |
| `observations` | Count, count by collection status, how many had `digest_valid: false` (with their IDs, up to 1,000), how many payloads were withheld, and how many observations have no payload (could not observe). |
| `collection_errors` | Projects and groups in scope by type and state, and how many carry an error message. |
| `exceptions` | How many exceptions were exported, by effective state at export time. |
| `runs_not_succeeded` | Exported collection and evaluation runs that failed, were partial or were still running, with their (redacted) error. |
| `freshness` | Collection status per kind with its last success, and evaluation status, at export time. |
| `digests` | How every hash in the package is computed. |
| `population` | Which groups and projects the export can cover: `basis` (`instance`, `token_visibility` or `unknown`), `token_username`, `is_admin`, `admin_access` and `statement`, from the collection freshness at export time. Projects the token cannot see are absent from every section. Added in manifest v3. |
| `governance` | `require_review`: whether a baseline change needed a second person's approval when the export was made. Each change's own record (`policy_change`) says whether review was required for it and who approved it. |
| `operations` | The operating profile Baluarte itself ran to when the export was made ([COMPLIANCE.md](COMPLIANCE.md)): `configured`, `frameworks`, the operating `settings` (retention periods, session limits, review, legal hold, backup interval), the `posture` (`summary` by status and each control's `status`) and `exceptions_in_force` (`id`, `control_id`, `expires_on`). It describes Baluarte's own operation, not the GitLab instance. |
| `limitations`, `verification` | What the export does not establish, and how to check it. The population statement is the last limitation. |

Hashes use SHA-256. Canonical JSON means keys sorted, no insignificant whitespace, non-ASCII characters unescaped, UTF-8: the same form as observation and audit digests. A ZIP's `SHA256SUMS` lists every file including `manifest.json` in `sha256sum` format, so `sha256sum -c SHA256SUMS` (or `shasum -a 256 -c SHA256SUMS`) checks an extracted package with standard tools.

**Anchoring the manifest.** A file cannot hold its own hash, and anyone who can edit a package can rewrite its manifest and `SHA256SUMS` to match. So `manifest_sha256`, the SHA-256 of the manifest's canonical JSON, is recorded in the `evidence.export_completed` audit event and nowhere in the package. Comparing the two is what ties a package to what Baluarte produced; it is only as trustworthy as the audit record it is compared with, so prefer the copy shipped outside the database by [audit log shipping](COMPLIANCE.md#audit-log-shipping) or in the `baluarte.audit` log stream (see [Audit limitations](#audit-limitations)).

**Verifying.** `baluarte-verify-export <file> --manifest-sha256 <value from the audit event>` checks a ZIP or JSON export offline, with no database: every file or section against the manifest, a ZIP against `SHA256SUMS`, no file missing or unlisted, the counts, and every observation's `payload_digest` recomputed from its payload. It exits 0 when every check passes, 1 when any fails and 2 when the file cannot be read. The verifier prints the manifest's population statement when it has one; a manifest from before population (v2) still verifies. Evidence that was already invalid when exported (`digest_valid: false`) and withheld payloads are reported as warnings: the export shows them truthfully, so they do not fail the package. Without `--manifest-sha256` it says the manifest itself was not checked. A `baluarte.evidence-export/v2` or `v3` package must contain its exceptions (`exceptions.csv`, or the `exceptions` section), and the manifest's exception count must match; a package from before exceptions (`v1`) is still accepted and checked against what its own manifest lists. The verifier is one standard-library file (`backend/src/baluarte/evidence/verify.py`) and also runs as `python3 verify.py <file>` on Python 3.9 or later.

What verification does not show: rows changed in the database before the export together with their digests, deleted or forged rows, or anything about GitLab's own records. The digests are not anchored outside the database.

## Evidence package for an audit period

`POST /api/v1/evidence/packages` (`evidence:package`: admin, analyst and auditor) builds one ZIP for a period: results as of its end, every change that landed in it, a reproducible sample of merge requests and direct pushes (drawn with a recorded seed), every version of each sampled change, the control mapping and monitoring scope in force at the end, and a README on verifying it. Its manifest adds `evidence_package`; `baluarte-verify-export` also checks the change evidence digests and redraws the sample. Audited as `evidence.package_created` and `evidence.package_completed`; `GET /api/v1/evidence/packages/{export_id}` returns the seed and manifest hash. See [CHANGE_EVIDENCE.md](CHANGE_EVIDENCE.md#evidence-package).

## Audit language

A rule evaluation is supporting evidence for an organization-defined control objective; it is **not** a SOC 2 certification, ISO 27001 certification, or NIST authorization. Mapping must be reviewed by the organization's compliance owner.

## Retention

The operating profile sets how long audit events, evidence and jobs are kept (365 days or more for audit events and evidence under SOC 2, ISO 27001 and NIST 800-53), and a daily job deletes what has expired; legal hold stops it. See [OPERATIONS.md](OPERATIONS.md#retention) for exactly what is deleted and what is always kept, and [COMPLIANCE.md](COMPLIANCE.md) for the profile.

Expiring audit events is itself evidence: each run is audited as `retention.expired` with the cutoffs, the counts per table and, for audit events, the first and last IDs deleted and a SHA-256 over their `details_digest` values in ID order. An export made before the deletion lets an auditor recompute that digest and confirm exactly which events were removed. Keep exports for at least as long as the period they cover. Not implemented: archiving before deletion, storage encryption (attested in the posture, not observed) and independent integrity anchors. Never export credentials or sensitive token values.

## Audit limitations

What the audit trail and evidence do and do not establish today:

- **Database administrators can rewrite history.** The `audit_event` triggers reject `UPDATE` and `DELETE`, but they are row-level: a PostgreSQL superuser or the table owner (the application's own database user, which in the Compose stack is also a superuser) can disable them, and `TRUNCATE` does not fire them at all.
- **Digests are not anchored.** `details_digest` (and `observation.payload_digest`) detects a row whose details changed without its digest; it does not detect a rewrite that recomputes the digest, a deleted row, or a forged one. Nothing chains rows or publishes digests outside the database yet.
- **The shipped copy is the independent record.** [Audit log shipping](COMPLIANCE.md#audit-log-shipping) copies every event, with its ID and `details_digest`, to a syslog or Elasticsearch/OpenSearch destination outside Baluarte, and retention keeps any event not yet delivered. Each committed event is also written to the `baluarte.audit` log, which a platform collector can ship instead. A copy the database administrators cannot alter is the main mitigation; without one the database is the only record. Comparing the destination's copy with the database (same IDs, same digests) shows rewritten or deleted rows up to the moment of shipping; a row changed before it shipped is shipped as changed.
- **Restores lose recent history.** Restoring a backup discards every event, observation and run recorded after the backup point; the shipped copy keeps the audit events. Record the restore itself (time, backup used, operator) outside Baluarte; the `restore-test` attestation records the yearly test. Backups are encrypted to a key Baluarte does not hold, and each run's file name, size and SHA-256 are kept in `backup_run`.
- **Time comes from the database clock.** `occurred_at` and run times are PostgreSQL's `now()`; keep the database host on NTP.
- **Collection is at least once.** A job can run more than once after a crash or expired lease. Inventory upserts are idempotent, but run history can show an interrupted attempt next to the retry.
- **The inventory is what the token can see.** A token that is not a GitLab administrator sees only its user's groups and projects, so others are neither inventoried nor checked. The freshness report, the Dashboard, the Inventory page and every export state this population (`population`), but Baluarte cannot list what the token cannot see.
- **GitLab limits what can be shown.** Results depend on the token's visibility, the GitLab edition and tier, and GitLab's own retention of audit events and artifacts; `missing` means deleted or hidden, and capability probes record what was refused rather than inferring it.
- **The audit log checks itself on read.** `GET /api/v1/audit-events` (and the Audit log page) reports `digest_valid` for each event, recomputed from its stored details; like every digest here, it catches an edit that left the digest unchanged, not a rewrite.
- **Evidence is not certification.** See [Audit language](#audit-language): Baluarte records configuration evidence; it does not by itself establish compliance with any framework.

`deploy/compose/backup-restore-test.sh` demonstrates both the digest check and its limit: it rewrites a row with the trigger disabled and shows that the check catches it only because the digest was left unchanged.
