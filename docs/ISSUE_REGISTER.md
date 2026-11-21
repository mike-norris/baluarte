# Auditable issue register

Roadmap milestone M12 ([ROADMAP.md](ROADMAP.md#m12--auditable-issue-register)), decided in [ADR-0015](architecture/ADR-0015-auditable-issue-register.md). The register lists the issues an auditor should examine, says why each one is there, and, from later steps, exports its evidence against controls.

## Status

| Step | What it delivers | State |
|---|---|---|
| M12.1 Register and selection | The issue index per connection (any type and state); selection rules by label, project and issue type; including or excluding one issue with a reason; categories; a register page (`/register`, `/api/v1/register`) with filters and why each issue is listed (migration `0033`) | Done |
| M12.2 Issue evidence | For auditable issues only: the description, comments, status history, linked issues, closing and related merge requests with their approvals and pipelines, releases for the issue's milestone, and attachments captured with SHA-256 checksums and stored once; sources that could not be read are gaps on the issue; an issue page (`/register/issues/:project/:iid`) (migration `0034`) | Done |
| M12.3 Control mappings | Many-to-many issue-to-control mappings with a rationale per evidence item, proposed and reviewed like control mapping changes; audit periods, with evidence inside the period kept apart from later remediation; gaps per issue and control | Planned |
| M12.4 Exports | Packages for one issue, a selection, every issue mapped to a control, or a period across selected controls: summary, manifest, issue-to-control index with framework versions, shared evidence stored once, provenance and checksums, assessments, approvals, exceptions and remediation status, gaps and redactions; verified offline with `baluarte-verify-export` | Planned |

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
- **Not evidence of effectiveness.** A captured closing merge request or approval shows what happened, not that a control works; mapping evidence to controls, with a reviewed rationale, is M12.3.

## Limits

- Epics and other group-level planning records are not listed yet.
- Releases are matched through the issue's milestone, not through commits.
- Captured files live in PostgreSQL and count toward its size and its backups.
- An issue deleted in GitLab stays in the register as last read.
- Confidential issues are listed (Baluarte's token can read them); the page shows "Confidential issue" in place of the title.
- Closing an issue is never treated as proof that a control works.
