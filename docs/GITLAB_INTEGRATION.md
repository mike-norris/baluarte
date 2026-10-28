# GitLab integration contract

## Initial scope

Self-managed GitLab REST API, one instance, read-only credential, group/subgroup/project enumeration, default-branch protections, project visibility, and capability detection.

## Connection

Configured with `BALUARTE_GITLAB_URL` (https, no `/api/v4`; relative-URL installs such as `https://example.internal/gitlab` work), `BALUARTE_GITLAB_TOKEN_FILE` and optionally `BALUARTE_GITLAB_TIMEOUT_SECONDS` (default 15). The client lives in `baluarte.integrations.gitlab`.

Requests are retried up to three attempts on 429, 502, 503, 504, timeouts and connection errors, with exponential backoff and `Retry-After` honoured up to 30 seconds. 401, 403 and 404 are reported as `unauthorized`, `forbidden` and `not_found` and never retried; TLS failures are `tls_error`.

## Diagnostics

`POST /api/v1/gitlab/diagnostics` runs these checks in order and returns each as `ok`, `warning`, `failed` or `unknown`, with an overall `status` that is `ok` only when every check is:

| Check | Endpoint | Outcome |
|---|---|---|
| `connectivity` | `GET /version` | Version reported; failure here marks the other checks `unknown` |
| `token` | `GET /personal_access_tokens/self` | Scopes, active/revoked and expiry. `unknown` on 404 (before GitLab 15.5, or a token type this endpoint does not describe) |
| `user` | `GET /user` | Blocked user fails; administrator warns |
| `group_visibility` | `GET /groups?per_page=1&all_available=true` | No visible groups warns (inventory would be empty) |

These behaviours are taken from GitLab's API documentation and exercised only against scripted responses in `tests/gitlab_fake.py`. They have not yet been verified against a live instance; the capability matrix will record results from real versions and tiers.

## Capability detection

The `gitlab.detect_capabilities` worker job (queued by `POST /api/v1/gitlab/capabilities/refresh`) records what GitLab actually returns for the configured token, rather than inferring support from the version number. Results are stored on `gitlab_instance` and shown by `GET /api/v1/gitlab/instance`.

| Probe | Request | Core | What it shows |
|---|---|---|---|
| `version` | `GET /version` | yes | Version, and edition from the `enterprise` flag or an `-ee` suffix. A plain version string leaves the edition unknown rather than assuming CE. If this fails, nothing else runs and the snapshot is `failed` |
| `groups.list`, `groups.visibility` | `GET /groups?per_page=1` | yes | Groups can be listed and carry `visibility` |
| `projects.list`, `projects.visibility`, `projects.default_branch` | `GET /projects?per_page=1` | yes | Projects can be listed and carry `visibility` and `default_branch` |
| `protected_branches.read` | `GET /projects/:id/protected_branches` | yes | Branch protections are readable for a visible project |
| `merge_request_approvals.read` | `GET /projects/:id/approvals` | no | Recorded for the approval-rule investigation |
| `license.read` | `GET /license` | no | Tier (`plan`). Administrator-only, so `forbidden` is expected for a least-privilege token and the tier is then unknown |

Each probe is `supported`, `forbidden` (401/403), `not_found` (404), `error` (rate limit, 5xx, timeout, missing field) or `skipped` (no visible project or group to probe). GitLab also answers 404 for resources hidden from the caller, so `not_found` alone does not prove a tier lacks a feature. The snapshot is `ok` only when every core probe is `supported`, otherwise `partial`. Rules must treat anything other than `supported` as unable to evaluate, never as compliant.

A failed detection replaces the stored snapshot, so an earlier `ok` is never shown after a failure; the last known version and edition are kept. The job retries a failure up to three attempts. Snapshots older than 24 hours are reported as `stale`. Periodic detection will be scheduled with inventory collection; until then it runs on request.

The probes have been run only against the scripted responses in `tests/gitlab_fake.py`. The capability matrix of real GitLab versions and tiers is still to be filled in from live instances.

## Reliability

Handle pagination, rate limits, 401/403, 404, 429, 5xx, timeouts, network partitions, and deleted resources. Do not conflate inaccessible with deleted. Reconcile periodically; system hooks/webhooks are optional accelerators, not the only source of truth. Verify signatures/secrets on any incoming hooks.

## Compatibility

GitLab version, license tier, and token privileges determine endpoint behavior. Create a tested capability matrix from actual responses. Avoid assuming that approval, security scanning, or compliance APIs are available in all tiers. Prefer feature detection over version-only assumptions.

## Data freshness

Every resource and finding must show `last_collected_at`, status, and collection errors. A failed scan cannot silently preserve a green dashboard.

## Initial rules

Visibility and default-branch protection are the first two rules. Approval rules, push restrictions, security scans, and deployments require separate endpoint/tier investigations before committing to implementation.
