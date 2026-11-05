# Product requirements — Baluarte v0.1

## Objective

Give auditors, managers and security leaders of organizations with many GitLab groups, subgroups and projects one trustworthy place to track evidence, compliance and consistency, and to prove the trail behind each result. Reduce manual SOC 2 / ISO 27001 / NIST audit preparation without claiming that a tool alone establishes compliance.

Baluarte is self-hosted and works on every GitLab tier. Its distinguishing promises are:

- **A provable trail:** every result names the observation it rests on, with a digest and its source, and can be shown as it stood on a past date.
- **Unknown, never assumed:** what Baluarte could not confirm stays unknown.
- **Reasoned decisions:** exclusions, exceptions and mapping changes carry a justification, a reviewer and an expiry.
- **Consistency at scale:** one baseline, one label taxonomy and one control mapping across every group.

## Personas

- Administrator: manages instance connection, users, and baselines.
- Security analyst: reviews findings and policy outcomes.
- Auditor: reads scoped, exported evidence without GitLab write access; samples changes and follows each result back to its evidence.
- Manager or director: follows posture, consistency and time spent across the groups and teams they own; tags projects in Baluarte; cannot change baselines or settings.

## Functional requirements

| ID | Requirement | Release | Verification |
|---|---|---|---|
| FR-001 | Configure one self-managed GitLab instance and test read-only connectivity | v0.1 | Connectivity and permission tests |
| FR-002 | Detect GitLab version, edition/tier where available, and API capabilities | v0.1 | Capability matrix with unsupported cases |
| FR-003 | Discover groups, subgroups, projects, archived state, and stable GitLab IDs | v0.1 | Paginated reconciliation test |
| FR-004 | Schedule full inventory reconciliation and expose collection freshness | v0.1 | New, changed, deleted/inaccessible test |
| FR-005 | Validate versioned YAML baselines and scope them to groups/projects | v0.1 | Schema and inheritance tests |
| FR-006 | Evaluate project visibility and protected default branch controls | v0.1 | Deterministic rule fixtures |
| FR-007 | Distinguish pass, fail, unknown, unsupported, and exception | v0.1 | Status transition tests |
| FR-008 | Show inventory, findings, evaluation details, and source timestamps in UI | v0.1 | Accessible UI tests |
| FR-009 | Store observation-time evidence and cryptographic digests | v0.1 | Evidence integrity tests |
| FR-010 | Export scoped evidence as CSV and JSON with manifest | v0.1 | Export reproducibility tests |
| FR-011 | Authenticate with OIDC and enforce server-side role checks | v0.1 | Authorization tests |
| FR-012 | Deploy identical OCI images via Docker Compose and Helm | v0.1 | Deployment smoke tests |
| FR-013 | Record MR approval, pipeline, deployment, and test evidence | Next (M7) | GitLab version/tier feasibility spike |
| FR-014 | Control mapping to SOC 2, ISO 27001, NIST | Later | Reviewed mapping documentation |
| FR-015 | Approved, dry-run-first, verified remediation | Later | Isolated integration tests |
| FR-016 | Multi-instance GitLab inventory | Later | Separate-tenant isolation tests |
| FR-017 | Show posture, findings and evidence as they stood on a past date | Next (M7) | Point-in-time reproducibility tests |
| FR-018 | Auditor evidence package: a period, a reproducible sample of changes, and each item's evidence | Next (M7) | Export reproducibility and seed tests |
| FR-019 | A label taxonomy defined in Baluarte, with drift reported across groups and projects | M8 | Drift fixtures (missing, misnamed, colour, duplicate) |
| FR-020 | Apply approved label plans in GitLab with a separate write token ([ADR-0009](architecture/ADR-0009-label-writes.md)) | M8 | Dry-run, idempotency and verification tests |
| FR-021 | Time tracking: spent and estimated time by group, project, label, milestone and person ([ADR-0010](architecture/ADR-0010-time-tracking-personal-data.md)) | M9 | Totals reconciliation and access tests |
| FR-022 | A manager role and space: results by group or team, trends and time | M9 | Authorization tests |
| FR-023 | Tags kept in Baluarte to group projects across the GitLab hierarchy | M9 | Tag scoping tests |

## Explicit exclusions from v0.1

Automatic GitLab writes (approved label plans are the first planned exception, FR-020); GitHub/Bitbucket integration; Kubernetes cluster auditing; compliance certification; claims that current GitLab settings prove historical compliance; hosted SaaS requirements.

## Nonfunctional requirements

Secure defaults, non-root containers, no mandatory internet egress, UTC timestamps, audit logging, bounded API rate usage, migration safety, backup/restore guidance, operational health endpoints, and meaningful unknown/error states. Target performance and retention limits must be measured and agreed before production GA.

## Open decisions

Supported minimum GitLab version and edition; initial OIDC provider and authorization mapping; baseline inheritance precedence; retention period; webhook availability; preferred PDF export timing; expected GitLab instance/project scale.
