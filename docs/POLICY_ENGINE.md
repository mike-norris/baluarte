# Policy engine specification

## Policy format

Versioned YAML validated against a published JSON schema. No executable Python, Jinja evaluation, or arbitrary shell commands in policy files.

```yaml
apiVersion: baluarte.io/v1alpha1
kind: GitLabBaseline
metadata:
  name: corporate-default
spec:
  scope:
    groups: ["*"]
  rules:
    - id: project-visibility
      type: gitlab.project.visibility
      allowed: [private]
    - id: protected-default-branch
      type: gitlab.branch.default_protected
      required: true
```

This is a **proposed** schema, not yet implemented. Exact group matching, inheritance, and override rules require explicit tests and documentation.

## Outcomes

`pass`, `fail`, `unknown`, `unsupported`, `excepted`. Unknown includes insufficient permissions, stale data, and failed collection; unsupported indicates unavailable GitLab feature; exceptions require approval and expiration metadata.

## Evaluation invariants

Evaluations reference immutable policy versions and observation IDs. A rule must provide expected value, observed value (if known), source, timestamp, reason, and GitLab capability status. Do not auto-remediate in v0.1.
