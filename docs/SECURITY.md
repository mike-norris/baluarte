# Security architecture and threat model

## Assets and trust boundaries

Protect GitLab API tokens, OIDC sessions, organization policies, inventory, audit evidence, exports, and database backups. Boundaries: browser/API, API/database, worker/GitLab, deployment/secret store, export recipient.

## Minimum controls

- OIDC authorization-code flow with PKCE where applicable; validate issuer, audience, signatures, expiration; CSRF-safe session handling.
- Server-side RBAC: admin, analyst, auditor. Deny by default.
- Read-only GitLab token for v0.1; minimize scopes and document required permissions.
- TLS to GitLab and web clients; no disabled certificate verification.
- Secrets through mounted files or external secret systems; never in repository, logs, or exports.
- API rate limiting, input validation, request timeouts, pagination bounds, retry/backoff.
- Safe export authorization, content-disposition, output escaping, no secret-bearing raw responses.
- Audit logs for login, policy changes, export generation, and administrative actions.
- Signed/versioned container release process and dependency scanning.

## Threats to test

Credential theft, privilege escalation, forged webhook payloads, stale inventory reported as green, manipulated evidence, unauthorized export, GitLab API outages, cross-project data leaks, job duplication, malicious YAML, and compromised dependencies.

## Security posture caveat

A SHA-256 hash detects changes only if the reference digest is protected. Stronger assurances require append-only storage, signed manifests, or independently anchored digests. Define evidence retention and backup controls before production use.
