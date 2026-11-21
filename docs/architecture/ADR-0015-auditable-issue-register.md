# ADR-0015: The auditable issue register

- Status: Accepted
- Date: 2026-11-21

## Context

Auditors need the issues that bear on a control, with their history and linked work, exported against those controls (roadmap M12, FR-027). Which issues are auditable is an audit decision: some follow from labels, projects or issue types, others are picked one by one. GitLab stays the system of record, and Baluarte must not imply that closing an issue proves a control works.

## Decision

- **Selection.** An issue is auditable when a selection rule of its workspace matches it, or someone includes it with a reason. A rule matches an issue that meets all of its non-empty criteria: any of its labels, one of its projects, one of its issue types. Each listed issue says which rules or decision put it there.
- **Exclusion is visible.** Excluding a rule-selected issue needs a reason. An excluded issue leaves the default view but stays listed, with the reason and who decided, so nothing leaves the register unseen.
- **Categories.** Each issue is an engineering issue, a security finding, an exception or a remediation task. An inclusion may name the category; otherwise the oldest matching rule's category applies; otherwise engineering.
- **Append-only decisions.** Rules and decisions are ended, never edited or deleted (database triggers), and every change is audited. Rules and decisions are not reviewed by a second person: they decide what is examined, not what is concluded, and the issue-to-control mappings that carry conclusions are reviewed (M12.3).
- **Who decides.** Administrators, analysts and auditors change the register (`register:write`); everyone who reads evidence reads it.
- **Collection only where needed.** Issues are read only for connections whose workspace has a register, incrementally by `updated_at`, with each project read stored as an observation. The issue index comes first (M12.1); descriptions, comments, history, links and attachments are captured only for auditable issues (M12.2).

## Consequences

- An auditor can see the whole selection, including what was taken out and why.
- A workspace without a register costs no reads.
- Changing a rule means ending it and adding another; the history of what was selected when stays reconstructable from the rules, decisions and audit log.
- Issues whose project leaves monitoring scope are no longer read; their stored rows stay.

## Revisit when

Rules need conditions beyond labels, projects and types (milestones, dates, free text), or a register must span workspaces.
