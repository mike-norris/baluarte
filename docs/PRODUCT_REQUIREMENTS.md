# Product requirements

## Objective

Baluarte extends GitLab and GitHub with:

- security governance and bulk configuration management;
- compliance assessments and auditable evidence;
- engineering portfolio planning and reporting;
- developer time tracking and labor cost allocation;
- financial reporting for CapEx and OpEx.

GitLab and GitHub remain the systems of record for repositories, code, issues, merge or pull requests and CI/CD. Baluarte connects those records to engineering, security, audit and finance work, for organizations that use GitLab, GitHub or both, with several connections of each. It reduces manual SOC 2, ISO 27001 and NIST audit preparation without claiming that a tool alone establishes compliance.

Baluarte is self-hosted. Its distinguishing promises are:

- **A provable trail:** every result, report total and export names the records it rests on, with their source, digest and time, and can be shown as it stood on a past date.
- **Unknown, never assumed:** what Baluarte could not confirm stays unknown; unsupported, inaccessible, stale and genuinely empty data are told apart. Neither provider is assumed to lack a feature: capabilities are verified and detected.
- **Reasoned decisions:** exclusions, exceptions, mapping changes, bulk changes, time approvals and finance classifications carry a justification, an approver and, where relevant, an expiry.
- **Consistency at scale:** one baseline, one set of configuration templates, one label taxonomy and one control mapping across every group, organization and provider.
- **Safe by default:** read-only until a change is explicitly authorized; changes are previewed, approved, verified and never silently weaken a control.

## Personas

- **Engineering managers and directors:** cross-team planning, roadmaps, dependencies, capacity, time approvals, delivery reporting and repository standards. Tag and organize projects; cannot change baselines or settings unless also an administrator.
- **Security auditors and compliance managers:** auditable issue registers, control assessments, evidence collection, exceptions, remediation and audit exports, without write access to the providers.
- **Platform and security administrators:** connections, repository discovery, configuration templates, bulk changes, policy enforcement and drift remediation.
- **Security analysts:** review findings and policy outcomes, request exceptions and plan fixes.
- **Finance managers and directors:** capitalization policies, labor cost allocation, rates, CapEx/OpEx reporting, reconciliation and period closing.
- **Developers:** record and submit their time.

## Functional requirements

| ID | Requirement | Release | Verification |
|---|---|---|---|
| FR-001 | Configure a GitLab connection and test read-only connectivity | v0.1 | Connectivity and permission tests |
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
| FR-013 | Record MR approval, pipeline, deployment, and test evidence | M7 | GitLab version/tier feasibility spike |
| FR-014 | Control mapping to SOC 2, ISO 27001, NIST | Done | Reviewed mapping documentation |
| FR-015 | Approved, dry-run-first, verified remediation ([REMEDIATION.md](REMEDIATION.md), [ADR-0012](architecture/ADR-0012-remediation-writes.md)) | M6 | Isolated integration tests against a fake GitLab |
| FR-016 | Workspaces and several GitLab and GitHub connections, isolated across APIs, jobs, storage and exports ([ADR-0013](architecture/ADR-0013-workspaces-and-connections.md)) | M11 | Workspace isolation tests on every router and job; upgrade keeps every digest |
| FR-017 | Show posture, findings and evidence as they stood on a past date | M7 | Point-in-time reproducibility tests |
| FR-018 | Auditor evidence package: a period, a reproducible sample of changes, and each item's evidence | M7 | Export reproducibility and seed tests |
| FR-019 | A label taxonomy defined in Baluarte, with drift reported across groups and projects | M8 | Drift fixtures (missing, misnamed, colour, duplicate) |
| FR-020 | Apply approved label plans in GitLab with a separate write token ([ADR-0009](architecture/ADR-0009-label-writes.md)) | M8 | Dry-run, idempotency and verification tests |
| FR-021 | Time tracking: spent and estimated time by group, project, label, milestone and person ([ADR-0010](architecture/ADR-0010-time-tracking-personal-data.md)) | M9 | Totals reconciliation and access tests |
| FR-022 | A manager role and space: results by group or team, trends and time | M9 | Authorization tests |
| FR-023 | Tags kept in Baluarte to group projects across the GitLab hierarchy | M9 | Tag scoping tests |
| FR-024 | Notifications to email and a chat webhook, off by default, each event sent once with a recorded delivery and no personal data ([NOTIFICATIONS.md](NOTIFICATIONS.md)) | M10 | Dedup, retry, TLS and privacy tests against local SMTP and webhook servers |
| FR-025 | Auditor report: a PDF of one framework's controls over an audit period, read as the evidence package reads it and verifiable against it ([AUDIT_EVIDENCE.md](AUDIT_EVIDENCE.md#auditor-report)) | M7 | Content, escaping, non-Latin text, caps, determinism and audit tests |
| FR-026 | Connection health, synchronization coverage, errors and last successful collection per connection; connection URLs checked against server-side request forgery | M11 | Health and SSRF tests |
| FR-027 | Auditable issue register: selection rules and explicit inclusion, why an issue is auditable, many-to-many issue-to-control mappings, evidence gaps, filters by framework, control, period, provider, project, owner, status and readiness | M12 | Register and mapping tests |
| FR-028 | Issue evidence collection (descriptions, comments, attachments, status history, approvals, linked merge or pull requests, reviews, CI/CD, releases, policy evaluations) with provenance and checksums; evidence in the audit period kept apart from later remediation | M12 | Provenance and period tests |
| FR-029 | Issue evidence exports for one issue, a selection, a control or a period: summary, manifest, issue-to-control index with framework versions, shared evidence, provenance, checksums, assessments, approvals, exceptions, remediation status, gaps and redactions | M12 | Export accuracy and offline verification tests |
| FR-030 | GitHub connection (GitHub App or fine-grained token; GitHub.com and Enterprise Server), discovery, settings, pull requests, reviews, workflow runs, deployments, releases and issues, through the common provider layer ([ADR-0014](architecture/ADR-0014-provider-integrations.md)) | M13 | Normalization, pagination, rate-limit and missing-permission tests |
| FR-031 | Provider webhooks validated and processed idempotently, de-duplicated against scheduled collection | M13 | Duplicate-delivery tests |
| FR-032 | Versioned configuration templates with inheritance, precedence, overrides and exceptions; unsupported settings and plan requirements shown | M14 | Template precedence tests |
| FR-033 | Bulk changes: target selection by filters and saved collections, preview of targets and before and after values, approval of the change set, staged rollout, background execution with per-target results, cancellation, resumption, concurrent-change detection and rollback where possible | M14 | Partial-failure, concurrency and rollback tests |
| FR-034 | Drift against assigned templates, with manual remediation and per-policy automatic remediation that is off by default | M14 | Drift and verification tests |
| FR-035 | Baluarte tags synchronized to provider-native equivalents on request, kept distinct from internal metadata | M14 | Tag sync tests |
| FR-036 | Framework versions as data; human control assessments separate from automated results; evidence uploads and references with review status and freshness; missing, stale, inaccessible and incomplete evidence reported | M15 | Assessment and evidence lifecycle tests |
| FR-037 | Finding owners and deadlines; exceptions reassessed before expiry | M15 | Lifecycle tests |
| FR-038 | Native time entries and timers, work outside issues, imported provider time without double counting; submission, approval, rejection and traceable correction; schedules, leave and calendars | M16 | Import, duplicate, approval and correction tests |
| FR-039 | Capitalization policies, CapEx/OpEx/mixed/pending classification with finance approval separate from engineering proposals | M17 | Classification and approval tests |
| FR-040 | Effective-dated rates and currencies, loaded costs, costs from approved time, decimal arithmetic with documented rounding, rate visibility restricted separately from time | M17 | Rate, rounding and permission tests |
| FR-041 | Period closing and locking, authorized reopening, audited adjustments, roll-ups without double counting | M17 | Locking and reconciliation tests |
| FR-042 | Portfolios, programs, initiatives, milestones, dependencies, capacity and budgets linked to provider records, time and costs | M18 | Linking and roll-up tests |
| FR-043 | Engineering, security, time and finance reports with drill-down, defined formulas and coverage, fiscal calendars, and CSV and spreadsheet exports traceable to source records | M19 | Report traceability tests |

## Acceptance workflows

- **Audit:** an auditor selects a framework and period, lists auditable issues, reviews mappings and evidence gaps, and exports a package tracing each issue to controls and supporting evidence.
- **Bulk governance:** an authorized user selects repositories, assigns tags and a configuration template, previews changes, approves a staged rollout and verifies the results. Failures and exceptions stay visible and traceable.
- **Time and finance:** a developer records project time, engineering approves it, finance approves its CapEx/OpEx allocation, and Baluarte calculates costs using effective rates. A monthly report reconciles totals by developer and project or epic. Finance closes the period, and later corrections stay traceable.
- **Portfolio:** a manager links provider work to a cross-team initiative, tracks dependencies and delivery, and reviews the approved time, budget and labor costs behind it.

## Exclusions

- Bitbucket and other providers; Kubernetes cluster auditing.
- Compliance certification, or claims that applying a template or closing an issue proves a control works.
- Claims that current settings prove historical compliance.
- Hours inferred from commits, merge or pull requests or other activity; ranking engineers by activity counts.
- A required hosted service or external AI service for core workflows.

## Nonfunctional requirements

Workspace isolation across APIs, jobs, storage and exports; credentials encrypted and redacted from logs and reports; connection URLs checked against server-side request forgery; validated uploads and protected downloads; decimal arithmetic with documented rounding for money; demo data clearly separated from live data. Secure defaults, non-root containers, no mandatory internet egress, UTC timestamps, audit logging, bounded API rate usage, migration safety, backup/restore guidance, operational health endpoints, and meaningful unknown/error states. Target performance and retention limits must be measured and agreed before production GA.

## Open decisions

Supported minimum GitLab and GitHub Enterprise Server versions; initial OIDC provider and authorization mapping; baseline inheritance precedence; retention period; expected scale per workspace (connections, repositories, time entries); fiscal calendar defaults; capitalization policy defaults.
