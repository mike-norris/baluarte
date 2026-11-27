# Assessments and evidence management

Roadmap M15. Decided in [ADR-0018](architecture/ADR-0018-assessments-and-evidence.md).

Baluarte already collects evidence from the providers (observations, change evidence, captured issue evidence) and judges it with rules mapped to controls ([CONTROL_MAPPING.md](CONTROL_MAPPING.md)). An auditor also needs what Baluarte cannot collect: a person's conclusion about a control for a period, the documents and screenshots behind it, and a clear account of what evidence is missing or out of date. M15 adds those, and gives findings owners and deadlines.

Automated results and human assessments stay separate. A rule result says what Baluarte observed; an assessment says what a person concluded, citing that result among other evidence. Neither overwrites the other.

## Steps

| Step | Delivers | Status |
|---|---|---|
| M15.1 | Framework versions as data: each framework edition a row, its controls rows, the mapping and every record pinned to a version; catalogue text limited to what its licence allows (migration `0050`) | Done |
| M15.2 | Evidence items: uploaded files (stored in the database, 25 MB each, by SHA-256) and external references, with owner, control links, review status, freshness, retention and access (migration `0051`) | Done |
| M15.3 | Control assessments: an assessor's conclusion for a control and period, citing uploaded and collected evidence, approved by a separate reviewer before it counts (migration `0052`) | Done |
| M15.4 | Evidence gaps: missing, stale, inaccessible and incomplete evidence per control and period, on a page and through the API | Planned |
| M15.5 | Finding owners and deadlines (per-severity defaults, editable with a reason, overdue shown and notified); exceptions reassessed before they expire | Planned |
| M15.6 | Exports and acceptance: assessments and evidence items in the evidence package and the register export, files verified offline by checksum; a control assessed by a person with uploaded and collected evidence, exported and verified | Planned |

## Framework versions (M15.1)

- `framework_version`: framework ID, version label (for example `2022`), edition text, publisher, licence note, and whether the catalogue may carry the control text (`text_redistributable`). The current editions (SOC 2 TSC 2017 with 2022 points of focus, ISO/IEC 27001:2022 Annex A, NIST SP 800-53 Rev. 5 moderate) are seeded from the built-in catalogue.
- `framework_control`: per version, the control ID, area and title. Titles stay Baluarte's paraphrases unless the version allows redistribution (NIST, as a US government work). An organization may add its own description of a control for its own use; that text is marked `organization` and never ships in the built-in catalogue.
- The mapping in force, assessments, evidence links and exports record the framework version they refer to. A new version is added beside the old one, never in place of it; records against the old version keep pointing at it.

As built (M15.1):

| Framework | Version | Publisher's text may be carried |
|---|---|---|
| `soc2` | `2017-r2022` | No (AICPA copyright) |
| `iso27001` | `2022` | No (ISO copyright) |
| `nist_800_53_moderate` | `rev5-moderate` | Yes (a US government work), though titles are still Baluarte's paraphrases |

- `control_mapping_change` and `issue_control_mapping` carry `framework_version_id`; rows made before migration `0050` take the seeded version of their framework, and a trigger keeps it fixed. Mapping changes in the API and issue mappings in the register export (`issue_controls.csv`, and the manifest's controls) show `framework_version`.
- Catalogue rows never change except which version is current, and are never deleted (triggers). A test keeps the seeded rows equal to the built-in catalogue (`baluarte.controls.catalogue`).
- An organization's description of a control is a `framework_control_description` row per workspace: each one kept, the latest in force, an empty one clearing it, audited as `framework_control.described` or `framework_control.description_cleared`. The control page shows it under **In our words**; those with `policy:write` edit it.

| Method and path | Permission | Notes |
|---|---|---|
| `GET /api/v1/frameworks` | `findings:read` | Every framework version with its edition, publisher, licence note, `text_redistributable`, `current` and number of controls |
| `GET /api/v1/frameworks/{framework}/{version}` | `findings:read` | One version and its controls (`items`), each with the organization's description in force or null; 404 for an unknown version |
| `PUT /api/v1/frameworks/{framework}/{version}/controls/{control_id}/description` | `policy:write` + CSRF | Body `{description}`, at most 4,000 characters; empty clears it. 404 for an unknown version or control |

`GET /api/v1/controls/{framework}/{control_id}` adds `framework_version` (the current version) and `description`.

## Evidence items (M15.2)

- An **evidence item** is either an uploaded file or an external reference (a URL to a document system or ticket), with a title, description, owner, the controls and framework version it supports, the period it covers, and `valid_until` (its freshness).
- **Uploads** go to the existing `captured_file` store: once per SHA-256, at most 25 MB, a content-type allowlist (PDF, PNG, JPEG, plain text, CSV, JSON, Office documents), checked by content rather than by name. Files are served for download only, never inline, with `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`. The database backups, off-site copies and restore tests cover them.
- **References** record the URL, who added it and, when Baluarte can reach it, the last check (status, time and the SHA-256 of what was fetched). A reference Baluarte cannot reach is `inaccessible`, not assumed fine. Checks are optional and off for hosts not allowed by the deployment.
- **Review status:** `draft` → `submitted` → `accepted` or `rejected`, reviewed by someone other than the person who added it. An accepted item never changes; a correction is a new item that supersedes it.
- **Retention:** evidence follows `evidence_retention_days` ([COMPLIANCE.md](COMPLIANCE.md)); an item cited by an approved assessment is kept while the assessment is. Files are removed only by the retention job, recorded in the audit log.
- **Access:** reading needs `evidence:read`; adding, `evidence:manage` (analysts, auditors, admins); reviewing, `evidence:review` (auditors, admins). An item may be marked restricted, visible only to those with `evidence:review`.

As built (M15.2):

- **Evidence, Evidence items** lists items newest first with their state and freshness; opening one shows its controls, file or link, last check and review. Those with `evidence:manage` add items there and upload, submit or delete their own drafts; reviewers accept, or reject with a note.
- `evidence_item` holds the item and `evidence_item_control` the controls it supports, each pinned to its framework version ([framework versions](#framework-versions-m151)). Triggers let a draft change or be deleted, a submitted item only be accepted or rejected, and a decided item change only in its link check and, through retention, lose its file. Control links change only while the item is a draft.
- **Uploads** are sent as the raw request body (`Content-Type: application/octet-stream`, the name in `filename`); nginx allows 26 MiB on that path only and the API refuses more than 25 MiB (413). The type is judged from the content: `%PDF-`, the PNG and JPEG signatures, an Office Open XML package whose parts match its `.docx`, `.xlsx` or `.pptx` name, or UTF-8 text without NUL bytes named `.txt`, `.md`, `.log`, `.csv` or `.json` (JSON must parse). Anything else, HTML included, is refused (422 `file_type`).
- **Link checks** run only when `BALUARTE_EVIDENCE_CHECK_HOSTS` (Compose; `evidence.checkHosts` in the chart) lists the link's host and the link is https. A check fetches once with a 10-second timeout and no redirects, and records `reachable` (2xx) or `unreachable`, the HTTP status and the SHA-256 of the body (up to 25 MiB). Decided items keep recording checks.
- **Retention** (the daily `retention.expire` job) removes the file of an accepted or rejected item whose evidence ended before the evidence period (`valid_until`, else the end of its period, else the day it was added); the item keeps `removed_sha256` and `file_removed_at`. The stored file is deleted only through `baluarte_expire_captured_file`, a SECURITY DEFINER function that refuses while any item or collected observation still cites it. Counts are in the `retention.expired` audit event (`evidence_item_file`, `captured_file`). Since M15.3, an item's file is kept while an assessment still in force cites it.
- Audit events: `evidence_item.added`, `edited`, `file_attached`, `deleted`, `submitted`, `accepted`, `rejected` and `checked`.

| Method and path | Permission | Notes |
|---|---|---|
| `GET /api/v1/evidence-items` | `evidence:read` | Newest first, at most 500; `state` and `control` (`framework:control_id`, any version) filters; restricted items only for reviewers and whoever added them. `checks_enabled` says whether any host may be checked |
| `GET /api/v1/evidence-items/{id}` | `evidence:read` | One item; 404 when restricted from the caller |
| `POST /api/v1/evidence-items` | `evidence:manage` + CSRF | Body `{kind, title, description?, owner_user_id?, period_start?, period_end?, valid_until?, restricted?, controls: [{framework, control_id, version?}], url?, supersedes_id?}`. 201 with a draft; 422 for an unknown control or a bad address; 409 when `supersedes_id` is not accepted |
| `PUT /api/v1/evidence-items/{id}` | `evidence:manage` + CSRF | Replace a draft's fields and controls; only whoever added it (403), only a draft (409) |
| `PUT /api/v1/evidence-items/{id}/file?filename=` | `evidence:manage` + CSRF | The raw file; 413 over 25 MiB, 422 empty or not an allowed type |
| `GET /api/v1/evidence-items/{id}/file` | `evidence:read` | Always `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, `Cache-Control: private, no-store` and `Digest: sha-256=` |
| `DELETE /api/v1/evidence-items/{id}` | `evidence:manage` + CSRF | A draft, by whoever added it. 204 |
| `POST /api/v1/evidence-items/{id}/submit` | `evidence:manage` + CSRF | Needs the file (file items) and at least one control |
| `POST /api/v1/evidence-items/{id}/accept`, `/reject` | `evidence:review` + CSRF | Body `{note?}`; a rejection needs the note. 403 for one's own outside development mode |
| `POST /api/v1/evidence-items/{id}/check` | `evidence:manage` + CSRF | 409 `check_not_allowed` unless https and an allowed host |

## Control assessments (M15.3)

- An **assessment** names a framework version, a control, a period, the assessor and a conclusion: `effective`, `partially_effective`, `not_effective` or `not_tested`, with a narrative of what was tested and how.
- It cites **evidence**: accepted evidence items, and collected evidence by reference (the control's automated status from an evaluation run, observations, change evidence, captured issue evidence, change sets), each with a rationale. Collected evidence is cited by its stored ID and digest, so the export can prove it is unchanged.
- A **reviewer**, someone other than the assessor, approves or rejects it; only an approved assessment counts. Development mode lets the assessor review their own, and records that.
- Approved assessments never change. A later assessment of the same control and period supersedes the earlier one, and both stay in the record.
- Permissions: `assessments:write` (analysts, auditors, admins) and `assessments:review` (auditors, admins).
- The control's page shows the automated status and the latest approved assessment side by side; they may disagree, and the disagreement is shown, not reconciled.

As built (M15.3):

- **Evidence, Assessments** lists assessments newest first; opening one shows its narrative, citations and review. Those with `assessments:write` start one there, cite evidence, and submit or delete their own drafts; reviewers approve, or reject with a note.
- `control_assessment` holds the assessment, pinned to the framework version's control ([framework versions](#framework-versions-m151)); `assessment_citation` holds what it cites. Triggers let a draft change or be deleted, a submitted assessment only be approved or rejected, and a decided one never change; citations are added or removed only while the assessment is a draft.
- A **citation** is one of: an accepted `evidence_item`, an `observation`, a `change_record` (a change on a default branch), a template `change_set`, or `control_status`, the control's automated status taken when cited (status, evaluation run and counts, stored as a snapshot). Its `digest` is the item's file SHA-256 (or the link's last check), the observation's payload digest (for a change, its observation's), the change set's digest, or the SHA-256 of the snapshot. Restricted evidence items can be cited only by those who may read them.
- Submitting needs at least one citation. `supersedes_id` names an approved assessment of the same control and version; it counts as superseded once the later one is approved, and an assessment is superseded at most once.
- **Retention** keeps any observation or change an assessment cites, and the file of an evidence item cited by an assessment still in force: a draft, a submitted one, or an approved one not yet superseded.
- The control page's **Assessed by a person** card shows the latest approved assessment in any version that no other supersedes, with `disagrees` when it says effective while the automated status is not met, or not effective while met.
- Audit events: `control_assessment.created`, `edited`, `deleted`, `cited`, `uncited`, `submitted`, `approved` and `rejected`.

| Method and path | Permission | Notes |
|---|---|---|
| `GET /api/v1/assessments` | `evidence:read` | Newest first, at most 500, with citations; `state` and `control` (`framework:control_id`, any version) filters |
| `GET /api/v1/assessments/{id}` | `evidence:read` | One assessment |
| `POST /api/v1/assessments` | `assessments:write` + CSRF | Body `{framework, control_id, version?, period_start, period_end, conclusion, narrative, supersedes_id?}`; 201 with a draft; 422 for an unknown control or a bad period; 409 for a `supersedes_id` that is not approved, of another control, or already superseded |
| `PUT /api/v1/assessments/{id}` | `assessments:write` + CSRF | Replace a draft's period, conclusion and narrative; only the assessor (403), only a draft (409) |
| `DELETE /api/v1/assessments/{id}` | `assessments:write` + CSRF | A draft, by its assessor; its citations go with it. 204 |
| `POST /api/v1/assessments/{id}/citations` | `assessments:write` + CSRF | Body `{kind, target_id?, rationale}`; `target_id` for every kind but `control_status`. 409 `not_accepted` for an evidence item not accepted |
| `DELETE /api/v1/assessments/{id}/citations/{citation_id}` | `assessments:write` + CSRF | Only on a draft, by its assessor |
| `POST /api/v1/assessments/{id}/submit` | `assessments:write` + CSRF | 409 `no_evidence` without a citation |
| `POST /api/v1/assessments/{id}/approve`, `/reject` | `assessments:review` + CSRF | Body `{note?}`; a rejection needs the note. 403 for one's own outside development mode |

`GET /api/v1/controls/{framework}/{control_id}` adds `assessment`: the latest approved assessment's ID, framework version, conclusion, period, assessor, reviewer and `disagrees`, or null.

## Evidence gaps (M15.4)

For a framework version and period, each control in scope is reported with:

| Gap | Meaning |
|---|---|
| `missing` | No accepted evidence item and no collected evidence for the period |
| `stale` | Every accepted item's `valid_until` is before the end of the period, or collected evidence is older than its freshness window |
| `inaccessible` | A cited reference failed its last check, or a cited file is gone (removed by retention) |
| `incomplete` | An assessment cites draft or rejected items, has no evidence, or the period has no approved assessment |

## Finding owners and deadlines (M15.5)

- A finding gets an **owner** (a user) and a **deadline**. When a finding opens, its deadline comes from workspace defaults per severity (critical 7 days, high 30, medium 90, low 180; editable in Settings).
- An owner, or someone with `findings:manage` (admins, managers for their groups), may change the owner or deadline with a reason. Each change is audited, and the history stays with the finding.
- Overdue findings are shown in the findings list, the dashboard and the manager space, and notify the owner and the configured channels ([NOTIFICATIONS.md](NOTIFICATIONS.md)) once when they become overdue.
- **Exceptions are reassessed before they expire:** policy and template exceptions notify their requester and approvers a set number of days before expiry (default 14). Reassessing one is a new request that cites the old one and needs a fresh approval; nothing is extended silently.

## Exports and acceptance (M15.6)

- The evidence package (format `baluarte.evidence-package/v3`) and the register export add `framework_versions.csv`, `assessments.csv`, `assessment_evidence.csv`, `evidence_items.csv` and the accepted files under `evidence/files/<sha256>`; restricted items are included only for those who may read them, and their absence is listed.
- `baluarte-verify-export` checks each file against its SHA-256, each citation against an included item or collected record, and that no assessment was reviewed by its assessor outside development mode.
- **Acceptance:** a control assessed by a person, citing an uploaded file and collected evidence, approved by a reviewer, exported and verified offline, with a stale item and a missing control reported as gaps.

## Not in M15

- Object storage for evidence files (files stay in the database; revisit for files over 25 MB).
- Requests from auditors to the organization (a request list with due dates) and auditor portals.
- Importing licensed catalogue text in bulk; organizations add their own descriptions.
