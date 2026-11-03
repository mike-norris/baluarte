# GitLab integration contract

## Initial scope

Self-managed GitLab REST API, one instance, read-only credential, group/subgroup/project enumeration, default-branch protections, project visibility, access controls (approval settings, members, project access tokens, two-factor enforcement), and capability detection. GitLab security policies are read through GraphQL, with queries sent as GET (see [Security policies](#security-policies)).

## Connection

Configured with `BALUARTE_GITLAB_URL` (https, no `/api/v4`; relative-URL installs such as `https://example.internal/gitlab` work), `BALUARTE_GITLAB_TOKEN_FILE` and optionally `BALUARTE_GITLAB_TIMEOUT_SECONDS` (default 15). The client lives in `baluarte.integrations.gitlab`.

When `BALUARTE_GITLAB_URL` is not set, an administrator can enter the same values in the web UI: in the setup wizard or on the Settings page (`PUT /api/v1/settings/gitlab`). The token is sealed in the database ([ADR-0008](architecture/ADR-0008-settings-in-the-database.md)). The same checks apply. "Test connection" (`POST /api/v1/settings/gitlab/test`) runs the diagnostics below with the values entered, before saving. API and worker processes pick up a saved change within 10 seconds, and the worker reschedules collection within a minute. Disconnecting (`DELETE /api/v1/settings/gitlab`) removes the URL and token but keeps everything collected.

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
| `user.read` | `GET /user` | no | Who the token acts as: `token_user: {username, is_admin}` on the snapshot. GitLab includes `is_admin` only for administrators, so its absence means not one. `token_user` is null when this is refused |
| `admin.read` | `GET /application/statistics` | no | Run only for an administrator: whether the token can use administrator access (`token_user.admin_access`). With Admin Mode on, an administrator's token without the `admin_mode` scope is refused here and sees only what the account sees as a member. Not run, and not recorded, for anyone else |
| `groups.list`, `groups.visibility` | `GET /groups?per_page=1` | yes | Groups can be listed and carry `visibility` |
| `groups.two_factor` | `GET /groups?per_page=1` | no | Groups carry `require_two_factor_authentication`, for the two-factor rule |
| `projects.list`, `projects.visibility`, `projects.default_branch` | `GET /projects?per_page=1` | yes | Projects can be listed and carry `visibility` and `default_branch` |
| `protected_branches.read` | `GET /projects/:id/protected_branches` | yes | Branch protections are readable for a visible project |
| `merge_request_approvals.read` | `GET /projects/:id/approvals` | no | Project approval settings are readable (Premium or Ultimate); the merge request approvals rule needs it |
| `merge_request_approval_rules.read` | `GET /projects/:id/approval_rules` | no | Project approval rules are readable |
| `project_members.read` | `GET /projects/:id/members/all` | no | Members, direct and inherited, are readable |
| `project_access_tokens.read` | `GET /projects/:id/access_tokens` | no | Project access tokens are readable. Needs the Maintainer role on the sample project, so `forbidden` is common and says nothing about other projects |
| `license.read` | `GET /license` | no | Tier (`plan`). Administrator-only, so `forbidden` is expected for a least-privilege token and the tier is then unknown |
| `security_policies.read` | `GET /api/graphql`: `group(fullPath) { securityPolicyProject }` on the first visible group | no | Security policy links are readable |
| `security_policies.scan_execution`, `.approval`, `.pipeline_execution`, `.vulnerability_management` | `GET /api/graphql`: one page of the type's policies on the same group, with the exact selection discovery uses | no | That policy type can be read. Security policies need GitLab Ultimate, but a refusal is recorded as `forbidden`, never as "not Ultimate" |

Each probe is `supported`, `forbidden` (401/403, or a GraphQL permission error), `not_found` (404, or GraphQL returned no group), `error` (rate limit, 5xx, timeout, missing field, any other GraphQL error), `unsupported` (GraphQL does not have the field on this version or edition) or `skipped` (no visible project or group to probe). GitLab also answers 404 for resources hidden from the caller, so `not_found` alone does not prove a tier lacks a feature. The snapshot is `ok` only when every core probe is `supported`, otherwise `partial`. Rules must treat anything other than `supported` as unable to evaluate, never as compliant.

A failed detection replaces the stored snapshot, so an earlier `ok` is never shown after a failure; the last known version, edition and `token_user` are kept. The job retries a failure up to three attempts. Snapshots older than 24 hours are reported as `stale`. Periodic detection will be scheduled with inventory collection; until then it runs on request.

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
- **Counts:** each groups or projects run records two halves in `collection_run.counts`:
  - `listed`: what the listing returned, by the row's state before this run: `new` (not stored before), `known` (already `active`) and `restored` (was `missing` or `inaccessible`).
  - `active`, `inaccessible`, `missing` and `unchanged`: what happened to the previously active resources the listing did not return, by the outcome of looking each one up. `unchanged` means it could not be confirmed, which makes the run `partial`.

  The worker logs the same, for example `gitlab projects: 107 listed (107 new, 0 known, 0 restored); 0 not listed: 0 still active, 0 inaccessible, 0 missing, 0 could not be confirmed`.
- **Evidence:** each listed or confirmed resource, and each one that could not be read, is recorded as an observation with an allowlisted payload and its SHA-256 digest, written only when it changed ([AUDIT_EVIDENCE.md](AUDIT_EVIDENCE.md#observations)).
- **Failures:** if the listing fails part way, pages already stored are kept, nothing is marked `missing` or `inaccessible`, and the job retries (up to five attempts). The job error holds the safe client message, for example `project discovery failed: GitLab rejected the token (401)`.

Results are read with `GET /api/v1/gitlab/groups` and `GET /api/v1/gitlab/projects`, and one at a time with `GET /api/v1/gitlab/groups/{gitlab_id}` and `GET /api/v1/gitlab/projects/{gitlab_id}` (`inventory:read`; 404 when the item is not in the inventory or GitLab is not configured). The single reads are keyed by GitLab ID, like the effective-policies, evaluations and observations reads:

```text
GroupDetail   { group: Group, ancestry: Ancestry, subgroups: int, projects: int,
                collection: KindFreshness }      # the groups collection
ProjectDetail { project: Project, ancestry: Ancestry,
                collection: KindFreshness,       # the projects collection
                not_evaluated: "archived" | null }  # why baselines skip it; see EVALUATION.md
Ancestry      { groups: [{ gitlab_id, full_path, state }],   # nearest first
                status: "complete" | "incomplete",
                gaps: [{ gitlab_id, reason }] }
```

- **`Group` and `Project`:** the same items as the lists.
- **`ancestry`:** a group's parents, or a project's namespace group and its parents; empty and `complete` for a top-level group or a personal project. Gaps use the reasons of [policy resolution](POLICY_ENGINE.md#assignment-and-inheritance).
- **Counts:** `subgroups` and `projects` count the group's direct children in the inventory, personal projects excluded.
- **`collection`:** the freshness of the discovery that records the item, in the shape of `/gitlab/freshness` `collections`. A `state` from a failed or stale collection is not current.

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
- **Counts:** the projects run records `protected`, `unprotected` and `unread` under `counts.protection`. When any read failed (`unread` above zero), the run is `partial`, with the error "default-branch protection of N projects was not read". Each unread project's rule is `unknown`.
- **Unverified:**
  - Whether a role below Maintainer may list protected branches. A token that may not is recorded as `forbidden`, so the rule is `unknown`.
  - Whether group-level protected branches (Premium) appear in a project's list. If they do not, a branch protected only at group level reads as unprotected and fails: a false failure, never a false pass.

### Access controls

In the same pass, after protection, each `active` project is read for the access-control rules ([EVALUATION.md](EVALUATION.md#access-control-and-merge-request-approval-rules)). Every request is a GET with the same token; each source gets one observation per project, written only when it changes.

| Source | Requests | Kept | Not kept |
|---|---|---|---|
| `GET /projects/:id/approvals` | `GET /projects/:id/approvals` and every page of `GET /projects/:id/approval_rules`, for projects with a default branch | `branch`, `branch_protected` (from this pass's protection read, `null` when it failed), `settings` (`merge_requests_author_approval`, `merge_requests_disable_committers_approval`, `reset_approvals_on_push`, `disable_overriding_approvers_per_merge_request`, `require_reauthentication_to_approve`, or the older `require_password_to_approve`), and `rules`: each rule's `name`, `rule_type`, `approvals_required`, `applies_to_all_protected_branches` and protected branch names | Approvers (users and groups), eligible approvers |
| `GET /projects/:id/members/all` | Every page | `total`, `privileged_count`, `by_access_level`, `by_state`; `privileged` and `inactive`: `username`, `access_level`, `state`, `expires_at` of members with Maintainer or Owner access, and of members whose account is not `active` | Every other member's name; names, emails, avatars, IDs |
| `GET /projects/:id/access_tokens` | Every page | Active tokens only: `id`, `name`, `scopes`, `access_level`, `created_on`, `expires_at`, `last_used_on` (dates only), and `as_of`, the date of the read, when there is any token | Token values, user IDs, revoked or expired tokens |
| `GET /groups (two-factor settings of the project's groups)` | None per project. Group discovery stores each group's `require_two_factor_authentication` and `two_factor_grace_period`; with administrator access (`token_user.admin_access`), `GET /application/settings` is read once per pass | `groups` (top-down: `path`, the two settings, the group's latest observation ID), `ancestry` (`incomplete` when a group above the project is not in the inventory), `instance` (the two instance settings, or `null` when not read), `enforced_by` (the nearest group that requires it, `instance`, or `null`) | Every other application setting |

- **Dates:** token and member dates are cut to the day, so a token used again the same day does not make a new observation. A project with tokens gets a new observation at most once a day (`as_of`).
- **Failures:** a 403 or 404 is an answer about that project (a role, or a feature the edition lacks) and is recorded as `forbidden` or `not_found`; the rule is then `unknown` for it. Only `failed` reads count toward stopping a reader after 20 in a row and make the run `partial` ("members of N projects could not be read"). A 401 from any reader stops every reader, as for protection.
- **Counts:** the run records each reader's counts by status under `counts.reads` (`approvals`, `members`, `access_tokens`, `two_factor`).
- **Cost:** three to four more requests per project per discovery (approvals and approval rules only with a default branch).
- **Unverified:** whether the approval endpoints answer 403 or 404 on GitLab Free, and whether `members/all` lists blocked or deactivated users on every version. Either way the rule is `unknown`, or a member who is not listed is not judged.

## Security policies

GitLab security policies live in a security policy project linked to a group or project, and apply to everything below that link. Baluarte discovers four types and lists them; it never creates, links or changes one ([ADR-0003](architecture/ADR-0003-read-only-v0-1.md)). Coverage gaps become findings only through a baseline rule (`gitlab.security_policy.*`, see [EVALUATION.md](EVALUATION.md#gitlab-security-policy-rules)); discovery alone reports them.

| Type | GraphQL field |
|---|---|
| `scan_execution` | `scanExecutionPolicies` |
| `approval` (merge request approval) | `approvalPolicies` |
| `pipeline_execution` | `pipelineExecutionPolicies` |
| `vulnerability_management` | `vulnerabilityManagementPolicies` |

### GraphQL over GET

`GitLabClient.graphql(query, variables)` sends a query to `<BALUARTE_GITLAB_URL>/api/graphql` as GET parameters (`query`, and `variables` as JSON). GitLab runs queries but never mutations over GET, and the client also refuses anything that is not a query, so this path is read-only too. The URL is built from the configured instance only; redirects are not followed; retries and status handling are the same as REST. The encoded request is limited to 6,000 bytes. A response with `errors` is a failure even if some `data` came back: an unknown field is `UnsupportedField` (`unsupported`), a permission error `GraphQLForbidden` (`forbidden`), anything else `GraphQLError`. Error messages are cut to 200 characters.

### Discovery

`gitlab.discover_security_policies` (queued after each project discovery when readable, or by `POST /api/v1/gitlab/security-policies/discover` with `settings:manage`) reads every active group and project:

- `securityPolicyProject`: the policy project linked directly to the target, if any.
- For each type the capability probe marked `supported`: `relationship: INHERITED`, so the policies of the target and its ancestors. **Projects also pass `includeUnscoped: false`**, so GitLab returns only the policies whose `policy_scope` (projects, groups, compliance frameworks, archived and personal exclusions) includes the project. Baluarte does not re-implement scope matching.
- Four types in one query exceed GitLab's default query complexity limit of 300 (about 87 per type, measured on GitLab 19.5), so each target is read with two queries of two types.

Each target gets one observation (sources `GET /api/graphql group security policies` and `GET /api/graphql project security policies`, `source_ref` the path), deduplicated like the others:

```json
{
  "security_policy_project": "corporate/security-policy-project",
  "types": {
    "scan_execution": {
      "status": "ok",
      "truncated": false,
      "policies": [
        {
          "name": "Scan policy",
          "description": "",
          "enabled": true,
          "yaml": "<the policy YAML, verbatim>",
          "updated_at": "2026-10-23T14:06:13Z",
          "source": {"type": "group", "path": "finance", "inherited": true},
          "scope": {
            "compliance_frameworks": [], "including_projects": [], "excluding_projects": [],
            "including_groups": [], "excluding_groups": [],
            "excluding_archived_projects": false, "excluding_personal_projects": false,
            "truncated": false
          }
        }
      ]
    },
    "approval": {"status": "ok", "truncated": false, "policies": []},
    "pipeline_execution": {"status": "unsupported", "truncated": false, "policies": []},
    "vulnerability_management": {"status": "unknown", "truncated": false, "policies": []}
  }
}
```

- A type's `status` is `ok` (read), `unsupported` (this GitLab has no such field) or `unknown` (the probe could not check it). **Neither `unsupported` nor `unknown` ever means "no policy".** `truncated` is true when GitLab reported another page (more than 100 policies of a type, or 50 scope entries).
- The observation is `ok` when every type was read in full, otherwise `partial`. A target GitLab would not answer for has no payload: `forbidden`, `not_found` (hidden, or GraphQL returned null), `unsupported` or `failed`. A 401 stops the run and records the remaining targets as not read, like branch protection; so do 20 failed reads in a row.
- The YAML is stored verbatim because it is the policy GitLab enforces. It holds rules, branch names and approver usernames, not credentials.

The catalogue (`gitlab_security_policy`) is then rebuilt: one row per policy, keyed by the policy project that holds it, which is the `securityPolicyProject` of the policy's source when Baluarte read that source. So a policy project linked to three groups gives one row, with three entries in `linked_from`. When the source is not visible, the row is keyed by the source path (`origin_kind` `linked_source`). A policy no target listed becomes `missing` only after a run that read every target and every type.

The run (`kind` `security_policies`) is `succeeded`, `partial` (some targets not read, or some type unknown, unsupported or cut short) or `failed` (no target read, or security policies not readable), with `counts`: `groups` and `projects` (`read`, `unread`), `policies` (`active`, `new`, `missing`), `by_type` and `types_unavailable`.

Verified read-only against GitLab 19.5.1-ee on 2026-10-31: the probes and both queries return the linked policy projects, scan execution and approval policies, and inherited sources.

### Read API

All need `inventory:read` (every role) and never call GitLab. Each response carries the discovery context:

```text
readable:     bool                       # links and at least one type are readable
availability: {<type>: "ok" | "unsupported" | "unknown"}
collection:   KindFreshness              # same shape as /gitlab/freshness collections:
              # {status: failed|never|stale|partial|fresh, running, last_run, last_success_at}
```

`GET /api/v1/gitlab/security-policies`. Query: `policy_type`, `state` (`active`, `missing`), `enabled`, `search` (name), `after_id`, `limit` (1–500, default 100).

```text
{ readable, availability, collection,
  items: [SecurityPolicySummary], next_after_id: int | null }

SecurityPolicySummary {
  id, policy_type, name, description: str | null, enabled: bool | null,
  origin_kind: "policy_project" | "linked_source", origin_path,
  linked_from: [{type: "group" | "project", path}],
  state: "active" | "missing", yaml_digest: str | null, gitlab_updated_at: datetime | null,
  first_seen_at, last_seen_at,
  reaches: {groups: int, projects: int}   # active targets whose latest discovery lists it
}
```

`GET /api/v1/gitlab/security-policies/{id}`: 404 when not on the configured instance.

```text
SecurityPolicyDetail = SecurityPolicySummary + {
  readable, availability, collection,     # the discovery context, as on every read
  yaml: str | null,                       # verbatim
  scope: {compliance_frameworks, including_projects, excluding_projects, including_groups,
          excluding_groups: [str], excluding_archived_projects, excluding_personal_projects:
          bool | null, truncated: bool},
  reached: {groups: [ReachedTarget], projects: [ReachedTarget]}
}
ReachedTarget { id, gitlab_id, path, via: str | null, inherited: bool | null,
                observation_id, collected_at }
```

`GET /api/v1/gitlab/security-policies/coverage`. Query: `kind` (`group`, `project`), `status` (`covered`, `not_covered`, `unknown`). `summary` always counts every active target.

```text
{ readable, availability, collection,
  summary: {groups: Counts, projects: Counts},   # Counts {covered, not_covered, unknown}
  groups: [CoverageRow], projects: [CoverageRow] }  # sorted by path

CoverageRow {
  id, gitlab_id, path,
  status: "covered" | "not_covered" | "unknown",
  reason: str | null,                      # why unknown
  security_policy_project: str | null,     # linked directly to this target
  observation: {id, collected_at, collection_status, error} | null,
  types: {<type>: {status: "ok" | "unsupported" | "unknown", truncated: bool,
                   enabled: int, disabled: int,
                   policies: [{id: int | null, name, enabled, via, inherited}]}}
}
```

`covered`: at least one enabled policy reaches the target. `not_covered`: every type was read in full and none is enabled. `unknown`: never read, not read, or a type unsupported, unknown or cut short with nothing enabled found.

## Scheduling and freshness

- **Security policies:** each finished project discovery queues `gitlab.discover_security_policies` (once per run), but only when the latest capability snapshot has `security_policies.read` and at least one policy type `supported`.
- **Schedule:** the worker queues capability detection, group discovery and project discovery every `BALUARTE_GITLAB_SYNC_INTERVAL_MINUTES` (default 360; `0` collects only on request), or the interval saved in the web UI. It re-reads the setting every minute. The interval is divided into buckets on the database clock, and each job's idempotency key names its bucket, so any number of workers queue each collection once per interval.
- **Retries:** a scheduled job retries 1, 5 and 15 minutes after each failed attempt (±20% jitter), so an outage of up to about 20 minutes is ridden out. Only the delays that fit in half the interval are used: from a 42-minute interval all three (four attempts), below that 1 and 5 minutes (three attempts). The delays are kept in the job's payload (`retry_delays_seconds`). Jobs queued on request ("Collect now") keep the queue's default backoff (30 s, then doubling), so they report quickly.
- **Runs:** every job attempt writes a `collection_run` row before calling GitLab and closes it as `succeeded`, `partial` (some resources could not be confirmed, or some capabilities are not supported) or `failed`, with counts and the safe error. Per-project reads that fail make a project run `partial` (see [Default-branch protection](#default-branch-protection) and [Access controls](#access-controls)). The row is closed even when the job will be retried. A run left `running` by a worker that died is closed as failed (`interrupted: ...`) when a later attempt of the same kind starts, provided its own job attempt is no longer running.
- **Per resource:** a group or project whose reconciliation lookup failed keeps its state and gets `last_error` and `last_error_at`. These are cleared when it is listed again.
- **Freshness:** `GET /api/v1/gitlab/freshness` reports each kind as:

  | Status | Meaning |
  |---|---|
  | `failed` | The latest finished run failed, even if an earlier one succeeded |
  | `never` | No run has finished |
  | `stale` | The last success is older than twice the interval (24 hours when scheduling is off) |
  | `partial` | The latest run finished but some resources could not be confirmed |
  | `fresh` | The latest run succeeded within the threshold |

  Each kind also reports `consecutive_failures` (finished runs since its last success that failed) and `failing_since` (when the first of them started).

  The overall status is the worst of the three, in that order, together with resource counts by state and with errors. **A failed collection is therefore never hidden behind an older success.** Security policy discovery is not one of the three: an instance without Ultimate would otherwise never be fresh. Its freshness is reported by the security policy endpoints (`collection`), with the same statuses.
- **Alert:** the report's `alert` is set while collection is unhealthy, and null otherwise:

  | `reason` | When |
  |---|---|
  | `failing` | A kind's last 3 finished runs all failed (`FAILURE_ALERT_AFTER`). Each attempt is a run, so a scheduled collection whose retries all fail reaches it; one failure the next attempt recovers from does not |
  | `overdue` | Collection is scheduled and a kind's last success is older than twice the interval, with no failures to explain it: for example, no worker is running |

  `failing` wins when both apply. The alert lists the affected `kinds`, the highest `consecutive_failures`, the earliest `failing_since`, the oldest `last_success_at` (null if a kind never succeeded), the latest run's `last_error` and a `message`. The web UI shows it as a banner on every page.
- **Operator log:** every worker re-checks the alert each minute and logs one `WARNING` when collection becomes unhealthy (`event: gitlab.collection_unhealthy`, with `reason`, `kinds`, `consecutive_failures`, `failing_since`, `last_success_at`, `last_error`) and one when it recovers (`event: gitlab.collection_recovered`), never once per run. Alert on these events (see [OPERATIONS.md](OPERATIONS.md#monitoring-collection)). The state is held per worker process, so a worker that starts while collection is unhealthy logs it once, and each worker logs its own transitions.
- **Population:** the report's `population` states which groups and projects the inventory can cover, from the latest snapshot's `token_user`: `basis` `instance` (an administrator's token whose `admin.read` succeeded: every group and project; `is_admin` alone is not enough, since Admin Mode can withhold administrator access from the token), `token_visibility` (only what `token_username` can see) or `unknown` (the token's user was never read), with `token_username`, `is_admin`, `admin_access` and a plain-language `statement`. The Dashboard and Inventory pages show the statement, and evidence exports carry it ([AUDIT_EVIDENCE.md](AUDIT_EVIDENCE.md#manifest-and-verification)). Projects the token cannot see are not in the inventory and are not checked.
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
