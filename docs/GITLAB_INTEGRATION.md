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

## Reliability

Handle pagination, rate limits, 401/403, 404, 429, 5xx, timeouts, network partitions, and deleted resources. Do not conflate inaccessible with deleted. Reconcile periodically; system hooks/webhooks are optional accelerators, not the only source of truth. Verify signatures/secrets on any incoming hooks.

## Compatibility

GitLab version, license tier, and token privileges determine endpoint behavior. Create a tested capability matrix from actual responses. Avoid assuming that approval, security scanning, or compliance APIs are available in all tiers. Prefer feature detection over version-only assumptions.

## Data freshness

Every resource and finding must show `last_collected_at`, status, and collection errors. A failed scan cannot silently preserve a green dashboard.

## Initial rules

Visibility and default-branch protection are the first two rules. Approval rules, push restrictions, security scans, and deployments require separate endpoint/tier investigations before committing to implementation.
