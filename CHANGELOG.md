# Changelog

All notable changes to Baluarte are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Before 1.0, a minor version may change behaviour,
the API or the database schema; the release notes say how to upgrade.

## [Unreleased]

### Added

- Approved remediation plans can be applied from Baluarte ([ADR-0012](docs/architecture/ADR-0012-remediation-writes.md),
  [REMEDIATION.md](docs/REMEDIATION.md#applying-a-plan)). This is off by default. It needs
  `BALUARTE_REMEDIATION_WRITES=true` (Helm: `gitlab.remediationWrites`) and a remediation write
  token, separate from the read and label write tokens, entered under Settings.
  - Each step is checked with a fresh read, skipped when already as intended, and left alone when
    the project changed since the plan.
  - Otherwise the step is sent once and verified by reading back with the read-only token. A plan
    stops at the first failed step.
  - Every request and outcome is audited.
- `POST /api/v1/remediation/plans/{id}/apply` and `/api/v1/settings/remediation-write-token`.
  Plans now report `can_apply`, `progress`, and where a failed plan stopped.
- In-app help under **Help** in the sidebar, with a help button in the header that opens the
  topic for the current page. It walks through each part of Baluarte step by step, and a
  **Gaps in GitLab that Baluarte covers** topic sets out what GitLab leaves to each tier and how
  Baluarte covers it.

### Changed

- The product scope now covers GitLab and GitHub, bulk configuration, the auditable issue register,
  portfolio planning, time approval and CapEx/OpEx reporting. The [roadmap](docs/ROADMAP.md) sets
  out where Baluarte stands, the constraints and assumptions, and milestones M11 to M19 with
  acceptance criteria. The product requirements, architecture notes and README follow it.
  [ADR-0013](docs/architecture/ADR-0013-workspaces-and-connections.md) (workspaces and several
  connections) supersedes ADR-0004, and
  [ADR-0014](docs/architecture/ADR-0014-provider-integrations.md) sets the common provider layer.
- Database migration `0028`: remediation plans gain the `applying`, `applied` and `failed`
  states and their progress. Upgrading runs it in the migration Job, as usual.

### Fixed

- The Remediation page description and the policy engine docs still said plans never change
  GitLab.

## [0.1.1]

A security release. Upgrade with `helm upgrade … --version 0.1.1`, or with
`BALUARTE_VERSION=0.1.1` for Compose. There is no database migration.

### Upgrading

- The Helm chart now creates NetworkPolicies by default. Where the CNI enforces them, only the web
  pod accepts connections from outside the release. If anything outside the release calls the API
  Service directly, such as a monitoring probe or an Ingress routed to the API, allow it or set
  `networkPolicy.enabled=false`.
- `global.imageRegistry` is unchanged (`ghcr.io/mike-norris`).

### Security

- Behind a TLS-terminating Ingress or proxy, the web server now passes on the proxy's
  `X-Forwarded-Proto` instead of replacing it with `http`, so the setup session cookie is marked
  `Secure`.
- Strict-Transport-Security on responses served over HTTPS; Permissions-Policy,
  Cross-Origin-Opener-Policy, Cross-Origin-Resource-Policy and `object-src 'none'` on all.
- Sign-in and setup requests are rate limited per client address; request bodies are limited to
  2 MB.
- The Helm chart adds NetworkPolicies (on by default, `networkPolicy.enabled`): only the web pod
  accepts connections from outside the release.

### Documentation

- docs/HARDENING.md: what the defaults enforce and a production checklist.
- docs/QUICKSTART.md: from the released images to findings and a verified evidence export.

## [0.1.0]

The first release: governance and audit evidence for one self-managed GitLab instance, read-only
by default.

### Collect and evaluate

- Read-only GitLab client with connection diagnostics, version, edition and capability detection.
- Discovery of groups, subgroups and projects, with collection health, run history and per-resource
  errors; archived projects are inventoried but not evaluated.
- Versioned baselines in YAML (`GitLabBaseline` schema), applied to the instance, groups or
  projects with inheritance, and changed only through review.
- Eleven rule types: project visibility, protected default branch, merge request approvals,
  privileged members, group two-factor authentication, project access tokens, changes reviewed,
  pipelines passed, deployed after review, and (GitLab Ultimate) scan execution and approval
  policies; plus label consistency against a label taxonomy.
- A recommended baseline that switches on every rule the instance supports, a rule coverage card,
  and "Add recommended rules" for existing baselines.
- Findings followed from first failure to resolution. An unknown or unsupported result is never
  counted as compliant.

### Govern

- Two-person review of baseline versions, assignments, control mapping changes, exclusions from
  monitoring, label taxonomies and plans, with a review queue. Development mode lets one person
  decide their own changes, recorded as such.
- Policy exceptions with expiry, and compliance exceptions for the operating profile.
- Control mapping to SOC 2, ISO/IEC 27001 and NIST SP 800-53 (moderate), adjustable with
  justification, with control pages showing the evidence behind each status.
- Monitoring scope: exclude projects or groups from monitoring with a justification.
- Dry-run remediation plans with separate approval; approved plans are applied by hand.
- Optional, off by default: approved label plans and issue time plans applied with a separate
  write token (`BALUARTE_LABEL_WRITES`, `BALUARTE_ISSUE_WRITES`).

### Evidence

- Every observation stored with its source and a SHA-256 digest; evaluations reference the
  observation they judged.
- Scoped evidence export as JSON or a ZIP of CSV files, each with an integrity manifest and an
  offline verifier (`baluarte-verify-export`).
- Audit-period evidence packages: results as of the period's end, the changes in the period and a
  reproducible sample of them with their evidence.
- An auditor report as PDF for one framework and period, optionally included in the package.
- Change evidence for merge requests and direct pushes, and a past-date view.
- An append-only audit log, optionally shipped to syslog over TLS or Elasticsearch/OpenSearch, and
  encrypted scheduled backups.

### People and operations

- OIDC sign-in with server-side sessions and roles (admin, analyst, auditor, manager).
- A first-run setup wizard; settings and sealed secrets stored in the database.
- Manager space: tags, time tracking per project and (optionally) per person, trends and timecards.
- Email (SMTP over TLS) and chat webhook notifications: new findings, collection health, review
  reminders and a weekly digest. Off until configured.
- Web UI on Tabler with dark and light themes, jump search and keyboard shortcuts; phones can
  approve and read.

### Deployment

- Images for linux/amd64 and linux/arm64 at `ghcr.io/mike-norris/baluarte-app`, `baluarte-web` and
  `baluarte-backup`, signed with cosign, with an SBOM and build provenance.
- A Helm chart at `oci://ghcr.io/mike-norris/charts/baluarte`, and a Docker Compose file for
  evaluation and development.

### Known limitations

- One GitLab instance per deployment.
- Baluarte does not change GitLab configuration: remediation plans are applied by hand.
- The Docker Compose stack binds to 127.0.0.1 and is not a production configuration; use the Helm
  chart, or put Compose behind a TLS reverse proxy as described in docs/DEPLOYMENT.md.
- The control mapping is guidance for an organization and its auditor, not a certification.

[Unreleased]: https://github.com/mike-norris/baluarte/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/mike-norris/baluarte/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/mike-norris/baluarte/releases/tag/v0.1.0
