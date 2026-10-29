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

## Inventory discovery

Groups and projects are discovered by worker jobs, queued by `POST /api/v1/gitlab/groups/discover` and `POST /api/v1/gitlab/projects/discover`:

| Job | Listing | Notes |
|---|---|---|
| `gitlab.discover_groups` | `GET /groups?all_available=true&order_by=id&sort=asc` | Every group the token can see, nested subgroups included. `parent_gitlab_id` records the hierarchy. A subgroup can be visible while its parent is not, so a parent ID may have no row |
| `gitlab.discover_projects` | `GET /projects?pagination=keyset&order_by=id&sort=asc` | Every project the token can see, including personal projects (`namespace_kind` is `user` and `namespace_gitlab_id` is the user namespace; an unrecognised kind is stored as null) and archived projects (`archived` is true). Keyset pagination is used because GitLab limits offset pagination of `/projects` on large instances. `default_branch` is null for an empty repository |

Both jobs share the following behaviour:

- **Pagination:** list endpoints are read with `per_page=100`, following the `Link: rel="next"` header until it is absent. GitLab sends that header for both offset and keyset pagination. A next link outside the configured API URL is refused, so the token is never sent to another host. A link that repeats, or more than 100,000 pages, is an error.
- **Storage:** each page is upserted on `(instance_id, gitlab_id)` and committed on its own. A renamed or moved resource keeps its row and `first_seen_at`; `last_seen_at` is the last time a listing returned it. Unrecognised visibility values are stored as null, not guessed.
- **Reconciliation:** this runs only after the listing completes. Each previously `active` resource the listing did not return is looked up with `GET /groups/:id` or `GET /projects/:id`:
  - 200: stays `active`.
  - 401 or 403: `inaccessible` (it exists, but the token may not read it).
  - 404: `missing`. GitLab answers 404 both for deleted resources and for resources hidden from the token, so `missing` does not prove deletion.
  - Any other failure leaves the row unchanged.

  A resource that is listed again becomes `active`.
- **Evidence:** each listed or confirmed resource, and each one that could not be read, is recorded as an observation with an allowlisted payload and its SHA-256 digest, written only when it changed ([AUDIT_EVIDENCE.md](AUDIT_EVIDENCE.md#observations)).
- **Failures:** if the listing fails part way, pages already stored are kept, nothing is marked `missing` or `inaccessible`, and the job retries (up to five attempts). The job error holds the safe client message, for example `project discovery failed: GitLab rejected the token (401)`.

Results are read with `GET /api/v1/gitlab/groups` and `GET /api/v1/gitlab/projects`.

### Default-branch protection

After a complete project listing and reconciliation, project discovery reads the protected branches of every `active` project that has a default branch, for the `gitlab.branch.default_protected` rule ([EVALUATION.md](EVALUATION.md#gitlabbranchdefault_protected)):

- **Request:** `GET /projects/:id/protected_branches`, every page, with the same read-only token (`read_api`). One request per project per discovery, usually a single page. Large instances should expect one extra call per project on top of the listing, subject to the client's retries and GitLab's rate limits. Empty repositories and projects that are `missing` or `inaccessible` are not read.
- **Why the list:** `GET /projects/:id/protected_branches/:name` is not used. Whether it resolves wildcard rules for a concrete branch name is not documented (open question 4 in [REMEDIATION_RESEARCH.md](REMEDIATION_RESEARCH.md)), and missing a `*` or `release/*` rule would report a protected branch as unprotected.
- **Matching:** a rule covers the default branch when its name equals the branch, or when it contains `*` and matches with `*` standing for any run of characters, `/` included. This mirrors GitLab's wildcard protected branches. It has not been compared with a live instance.
- **Evidence:** each project gets an observation with source `GET /projects/:id/protected_branches`:
  - **Read:** status `ok` with `branch`, `protected` and the matching rules. For each rule only its name, the push, merge and unprotect access levels (as `kind`: role, user, group or deploy key, and `access_level`), `allow_force_push` and `code_owner_approval_required` are kept. User, group and deploy key names and IDs are not stored.
  - **Not read:** `forbidden` (401, 403), `not_found` (404) or `failed` (anything else), with no payload.

  It is compared only with the project's earlier protection observations, so it is written only when it changes.
- **Failures:** a read that fails is recorded for that project and does not stop discovery. If GitLab rejects the token (401), or 20 reads in a row fail, the remaining projects are recorded as `failed` ("not read: ...") without calling GitLab. A stale protection observation therefore never outlives a collection that did not confirm it.
- **Counts:** the discovery result reports `protected`, `unprotected` and `unread`. The run does not yet include these counts or become `partial` when reads fail. Each unread project's rule is `unknown` regardless.
- **Unverified:**
  - Whether a role below Maintainer may list protected branches. A token that may not is recorded as `forbidden`, so the rule is `unknown`.
  - Whether group-level protected branches (Premium) appear in a project's list. If they do not, a branch protected only at group level reads as unprotected and fails: a false failure, never a false pass.

## Scheduling and freshness

- **Schedule:** the worker queues capability detection, group discovery and project discovery every `BALUARTE_GITLAB_SYNC_INTERVAL_MINUTES` (default 360; `0` collects only on request). The interval is divided into buckets on the database clock, and each job's idempotency key names its bucket, so any number of workers queue each collection once per interval. Scheduled jobs have three attempts.
- **Runs:** every job attempt writes a `collection_run` row before calling GitLab and closes it as `succeeded`, `partial` (some resources could not be confirmed, or some capabilities are not supported) or `failed`, with counts and the safe error. Default-branch protection reads that fail do not yet make a project run `partial` (see [Default-branch protection](#default-branch-protection)). The row is closed even when the job will be retried. A run left `running` by a worker that died is closed as failed (`interrupted: ...`) when a later attempt of the same kind starts, provided its own job attempt is no longer running.
- **Per resource:** a group or project whose reconciliation lookup failed keeps its state and gets `last_error` and `last_error_at`. These are cleared when it is listed again.
- **Freshness:** `GET /api/v1/gitlab/freshness` reports each kind as:

  | Status | Meaning |
  |---|---|
  | `failed` | The latest finished run failed, even if an earlier one succeeded |
  | `never` | No run has finished |
  | `stale` | The last success is older than twice the interval (24 hours when scheduling is off) |
  | `partial` | The latest run finished but some resources could not be confirmed |
  | `fresh` | The latest run succeeded within the threshold |

  The overall status is the worst of the three, in that order, together with resource counts by state and with errors. **A failed collection is therefore never hidden behind an older success.**
- **History:** `GET /api/v1/gitlab/runs` lists run history.

## Reliability

Handle pagination, rate limits, 401/403, 404, 429, 5xx, timeouts, network partitions, and deleted resources. Do not conflate inaccessible with deleted. Reconcile periodically; system hooks/webhooks are optional accelerators, not the only source of truth. Verify signatures/secrets on any incoming hooks.

## Compatibility

GitLab version, license tier, and token privileges determine endpoint behavior. Create a tested capability matrix from actual responses. Avoid assuming that approval, security scanning, or compliance APIs are available in all tiers. Prefer feature detection over version-only assumptions.

## Data freshness

Every resource and finding must show `last_collected_at`, status, and collection errors. A failed scan cannot silently preserve a green dashboard.

## Initial rules

Visibility and default-branch protection are the first two rules. Approval rules, push restrictions, security scans, and deployments require separate endpoint/tier investigations before committing to implementation.

## Remediation research (M6)

v0.1 makes no writes. [REMEDIATION_RESEARCH.md](REMEDIATION_RESEARCH.md) covers what a future remediation feature would depend on: the endpoints, token scopes, tiers, idempotency and audit behaviour. The proposed constraints are in [ADR-0007](architecture/ADR-0007-remediation-constraints.md).
