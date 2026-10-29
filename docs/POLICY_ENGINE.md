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
| `POST /api/v1/policies` | `policy:write` + CSRF | Saves the next version. 201 when created, 200 when identical to the latest version, 422 with `detail.errors` when invalid. Creating a version writes a `policy.version_created` audit event with the name, version and digest; the source is not copied into the audit log. |
| `GET /api/v1/policies` | `findings:read` | Each policy name with its latest version and version count. |
| `GET /api/v1/policies/{name}/versions?limit=` | `findings:read` | Versions newest first, without the source. |
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
| `POST /api/v1/policy-assignments` | `policy:write` + CSRF | Body: `policy_name`, `version`, `target_type` (`instance`, `group`, `project`) and `target_id` (the GitLab ID; omitted for the instance).<br>Responses:<ul><li>201 when assigned, with a `policy.assigned` audit event.</li><li>200 when already assigned.</li><li>404 for an unknown version, or a target not in the inventory.</li><li>409 when GitLab is not configured, or the target is not `active`.</li></ul> |
| `DELETE /api/v1/policy-assignments/{id}` | `policy:write` + CSRF | 204, with a `policy.unassigned` audit event. 404 if there is no such assignment. |
| `GET /api/v1/policy-assignments` | `findings:read` | Assignments in ID order, with version, digest, and the target's path and state. Filters: `policy_name`, `target_type`, `target_id`. Paged with `after_id` and `limit`. |
| `GET /api/v1/gitlab/projects/{gitlab_id}/effective-policies` | `findings:read` | Returns:<ul><li>`target` and `path`</li><li>`ancestry` and `gaps`</li><li>`policies`: the effective ones</li><li>`considered`: every assignment on the path with its outcome</li></ul>404 if the project is not in the inventory. |
| `GET /api/v1/gitlab/groups/{gitlab_id}/effective-policies` | `findings:read` | The same, for a group. |

Audit events record the policy name, version, digest, target type, target ID and target path.

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

v0.1 does not auto-remediate.
