# Control mapping

Baluarte maps each baseline rule type ([POLICY_ENGINE.md](POLICY_ENGINE.md#rule-types)) to the SOC 2, ISO/IEC 27001:2022 and NIST SP 800-53 Rev. 5 (moderate) controls it supports, and shows each control's status from the latest successful evaluation run. An organization changes the mapping to fit its own control set: it adds or removes a rule type, scopes a control out, or records that a control is met outside GitLab, each with a justification and, when [review is required](POLICY_ENGINE.md#reviewing-baseline-changes), a second person's approval.

The mapping is guidance for an organization and its auditor. A met control means every mapped rule passed in GitLab; it is not a SOC 2 certification, ISO 27001 certification or NIST authorization, and it says nothing about the parts of a control outside source code governance. The auditor decides whether the evidence supports the organization's control objective.

## Catalogue

The catalogue (`baluarte.controls.catalogue`) holds the controls that source code governance in GitLab can support. Titles are short paraphrases written for Baluarte, not the text of the standards; read the standard for the control itself. `MAPPING_VERSION` (now `2026.11`) changes whenever a control or a default pair changes, and every change records the version it was made against.

| Framework | Control | Title | Area |
|---|---|---|---|
| SOC 2 | CC6.1 | Logical access to information assets is restricted | Access control |
| SOC 2 | CC6.2 | People are registered and authorized before they get access | Access control |
| SOC 2 | CC6.3 | Access is granted, changed and removed by role | Access control |
| SOC 2 | CC6.6 | Access from outside the system boundary is restricted | Access control |
| SOC 2 | CC6.8 | Unauthorized or malicious software is prevented or detected | Secure development |
| SOC 2 | CC7.1 | Configuration changes and vulnerabilities are detected | Vulnerability management |
| SOC 2 | CC8.1 | Changes are authorized, tested and approved before release | Change management |
| ISO 27001 | A.5.15 | Access rules are defined and enforced | Access control |
| ISO 27001 | A.5.17 | Authentication secrets are issued and managed | Authentication |
| ISO 27001 | A.5.18 | Access rights are granted, reviewed and removed | Access control |
| ISO 27001 | A.8.2 | Privileged access is limited and managed | Access control |
| ISO 27001 | A.8.3 | Access to information follows the access rules | Access control |
| ISO 27001 | A.8.4 | Read and write access to source code is controlled | Access control |
| ISO 27001 | A.8.5 | Authentication is strong enough for the access it grants | Authentication |
| ISO 27001 | A.8.8 | Technical vulnerabilities are found and addressed | Vulnerability management |
| ISO 27001 | A.8.9 | Configurations are defined, applied and monitored | Configuration management |
| ISO 27001 | A.8.25 | Security is part of the development life cycle | Secure development |
| ISO 27001 | A.8.28 | Code is checked against secure coding practice | Secure development |
| ISO 27001 | A.8.29 | Security is tested during development and acceptance | Secure development |
| ISO 27001 | A.8.32 | Changes follow a controlled process | Change management |
| NIST 800-53 | AC-2 | Accounts are managed from creation to removal | Access control |
| NIST 800-53 | AC-3 | Approved authorizations are enforced | Access control |
| NIST 800-53 | AC-5 | Duties are separated between people | Access control |
| NIST 800-53 | AC-6 | Only the access a task needs is granted | Access control |
| NIST 800-53 | AC-6(5) | Privileged accounts are limited to designated people | Access control |
| NIST 800-53 | IA-2(1) | Privileged accounts sign in with more than one factor | Authentication |
| NIST 800-53 | IA-5 | Authenticators are managed, including their lifetime | Authentication |
| NIST 800-53 | CM-2 | A baseline configuration is kept current | Configuration management |
| NIST 800-53 | CM-3 | Configuration changes are reviewed and approved | Change management |
| NIST 800-53 | CM-5 | Who can change the system is restricted | Change management |
| NIST 800-53 | CM-6 | Security settings are defined and enforced | Configuration management |
| NIST 800-53 | RA-5 | Systems are scanned for vulnerabilities | Vulnerability management |
| NIST 800-53 | SA-11 | Developers test and evaluate security | Secure development |
| NIST 800-53 | SI-2 | Flaws are found and corrected | Vulnerability management |

## Default mapping

Each default pair carries a one-sentence rationale. Every rule type supports at least one control in each framework.

| Rule type | Control | Rationale |
|---|---|---|
| `gitlab.project.visibility` | SOC 2 CC6.1 | Visibility decides who can read a project's code at all. |
| `gitlab.project.visibility` | SOC 2 CC6.6 | Internal or public visibility opens the code beyond its members. |
| `gitlab.project.visibility` | ISO 27001 A.5.15 | Allowed visibility levels are an access rule for code. |
| `gitlab.project.visibility` | ISO 27001 A.8.3 | Visibility restricts access to the information in a project. |
| `gitlab.project.visibility` | ISO 27001 A.8.4 | Visibility is the first control on read access to source code. |
| `gitlab.project.visibility` | NIST 800-53 AC-3 | GitLab enforces the visibility level on every read. |
| `gitlab.branch.default_protected` | SOC 2 CC8.1 | A protected default branch takes changes only through its rules. |
| `gitlab.branch.default_protected` | ISO 27001 A.8.4 | Branch protection controls write access to released code. |
| `gitlab.branch.default_protected` | ISO 27001 A.8.32 | Protection routes changes to the default branch through a process. |
| `gitlab.branch.default_protected` | NIST 800-53 CM-3 | Protection keeps unreviewed changes off the default branch. |
| `gitlab.branch.default_protected` | NIST 800-53 CM-5 | Protection limits who can push or merge to the default branch. |
| `gitlab.security_policy.scan_execution` | SOC 2 CC7.1 | Required scans detect vulnerabilities as code changes. |
| `gitlab.security_policy.scan_execution` | SOC 2 CC8.1 | Scans run as part of testing every change. |
| `gitlab.security_policy.scan_execution` | ISO 27001 A.8.8 | Dependency and container scans find technical vulnerabilities. |
| `gitlab.security_policy.scan_execution` | ISO 27001 A.8.28 | SAST and secret detection check code against secure practice. |
| `gitlab.security_policy.scan_execution` | ISO 27001 A.8.29 | Scans are security testing built into development. |
| `gitlab.security_policy.scan_execution` | NIST 800-53 RA-5 | Scheduled and pipeline scans are vulnerability scanning. |
| `gitlab.security_policy.scan_execution` | NIST 800-53 SA-11 | Scans in the pipeline are developer security testing. |
| `gitlab.security_policy.scan_execution` | NIST 800-53 SI-2 | Scan results identify flaws to correct. |
| `gitlab.security_policy.approval` | SOC 2 CC8.1 | An approval policy requires changes to be approved before merge. |
| `gitlab.security_policy.approval` | ISO 27001 A.8.25 | Required approvals put review into the development life cycle. |
| `gitlab.security_policy.approval` | ISO 27001 A.8.32 | Approval before merge is a step of the change process. |
| `gitlab.security_policy.approval` | NIST 800-53 CM-3 | Changes are approved by someone before they are merged. |
| `gitlab.security_policy.approval` | NIST 800-53 AC-5 | Forbidding author approval separates writing from approving. |
| `gitlab.project.merge_request_approvals` | SOC 2 CC8.1 | Project approval settings require changes to be approved. |
| `gitlab.project.merge_request_approvals` | ISO 27001 A.8.32 | Approval settings make review part of every change. |
| `gitlab.project.merge_request_approvals` | NIST 800-53 CM-3 | Changes need the configured approvals before merge. |
| `gitlab.project.merge_request_approvals` | NIST 800-53 AC-5 | Forbidding author and committer approval separates duties. |
| `gitlab.project.privileged_members` | SOC 2 CC6.1 | Fewer privileged members means fewer people who can change access. |
| `gitlab.project.privileged_members` | SOC 2 CC6.2 | Inactive accounts that keep access show deprovisioning gaps. |
| `gitlab.project.privileged_members` | SOC 2 CC6.3 | A cap on Maintainers and Owners keeps access to what roles need. |
| `gitlab.project.privileged_members` | ISO 27001 A.5.15 | The cap is an access rule for privileged roles. |
| `gitlab.project.privileged_members` | ISO 27001 A.5.18 | Inactive accounts with access show rights that were not removed. |
| `gitlab.project.privileged_members` | ISO 27001 A.8.2 | Maintainer and Owner are the privileged access rights in GitLab. |
| `gitlab.project.privileged_members` | NIST 800-53 AC-2 | Inactive accounts holding access show account management gaps. |
| `gitlab.project.privileged_members` | NIST 800-53 AC-6 | The cap limits access to what is needed. |
| `gitlab.project.privileged_members` | NIST 800-53 AC-6(5) | Maintainers and Owners are the privileged accounts of a project. |
| `gitlab.group.two_factor_required` | SOC 2 CC6.1 | Two-factor sign-in protects access to the code. |
| `gitlab.group.two_factor_required` | ISO 27001 A.5.17 | Requiring a second factor governs how people authenticate. |
| `gitlab.group.two_factor_required` | ISO 27001 A.8.5 | Two-factor authentication is secure authentication. |
| `gitlab.group.two_factor_required` | NIST 800-53 IA-2(1) | Enforced two-factor sign-in covers privileged accounts. |
| `gitlab.project.access_tokens` | SOC 2 CC6.1 | Tokens are credentials with access to the project. |
| `gitlab.project.access_tokens` | SOC 2 CC6.2 | Expiring and unused tokens show whether credentials are retired. |
| `gitlab.project.access_tokens` | ISO 27001 A.5.17 | Token lifetimes are part of managing authentication secrets. |
| `gitlab.project.access_tokens` | ISO 27001 A.5.18 | Unused tokens are access rights that should be removed. |
| `gitlab.project.access_tokens` | NIST 800-53 IA-5 | A maximum token lifetime is authenticator management. |
| `gitlab.project.access_tokens` | NIST 800-53 AC-2 | Project tokens act as accounts and need the same lifecycle. |

### Change rules

Mapping version `2026.11.1` adds the change rules:

| Rule type | SOC 2 | ISO/IEC 27001:2022 | NIST SP 800-53 |
|---|---|---|---|
| `gitlab.change.reviewed` | CC8.1 | A.8.32 | CM-3, AC-5 |
| `gitlab.change.pipeline_passed` | CC8.1 | A.8.29 | SA-11 |
| `gitlab.change.deployed_after_review` | CC8.1 | A.8.32 | CM-3 |

## Changes

An organization changes the mapping through `control_mapping_change` rows. Each names a control and carries a justification of 20 to 2,000 characters.

| Kind | Does | Also needs |
|---|---|---|
| `map` | Adds a rule type to a control. Refused when the rule type is already mapped to it. | `rule_type` |
| `unmap` | Removes a default pair. Only a default pair can be unmapped; withdraw a `map` instead. | `rule_type` |
| `not_applicable` | Scopes the control out. | `review_by` |
| `manual` | Records that the control is met outside GitLab. | `review_by`, `evidence_ref` (where the evidence is, up to 500 characters) |
| `withdraw` | Ends an active change: an unmapped pair returns, a mapped one goes, a scoped-out or manual control reverts. | `supersedes_id` |

`review_by` is after today and at most 365 days ahead. A control has at most one `not_applicable` or `manual` change at a time; to renew one, or to change one into the other, propose the new one with `supersedes_id` set to the current one, and the current one is withdrawn when the new one takes effect. The same change cannot be in force or waiting twice, and only one change at a time may supersede a given change.

**Review.** Changes follow the `governance` setting, like baseline changes. Without required review a change takes effect at once, recorded as `self_approved` by its proposer. With it the change is `proposed` until someone other than the proposer approves it (`active`) or rejects it with a note (`rejected`); a proposer cannot decide their own change. Approval checks the change again, so a stale or conflicting proposal is refused. A change that is withdrawn becomes `withdrawn`.

Who may change the mapping: `policy:write` (admins and analysts). Reading needs `findings:read`.

**Append-only.** A trigger refuses `DELETE` and allows only `proposed` to `active` or `rejected` (recording the decision) and `active` to `withdrawn` (nothing else changes). What a change proposed, including its justification, never changes. Each step is audited: `control_mapping.proposed`, `control_mapping.approved`, `control_mapping.rejected` and `control_mapping.withdrawn`, with the change's details.

## Status

Each control's status comes from the results of its mapped rule types in the latest successful evaluation run, and from its active `not_applicable` or `manual` change. The first that applies wins:

| Status | When |
|---|---|
| `not_applicable` | The control is scoped out. |
| `review_overdue` | The control is scoped out and its `review_by` has passed. |
| `not_met` | A mapped rule failed for some project. A failure outranks a `manual` record. |
| `manual` | The control is recorded as met outside GitLab (`review_overdue` once `review_by` has passed). |
| `not_covered` | No rule type is mapped to it. |
| `no_results` | Rule types are mapped, but no assigned baseline has a rule of those types, or no run has succeeded. |
| `unknown` | No failure, but some result is `unknown` or `unsupported`. |
| `met` | Every result passed or is excepted by an approved exception. |

Counts are results (one per project and rule): `pass`, `fail`, `unknown` (including unsupported), `excepted`, and the number of projects with at least one result.

## API

| Endpoint | Needs | Does |
|---|---|---|
| `GET /api/v1/controls?framework=` | `findings:read` | Every control with its status, mapped rule types, removed default pairs (`unmapped`, with the change to withdraw), its status change and counts; and `mapping_version`, `evaluation_run_id` and the frameworks. |
| `GET /api/v1/controls/{framework}/{control_id}` | `findings:read` | The same for one control, plus `rules` (each baseline rule and version that produced results, with counts) and `changes` (its history, newest first). |
| `GET /api/v1/control-mapping/changes` | `findings:read` | Changes newest first: `items`, `review_required` and `next_before_id`. Filters: `state`, `framework`, `control_id`, `before_id`, `limit`. |
| `POST /api/v1/control-mapping/changes` | `policy:write` | Proposes a change: 201 with the change. A body that does not fit its kind is 422. |
| `POST /api/v1/control-mapping/changes/{id}/approve` | `policy:write` | `{}` or `{"note": ...}`. |
| `POST /api/v1/control-mapping/changes/{id}/reject` | `policy:write` | `{"note": ...}`, required. |

`proposed_by` and `decided_by` are `{actor, user_id, display_name}`. A conflict is 409 with `{code, message, reasons: [{code, message}]}`, `code` being the first reason: `control_unknown`, `rule_type_unknown`, `duplicate`, `not_default`, `already_mapped`, `status_exists`, `supersedes_not_active`, `supersedes_other_control`, `supersedes_invalid_kind`, `supersede_pending`, `review_by_required`, `review_by_past`, `review_by_too_far`, or `not_proposed` when the change was already decided. Deciding your own change is 403 with code `own_change`.

Findings carry `controls: [{framework, id}]`, the controls their rule type supports under the mapping in force, in catalogue order.

## Evidence

An export can be scoped to one control with `framework` and `control_id` ([AUDIT_EVIDENCE.md](AUDIT_EVIDENCE.md#export)): evaluations, findings and exceptions of the rule types mapped to it. Every manifest records the mapping in force as `control_mapping`: `mapping_version`, every effective pair (`framework`, `control_id`, `rule_type`, `source` default or organization, and `change_id`), and every active change with the SHA-256 of its justification, so an auditor can tie the package to the audit events that record each change.

Control status and the mapping can be read as of a past moment with `as_of`: the mapping then is every change decided by then and not superseded by then ([CHANGE_EVIDENCE.md](CHANGE_EVIDENCE.md#reading-as-of-a-moment)).

## Limits

- The catalogue is fixed; an organization cannot add its own controls or frameworks.
- Status is per control across the whole instance, not per project or group.
- A mapping change takes effect for status and findings at once; it does not re-run evaluations, and exports made earlier keep the mapping they recorded.
