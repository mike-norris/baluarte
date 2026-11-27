# Roadmap

Baluarte extends GitLab and GitHub with security governance and bulk configuration management, compliance assessments and auditable evidence, engineering portfolio planning, developer time tracking, and CapEx/OpEx labor cost reporting. GitLab and GitHub stay the systems of record for code, issues, merge or pull requests and CI/CD; Baluarte connects those records to engineering, security, audit and finance work. The product requirements are in [PRODUCT_REQUIREMENTS.md](PRODUCT_REQUIREMENTS.md).

This page records where Baluarte stands against that goal, the constraints that shape the work, the assumptions made, and the order in which the rest is built. Each milestone is a complete, usable workflow with acceptance criteria; a milestone is done only when its criteria are met and tested. No delivery dates are promised. Milestone work is tracked in GitHub issues.

## Where Baluarte stands

Released as v0.1.0 and v0.1.1, plus remediation writes and in-app help on main. Everything below works today for **one self-managed GitLab instance per deployment**.

| Area | Works today | Partial | Missing |
|---|---|---|---|
| A. Provider integrations and inventory | GitLab connection, capability detection, paginated discovery of groups and projects, reconciliation (missing, inaccessible), freshness per collection kind, observations with provenance and SHA-256 digests | Collection is scheduled polling only; capability differences are explicit for GitLab only | GitHub; several connections; workspaces; incoming webhooks with idempotent processing; collection of issues and epics as auditable records |
| B. Security governance | Versioned YAML baselines with inheritance, 12 rule types, findings with history, exceptions with justification, approval and expiry, two-person review, read-only by default | Remediation applies two allowlisted fixes (visibility, default branch protection) behind a switch and a separate token | Finding owners and deadlines; reassessment of exceptions before expiry; human assessments separate from rule results |
| C. Bulk administration | Baluarte tags with inheritance; label taxonomy with drift and reviewed, applied label plans; remediation plans with digest-bound approval, per-step read-back and audit | Plans are fixes for findings, not templates; no staged rollout or rollback | Configuration templates; target selection by filters and saved collections; tag sync to provider topics or labels; staged rollout, cancellation and resumption; rollback; automatic remediation (off by default) |
| D. Compliance and evidence | Control catalogue and reviewed mapping for SOC 2, ISO 27001 and NIST 800-53; control status; evidence retention; Baluarte's own operating profile with attestations | Evidence is what Baluarte collects; no review status | Framework versions as data; uploads and external references; evidence review and freshness per control; human assessments |
| E. Auditable issue register | Change evidence per merge request, direct push and deployment; audit packages with reproducible samples; auditor report PDF; offline verification | Packages are organized by period and control, not by issue | The issue register, issue-to-control mappings, issue evidence collection and exports per issue, selection, control or period |
| F. Portfolio management | Manager space: posture, review, drift and time by group or tag, with trends | | Portfolios, products, programs, initiatives, milestones, dependencies, capacity, budgets |
| G. Time tracking | Import of GitLab time logs and estimates; summaries; time per person with scopes and audit; timecards; closed-issue rules | Imported time only | Manual entries and timers; work outside issues; submission and approval; corrections; schedules, leave and calendars |
| H. CapEx/OpEx and labor cost | | | Everything: capitalization policies, classification, rates, costing, reconciliation, period closing |
| I. Reporting | Dashboard, manager overview and trends, CSV and JSON exports, auditor PDF | | Finance reports, capitalization schedules, budget versus actual, fiscal calendars, executive portfolio reports |
| J. Architecture and security | Modular monolith, PostgreSQL job queue, OIDC, server-side RBAC, sealed secrets, append-only audit log with shipping, Compose and Helm, hardening | One instance, no tenant boundary | Workspace isolation across APIs, jobs, storage and exports; SSRF checks for every connection URL; upload handling; demo data separated from live data |
| K. User experience | Tabler UI, role-aware navigation, phone rules, loading, empty, stale and permission states, in-app help | Navigation follows today's areas | Connections, templates and bulk changes, auditable issues, portfolios, time approvals, finance and period closing, reports |

## Constraints

- **One instance in the data model.** Every GitLab table is keyed by `instance_id` and GitLab IDs, and ADR-0004 assumes one instance per deployment. Workspaces and several connections touch every table, job, query and export. They come first, so GitHub and the new areas are built on them once.
- **GitLab-shaped domain.** Rules, observations, changes and labels name GitLab concepts. A provider layer has to normalize shared concepts (organization or group, repository, branch protection, review, pipeline) without losing provider-specific fields, and keep the current GitLab behaviour and evidence intact.
- **Evidence is immutable.** Existing observations, evaluations and audit events cannot be rewritten. Migrations add columns and back-fill references; they never re-key evidence or change digests.
- **Writes stay narrow.** Every write path so far has its own token, deployment switch, allowlist, send-once request and read-back. Bulk changes and templates must keep those guarantees at a larger scale.
- **Personal and financial data.** Time per person is personal data (ADR-0010). Rates and compensation are more sensitive still and need their own permissions, separate from time.
- **Released contract.** The `/api/v1` API, the export manifest formats and the Helm values are published. Changes are additive, or versioned.

## Assumptions

Made to keep the work moving; each is revisited in the milestone that depends on it.

- A **workspace** is the isolation boundary: an organization's connections, inventory, baselines, evidence, time and finance. One deployment can host several; existing data moves into a default workspace.
- A **connection** is one GitLab or GitHub deployment (hosted or self-managed) with its own credentials. A workspace can have several.
- GitHub is connected through a **GitHub App** by preference, with a fine-grained personal access token as the fallback. Exact permissions, endpoints and plan requirements are verified against GitHub's official documentation in M13 before any rule depends on them; nothing is assumed missing on either provider.
- Imported provider time and native time entries are one ledger. Imported entries keep the provider's ID, so a re-import never double counts.
- Hours are never inferred from commits, merge requests or other activity.
- Money is stored as decimals with a currency per amount and a workspace reporting currency. Rounding rules are documented and tested.
- A new **finance** role holds classification, rates, exports, reconciliation and period closing; rate visibility is a separate permission.
- Planning records link to provider issues and epics rather than copying them; field ownership and write-back are explicit per field.
- Core workflows never need an external AI service.

## Done

| Milestone | Delivered |
|---|---|
| M0 Foundation | Docs, standards, scaffolding, CI, Docker and Helm smoke tests |
| M1 App shell | FastAPI, React, PostgreSQL migrations, OIDC, RBAC, jobs |
| M2 GitLab inventory | Connection, capability detection, paginated discovery, reconciliation, UI |
| M3 Policy evaluation | Schema, inheritance, rules, findings |
| M4 Dashboard | Posture, drill-down, filters, freshness, failure states |
| M5 Evidence | Observation history, digests, export manifests, CSV and JSON |
| M6 Remediation | Dry-run plans with separate approval, exceptions, and approved plans applied with a separate write token ([ADR-0012](architecture/ADR-0012-remediation-writes.md)) |
| M7 Evidence trail | Change evidence, past-date view, audit packages with sampling, auditor report PDF |
| M8 Consistency | Label taxonomy, drift, applied label plans ([ADR-0009](architecture/ADR-0009-label-writes.md)) |
| M9 Manager space | Manager role, results by group or tag, trends, time tracking, tags ([ADR-0010](architecture/ADR-0010-time-tracking-personal-data.md)) |
| M10 Notifications | Email and chat webhook, once each, off by default ([NOTIFICATIONS.md](NOTIFICATIONS.md)) |
| Releases | v0.1.0 and v0.1.1; then remediation writes and in-app help |

## Next

### Hardening

Decided 2026-11-24, one change each, in this order:

1. **TLS** (done): HTTPS from nginx and PostgreSQL TLS with client certificates and `sslmode=verify-full`, through a Compose overlay and a script that creates a local CA and certificates for testing ([DEPLOYMENT.md](DEPLOYMENT.md#tls)).
2. **Off-site backups** (done): an S3-compatible target (SeaweedFS for local testing) receiving the encrypted backups; its storage must be a mount, NAS or block storage, not the host's own disk.
3. **Provable backups** (done): list backups, restore one, and record restore runs, so `backup-offsite` and `restore-test` become checks.
4. **MFA** (done): Okta and Auth0 presets with MFA verified from the token's `amr` claim, and built-in TOTP as the fallback.

### Product milestones

In this order. The first usable vertical slice (connect, synchronize, evaluate, find, map, register, export) already exists for GitLab except the auditable issue register, so M11 and M12 complete it on the new foundation before GitHub repeats it.

### M11 — Workspaces and connections

Several provider connections per workspace, and several workspaces per deployment ([ADR-0013](architecture/ADR-0013-workspaces-and-connections.md)). Delivered in four steps (M11.1 to M11.4), all done; see [WORKSPACES.md](WORKSPACES.md).

- A `workspace` and a `connection` model. Existing data moves into a default workspace and its GitLab connection; nothing is re-keyed and every digest still verifies.
- Every API query, job, export and file is scoped to the caller's workspace. Roles are granted per workspace.
- Connections page: add, test, pause and remove a connection, with health, coverage, errors and last successful collection per connection.
- Connection URLs are checked against server-side request forgery (scheme, resolution to private or link-local addresses, redirects), with an explicit allowance for self-managed hosts on private networks.

Acceptance:
- An existing install upgrades with no change to results, findings, evidence or exports.
- Two workspaces, each with its own GitLab connection, never see each other's data through any API, job or export (isolation tests on every router).
- A second GitLab connection in one workspace is discovered and evaluated, and its results carry the connection.

### M12 — Auditable issue register

Auditors list auditable issues and export their evidence against controls. Delivered in four steps (M12.1 to M12.4), all done; see [ISSUE_REGISTER.md](ISSUE_REGISTER.md).

- Issues and epics (or their equivalents) are collected with descriptions, comments, attachments, status history and links to merge requests, reviews, pipelines and releases, each with source IDs, URLs, original and collection timestamps, and checksums for captured files.
- Selection rules (labels, projects, types) and explicit inclusion decide what is auditable; each issue says why it is.
- Many-to-many issue-to-control mappings with a rationale per evidence item, reviewed like control mapping changes.
- Engineering issues, security findings, exceptions and remediation tasks are distinguished.
- Evidence within the audit period is kept apart from later remediation. Gaps are listed, and closing an issue is never treated as proof that a control works.
- Exports for one issue, a selection, all issues mapped to a control, or a period across selected controls: a readable summary, a machine-readable manifest, an issue-to-control index with framework versions, captured evidence shared rather than duplicated, provenance and checksums, assessments, approvals, exceptions and remediation status, gaps and limitations, and disclosed redactions.

Acceptance: the audit workflow in [PRODUCT_REQUIREMENTS.md](PRODUCT_REQUIREMENTS.md#acceptance-workflows), on GitLab, verified offline with `baluarte-verify-export`.

### M13 — GitHub

GitHub at parity with GitLab, through the common provider layer ([ADR-0014](architecture/ADR-0014-provider-integrations.md)). Steps, findings and status: [GITHUB_INTEGRATION.md](GITHUB_INTEGRATION.md). GitHub.com first; GitHub Enterprise Server later.

- A feasibility spike against GitHub's official documentation first: authentication (GitHub App, fine-grained tokens), GitHub.com and GitHub Enterprise Server versions, endpoints, permissions, plan requirements and rate limits for each collection and rule. Results go in a capability matrix like the GitLab one.
- Discovery of organizations, teams, repositories and memberships; settings including branch protection and rulesets, required reviews, visibility and security features; pull requests, reviews, workflow runs, deployments and releases; issues for the register.
- Rate limits, retries and incremental synchronization; webhooks validated by signature and processed idempotently, de-duplicated against scheduled collection.
- Each rule type says which providers it supports; a provider without the setting gives `unsupported`, never `pass`.

Acceptance: the first vertical slice on GitHub, and a workspace with one GitLab and one GitHub connection evaluated by one baseline, with provider-specific evidence kept.

### M14 — Configuration templates and bulk changes

Builds on remediation plans and label plans. Decided in [ADR-0016](architecture/ADR-0016-configuration-templates.md); delivered in six steps (M14.1 to M14.6), with steps and status in [CONFIGURATION_TEMPLATES.md](CONFIGURATION_TEMPLATES.md). M14.1 (templates with inheritance, precedence and overrides), M14.2 (drift, template exceptions and alignment) M14.3 (change sets with target selection, checks, warnings and approval), M14.4 (staged execution with verification, cancellation, resumption and rollback, behind its own switch and write token) and M14.5 (fixing drift as a change set, checking applied changes against collected evidence, automatic proposals that people approve, and tags synchronized to GitLab topics when asked) and M14.6 (records of requester, approvers, executors, rationale, template versions, scope, timestamps, values and results, linked to controls and in evidence packages; approvers never apply; the workflow end to end as an acceptance test) are done: M14 is done.

- Versioned templates for branch protection, required reviews, merge strategies, access, security features, labels, repository metadata and CI/CD settings, with inheritance, precedence, overrides and approved exceptions. Settings a provider or plan cannot hold are shown as unsupported.
- Target selection by hierarchy, owner, provider, tags, business unit, policy and compliance status, and saved collections. Baluarte tags sync to provider topics or labels only when asked, and the UI shows which are internal.
- Preview of the exact targets and before and after values; checks for permissions, capabilities, conflicting policies and exceptions; warnings for changes that weaken protection, remove access or disrupt work.
- Approval of the specific change set, with extra approvals for sensitive operations; staged rollout to a small set first; background execution with per-target progress, rate-limit handling, retries, cancellation and safe resumption.
- Settings are read again before each change, concurrent changes are flagged, previous values are kept, and rollback is offered where the provider allows it, with irreversible steps disclosed. Destructive actions need their own authorization.
- Drift against assigned templates, with manual remediation and automatic remediation that is off by default and switched on per policy.
- Requester, approver, executor, rationale, template version, scope, timestamps, before and after values and per-target results are recorded, linked to controls and exported.

Acceptance: the bulk governance workflow in the requirements, including a partial failure and an exception that stay visible.

M14.6 also requires that whoever applies a change set is not one of its approvers (decided 2026-11-25).

### Release governance

Decided 2026-11-25, after M14.5 and before M14.6 ([ADR-0017](architecture/ADR-0017-release-governance.md)); steps and status in [RELEASE_GOVERNANCE.md](RELEASE_GOVERNANCE.md).

- Release practices, presets and custom (merge request to the default branch; protected release tag with deployment approval; release branch), assigned to the connection, a group or a project, with the groups and projects that follow each and what their releases actually show.
- Release evidence, read-only: protected tags, protected environments of projects and their groups, the self-approval setting, and for each production deployment its tag, approvals, deployer, tag creator and GitLab release.
- Each production release judged against its practice, including separation of duties as GitLab records it: the tag creator does not approve, and the deployer is not an approver.
- An audit release export per production release, never source code, with small pipeline reports (tests, SBOM, security scans) and checksums.

Acceptance: a tag-gated release with a group-level deployment approval passes; one approved by its tag creator fails with the people named; its audit release verifies offline.

### M15 — Assessments and evidence management

Decided 2026-11-27 ([ADR-0018](architecture/ADR-0018-assessments-and-evidence.md)); steps and status in [EVIDENCE_MANAGEMENT.md](EVIDENCE_MANAGEMENT.md).

- Framework versions as data, with licensing respected for catalogue text.
- Human assessments of controls, separate from automated rule results, with assessor, period, conclusion and evidence.
- Evidence uploads and external references, validated and stored with access controls, retention and checksums; review status and freshness per evidence item; reports of missing, stale, inaccessible and incomplete evidence.
- Finding owners and deadlines; exceptions reassessed before they expire.

Acceptance: a control assessed by a person, with uploaded and collected evidence, exported and verified.

### M16 — Time capture and engineering approval

- Manual entries and timers, for issues and for work outside them (meetings, support, training, administration), alongside imported GitLab and GitHub time, without double counting.
- Work date, duration, description, source and approval state; submission, review, approval, rejection and correction, with approved history kept.
- Work schedules, leave and reporting calendars; missing submissions and unallocated time shown; documented rules for overlapping timers, rounding, time zones and entries that span periods.

Acceptance: a developer's week of mixed imported and native time submitted, approved, corrected and still traceable.

### M17 — CapEx/OpEx and labor cost

- Capitalization policies kept by finance users, with versions and effective dates; classification as proposed CapEx, OpEx, mixed or pending, by project, epic, issue, work category and phase, with percentages, rationale and finance approval kept apart from engineering proposals.
- Effective-dated employee and contractor rates, currencies and loaded costs; costs from approved time only; actual, provisional, estimated and unpriced amounts; missing rates and conversion assumptions flagged; rate visibility restricted separately from time.
- Period closing that locks a period, with authorized reopening or traceable adjustments; every change to entries, rates, allocations, approvals, classifications and period state audited; roll-ups that never double count.

Acceptance: the time and finance workflow in the requirements, reconciled by developer and project and reproducible after the period closes.

### M18 — Portfolio planning

- Portfolios, products, programs, initiatives, milestones and teams, linked to provider issues, epics and merge or pull requests across repositories and providers.
- Priorities, owners, target dates, risks, dependencies, delivery status, capacity, allocations and assumptions; manual estimates kept apart from observed data; synchronization direction and field ownership explicit.
- Initiatives linked to approved time, budgets and labor costs.

Acceptance: the portfolio workflow in the requirements.

### M19 — Reporting and navigation

- Engineering and security dashboards with drill-down, defined formulas, source coverage, windows and limitations; delivery metrics only where the source data exists; no ranking of engineers by activity.
- Hours and costs by period, person, team, department, cost center, project, epic, portfolio, classification, employee or contractor, and approval state; monthly capitalization schedules, labor cost summaries, budget versus actual, reconciliation, fiscal calendars, and CSV and spreadsheet exports traceable to entries, rates and approvals.
- Navigation regrouped around the product areas: overview, connections and inventory, templates and bulk changes, policies and findings, compliance and evidence, portfolios, time and approvals, finance and period closing, reports, administration.

Acceptance: every report total drills down to its source records and calculation.

## Each milestone also

- Tests the behaviour that matters for its area, including isolation, authorization, partial failures and provider capability gaps.
- Updates setup instructions, architecture notes, provider permissions, environment examples, known limitations and in-app help.
- Keeps existing workflows working, and marks a feature complete only when implementation and tests support it.
- Keeps a clearly labelled demo that runs without provider credentials.
