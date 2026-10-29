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

Status: the format, its validation and versioned storage are implemented (BAL-017) in `backend/src/baluarte/policy/`. Evaluating rules against inventory is not implemented yet. Neither is assigning policies to groups and projects or inheriting them (see [Not yet defined](#not-yet-defined)).

### Fields

| Path | Type and limits |
| --- | --- |
| `apiVersion` | Exactly `baluarte.io/v1alpha1`. |
| `kind` | Exactly `GitLabBaseline`. |
| `metadata.name` | 1 to 63 lowercase letters, digits and hyphens, starting and ending with a letter or digit. This names the policy. |
| `metadata.description` | Optional string, up to 1000 characters. |
| `spec.scope.groups` | 1 to 200 distinct entries. Each is `"*"` (every group) or a GitLab group full path such as `platform/backend`. |
| `spec.rules` | 1 to 100 rules. Rule `id`s are unique and follow the same pattern as `metadata.name`. |
| `spec.rules[].severity` | Optional: `low`, `medium` (default), `high` or `critical`. |
| `spec.rules[].description` | Optional string, up to 1000 characters. |

### Rule types

| `type` | Fields | Passes when |
| --- | --- | --- |
| `gitlab.project.visibility` | `allowed`: 1 to 3 distinct values from `private`, `internal`, `public` | The project's visibility is in `allowed`. |
| `gitlab.branch.default_protected` | `required`: boolean | With `required: true`, the project's default branch is protected. |

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

## Not yet defined

These are deferred to BAL-018 and need explicit tests and documentation before they are implemented:

- Assigning policies to groups and projects (`policy_assignment`).
- How `spec.scope.groups` matches subgroups.
- Inheritance and override precedence.
- How `effective_at` is scheduled. Today a version takes effect when it is saved.

Until then, a saved policy is stored and readable but not applied to anything.

## Outcomes

`pass`, `fail`, `unknown`, `unsupported`, `excepted`.

- `unknown` covers insufficient permissions, stale data and failed collection. It never counts as compliant.
- `unsupported` means the GitLab feature is unavailable.
- An exception requires approval and expiration metadata.

## Evaluation invariants

Evaluations reference immutable policy versions and observation IDs. For each rule, an evaluation records:

- expected value
- observed value, if known
- source
- timestamp
- reason
- GitLab capability status

v0.1 does not auto-remediate.
