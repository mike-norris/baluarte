# Policy engine specification

## Policy format

Policies are versioned YAML documents. The JSON Schema is published at [`schemas/gitlab-baseline.v1alpha1.json`](schemas/gitlab-baseline.v1alpha1.json) (draft 2020-12). Policy files contain no executable Python, no Jinja evaluation and no shell commands.

```yaml
apiVersion: baluarte.io/v1alpha1
kind: GitLabBaseline
metadata:
  name: corporate-default
  description: Company-wide minimum # optional, up to 1000 characters
spec:
  scope:
    groups: ["*"]
  rules:
    - id: project-visibility
      type: gitlab.project.visibility
      allowed: [private]
      severity: high # optional: low, medium (default), high, critical
    - id: protected-default-branch
      type: gitlab.branch.default_protected
      required: true
```

Status: the format, its validation and versioned storage (BAL-017), and assignment and inheritance (BAL-018) are implemented in `backend/src/baluarte/policy/`. Evaluating rules against inventory is described in [EVALUATION.md](EVALUATION.md): the project visibility (BAL-019) and protected default branch (BAL-020) rules are implemented, and results and findings are stored and served (BAL-021; see [Evaluations and findings](DATA_MODEL.md#evaluations-and-findings)).

### Fields

| Path | Type and limits |
| --- | --- |
| `apiVersion` | Exactly `baluarte.io/v1alpha1`. |
| `kind` | Exactly `GitLabBaseline`. |
| `metadata.name` | 1 to 63 lowercase letters, digits and hyphens, starting and ending with a letter or digit. This names the policy. |
| `metadata.description` | Optional string, up to 1000 characters. |
| `spec.scope.groups` | 1 to 200 distinct entries. Each is `"*"` or a GitLab group full path such as `platform/backend`. The scope narrows where an assigned version applies (see [Scope](#scope)). |
| `spec.rules` | 1 to 100 rules. Rule `id`s are unique and follow the same pattern as `metadata.name`. |
| `spec.rules[].severity` | Optional: `low`, `medium` (default), `high` or `critical`. |
| `spec.rules[].description` | Optional string, up to 1000 characters. |

### Rule types

| `type` | Fields | Passes when |
| --- | --- | --- |
| `gitlab.project.visibility` | `allowed`: 1 to 3 distinct values from `private`, `internal`, `public` | The project's visibility is in `allowed`. |
| `gitlab.branch.default_protected` | `required`: boolean | With `required: true`, a protected-branch rule (exact or wildcard) covers the project's default branch. `required: false` asserts nothing. An empty repository is `unknown`. |
| `gitlab.security_policy.scan_execution` | `scans`: 1 to 6 distinct values from `secret_detection`, `sast`, `sast_iac`, `dependency_scanning`, `container_scanning`, `dast`. `allow_skip_ci`: boolean, default `true` | Every scan in `scans` runs on the default branch in an enabled scan execution policy that reaches the project. With `allow_skip_ci: false`, only policies that `[skip ci]` cannot skip count. |
| `gitlab.security_policy.approval` | `min_approvals`: integer, 1 to 20. `forbid_author_approval`, `forbid_committer_approval`: boolean, default `false` | An enabled approval policy that reaches the project requires at least `min_approvals` approvals on every merge request to protected branches, and prevents approval by the author or committers when asked. |

The security policy rules read what security policy discovery stored; a project whose policies were not read in full is `unknown`. [EVALUATION.md](EVALUATION.md#gitlab-security-policy-rules) gives the exact semantics.

Any other `type` is rejected. New rule types are added to the schema, and old ones are never redefined; incompatible changes get a new `apiVersion`.

### Validation

A document is valid only if every check passes. Every error is reported, not just the first.

- **Strict structure.** Every object rejects unknown fields. Values are strictly typed: `"yes"` or `1` is not a boolean, and a number is not a string.
- **Semantic checks.** These go beyond the JSON Schema: rule ids are unique, and `allowed` and `scope.groups` contain no repeats.
- **Bounded size.** At most 64 KiB of UTF-8, one YAML document, and 20 levels of nesting.
- **Safe YAML.** Documents are parsed with a restricted safe loader:
  - Explicit tags are rejected, including `!!python/...`, `!!binary`, `!!str` and custom tags, so YAML can never construct objects.
  - Anchors and aliases are rejected. This also prevents alias-expansion ("billion laughs") attacks.
  - Duplicate keys are rejected instead of silently keeping the last one.
  - `<<` is an ordinary key, so it is reported as an unknown field rather than merged.
  - Dates stay strings.
- **Error messages.** Each error has a `path` and a `message`, for example `spec.rules[1].allowed` with "List should have at least 1 item after validation, not 0".
  - YAML syntax errors use the path `(document)` and give a line number.
  - Messages never repeat input values, so a secret pasted into a policy by mistake is not echoed back.
  - Paths name keys as written.

`backend/tests/test_policy_schema.py` checks that the published schema matches the models in `baluarte.policy.schema` exactly.

## Versions

Policies are stored as immutable versions in the `policy` table. A version's row holds:

- `name`: the document's `metadata.name`
- `version`
- `source_yaml`: exactly as submitted, comments included
- `yaml_digest`: SHA-256 of the exact UTF-8 bytes of the source
- `effective_at`

Saving a document whose name already exists adds the next version (2, 3, ...). Saving source byte-for-byte identical to the latest version returns that version and adds nothing. Reverting to an older text is a new version. Concurrent saves of the same name are serialised, so each gets a distinct version. A database trigger rejects any `UPDATE` of a saved version (migration `0005`).

## API

| Endpoint | Permission | Behaviour |
| --- | --- | --- |
| `POST /api/v1/policies/validate` | `policy:write` + CSRF | Validates `{"source_yaml": "..."}` without saving. 200 with `valid`, `errors`, and, when valid, `name`, `yaml_digest` and `rule_ids`. |
| `POST /api/v1/policies` | `policy:write` + CSRF | Saves the next version. 201 when created, 200 when identical to the latest version, 422 with `detail.errors` when invalid. Creating a version writes a `policy.version_created` audit event with the name, version, digest and `change_id`; the source is not copied into the audit log. While review is required: 202 with the proposed change (`PolicyChangeDetail`) instead, and 409 when a different version of the same policy is already waiting (see [Reviewing baseline changes](#reviewing-baseline-changes)). |
| `GET /api/v1/policies` | `findings:read` | Each policy name with its latest version and version count. |
| `GET /api/v1/policies/{name}/versions?limit=` | `findings:read` | Versions newest first, without the source. Each carries `review`: the change that put it in force. |
| `GET /api/v1/policies/{name}/versions/{version}` | `findings:read` | One version: the exact source, its digest and the parsed document. |

Validation also needs `policy:write`, because it exists to support authoring.

Reading needs `findings:read`, which every role holds. Every finding names the policy and rule that produced it, and an auditor must be able to see the exact baseline version in force. A separate `policy:read` permission would only ever be granted alongside `findings:read`.

## Assignment and inheritance

A saved version applies to nothing until it is assigned. Implemented in `baluarte.policy.assignments` (BAL-018).

### Assignments pin a version

An assignment pins one exact version (`name` and `version`) to a target: the GitLab instance, a group or a project. It never follows "the latest version" of a name, for two reasons:

- Saving a version must not silently change what is enforced across the instance.
- Every evaluation must name the immutable version it was checked against.

Rolling out a new version is a separate, audited assignment.

A target holds at most one version of each policy name. Assigning another version of a name already on the target replaces it. The audit event records the replaced assignment and version. Different policy names on one target add up. The database enforces one version per name and target with a unique constraint. A trigger keeps the stored policy name equal to the version's name.

Only a target that is `active` in the inventory can be assigned to. A missing or inaccessible group or project cannot be confirmed in GitLab, so what the policy would govern is unknown. If a target goes missing later, its assignments stay and are listed with the target's state.

### Resolution

The policies in force for a project come from its *path*, nearest first:

1. the project
2. its namespace group
3. that group's ancestors, up to the top-level group
4. the instance

A personal project has no groups on its path (see below).

A group's path starts at the group itself.

For each policy name assigned anywhere on the path:

1. **Nearest wins.** The assignment nearest the target decides the version. Assignments of the same name farther up are `overridden`. Each level holds one version of a name, so there are no ties.
2. **Then scope.** If the winning version's `spec.scope.groups` does not cover the target, the name is `out_of_scope` and does not apply. Resolution does not fall back to a farther assignment, because that would apply a version that a nearer owner replaced.
3. Every other name is `effective`.

Policies cannot be removed from a subtree. To exempt it, assign a version whose scope excludes it.

### Scope

`spec.scope.groups` is checked against the target's namespace path. For a project, that is its `path_with_namespace` without the last segment. For a group, it is its own full path.

- `"*"` covers every target the version is assigned to.
- A group path covers that group and its subgroups, on segment boundaries: `platform` covers `platform/backend`, but `platform/back` does not.
- Paths compare case-insensitively, as GitLab paths are unique regardless of case.

An assignment decides *where* a version could apply, and its scope narrows that. A version with `groups: ["*"]` assigned to the instance applies to everything.

### Incomplete ancestry

Resolution follows `parent_gitlab_id` through the group inventory. Some steps cannot be confirmed. When that happens, `ancestry` is `incomplete` and `gaps` names each group and the reason:

| Reason | Meaning | Walk |
| --- | --- | --- |
| `not_in_inventory` | The namespace or parent group has not been discovered, or the project's namespace kind is unknown and no group has its ID (below). | Stops; the instance is still included |
| `missing`, `inaccessible` | The group is in the inventory but was not confirmed by the last discovery. | Continues with its last known parent |
| `cycle`, `too_deep` | The recorded parents loop, or exceed GitLab's 20 levels of subgroups. | Stops |

What can be resolved is still returned. However, an incomplete result may lack policies assigned above a gap, and it may lack a nearer version that would override one listed. Evaluation must not treat a project with incomplete ancestry as fully governed. Any rule result that depends on the missing part is `unknown`, never compliant.

A project's or group's own `missing` or `inaccessible` state is reported in `target.state`, not as a gap.

### Personal projects

A personal project's namespace is a user, not a group. Project discovery records the namespace kind (`gitlab_project.namespace_kind`), and a project in a `user` namespace resolves with a complete ancestry: its path is the project, then the instance. Only `"*"` scopes cover it in practice, because no group shares a username's path.

A null `namespace_kind` means the kind is unknown: the project was discovered before kinds were recorded, or GitLab returned a kind Baluarte does not recognise. Such a project is not assumed to be personal. Its namespace is looked up as a group, and if no group has that ID it is a `not_in_inventory` gap, so the ancestry is `incomplete` until the next project discovery records the kind.

### API

| Endpoint | Permission | Behaviour |
| --- | --- | --- |
| `POST /api/v1/policy-assignments` | `policy:write` + CSRF | Body: `policy_name`, `version`, `target_type` (`instance`, `group`, `project`) and `target_id` (the GitLab ID; omitted for the instance).<br>Responses:<ul><li>201 when assigned, with a `policy.assigned` audit event.</li><li>202 with the proposed change while review is required; 409 when another change to that policy at that target is waiting.</li><li>200 when already assigned.</li><li>404 for an unknown version, or a target not in the inventory.</li><li>409 when GitLab is not configured, or the target is not `active`.</li></ul> |
| `DELETE /api/v1/policy-assignments/{id}` | `policy:write` + CSRF | 204, with a `policy.unassigned` audit event. 202 with the proposed change while review is required. 404 if there is no such assignment. |
| `GET /api/v1/policy-assignments` | `findings:read` | Assignments in ID order, with version, digest, the target's path and state, and `review`. Filters: `policy_name`, `target_type`, `target_id`. Paged with `after_id` and `limit`. |
| `GET /api/v1/gitlab/projects/{gitlab_id}/effective-policies` | `findings:read` | Returns:<ul><li>`target` and `path`</li><li>`ancestry` and `gaps`</li><li>`policies`: the effective ones</li><li>`considered`: every assignment on the path with its outcome</li><li>`not_evaluated`: `"archived"` when evaluation skips the project, otherwise `null` (always `null` for groups)</li></ul>404 if the project is not in the inventory. |
| `GET /api/v1/gitlab/groups/{gitlab_id}/effective-policies` | `findings:read` | The same, for a group. |
| `GET /api/v1/policy-assignments/tree` | `findings:read` | Every group, and every project with an assignment of its own, with what it has in effect, in one read (see [The assignment tree](#the-assignment-tree)). 404 when GitLab is not configured or nothing is collected yet. |

#### The assignment tree

The tree applies the same rules as the effective-policies reads to data read once, so its entry for any group or project equals that target's own read (`test_tree_matches_every_effective_policies_read`). The web UI's "Where baselines apply" view is built from it.

```text
AssignmentTree {
  instance: Target                      # target_type "instance"
  instance_assignments: [TreeAssignment]
  groups:   [TreeNode]                  # every group, ordered by path
  projects: [TreeProject]               # only projects with their own assignment
}
TreeAssignment { assignment_id, policy_id, name, version, yaml_digest }
TreeNode {
  target: Target                        # { target_type, target_id (GitLab ID), path, state }
  parent_gitlab_id: int | null          # a group's parent; a project's namespace group,
                                        # null for a personal project
  ancestry: "complete" | "incomplete"
  gaps: [{ gitlab_id, reason }]         # not_in_inventory, missing, inaccessible, cycle, too_deep
  assignments: [TreeAssignment]         # made to this target itself
  in_effect: [TreeEffect]               # one per policy name assigned on its path
}
TreeProject = TreeNode + { namespace_gitlab_id, namespace_kind }
TreeEffect {
  name, assignment_id, policy_id, version, yaml_digest
  status: "set_here" | "inherited" | "out_of_scope"
  assigned_to: Target                   # where the deciding assignment is
  replaces: [Resolved]                  # farther assignments of the same name it overrides
}
```

- **`status`:** `set_here` and `inherited` apply. `out_of_scope` means the deciding version's scope excludes the target, which does not fall back to a farther assignment.
- **Projects without their own assignment** are not listed: they have what their namespace group has, with the scope judged on that same namespace path. A personal project without its own assignment has only what the instance has.

Audit events record the policy name, version, digest, target type, target ID and target path.

## Reviewing baseline changes

Implemented in `baluarte.policy.changes`. Changing a baseline or where it applies changes what every project is judged against, and removing a rule resolves its findings as `no_longer_applies`, so these changes can require a second person.

- **Setting:** `PUT /api/v1/settings/governance` `{"require_review": true}` (`settings:manage`, audited as `settings.governance_updated`). Off by default, so a single-user install is not locked out; the Governance views warn while it is off.
- **Every change is recorded** in `policy_change`: a new version, an assignment or an unassignment, with who proposed it, who decided it and when.
- **Review required:** the change is `proposed` and has no effect. `policy` and `policy_assignment`, which evaluation reads, hold only approved changes. Someone with `policy:write` other than the proposer approves it (optionally with a note), which applies it as if it had just been made and queues an evaluation for an assignment change, or rejects it with a note. The API refuses the proposer (403) and a CHECK constraint enforces it again. Approval fails with 409 when the change can no longer be applied (its target is no longer active, or the assignment is gone); reject it instead.
- **Review not required:** the change is applied at once and recorded `approved` with `self_approved` true and the proposer as approver, so the record shows no one else reviewed it.
- **Audit:** `policy.change_proposed`, `policy.change_approved`, `policy.change_rejected`, and on approval the usual `policy.version_created`, `policy.assigned` or `policy.unassigned` with the approver as actor and `change_id` and `proposed_by` in the details.
- **One at a time:** one proposed version per policy name, and one proposed assignment change per target and policy name; proposing the same change again returns the waiting one.

| Endpoint | Permission | Behaviour |
| --- | --- | --- |
| `GET /api/v1/policy-changes` | `findings:read` | `{review_required, items: [PolicyChange], next_before_id}`, newest first. Filters: `state`, `kind`, `policy_name`; paged with `before_id` and `limit` (1–200, default 50). |
| `GET /api/v1/policy-changes/{id}` | `findings:read` | `PolicyChangeDetail`: the change, plus `source_yaml` and `base_source_yaml` (the version in force when it was proposed) for a version change. |
| `POST /api/v1/policy-changes/{id}/approve` | `policy:write` + CSRF | Body `{note?}`. 403 for the proposer, 409 when already decided or no longer applicable. |
| `POST /api/v1/policy-changes/{id}/reject` | `policy:write` + CSRF | Body `{note}` (required). 403 for the proposer, 409 when already decided. |

```text
PolicyChange {
  id, kind: "version" | "assign" | "unassign", state: "proposed" | "approved" | "rejected",
  policy_name,
  version: int | null          # version: what it became once approved; otherwise the version pinned
  yaml_digest, base_version    # version changes
  target_type, target_id, target_path, assignment_id   # assignment changes
  review_required, self_approved, migrated: bool
  proposed_by: Person, proposed_at, decided_by: Person | null, decided_at, decision_note
}
Person { actor: "user:<id>" | "system:..." | "unknown", user_id: int | null, display_name }
Review { change_id, reviewed, self_approved, migrated, proposed_by, decided_by, decided_at }
```

`review` on versions and assignments is the approved change that put them in force; `reviewed` is true only when review was required and someone else approved it.

## Not yet defined

- Exceptions for a group subtree or a whole policy (see [Exceptions](#exceptions)).
- How `effective_at` is scheduled. Today a version takes effect when it is saved.

## Outcomes

`pass`, `fail`, `unknown`, `unsupported`, `excepted`.

- `unknown` covers insufficient permissions, stale data and failed collection. It never counts as compliant.
- `unsupported` means the GitLab feature is unavailable.
- `excepted` means a `fail` waived by an approved, unexpired exception. See [Exceptions](#exceptions).

## Exceptions

Implemented in `baluarte.policy.exceptions` (BAL-033). An exception waives one rule (`policy_name`, `rule_id`) for one project, for a limited time:

- **Request:** `exception:request` (admin, analyst) with a justification and `expires_at`, at most 365 days ahead. The rule must exist in the policy's latest version, and only one exception per project and rule may be requested or approved at a time.
- **Separation of duties:** approving or rejecting needs `exception:approve` (admin) and must be done by someone other than the requester. The API refuses it (403) and a CHECK constraint enforces it again in the database.
- **Lifecycle:** `requested` -> `approved` or `rejected`; `requested` or `approved` -> `revoked` (by an approver, with a reason) or `expired` (once `expires_at` has passed, database clock). A trigger rejects any other change, any edit of what was requested, and `DELETE`. Every transition is audited (`policy_exception.requested`, `.approved`, `.rejected`, `.revoked`, `.expired`).
- **Effect:** while an exception is approved and unexpired, a backed `fail` for its project and rule is stored as `excepted` with the exception ID; the expected and observed values and the evidence are kept, and the finding's state is `excepted`. `unknown` and `unsupported` are never excepted: an exception waives a known failure, not missing or untrustworthy evidence.
- **Ending:** revoking reopens the exception's findings at once. Expiry takes effect at `expires_at` in every API response (an expired exception reads `expired` and its finding `open`), and each evaluation run first marks due exceptions `expired` and reopens their findings before judging anything. A run is queued after every approval, rejection and revocation.
- **Not yet:** exceptions for a group subtree or a whole policy, renewal (request a new exception instead), and exceptions in evidence exports.

## Evaluation invariants

Evaluations reference immutable policy versions and observation IDs ([EVALUATION.md](EVALUATION.md) describes the result model). For each rule, an evaluation records:

- expected value
- observed value, if known
- source
- timestamp
- reason
- GitLab capability status

v0.1 does not auto-remediate. Dry-run remediation plans, built from open failing findings and approved by someone other than their creator, are described in [REMEDIATION.md](REMEDIATION.md); they change nothing in GitLab.
