# ADR-0018: Assessments and evidence management

- Status: Accepted
- Date: 2026-11-27

## Context

Baluarte judges collected provider evidence with rules mapped to controls. Audits also need a person's conclusion about a control for a period, documents Baluarte cannot collect, a report of missing or out-of-date evidence, and findings with owners and deadlines (FR-036, FR-037). The catalogue is code, tied to one edition of each framework, so records cannot say which version they were made against once a framework changes.

## Decision

- Framework versions and their controls become data. Records pin the version they refer to; a new version is added beside the old one. Catalogue text is Baluarte's paraphrase unless the version's licence allows redistribution.
- Evidence items are uploads or external references, with owner, control links, review by a second person, freshness and retention. **Uploads are stored in the database** (the existing `captured_file` store, once per SHA-256, at most 25 MB each), so backups, off-site copies and restore tests already cover them; no object storage in M15.
- **A control assessment needs an assessor and a separate reviewer**; only approved assessments count, and they never change. Development mode lets one person do both, recorded. Assessments stay separate from automated results and may disagree with them.
- **Finding deadlines default from per-severity workspace values and may be changed with a reason**, audited; overdue findings are shown and notified. Exceptions notify before expiry, and reassessment is a new approval.
- Evidence packages and register exports carry assessments, evidence items and files, verified offline by checksum.

## Consequences

- The database grows with uploads; the 25 MB cap and retention bound it, and backup sizes should be watched.
- Evidence review and assessment review add work for auditors and admins, as two-person review does elsewhere.
- Existing mappings and records are migrated to the seeded framework versions.

## Revisit when

Evidence files routinely exceed 25 MB or the database grows beyond what backups handle comfortably (move files to object storage), or organizations need to import licensed catalogue text in bulk.
