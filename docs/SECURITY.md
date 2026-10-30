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
| `exception:request` | yes | yes | no |
| `exception:approve` (approve, reject, revoke; never one's own request) | yes | no | no |
| `remediation:plan` (create and submit dry-run remediation plans) | yes | yes | no |
| `remediation:approve` (approve or reject a plan; never one's own) | yes | no | no |
| `evidence:export` | yes | no | yes |
| `audit_log:read` | yes | no | yes |
| `settings:manage` (includes running GitLab diagnostics) | yes | no | no |
| `jobs:read` | yes | no | no |

Exceptions separate duties: the user who requests one cannot approve or reject it, which the API and a database constraint both enforce. With the default role mapping only admins approve, so a deployment that wants two people involved needs at least two admins, or an analyst requesting and an admin approving.

Sign-in, failed sign-in (with a reason code) and sign-out are written to `audit_event`. Callbacks that do not match a sign-in started by the same browser are logged but not audited, so anonymous requests cannot grow the append-only audit table. Rate limiting of the sign-in endpoints is not built yet; apply it at the ingress.

## Secrets and logs

- Secrets set by the deployment are read only from files named by `<NAME>_FILE` (Docker or Kubernetes secret mounts). Setting a secret directly in the environment, or a password inside `BALUARTE_DATABASE_URL`, is a configuration error. Secret values are never returned by the API: `GET /api/v1/settings` reports them only as `configured` or `not_set`.
- The GitLab token and OIDC client secret can instead be entered in the web UI. See the next section.
- All processes log one JSON object per line to stdout (`BALUARTE_LOG_FORMAT=text` for development). Uvicorn's loggers are routed through the same handler.
- Every log line is redacted after formatting: values of secrets read from files, credentials in URLs, `Bearer`/`Basic` credentials, GitLab tokens (`glpat-` and other `gl*-` prefixes), JWTs, and the values of sensitive parameters (`private_token`, `access_token`, `token`, `password`, `secret`, `client_secret`, `code`, `state`, ...). This covers the authorization code and state in the OIDC callback's access-log line. Redaction is pattern-based defence in depth; code must still avoid logging secrets.
- Background job errors store only messages written for storage; unexpected exceptions are recorded by type name.

## Setup and secrets entered in the web UI

[ADR-0008](architecture/ADR-0008-settings-in-the-database.md) has the reasoning.

- **Setup code.**
  - Until setup completes, each API start logs a new one-time code at WARNING (`setup: ... setup code XXXX-XXXX-XXXX`). `baluarte-setup-code`, run in the API or worker container, issues a new one and ends every setup session.
  - Only its SHA-256 digest is stored. Five wrong codes within 15 minutes return 429 until the window ends. Wrong codes are logged, not audited, so anonymous requests cannot grow the audit table.
  - The code unlocks a 60-minute setup session (cookie and CSRF token, as for sign-in) that can save and test the identity provider and GitLab settings.
- **Completion.** Setup completes when the first user who maps to `admin` signs in through the settings entered in the wizard. After that every setup endpoint answers 409, and changes need `settings:manage` on the Settings page (`/api/v1/settings/oidc`, `/api/v1/settings/gitlab`, CSRF-protected).
- **Precedence.** When the environment configures sign-in or GitLab, that section comes only from the environment and the UI cannot change it (409). When sign-in comes from the environment there is no setup step and no setup code.
- **Sealing.**
  - Stored secrets are sealed with AES-256-GCM under the key in `BALUARTE_SECRET_KEY_FILE` (32 random bytes, base64: `openssl rand -base64 32`), with a random nonce and the secret's name as associated data. They are never returned, and audit events record only that a secret was replaced or cleared.
  - Without the key, secrets cannot be saved. With a different key, they cannot be read, and sign-in or collection reports a configuration error.
  - Back up the key separately from the database: a database backup alone does not expose the secrets, and the key alone is useless.
- **Rotating the key.** Write a new key to `BALUARTE_SECRET_KEY_FILE`, the old one to `BALUARTE_SECRET_KEY_PREVIOUS_FILE` (read only to decrypt), restart, run `baluarte-reencrypt-secrets`, then remove the previous key. If the key is lost, enter the GitLab token and client secret again on the Settings page; nothing else is affected.
- **Where stored secrets go.** A stored secret is only ever sent to the host it was entered for. Changing the issuer or GitLab URL requires entering it again, and "test with the stored token" works only for the stored URL.
- **Outbound requests.** The discovery and connection tests make the API send `GET` requests to the URL entered, https only, never following redirects.
- **Risk while setup is open.** Anyone who can read the API log or run a command in its container can complete setup. Complete it promptly after installing, and keep API logs private.

## GitLab credential

- One token for one instance (ADR-0004), read from `BALUARTE_GITLAB_TOKEN_FILE` or entered in the web UI (sealed, see above). Baluarte only issues `GET` requests and needs only the `read_api` scope. Use a dedicated non-admin bot or auditor user; an administrator token sees everything and is flagged by diagnostics.
- The token travels only in the `PRIVATE-TOKEN` header to `BALUARTE_GITLAB_URL`, which must be `https`. Redirects are never followed, and pagination refuses `Link` next-page URLs outside the configured API, so neither can forward the token to another host. TLS verification is always on; a private CA is added with `SSL_CERT_FILE`.
- Error messages stored or returned for GitLab failures contain the status and endpoint path only, never response bodies, query strings or the token.
- The token is mounted into the API (diagnostics) and the worker (capability detection and collection) only. Capability detection calls `GET /license`, which is administrator-only; a least-privilege token gets 403 there by design and the tier is reported as unknown.
- Diagnostics (`POST /api/v1/gitlab/diagnostics`, admin, CSRF-protected) warn when the token has write scopes (`api`, `write_repository`, ...), belongs to an administrator, or expires within 30 days, and fail when it is revoked, expired, lacks `read_api`, or its user is blocked. Checks that cannot run are `unknown`, never `ok`. Each run is audited (`gitlab.diagnostics_run`).

## Remediation plans

Dry-run plans (BAL-031, [REMEDIATION.md](REMEDIATION.md)) describe GitLab changes that would fix open findings; **creating, submitting or approving one changes nothing in GitLab**. Baluarte still sends only `GET` requests with the `read_api` token, and no write credential exists. Tests run every planning path with network requests forbidden, and check that the GitLab client can only send `GET`.

When execution is built (BAL-032), it must use a separate write credential: its own secret file and setting, mounted only into the executing worker, and absent unless remediation is enabled ([ADR-0007](architecture/ADR-0007-remediation-constraints.md)). The read token must never gain write scopes. On GitLab Free, GitLab keeps no audit record of such changes, so Baluarte's audit trail would be the only one.

Plans separate duties like exceptions: the creator cannot approve or reject their own plan (API and database constraint). Approval names the plan digest the approver reviewed and is refused when the plan is stale.

## Audit trail

`audit_event` rows are append-only (database triggers) and carry a SHA-256 digest of their canonical details. Details are redacted before hashing. Each committed event is also written to the `baluarte.audit` logger as a JSON line with an `audit` field (id, actor, action, resource, details, digest), so a log collector can keep an independent copy; rolled-back actions are not logged. Admins and auditors read the trail through `GET /api/v1/audit-events`.

## Backups

Database backups contain inventory, evidence and the audit trail but no GitLab token or OIDC secret, and session cookies only as digests. Encrypt them, store a copy off the database host, restrict restore access like database access, and revoke all sessions after restoring to an earlier point. Procedures, verification and the current retention gaps are in [OPERATIONS.md](OPERATIONS.md); what the audit trail can and cannot prove is in [AUDIT_EVIDENCE.md](AUDIT_EVIDENCE.md#audit-limitations).

## Threats to test

Credential theft, privilege escalation, forged webhook payloads, stale inventory reported as green, manipulated evidence, unauthorized export, GitLab API outages, cross-project data leaks, job duplication, malicious YAML, and compromised dependencies.

## Security posture caveat

A SHA-256 hash detects changes only if the reference digest is protected. Stronger assurances require append-only storage, signed manifests, or independently anchored digests. Define evidence retention and backup controls before production use.
