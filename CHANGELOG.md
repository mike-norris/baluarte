# Changelog

All notable changes to Baluarte are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Before 1.0, a minor version may change behaviour,
the API or the database schema; the release notes say how to upgrade.

## [Unreleased]

### Added

- Evidence items (step M15.2): files (PDF, images, text, CSV, JSON and Office documents, up to
  25 MiB, judged by their content and stored once by SHA-256 in the database) and links, each
  linked to controls in a framework version, with an owner, period, freshness date and an
  optional restriction to reviewers. A draft is submitted and then accepted or rejected by
  someone else (`evidence:review`); decided items never change, and corrections supersede
  them. Links are checked only against hosts listed in `BALUARTE_EVIDENCE_CHECK_HOSTS`
  (`evidence.checkHosts` in the chart). Retention removes old items' files but keeps the record.
  New page Evidence, Evidence items; new permissions `evidence:manage` and `evidence:review`.
  Migration `0051`.
- Framework versions as data (step M15.1): each framework edition and its controls are stored,
  one version per framework is current, and control mapping changes and issue mappings record
  the version they were made against (shown in the API and the register export). A later
  edition is added beside the current one. An organization can describe a control in its own
  words on the control page (In our words), kept per workspace and audited; titles stay
  Baluarte's paraphrases. New endpoints under `/api/v1/frameworks`. Migration `0050`.
- Change set records (step M14.6, finishing configuration templates): a change set now needs a
  rationale (20 to 2,000 characters, never changed), and records each apply, continue and
  rollback with who started it. Whoever approved a set can no longer apply, continue or roll it
  back; the database enforces this, and development mode lets an approver do it while recording
  that. The change set page shows the record: why, who requested, approved and ran it, the
  template versions and the controls its settings support. Audit-period evidence packages
  (format `baluarte.evidence-package/v2`) add `change_sets.csv` and `change_set_items.csv` for the
  sets active in the period, with each change's result and each skip (such as an approved
  exception), and `baluarte-verify-export` checks that no approver ran a set. Migration `0049`.
- Pipeline starter approval (release governance step RG.5): the baseline rule
  `gitlab.deployment.triggerer_cannot_approve` fails when GitLab lets whoever started a pipeline,
  such as a release tag's creator, approve its deployments, and is unsupported without deployment
  approvals (Premium or Ultimate). The template setting
  `deployments.forbid_pipeline_triggerer_approval` holds it: drift is judged by the rule, and
  change sets turn the project setting off (or back), read again before writing and rolled back
  on request, under the template write switch. It maps to SOC 2 CC8.1, ISO/IEC 27001 A.8.32 and
  NIST AC-5 (control mapping version `2026.11.3`), and the baseline editor offers it, off by
  default. The release governance guide shows a template that closes the merge-request gap of
  tag releases.
- Audit release export (release governance step RG.4): **Governance, Releases** lists recorded
  production releases with who started, approved and deployed each, and builds an audit release
  for one: a ZIP with the release, its notes, the practice and judgment, the release protections
  in force, the merge requests since the previous production release with approvals, pipelines
  and tests, every observation it rests on, and the release pipeline's reports (test report,
  SBOM, security scans) read from GitLab read-only, at most 10 MB each and 50 MB per release, the
  rest listed with why. Never source code. `baluarte-verify-export` checks it offline; it is
  audited as `release.export_created` and `release.export_completed`. Needs `evidence:package`.
- Release checks (release governance step RG.3): the baseline rule
  `gitlab.release.follows_practice` judges each production release against the project's
  release practice: the ref it expects, a protected tag for a tag practice, the required
  deployment approvals given before it finished, no approval by whoever started the release or
  by the deployer unless the practice allows it, and a reviewed change when required. Failing
  releases name who started, approved and deployed them; what cannot be read is unknown, and a
  project with no practice is unknown. The result rests on the deployment and change
  observations and the release protections observation, each checked before it is stored. It
  maps to SOC 2 CC8.1, ISO/IEC 27001 A.8.32 and NIST CM-3 and AC-5 (control mapping version
  `2026.11.2`), and the baseline editor offers it, off by default.
- Release practices (release governance step RG.2): **Governance, Release practices** defines
  how projects are meant to reach production, from three presets (merge request to the default
  branch; protected release tag with deployment approval; release branch) or a custom practice,
  with ref patterns, environments, required approvals and who may approve. A practice is assigned
  to the connection, a group or a project, nearest first, and each project's production
  deployments of the last 90 days are compared with it: follows, differs (with the refs it did
  not expect), not known or no releases. Changes are audited (migration `0048`).
- Release evidence (release governance step RG.1, read-only): each production deployment now
  records who GitLab says deployed it, whether it deployed a tag, its deployment approvals
  (GitLab Premium or Ultimate), who started its pipeline (for a tag, the tag's creator) and the
  tag's GitLab release. Deployments recorded earlier are read again once. The change-evidence job
  also records each project's protected tags, protected environments (the project's and its
  groups') and whether the pipeline triggerer may approve deployments.

- Tags to GitLab topics, only when asked: a change set can synchronize chosen tag keys to
  project topics as `key:value`, adding what tags call for and removing stale topics of those
  keys while keeping every other topic. It is reviewed, applied with the template write token,
  verified and rolled back like other change sets. Project discovery now collects topics
  (migration `0047`).
- Automatic drift proposals: a template assignment can be set to **propose fixes for drift
  automatically**. After each evaluation Baluarte builds a change set for the drifting settings
  that assignment decides, marked as proposed by Baluarte, for people to approve and apply as
  usual; nothing is approved or applied automatically. An open or identical rejected proposal
  is not repeated (migration `0046`).
- Fixing template drift: a change set can take each project's own effective settings (its
  templates and overrides) instead of one template or set of values, and each change names the
  template and assignment that decided it. **Propose a fix** on a project's drift, or for all
  drifting projects, builds one for review. Applied changes are checked again against the
  evidence discovery collects after the run: verified, changed again, awaiting collection or
  cannot tell.
- Built-in TOTP when MFA is required and the identity provider does not prove it: an
  authenticator app (such as Authy) is set up at the first sign-in, with 10 one-time recovery
  codes. Codes are never accepted twice, and 5 wrong codes end the sign-in. Administrators
  reset a lost authenticator in Settings. Secrets are sealed with the secret key and re-sealed
  by `baluarte-reencrypt-secrets` (migration `0045`).
- MFA at sign-in: provider presets (`generic`, `okta`, `auth0`) ask for MFA with `acr_values`
  and read it from the ID token's `amr` claim. **Require MFA** refuses a sign-in without that
  proof. Each session records how MFA was proven (migration `0044`), and each sign-in audit
  event records whether it was proven and the `amr` values sent. `identity-mfa` is now a check,
  still attestable when MFA is not required in Baluarte.
- Provable backups: `baluarte-backup restore-test` restores a backup into a temporary
  PostgreSQL inside the backup container, preferring the off-site copy. It checks the file's
  SHA-256, the schema revision, the tables, the audit triggers and every audit digest, and
  records a `restore_run` (migration `0043`), shown on the Compliance page.
  `baluarte-backup list` compares the recorded backups with the stored files, and
  `baluarte-backup restore` recovers into an empty database without needing the live one.
  `backup-offsite` and `restore-test` are now checks, with attestation still accepted when no
  off-site target or restore test is recorded. Helm `backup.restoreTest` adds an optional
  monthly restore test.
- Off-site backup copies: the backup service copies each encrypted backup to an
  S3-compatible bucket with rclone when `BALUARTE_BACKUP_S3_ENDPOINT` is set (Helm:
  `backup.offsite`), reads it back to check its size and SHA-256, keeps the newest copies, and
  records `copied` or `failed` per run, shown on the Compliance page. A Compose overlay
  (`deploy/compose/offsite.yaml`) adds a SeaweedFS container as a local S3 target, whose storage
  must be a mount that is not the host's own disk; `deploy/compose/offsite-test.sh` checks it in
  CI. Migration `0042`.
- TLS for the Compose stack: `deploy/compose/tls.yaml` serves the site over HTTPS only (TLS 1.2
  and 1.3, HSTS) and makes PostgreSQL accept only TLS connections with a client certificate
  naming the role plus its password; every client connects with `sslmode=verify-full`.
  `deploy/compose/make-tls-certs.sh` creates a local CA and the certificates for testing, and
  `deploy/compose/tls-smoke-test.sh` checks it in CI. The web image's nginx configuration is
  split so the plain and TLS servers share their locations.
- Applying template change sets (roadmap M14.4): an approved set is applied in stages by a
  background job, each setting read again first (already as wanted is skipped, a concurrent
  change is flagged and left), written once with a separate template write token and verified
  by reading back, with retries on rate limits and server errors. Sets can be continued,
  cancelled, resumed after a restart and rolled back in reverse order; changes that cannot be
  fully undone are disclosed and must be acknowledged. Off unless
  `BALUARTE_TEMPLATE_WRITES=true` (Helm: `gitlab.templateWrites`) and a template write token is
  stored on the Settings page. Change sets now have their own permissions: `changes:plan`
  (analysts) builds and withdraws, `changes:approve` (admins) decides and applies; they no
  longer use the remediation permissions. Migration `0041`.
- Template change sets (roadmap M14.3): choose projects by group, path, tag (owner, business
  unit), template, drift and findings, preview and save the selection, and build a change set
  from a template version or settings. Each project's current value comes from stored
  evidence, with the wanted value, warnings for changes that weaken protection, remove access
  or disrupt work, and skips for unknown values, unsupported settings, approved exceptions and
  baseline conflicts. Someone other than the creator approves the set's digest; a sensitive set
  needs two such approvers, and approval is refused once the evidence has moved. Nothing is
  applied yet. A Change sets tab under Governance. Migration `0040`.
- Template drift (roadmap M14.2): each project's effective template settings are compared
  with collected evidence by the evaluators that judge baselines, so a setting is `aligned`,
  `drift`, `excepted`, `unknown` or `unsupported`, never aligned without evidence. Exact values
  are checked from both sides (a project needing more approvals than the template drifts).
  Template exceptions waive one setting for one project until they end, with the policy
  exception lifecycle and separate approval; approved ones ending within 14 days are flagged.
  The Templates tab shows alignment across the connection by template and group, the projects
  needing attention, each project's drift, and the exceptions. `labels_consistent` in a
  template now accepts only `true`. Migration `0039`.
- Configuration templates (roadmap M14.1, ADR-0016): versioned YAML documents
  (`kind: ConfigurationTemplate`) that name exact values for settings Baluarte reads
  (visibility, default branch protection, merge request approvals, GitHub security features and
  label consistency). A template can extend one exact version of another; versions are
  immutable. A version is assigned to a connection, group or project, with optional overrides
  that need a reason; for each setting the nearest assignment decides, and each project's
  effective settings say which template, version and assignment decided every value and what it
  replaced. Settings a provider has no equivalent for, or that depend on its plan, are shown as
  such. Saving and assigning need `policy:write` and are audited; nothing is changed on GitLab or
  GitHub. New Governance tab *Templates* and migration `0038`.
- GitHub issues in the auditable issue register (roadmap M13.5): when a workspace has a
  register, each GitHub collection also reads its repositories' issues, pull requests left out,
  as an `issues` run, and captures the evidence of auditable ones: the description, comments,
  timeline history, mentioning issues, and mentioning pull requests with their reviews and
  workflow runs. Rules, decisions, mappings and exports work across GitLab and GitHub issues
  alike, and the issue page and mappings use GitHub's words. *Read issues now*, new rules and
  the `issues` and `issue_comment` webhook events queue the read. Files attached on GitHub are
  listed as gaps, not downloaded. An acceptance test evaluates one workspace with a GitLab and a
  GitHub connection against one baseline.
- Signed GitHub webhooks (roadmap M13.4b): an admin can set up a webhook on a GitHub connection
  (the secret is generated, sealed and shown once) and GitHub's deliveries to
  `/api/v1/webhooks/github/{id}` are verified by their HMAC-SHA256 signature before anything is
  read. Each delivery is recorded once per delivery ID, without its body, and reads the
  repository's changes, branch settings, security features or labels again within seconds; a
  burst of events is read once. Webhook reads record exactly what scheduled collection would and
  never make a collection look fresh. The connection shows its latest deliveries. Migration 0037.
- GitHub change evidence (roadmap M13.4a): each GitHub collection also reads, as a `changes`
  run, every repository's merged pull requests with their reviews, the Actions workflow runs of
  each change, direct pushes to the default branch and successful production deployments, in
  the shape GitLab changes are recorded in. `gitlab.change.reviewed`,
  `gitlab.change.pipeline_passed` and `gitlab.change.deployed_after_review` are judged on GitHub;
  `require_tests` is `unsupported` there. Pull requests are named `#12` in reasons and on the
  Code changes page.
- GitHub security features and label drift (roadmap M13.3b): a GitHub-only rule type,
  `github.repository.security_features`, requires secret scanning, push protection, Dependabot
  security updates or code scanning, read for each repository after discovery; a setting GitHub
  does not report is `unknown`, and code scanning without Advanced Security is `unsupported`.
  Repository labels are collected as a `labels` run and `gitlab.labels.consistent` is judged on
  GitHub, each repository defining the taxonomy's labels itself. Default control mappings for the
  new rule type. Label plans are refused on GitHub connections.
- GitHub repositories are evaluated (roadmap M13.3a): each rule type declares the providers it is
  judged on, and on any other its result is `unsupported`, never `pass`. On GitHub, visibility,
  default-branch protection (classic protection or ruleset rules), required reviews and the
  organization's two-factor requirement are judged from new evidence: each repository's
  default-branch settings, read after discovery. A plan without branch protection gives
  `unsupported`. Capability messages name the provider.
- GitHub.com organizations as connections (roadmap M13.2): added on the Connections page with a
  fine-grained token or a GitHub App installation, and collected by one `github.collect` job:
  capability probes, then the organization, repositories, teams and memberships, normalized into
  the inventory GitLab uses. Honours GitHub's rate limits and pagination; reads only. Not
  evaluated or in the register yet. Migration `0036`.
- GitHub feasibility spike (roadmap M13.1): docs/GITHUB_INTEGRATION.md records what GitHub's
  documentation says about authentication, versions, rate limits, webhooks, each collection and
  each rule type, what is still to be verified, and the M13 step plan. GitHub.com comes first.
- Issue register exports (roadmap M12.4): a ZIP for one issue, a selection, or every issue
  mapped to chosen controls, optionally against an audit period, with a readable summary, the
  issue-to-control index with framework editions, cited evidence and rationales, decisions,
  each capture and attachment stored once, gaps, redactions and a manifest. Checked offline by
  `baluarte-verify-export`, which now also checks register exports. Audited as
  `register.export_created` and `register.export_completed`.
- Issue-to-control mappings (roadmap M12.3): map an auditable issue to a framework control,
  citing items of its captured evidence each with a rationale; reviewed like control mapping
  changes and shown in the review queue; pinned to the capture they cite. Audit periods keep
  evidence inside the period apart from later remediation, and gaps are listed per mapping and
  counted per control on the register's new Controls tab. Migration `0035`.
- Issue evidence (roadmap M12.2): for auditable issues, the description, comments, status
  history, linked issues, merge requests with approvals and pipelines, releases and attachments
  are captured as one observation, with every unread source listed as a gap. Attachments are
  stored once by SHA-256 and served only as downloads, up to `BALUARTE_ISSUE_ATTACHMENT_MAX_BYTES`
  (Helm `issueRegister.attachmentMaxBytes`). An issue page shows the capture. Migration `0034`.
- Auditable issue register (roadmap M12.1): selection rules by label, project and issue type,
  including or excluding one issue with a reason (excluded issues stay listed), categories, and
  a register page with filters and why each issue is listed (`/register`, `/api/v1/register`).
  Issues are read only for workspaces with a register. New permission `register:write`.
  Database migration `0033`.
- Several workspaces (roadmap M11.4): a Workspaces page and `/api/v1/workspaces`; roles per
  workspace from identity provider groups; a workspace and connection switcher, with every page
  working on the chosen connection; every query and export scoped to the request's workspace
  (`X-Baluarte-Workspace`, `X-Baluarte-Connection`); audit events record their workspace.
  Database migration `0032`.
- Connections (roadmap M11.3): a Connections page and `/api/v1/connections` to add, test,
  collect, pause and remove GitLab connections, each with its own sealed token, timeout,
  schedule and health. Hosts must resolve to public addresses unless a connection allows a
  private network; loopback, link-local and metadata addresses are always refused, before every
  job too. Collection jobs name their connection. Database migration `0031`.
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

- Backend dependencies are pinned with hashes in `backend/requirements.txt` and
  `backend/requirements-dev.txt`; the image and CI install only those versions, and the build
  backend is pinned. Previously each build took the newest release in range.
- Deployment settings (identity provider, primary GitLab, write tokens, notifications, audit
  shipping, compliance) are managed only while acting in the default workspace.
- The product scope now covers GitLab and GitHub, bulk configuration, the auditable issue register,
  portfolio planning, time approval and CapEx/OpEx reporting. The [roadmap](docs/ROADMAP.md) sets
  out where Baluarte stands, the constraints and assumptions, and milestones M11 to M19 with
  acceptance criteria. The product requirements, architecture notes and README follow it.
  [ADR-0013](docs/architecture/ADR-0013-workspaces-and-connections.md) (workspaces and several
  connections) supersedes ADR-0004, and
  [ADR-0014](docs/architecture/ADR-0014-provider-integrations.md) sets the common provider layer.
- Database migration `0030` (roadmap M11.2): governance records belong to a workspace. Baseline
  names and versions, taxonomy versions and tag keys and values are unique per workspace instead
  of across the deployment or per connection. Existing rows take their connection's workspace,
  or the default one.
- Database migration `0029` (roadmap M11.1, [WORKSPACES.md](docs/WORKSPACES.md)): a `workspace`
  table with a `default` workspace, and GitLab instances become connections in it (`workspace_id`,
  `provider`, `name`, `paused`). Existing data joins the default workspace unchanged; nothing
  else changes for an install with one GitLab.
- Database migration `0028`: remediation plans gain the `applying`, `applied` and `failed`
  states and their progress. Upgrading runs it in the migration Job, as usual.

### Fixed

- A production deployment of a release tag whose commit is not among the recorded changes of the
  default branch is now `unknown`, for the release checks to judge, instead of failing because
  its ref is not the default branch. A deployment of another branch still fails.
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
