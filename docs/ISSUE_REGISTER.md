# Auditable issue register

Roadmap milestone M12 ([ROADMAP.md](ROADMAP.md#m12--auditable-issue-register)), decided in [ADR-0015](architecture/ADR-0015-auditable-issue-register.md). The register lists the issues an auditor should examine, says why each one is there, and exports its evidence against controls.

## Status

| Step | What it delivers | State |
|---|---|---|
| M12.1 Register and selection | The issue index per connection (any type and state); selection rules by label, project and issue type; including or excluding one issue with a reason; categories; a register page (`/register`, `/api/v1/register`) with filters and why each issue is listed (migration `0033`) | Done |
| M12.2 Issue evidence | For auditable issues only: the description, comments, status history, linked issues, closing and related merge requests with their approvals and pipelines, releases for the issue's milestone, and attachments captured with SHA-256 checksums and stored once; sources that could not be read are gaps on the issue; an issue page (`/register/issues/:project/:iid`) (migration `0034`) | Done |
| M12.3 Control mappings | Many-to-many issue-to-control mappings with a rationale per evidence item, proposed and reviewed like control mapping changes and pinned to the capture they cite; audit periods, with evidence inside the period kept apart from later remediation; gaps per issue and control; a Controls tab (`/register/controls`) with coverage, and mappings on the issue page (migration `0035`) | Done |
| M12.4 Exports | Packages for one issue, a selection, every issue mapped to a control, or a period across selected controls: summary, manifest, issue-to-control index with framework versions, shared evidence stored once, provenance and checksums, assessments, approvals, exceptions and remediation status, gaps and redactions; verified offline with `baluarte-verify-export`; export buttons on the register, issue and Controls pages | Done |
| M13.5 GitHub issues | GitHub issues in the register, pull requests left out, with their evidence: comments, timeline, mentioning issues and pull requests with reviews and workflow runs ([GitHub](#github)) | Done, fake-tested only |

## Selection

- **Rules.** A rule names a category and any of: labels (an issue with any of them matches), projects and issue types (`issue`, `incident`, `task`, `test_case`, `requirement`, `ticket`). An issue must meet every criterion the rule sets. Rule names are unique among a workspace's active rules.
- **Decisions.** Including an issue makes it auditable whatever the rules say, and may set its category. Excluding takes a rule-selected issue out. Both need a reason of 20 to 2,000 characters. One open decision per issue; end it to make another.
- **Visible exclusion.** Excluded issues are left out of the register by default and listed, with the reason and who decided, with `include_excluded` ("Show excluded issues").
- **Category.** The inclusion's, when it names one; otherwise the oldest matching rule's; otherwise `engineering`.
- **History.** Rules and decisions are ended, never edited or deleted, which database triggers enforce. Creating and ending them are audit events (`register.rule_created`, `register.rule_ended`, `register.issue_included`, `register.issue_excluded`, `register.decision_ended`), as is asking for a read (`register.collect_requested`).
- **Permissions.** `evidence:read` lists the register; `register:write` (administrators, analysts and auditors) changes it.

## Filters

Connection, project, category, rule, state, issue type, label, assignee, title text, and an audit period: `period_start` and `period_end` keep issues open at any time in the period (opened by its end and not closed before its start). Results are ordered by when GitLab last changed the issue, newest first.

## Collection

- The `issues` collection runs after each project discovery, for each connection whose workspace has a register (an active rule or an open decision). Other connections are skipped without a run.
- A project's first read lists every issue in any state; later reads list only issues updated since the last one, with five minutes of overlap. Each project read is an observation (`GET /projects/:id/issues (register)`) holding what was read, and each stored issue names the observation that recorded it.
- Kept per issue: GitLab IDs, type, title, state, labels, author, assignees, milestone, URL, confidentiality, and GitLab's created, updated and closed times, beside the time Baluarte read it.
- Adding a rule, including an issue not yet read, or "Read issues now" queues the collection for the workspace's connections at once.
- A read that fails is recorded with its status and leaves stored issues as they were. Projects outside monitoring scope are not read.
- Every request is a GET with the connection's read-only token.

## Issue evidence

- **What is captured.** For each auditable issue (selected or included, not excluded): the issue with its description; comments and system notes, internal notes flagged; its state, label and milestone events; linked issues; merge requests that close it or are related to it, each with who approved it and its latest pipelines; the project's releases for the issue's milestone; and files uploaded to the description or comments.
- **Snapshots.** Each capture is one observation (`issue evidence (register)`, SHA-256 digest) holding all of it and a status per source. A capture identical to the previous one is not stored again. `issue_evidence` points at the latest capture.
- **Gaps.** A source that could not be read (refused, missing, unsupported, or over a limit) is recorded with why, listed on the issue and in the capture; the capture is then `partial`. An issue GitLab no longer serves is `failed`. Nothing is left out silently.
- **Attachments.** References to `/uploads/<secret>/<name>` are downloaded with `GET /projects/:id/uploads/:secret/:filename` (GitLab 17.4 or later) up to `BALUARTE_ISSUE_ATTACHMENT_MAX_BYTES` (default 20 MiB; Helm `issueRegister.attachmentMaxBytes`). Redirects, such as to object storage, are not followed. Files are stored once by SHA-256 in `captured_file`, which cannot be changed or deleted, and are served only as downloads (`/api/v1/register/files/<sha256>`, never rendered) to people who can read evidence in a workspace whose captures cite them. An upload already captured is not downloaded again.
- **When.** After the issue index, the `issues` collection captures the issues that have no capture, that GitLab changed since, whose capture is older than 7 days (merge requests and pipelines change without touching the issue), or whose last capture missed something more than an hour ago. At most 200 issues per collection, up to 20 merge requests and 50 attachments per issue; the rest follow or are listed as gaps.
- **Not evidence of effectiveness.** A captured closing merge request or approval shows what happened, not that a control works; mapping evidence to controls, with a reviewed rationale, is below.

## Control mappings

- **A mapping** names one framework control and one auditable issue, with a rationale (20 to 2,000 characters), and cites up to 50 items of the issue's latest capture, each with its own rationale (10 to 1,000 characters): the description, a comment, a history event, a linked issue, a merge request (with its approvals and pipelines), a pipeline, a release or an attachment. An issue can be mapped to many controls and a control to many issues.
- **Pinned.** A mapping rests on the capture it was made from. Each cited item's label and date are copied from it, so the mapping reads the same whatever is captured later; a newer capture is flagged as a gap to review.
- **Review.** Mappings follow the governance setting like [control mapping changes](CONTROL_MAPPING.md): while review is required a mapping is proposed (`register:write`) and someone else with `policy:write` approves or rejects it; otherwise it is in force at once and recorded as self-approved. Proposed mappings appear in the review queue. In development mode the proposer may decide, and the decision is marked.
- **Never edited.** A revision supersedes a mapping in force, which is withdrawn when the revision takes effect; a mapping in force can also be withdrawn with a reason (`policy:write`). Database triggers keep mappings, their cited items and audit periods from being rewritten or deleted. Every step is audited (`register.mapping_*`, `register.period_*`).
- **Audit periods** are named date ranges (they may overlap, one per framework for example), added and ended with `register:write`. Against a chosen period each cited item is in the period, before it, after it (later remediation) or undated, and a mapping with nothing in the period says so.
- **Gaps** are listed on each mapping, and counted per control on the Controls tab: awaiting review; the issue no longer auditable; a partial or failed capture; captured again since the mapping; only the closure cited (closing an issue is never proof that a control works); nothing in the period; only later remediation; undated items.
- **API.** `GET /api/v1/register/issues/{project}/{iid}/mappings` (with what a new mapping could cite), `GET /api/v1/register/mappings` (filters: state, framework, control, period), `POST /api/v1/register/mappings`, `POST /api/v1/register/mappings/{id}/approve|reject|withdraw`, `GET /api/v1/register/coverage`, and `GET|POST /api/v1/register/periods` with `POST /api/v1/register/periods/{id}/end`. A `period_id` query places evidence against that period.

## Exports

- **Scopes.** One issue, a selection of issues (up to 500), or every issue mapped to one or more controls (in force or awaiting review). Any scope may name an audit period, which dates each cited item against it. Issues must be auditable; a control scope with more than 500 issues, or captured files over 1 GiB, is refused with a reason.
- **Contents.** One ZIP: `README.txt`, a readable summary (scope, controls with their framework editions, counts, each issue with its mappings, gaps, limitations and how to verify); `issues.csv` (why each issue is auditable, category, remediation status, capture status); `issue_controls.csv`, the issue-to-control index (framework edition, catalogue version, state, rationale, who proposed and decided, review and development flags, the capture cited, items inside the period, gaps); `mapping_evidence.csv` (each cited item, its rationale and timing); `decisions.csv` (every inclusion and exclusion with its reason); `observations.jsonl` (each capture once, with `payload_digest`); `files.csv` and `files/<sha256>` (each attachment once, named by its hash); `gaps.csv`; `redactions.csv`; `period.csv` with a period; `manifest.json` and `SHA256SUMS`.
- **Shared, not duplicated.** A capture cited by several mappings, and a file cited by several captures, is in the package once.
- **Redactions.** Text passes through the log redactor and each changed field is listed in `redactions.csv`. A capture that would need redacting is withheld with its digest, not altered.
- **Gaps** in `gaps.csv`: each mapping's gaps (above), issues not captured or captured in part, files not stored, and controls in scope with no issue in force or none with evidence in the period. Exceptions are issues in the `exception` category; remediation status is the issue's state in GitLab, never proof that a control works.
- **Verifying.** `baluarte-verify-export <file> --manifest-sha256 <value>` checks it offline like any export, and also checks each `files/<sha256>` against its name, that every attachment a capture cites is in the package or listed as missing, and that every mapping's issue, cited items and capture are present. The manifest hash is in the `register.export_completed` audit event, and `GET /api/v1/register/exports/{export_id}` returns it.
- **API and audit.** `POST /api/v1/register/exports` with `issues` (`[{project_id, iid}]`) or `controls` (`[{framework, control_id}]`), and optionally `period_id`. Needs `evidence:package` (administrators, analysts and auditors). Audited as `register.export_created` (scope and counts) and `register.export_completed` (sizes and hashes); the package is built in full before the first byte is sent.

## GitHub

GitHub connections (roadmap M13.5, [GITHUB_INTEGRATION.md](GITHUB_INTEGRATION.md)) share the register: the same rules, decisions, mappings and exports apply, and GitHub issues are recorded in the shape GitLab issues are, so a register and a package can hold both. A repository is the project; an issue's number is its `iid`.

- **When.** The `issues` run follows each GitHub collection, for connections whose workspace has a register; adding a rule or *Read issues now* queues it alone (`github.collect_issues`), as do the `issues` and `issue_comment` webhook events.
- **The index.** `GET /repos/{owner}/{repo}/issues?state=all&sort=updated&direction=asc`, with `since` (the latest update read, less five minutes) after the first read. Pull requests, which GitHub lists there too, are left out. `open` is recorded as `opened`. GitHub's issue types are defined per organization: `Task` is `task`, every other issue `issue`. GitHub has no confidential issues.
- **Evidence.** The issue and its description, its comments (`…/issues/{number}/comments`), and its timeline (`…/timeline`): closed and reopened, labels and milestones added and removed as status history; issues that mention it as linked issues (`mentioned`); pull requests that mention it, up to 20, each with its reviewers' latest verdicts (`…/pulls/{number}/reviews`) and the latest run of each workflow for its head commit (`…/actions/runs?head_sha=`). A pull request whose merge commit closed the issue *closes* it; the others are *related*.
- **Not captured.** Releases (`unsupported`: GitHub releases are not tied to milestones). Files attached on GitHub are listed as a gap with their count, so such a capture is `partial`: GitHub serves them through redirects to signed storage, and Baluarte never follows redirects. Required review counts are a branch setting, judged by `gitlab.project.merge_request_approvals`, so `approvals_required` is empty.
- **On the page and in mappings**, GitHub's words are used: *Pull request #8*, *Workflow run 900 of #8*.
- **Every request is a GET** with the connection's read-only token or App installation.

## Limits

- Epics and other group-level planning records are not listed yet.
- Releases are matched through the issue's milestone, not through commits.
- Captured files live in PostgreSQL and count toward its size and its backups.
- An issue deleted in GitLab or GitHub stays in the register as last read.
- GitHub sub-issues and issue dependencies are not read; a mention in another issue is.
- Confidential issues are listed (Baluarte's token can read them); the page shows "Confidential issue" in place of the title.
- Closing an issue is never treated as proof that a control works.
