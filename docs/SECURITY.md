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

## Authentication and authorization

Implemented as described in [ADR-0006](architecture/ADR-0006-server-side-oidc-sessions.md): the API completes the OIDC authorization code flow with PKCE and gives the browser only an opaque `HttpOnly` session cookie. Sessions are stored as SHA-256 digests with idle (default 60 minutes) and absolute (default 8 hours) expiry. State-changing requests require the `X-CSRF-Token` header returned by `GET /api/v1/auth/me`. Every route requires a session except health, readiness, `/auth/login` and `/auth/callback`.

Roles come from the identity provider's groups claim (`BALUARTE_OIDC_*_GROUPS`); a user in no mapped group cannot sign in. Endpoints check permissions:

| Permission | admin | analyst | auditor |
|---|---|---|---|
| `inventory:read`, `findings:read`, `evidence:read` | yes | yes | yes |
| `policy:write` | yes | yes | no |
| `evidence:export` | yes | no | yes |
| `audit_log:read` | yes | no | yes |
| `settings:manage` | yes | no | no |
| `jobs:read` | yes | no | no |

Sign-in, failed sign-in (with a reason code) and sign-out are written to `audit_event`. Callbacks that do not match a sign-in started by the same browser are logged but not audited, so anonymous requests cannot grow the append-only audit table. Rate limiting of the sign-in endpoints is not built yet; apply it at the ingress.

## Secrets and logs

- Secrets are read only from files named by `<NAME>_FILE` (Docker or Kubernetes secret mounts). Setting a secret directly in the environment, or a password inside `BALUARTE_DATABASE_URL`, is a configuration error. Secret values are never returned by the API: `GET /api/v1/settings` reports them only as `configured` or `not_set`.
- All processes log one JSON object per line to stdout (`BALUARTE_LOG_FORMAT=text` for development). Uvicorn's loggers are routed through the same handler.
- Every log line is redacted after formatting: values of secrets read from files, credentials in URLs, `Bearer`/`Basic` credentials, GitLab tokens (`glpat-` and other `gl*-` prefixes), JWTs, and the values of sensitive parameters (`private_token`, `access_token`, `token`, `password`, `secret`, `client_secret`, `code`, `state`, ...). This covers the authorization code and state in the OIDC callback's access-log line. Redaction is pattern-based defence in depth; code must still avoid logging secrets.
- Background job errors store only messages written for storage; unexpected exceptions are recorded by type name.

## Audit trail

`audit_event` rows are append-only (database triggers) and carry a SHA-256 digest of their canonical details. Details are redacted before hashing. Each committed event is also written to the `baluarte.audit` logger as a JSON line with an `audit` field (id, actor, action, resource, details, digest), so a log collector can keep an independent copy; rolled-back actions are not logged. Admins and auditors read the trail through `GET /api/v1/audit-events`.

## Threats to test

Credential theft, privilege escalation, forged webhook payloads, stale inventory reported as green, manipulated evidence, unauthorized export, GitLab API outages, cross-project data leaks, job duplication, malicious YAML, and compromised dependencies.

## Security posture caveat

A SHA-256 hash detects changes only if the reference digest is protected. Stronger assurances require append-only storage, signed manifests, or independently anchored digests. Define evidence retention and backup controls before production use.
