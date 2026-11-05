# Roadmap

- **M0 — Foundation:** docs, standards, repo scaffolding, CI, Docker/Helm smoke tests.
- **M1 — App shell:** FastAPI, React, PostgreSQL migrations, OIDC, RBAC, jobs.
- **M2 — GitLab inventory:** connection, capability detection, paginated discovery, reconciliation, UI.
- **M3 — Policy evaluation:** schema, inheritance, visibility and default-branch rules, findings.
- **M4 — Dashboard:** posture, drill-down, filters, freshness, failure states.
- **M5 — Evidence:** observation history, digests, export manifests, CSV/JSON.
- **M6 — Remediation:** in v0.1, dry-run plans with separate approval (BAL-031) and the exception lifecycle (BAL-033). Deferred past v0.1: idempotent GitLab writes with verification (BAL-032); approved plans are applied by hand ([REMEDIATION.md](REMEDIATION.md#applying-an-approved-plan-in-v01)). See the [remediation research](REMEDIATION_RESEARCH.md) and [ADR-0007](architecture/ADR-0007-remediation-constraints.md).

- **M7 — Evidence trail:** change evidence for every change to a default branch (merge request, approvals, pipeline, tests, deployment, and direct pushes) (FR-013), a past-date view (FR-017) and auditor evidence packages with reproducible sampling (FR-018).
- **M8 — Consistency:** a label taxonomy and drift across groups (FR-019), then approved label plans applied with a separate write token (FR-020, [ADR-0009](architecture/ADR-0009-label-writes.md)).
- **M9 — Manager space:** a manager role, results by group or team, trends, time tracking including per person (FR-021, [ADR-0010](architecture/ADR-0010-time-tracking-personal-data.md)), and Baluarte tags (FR-022, FR-023).

**v0.1** targets M0–M5 in read-only mode. No delivery dates are promised until GitLab edition/version, project scale, and contributor capacity are established. Milestone work is tracked in GitHub issues.
