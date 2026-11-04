# Monitoring scope

The inventory holds everything the GitLab token can see. That usually includes research, sandbox and personal projects the organization does not want judged against its baselines. Monitoring scope lets an administrator or analyst take projects, whole groups, or every personal-namespace project out of monitoring with a justification, and bring them back later.

Implemented in `backend/src/baluarte/scope/exclusions.py` (migration `0020`) and `backend/src/baluarte/api/v1/scope.py`.

## What an exclusion covers

An exclusion names one target:

| Target | Covers |
|---|---|
| `project` | That project. |
| `group` | The group, its subgroups at any depth, and the projects in them. Personal-namespace projects are never in a group. |
| `personal_namespaces` | Every project whose `namespace_kind` is `user`, including ones discovered later. |

A project is excluded while an `active` exclusion covers it. When more than one does, the project's own exclusion is the one named, then its nearest excluded group, then `personal_namespaces`. Group coverage follows the inventory's group tree (`parent_gitlab_id`), so moving a project into or out of an excluded group in GitLab changes its scope at the next discovery.

`coverage(db, instance_id)` resolves every active exclusion once. `exclusion_for(project)` and `excluded_projects(db, instance_id)` use it, as does everything below, so there is one rule for what is excluded.

## What exclusion does and does not do

Excluded projects **stay in the inventory**. Discovery still lists them, so the inventory still shows everything the token can see, and an auditor can see what was left out and why.

| Area | Effect |
|---|---|
| Evaluation | No rule results. Open and excepted findings resolve with `resolution = project_excluded`, counted under the run's `findings.project_excluded`; the project is not counted in the run's `projects`. An archived project that is also excluded resolves as `project_archived`. |
| Collection | Branch protection, approvals, members, access tokens and two-factor reads skip excluded projects. Security policy discovery skips excluded projects and groups. A skip records nothing, so earlier observations stay as history. |
| Counts | `GET /evaluations/summary` and control posture (`GET /controls`) leave excluded projects out, including results the latest run recorded before the exclusion was made. |
| Findings | `GET /findings` leaves excluded projects out by default (`monitoring=included`); `monitoring=excluded` or `all` list them. A finding read by ID is always returned. |
| Remediation | Findings of excluded projects are not planned (`project_excluded`). |
| Inventory | The project and group lists hide excluded items by default (`monitoring=included`) and report how many were hidden in `excluded_hidden`. `monitoring=excluded` or `all` list them. Detail reads always return them. Baselines can still be assigned to excluded targets; they apply once the target is included again. |
| Evidence export | The inventory section keeps excluded projects, flagged. Every other section leaves them out. The manifest lists the exclusions in force. |

Exclusion does not hide a project from GitLab, change anything in GitLab, or delete evidence. Results and findings recorded before the exclusion stay in the database; findings are resolved, not removed.

## Review

Excluding narrows what is monitored, so it follows the `governance` setting exactly like baseline and control-mapping changes:

- **Review required:** the batch is `proposed` until someone with `policy:write` other than the proposer approves it (`active`) or rejects it with a note (`rejected`). The proposer cannot decide their own batch (`403`, `own_change`).
- **Review not required:** the batch is `active` at once and recorded `self_approved`.

Rows proposed together share a `batch_id` and are decided together. The database enforces the same rules as the other review tables: a decider other than the proposer while review is required, `self_approved` exactly when it is not, and a note on every rejection.

## Including again

Ending an exclusion (`active` -> `ended`) brings its projects back into monitoring. It needs `policy:write` and an end justification of 20 to 2000 characters, but no second person, because it widens monitoring. Ending queues an evaluation; the next run judges the projects again, and a rule that still fails reopens the same finding.

To bring back one project covered by a group or by `personal_namespaces`, end that exclusion and exclude the rest again, or leave it excluded.

## Integrity

`scope_exclusion` is append-only apart from the decision and the end: a trigger allows only `proposed` -> `active` | `rejected` and `active` -> `ended`, keeps everything proposed unchanged, and refuses `DELETE`. A partial unique index allows one open (`proposed` or `active`) exclusion per target. Every step is audited:

| Event | Details |
|---|---|
| `scope.exclusion_proposed` | `batch_id`, `targets` (`id`, `target_type`, `target_id`, `target_path`), `justification_sha256`, `state`, `self_approved` |
| `scope.exclusion_approved`, `scope.exclusion_rejected` | `batch_id`, `targets`, `justification_sha256`, `proposed_by`, `note` |
| `scope.exclusion_ended` | `batch_ids`, `targets`, `justification_sha256` of the end justification |

## API

All under `/api/v1`. State changes need the `X-CSRF-Token` header.

| Method and path | Permission | Does |
|---|---|---|
| `GET /scope/exclusions?state=` | `inventory:read` | `{items, review_required}`, newest first. |
| `POST /scope/exclusions` | `policy:write` | `{targets: [{type: "project" \| "group", gitlab_id}], personal_namespaces: bool, justification}` -> `201 {batch_id, items}`. At most 500 targets. |
| `POST /scope/exclusions/batches/{batch_id}/approve` | `policy:write`, not the proposer | `{note?}` -> `{batch_id, items}`. |
| `POST /scope/exclusions/batches/{batch_id}/reject` | `policy:write`, not the proposer | `{note}` -> `{batch_id, items}`. |
| `POST /scope/exclusions/end` | `policy:write` | `{ids, justification}` -> `{items}`. |

Each item: `id`, `batch_id`, `target_type`, `target_id` (null for personal namespaces), `target_path` (as proposed), `state`, `justification`, `review_required`, `self_approved`, `proposed_by`, `proposed_at`, `decided_by`, `decided_at`, `decision_note`, `ended_by`, `ended_at`, `end_justification`. People are `{actor, user_id, display_name}`.

A refusal is `409` with `{code, message, reasons: [{code, message}]}`:

| Code | Meaning |
|---|---|
| `no_targets` | Nothing to exclude or end. |
| `too_many_targets` | More than 500 targets or IDs. |
| `duplicate_target` | A target or ID is listed twice. |
| `unknown_target` | A project or group is not in the inventory. |
| `already_excluded` | A target already has a `proposed` or `active` exclusion of its own. |
| `exclusion_unknown`, `not_active` | An ID to end does not exist, or is not `active`. |
| `not_proposed` | The batch was already decided. |

Inventory reads carry `monitoring: {excluded, exclusion_id, via, path}` on every project and group. `via` is `project`, `group` or `personal_namespaces` (null when monitored), and `path` is the covering project or group's path. `GET /gitlab/projects/{gitlab_id}` returns `not_evaluated: "excluded"` for an excluded project.

## Evidence

The evidence export (format `v3`) flags excluded projects in the inventory with `monitoring_excluded`, `exclusion_id` and `excluded_via`, and leaves them out of every other section. The manifest (`v5`) records `monitoring_scope`:

```json
{
  "exclusions": [
    {
      "id": 4,
      "target_type": "group",
      "target_id": 11,
      "target_path": "acme/research",
      "since": "2026-11-04T15:00:00Z",
      "justification_sha256": "…"
    }
  ],
  "excluded_projects": 12
}
```

`since` is when the exclusion came into force; `excluded_projects` counts the inventory projects in the export's scope that are excluded. See [AUDIT_EVIDENCE.md](AUDIT_EVIDENCE.md).
