# Configuration templates and bulk changes

Roadmap milestone M14 ([ROADMAP.md](ROADMAP.md#m14--configuration-templates-and-bulk-changes)), decided in [ADR-0016](architecture/ADR-0016-configuration-templates.md). A template says exactly how repositories should be configured; bulk changes apply it, and drift shows where repositories differ from it. **Applying a template changes settings; it does not establish compliance.** Baselines and people still judge that.

## Status

| Step | What it delivers | State |
|---|---|---|
| M14.1 Templates | The template format, immutable versions, inheritance (`extends`), assignment to a connection, group or project with nearest-wins precedence and overrides with reasons; each project's effective settings with where each came from; settings a provider has no equivalent for, or that depend on the plan, shown as such; a Templates page (migration `0038`) | Done |
| M14.2 Drift | Effective settings compared with collected evidence per project and setting: aligned, drift, unknown, unsupported or excepted; exceptions per setting with expiry warnings; alignment coverage by template, group and connection (migration `0039`) | Done |
| M14.3 Change sets | Target selection by hierarchy, owner, provider, tags, business unit, policy and compliance status, and saved collections; a change set with the exact targets and before and after values from evidence; checks for permissions, capabilities, conflicting baselines and exceptions; warnings for changes that weaken protection, remove access or disrupt work; approval of the set, with a second approval for sensitive changes (migration `0040`) | Done |
| M14.4 Execution | Staged rollout to a first set of targets; background jobs with per-target progress, rate-limit handling, retries, cancellation and resumption; each setting read again before it is changed, concurrent changes flagged, previous values kept, rollback where the provider allows it and irreversible steps disclosed; behind its own switch and write token, off by default | Planned |
| M14.5 Alignment and tags | Manual remediation of drift as a change set; automatic remediation per template assignment, off by default; settings verified after each change; Baluarte tags synchronized to GitLab topics or GitHub topics only when asked | Planned |
| M14.6 Records and acceptance | Requester, approver, executor, rationale, template version, scope, timestamps, before and after values and per-target results recorded, linked to controls and in exports; the bulk governance workflow end to end, with a partial failure and an exception that stay visible | Planned |

## Template format

```yaml
apiVersion: baluarte.io/v1alpha1
kind: ConfigurationTemplate
metadata:
  name: service-repository        # lowercase letters, digits and hyphens, 1 to 63
  description: Services that ship to production
spec:
  extends: base-repository@2      # optional: one exact version of another template
  settings:
    visibility: private           # private, internal or public
    default_branch_protected: true
    merge_requests:
      min_approvals: 2
      forbid_author_approval: true
      forbid_committer_approval: false
      reset_approvals_on_push: true
      forbid_override: true
    security_features: [secret_scanning, secret_scanning_push_protection]
    labels_consistent: true       # the workspace's label taxonomy (LABELS.md); only true
```

- **Only settings Baluarte reads.** Each setting is one Baluarte collects and the rule type that judges it ([POLICY_ENGINE.md](POLICY_ENGINE.md#rule-types)), so drift can rest on evidence. Unknown keys are refused. At least one setting is needed, counting inherited ones.
- **Merge methods, repository metadata and CI/CD settings** are not settings yet: Baluarte does not collect them. They are added as their collection is.

## Settings and providers

| Setting | Judged as | GitLab | GitHub |
|---|---|---|---|
| `visibility` | `gitlab.project.visibility` with exactly that value | Supported | `private` and `public`; `internal` depends on the plan (Enterprise Cloud) |
| `default_branch_protected` | `gitlab.branch.default_protected` | Supported | Depends on the plan: not for private repositories on GitHub Free |
| `merge_requests.min_approvals`, `reset_approvals_on_push` | `gitlab.project.merge_request_approvals` | Depends on the plan (approval rules: Premium) | Depends on the plan, as branch protection |
| `merge_requests.forbid_author_approval` | same | Depends on the plan | Always holds: GitHub never lets an author approve |
| `merge_requests.forbid_committer_approval`, `forbid_override` | same | Depends on the plan | Unsupported: no equivalent |
| `security_features` | `github.repository.security_features` | Unsupported: a GitHub setting | Supported; code scanning depends on GitHub Advanced Security |
| `labels_consistent` | `gitlab.labels.consistent` | Supported | Supported |

The effective settings of every project list this support for its connection's provider. Whether a plan offers a setting is known only from what collection read, so "depends on the plan" becomes supported or unsupported per project in drift.

## Versions

Templates are stored like baselines: each save of a changed document under the same name adds the next version in the workspace, saving identical text returns the existing version, and a database trigger refuses any change to a saved version. Saving is audited (`template.version_created`). `extends` must name a saved version, the chain is at most 5 templates deep and may not loop; the parent's settings are copied into nothing, so the stored source is exactly what was submitted.

## Assignment and precedence

- **Targets.** A template version is assigned to a connection, a group or a project that is `active` in the inventory. A target holds at most one template assignment; assigning another replaces it (`template.assigned`, with the replaced assignment). Removing one is `template.unassigned`.
- **Overrides.** An assignment may override settings for its target, with a reason of 20 to 2,000 characters. Overrides use the template format's `settings` and are validated the same way.
- **Effective settings.** A project's path is walked nearest first, as for baselines ([POLICY_ENGINE.md](POLICY_ENGINE.md#resolution)): the project, its groups, the connection. For each setting (each field of `merge_requests` separately) the nearest assignment that sets it decides, through its overrides or its template chain (the child before its parent). Each effective setting names the value, the template and version, the assignment and target, and whether it came from an override, `extends` or the template itself; the values it replaced are listed beneath it.
- **Incomplete ancestry** is reported as for baselines: settings assigned above a gap may be missing.
- **Permissions.** `findings:read` reads templates and effective settings; `policy:write` saves, assigns and unassigns. Templates do not yet follow the governance review setting; change sets are where review applies.

## Drift

Each effective setting of a project is compared with what was last collected, by the evaluator of the rule type that already judges it ([EVALUATION.md](EVALUATION.md)). The template's value becomes a probe rule, so stale or failed collection, a missing project, incomplete ancestry, a token that cannot read the setting and a plan without it give `unknown` or `unsupported` exactly as for baselines; they are never `aligned`.

| Status | Meaning |
|---|---|
| `aligned` | The evidence shows exactly the template's value |
| `drift` | The evidence shows another value |
| `excepted` | Drift, waived by an approved, unexpired template exception |
| `unknown` | The evidence cannot tell (the reason says why) |
| `unsupported` | The provider or its plan has no such setting |

Templates name exact values, so the probes check both sides: `min_approvals: 2` is aligned when requiring 2 approvals passes and requiring 3 fails (a project requiring more drifts too); a boolean set to `false` is aligned when the setting is off; `visibility` must match exactly; every listed `security_features` entry must be enabled; `labels_consistent: true` means no label drift reaches the project.

A project is `drift` when any setting drifts, otherwise `unknown` when any setting is unknown, otherwise `aligned`. **Alignment** covers the connection the request works on: its active, monitored, unarchived projects, how many have templates and are aligned, drifting or cannot be told, setting counts by template version and by top-level group, the projects needing attention (drift first, at most 200), and approved exceptions ending within 14 days. It is computed on request from stored evidence; nothing is read from the provider.

## Exceptions

A template exception lets one project differ from one setting until it ends (at most 365 days), with a justification. It has the lifecycle of a policy exception ([POLICY_ENGINE.md](POLICY_ENGINE.md#exceptions)): requested with `exception:request`, approved or rejected by someone else with `exception:approve` (rejecting needs a note), revoked with a note, and expired at `expires_at`; a database trigger enforces the transitions, keeps the request unchanged and refuses deletion. Only a setting a template sets for the project can be excepted, and one project and setting holds at most one open exception. An approved exception turns `drift` into `excepted`; it never hides `unknown` or `unsupported`. Approved exceptions ending within 14 days are flagged. Every step is audited (`template_exception.requested`, `.approved`, `.rejected`, `.revoked`, `.expired`).

## Change sets

A change set lists the exact changes a template version, or settings written for the set, would make to chosen projects. It is built only from stored evidence; nothing is read from or written to the provider, and applying an approved set is M14.4.

**Choosing projects.** A selection gives any of these criteria, and a project must meet every one given. Only active, monitored, unarchived projects of the current connection are ever chosen, at most 500 per set.

| Criterion | Selects |
|---|---|
| `groups` | Projects in these groups or their subgroups, by full path |
| `projects` | These projects, by exact path |
| `tags` | Projects carrying every `key=value`, directly or from a group above; owner and business unit are tags ([TAGS.md](TAGS.md)) |
| `template` | Projects where this template decides at least one effective setting |
| `drift` | Projects whose drift status is one of `drift`, `unknown`, `aligned` |
| `findings`, `policy` | Projects with (`open`) or without (`none`) open baseline findings, optionally of one baseline |

The provider is the connection's. A selection can be previewed and saved under a name for reuse; a change set keeps the criteria it was built from.

**What a set records.** For each project and setting, the current value read from evidence through the same evaluators as drift (`before`), the wanted value (`after`) and the time of the evidence; or that it is unchanged; or a skip with its reason:

| Skip | When |
|---|---|
| `unsupported` | The provider or its plan has no such setting |
| `unknown` | The evidence cannot tell the current value: stale or failed collection, missing access, incomplete ancestry |
| `excepted` | An approved template exception lets the project differ |
| `conflicts_with_baseline` | A baseline in force would fail with the wanted value |
| `label_plans` | `labels_consistent`, which label plans change |

Each change carries warnings: **weakens protection** (wider visibility, fewer approvals, a protection or approval rule turned off), **removes access** (narrower visibility) or **disrupts work** (a newly protected default branch, more approvals, new approval rules, push protection). Permissions are checked twice: the person needs `remediation:plan` to build a set and `remediation:approve` to decide one, and the reading token's access shows up as `unknown`; the write token and its role are checked when applying (M14.4).

**Approval.** A set is `proposed` until someone other than its creator approves or rejects it (rejecting needs a note). A **sensitive** set, one with any change that weakens protection or removes access, needs two approvals by two different people, neither the creator; after the first it stays `proposed`. Approval names the set's digest and is refused when the set is stale: built again from today's evidence it no longer gives the same targets, values and skips. The creator may withdraw a proposed set; proposed and approved sets expire after their window (at most 7 days, 72 hours by default). The document, digest and decisions never change, rows are never deleted (a database trigger enforces this), and every step is audited (`template_change_set.created`, `.first_approval`, `.approved`, `.rejected`, `.withdrawn`, `.expired`; `saved_selection.created`, `.deleted`). In development mode the creator may give the approvals, recorded as `development_decision`.

## API

| Endpoint | Permission | Behaviour |
|---|---|---|
| `GET /api/v1/templates` | `findings:read` | Templates with their versions, newest first |
| `GET /api/v1/templates/{name}/versions/{version}` | `findings:read` | The source, its digest, its parsed settings and its resolved chain |
| `POST /api/v1/templates` | `policy:write` + CSRF | Body `{source_yaml}`. 201 with the new version, 200 when identical to the latest; 422 with the problems when invalid |
| `GET /api/v1/template-assignments` | `findings:read` | Every assignment of the workspace, with its target's state |
| `POST /api/v1/template-assignments` | `policy:write` + CSRF | Body `{connection_id?, name, version, target_type, target_id?, overrides?, override_reason?}`; without `connection_id`, the connection the request works on. 201; 404 for an unknown version or target; 409 for a target that is not active |
| `DELETE /api/v1/template-assignments/{id}` | `policy:write` + CSRF | 204 |
| `GET /api/v1/templates/settings` | `findings:read` | Each setting's support on GitLab and GitHub, with why |
| `GET /api/v1/templates/drift/{project_id}` | `findings:read` | Each effective setting's status, reason, evidence time, source and exception |
| `GET /api/v1/templates/alignment` | `findings:read` | Alignment of the current connection |
| `GET /api/v1/template-exceptions` | `findings:read` | Newest first, at most 500; `state` and `project_id` filters |
| `POST /api/v1/template-exceptions` | `exception:request` + CSRF | Body `{project_id, setting, justification, expires_at}`. 201; 404 when no template sets the setting for the project; 409 when one is already open |
| `POST /api/v1/template-exceptions/{id}/approve`, `/reject`, `/revoke` | `exception:approve` + CSRF | Body `{note}` (needed to reject or revoke). 403 for one's own request |
| `POST /api/v1/template-selections/preview` | `remediation:plan` + CSRF | Body: a selection. The number of projects and the first 200 |
| `GET /api/v1/template-selections` | `findings:read` | Saved selections of the current connection |
| `POST /api/v1/template-selections`, `DELETE /api/v1/template-selections/{id}` | `remediation:plan` + CSRF | Body `{name, selection}`. 409 when the name is taken |
| `GET /api/v1/template-change-sets` | `findings:read` | Newest first, at most 200; `state` filter |
| `GET /api/v1/template-change-sets/{id}` | `findings:read` | A change set with its document |
| `POST /api/v1/template-change-sets` | `remediation:plan` + CSRF | Body `{title, selection, template \| settings, expires_in_hours}`. 201; 422 for an empty selection, no matching project, more than 500, unknown template, invalid settings or nothing to change |
| `POST /api/v1/template-change-sets/{id}/approve` | `remediation:approve` + CSRF | Body `{digest, note?}`. 403 for the creator or a repeated approver; 409 when not proposed, the digest differs or the set is stale |
| `POST /api/v1/template-change-sets/{id}/reject` | `remediation:approve` + CSRF | Body `{note}` |
| `POST /api/v1/template-change-sets/{id}/withdraw` | `remediation:plan` + CSRF | Only the creator |
| `GET /api/v1/templates/effective/{project_id}` | `findings:read` | The project's effective settings with provenance and provider support, and the ancestry |
