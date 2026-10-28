# Audit evidence and export

## Evidence lifecycle

Collect -> normalize -> timestamp -> hash -> persist -> evaluate -> export. Preserve source provenance, collection errors, and policy version. Capture evidence when it exists; GitLab artifacts and audit events may be subject to retention and edition limits.

## Export package

ZIP containing `manifest.json`, `inventory.csv`, `evaluations.csv`, `observations.jsonl` (redacted), `collection_errors.csv`, and `README.txt` describing scope and limitations. Include per-file SHA-256 hashes and package generation metadata. PDF reports are a later enhancement.

## Audit language

A rule evaluation is supporting evidence for an organization-defined control objective; it is **not** a SOC 2 certification, ISO 27001 certification, or NIST authorization. Mapping must be reviewed by the organization's compliance owner.

## Retention

Configurable policy to be designed before production GA. Define deletion and legal-hold semantics, storage encryption, backup, and independent integrity anchors. Never export credentials or sensitive token values.
