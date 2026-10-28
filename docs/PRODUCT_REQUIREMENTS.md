# Product requirements — Baluarte v0.1

## Objective

Give IT and security leaders a trustworthy, centralized view of self-managed GitLab configuration posture and historical evidence. Reduce manual SOC 2 / ISO 27001 / NIST audit preparation without claiming that a tool alone establishes compliance.

## Personas

- Administrator: manages instance connection, users, and baselines.
- Security analyst: reviews findings and policy outcomes.
- Auditor: reads scoped, exported evidence without GitLab write access.

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
| FR-013 | Record MR approval, pipeline, deployment, and test evidence | Later | GitLab version/tier feasibility spike |
| FR-014 | Control mapping to SOC 2, ISO 27001, NIST | Later | Reviewed mapping documentation |
| FR-015 | Approved, dry-run-first, verified remediation | Later | Isolated integration tests |
| FR-016 | Multi-instance GitLab inventory | Later | Separate-tenant isolation tests |

## Explicit exclusions from v0.1

Automatic GitLab writes; GitHub/Bitbucket integration; Kubernetes cluster auditing; compliance certification; claims that current GitLab settings prove historical compliance; hosted SaaS requirements.

## Nonfunctional requirements

Secure defaults, non-root containers, no mandatory internet egress, UTC timestamps, audit logging, bounded API rate usage, migration safety, backup/restore guidance, operational health endpoints, and meaningful unknown/error states. Target performance and retention limits must be measured and agreed before production GA.

## Open decisions

Supported minimum GitLab version and edition; initial OIDC provider and authorization mapping; baseline inheritance precedence; retention period; webhook availability; preferred PDF export timing; expected GitLab instance/project scale.
