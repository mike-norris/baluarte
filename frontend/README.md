# Baluarte web

React, TypeScript (strict) and Vite single-page app. Requires Node.js 22+.

## Development

```sh
npm ci
npm run dev        # http://localhost:5173, proxies /api to the backend on 127.0.0.1:8000
npm test
npm run lint
npm run typecheck
npm run build      # static assets in dist/
```

## Sign-in

The app asks `GET /api/v1/auth/me` for the session on load and shows sections only to signed-in users. Sign-in is a full-page navigation to `/api/v1/auth/login`; the API handles OIDC and sets an `HttpOnly` session cookie that the app never reads. Sign-out sends `POST /api/v1/auth/logout` with the `X-CSRF-Token` header from `/auth/me`. Failed sign-ins land on `/sign-in?error=<code>`, which shows a fixed message for each code.

Hiding sections in the browser is a convenience only: the API enforces every permission. To sign in through the Vite dev server, set the backend's `BALUARTE_PUBLIC_URL=http://localhost:5173` and register `http://localhost:5173/api/v1/auth/callback` with your identity provider.

## Setup and Settings

`/setup` is the first-run wizard. It sits outside the signed-in shell and uses the API in `src/api/setup.ts`.

- **Routing:** a visitor without a session (401 or 503 from `/auth/me`) is sent to `/setup` when `GET /api/v1/setup/status` reports `needs_setup` or `awaiting_admin_sign_in`. A 404 (an API without setup) or any failure keeps the usual sign-in prompt.
- **Setup code:** the code from the API's log, or from `baluarte-setup-code`, is exchanged at `POST /api/v1/setup/session` for an `HttpOnly` setup cookie and a CSRF token. The token is kept in `sessionStorage` for that tab only, so a reload keeps the wizard open. Setup endpoints answer 409 once setup is complete, 401 without a setup session, and 403 when the CSRF token or Origin is refused.
  - **401 or 403:** the token is dropped and the code is asked for again, with a notice.
  - **409:** the status is fetched again. If setup is complete, the page leaves `/setup`, for `/sign-in`, or `/` when signed in. Otherwise the 409 was a refused change, and its reason is shown on the form.

  The Helm help notes that the API deployment is `<release>-api` when the release name already contains "baluarte", and that `fullnameOverride` changes it too.
- **Steps:**
  1. **Identity provider:** the address is prefilled from the page's origin, and the redirect URI to register is shown with a copy button. "Test discovery" checks the issuer. Groups are entered one per line, with at least one admin group required. There are hints for GitLab as the provider.
  2. **GitLab (optional):** "Test connection" shows the version, the token's user, its scopes and expiry, a warning for an administrator token, and every check. The step can be skipped.
  3. **Compliance:** the frameworks Baluarte itself must meet (SOC 2 Type II, ISO/IEC 27001:2022, NIST SP 800-53 Rev 5 Moderate, or "None of these") and the seven operating settings they constrain (`GET`/`PUT /api/v1/setup/compliance`). It explains that this governs Baluarte, not the GitLab baselines. Choosing a framework raises each setting to its requirement, the strictest when several are chosen; here a value below a requirement is refused before sending. A choice is required: there is no way past the step until a profile is saved. Servers that report no `compliance` section in the status have no such step.
  4. **Review and finish:** the saved values, then "Sign in to finish setup". The first sign-in by a user in an admin group completes setup on the server. Sign-in is not offered while the compliance profile is missing.
- **Read-only cases:** sections whose `source` is `deployment` are shown read-only. When `secret_key_configured` is false, the page explains `BALUARTE_SECRET_KEY_FILE` instead of asking for secrets.

`/settings` is for users with `settings:manage` (admins). The navigation hides it from everyone else, and the page fetches nothing for them.

- **GitLab connection:** edit, test, replace the token (it is never shown; a blank field keeps it) and disconnect after a confirmation.
- **Identity provider:** edit and test, with a warning that changing the issuer, client or admin groups can lock administrators out.
- **Baseline change review:** a switch for requiring a second person to approve baseline changes (`PUT /api/v1/settings/governance`), off by default.
- **Compliance profile:** the same form as the setup step (`/api/v1/compliance/profile`), shared from `src/compliance/`. Settings the deployment fixes are read-only. A value below a requirement is sent anyway: the API accepts it when an exception is in force, and otherwise answers 409 `below_minimum`, which the form lists with "Request an exception" for each control. Turning on legal hold shows what it does.
- **System:** version, database, logging, the secret-key state and session lifetimes, all read-only.

Both pages share the forms in `src/settings/`. Every check there is repeated by the API.

## Inventory

`/inventory` lists GitLab projects and groups from `GET /api/v1/gitlab/projects` and `/groups`, with the collection status from `GET /api/v1/gitlab/freshness` above them:

- **Collection status:** a failed or stale collection is announced as an alert before any data, so an old success never reads as current.
- **Unknown values:** these are shown as such: an unknown visibility is "Unknown", an empty repository has no default branch, and `missing` is explained as "deleted, or hidden from the token".
- **Personal projects:** projects in a user namespace carry a "Personal" tag. Projects discovered before namespace kinds were recorded have no tag until the next project discovery.
- **Filters:** search, state, visibility, archived and collection errors are kept in the URL (`?view=groups&state=missing`), so a filtered view can be shared. "Load more" follows `next_after_id`.
- **Errors and retries:** API errors show a retry; a 401 refreshes the session and returns to sign-in.
- **Times:** shown in UTC.

Each project and group links to its own page, `/inventory/projects/:gitlabId` or `/inventory/groups/:gitlabId` (`GET /api/v1/gitlab/projects|groups/{gitlab_id}`; one Baluarte hasn't collected reads "not found"):

- **Header:** what was collected, the groups above it as links, and the collection status. When a group above can't be followed, the page says that what applies from above may be missing.
- **Baselines in effect:** the API's resolution (`.../effective-policies`) as an inheritance chain, the baselines in effect and where from, and those replaced or out of scope.
- **Latest results (projects):** the latest result of each rule (`GET /api/v1/gitlab/projects/{gitlab_id}/evaluations`), grouped by baseline version, with unknown reasons in words and a link to the matching findings (`/findings?project=&policy=&rule=`).
- **GitLab security policies reaching this:** its row from `GET /api/v1/gitlab/security-policies/coverage?kind=project|group`, with why and the policies that reach it. When discovery couldn't read them, coverage is unknown, not empty.

Finding details and the dashboard's empty state link to these pages too.

`src/api/` holds the typed API client and the `useLoad` hook, and `src/inventory/` holds the page's helpers.

## Freshness

Signed-in users see one status in the header on every page, as text and colour (for example "GitLab data: Stale"): the worst of two signals, GitLab collection and policy evaluation. `src/freshness/` is the one place for freshness statuses, labels, colours and severity order.

- **When it checks:** the header calls `GET /api/v1/gitlab/freshness` and `GET /api/v1/evaluations/freshness` on sign-in, on every navigation and every minute while a page stays open. The previous status stays visible while a check is in flight; checks are aborted on navigation and unmount.
- **Worst wins:** a failed evaluation shows "Failed" even when collection is fresh. The badge links to the details of the signal that set it: `/inventory` for collection (also when both are equally bad), the dashboard for evaluation. An evaluation that never ran reads "Never evaluated".
- **Unknown is not current:** if either check fails, the header shows "Status unknown" and replaces any earlier "Up to date". Unknown ranks between failed and never, so it is never treated as compliant.
- **Announcements:** failed, stale and unknown are announced once through a polite live region, naming collection or evaluation. Re-checks that return the same status, and navigation, do not repeat the announcement.
- **Results behind collection:** `behind_collection` (a project discovery finished after the latest successful evaluation started) does not change the badge, because a queued evaluation follows every discovery and the badge would otherwise flash. The dashboard, the Findings pages and the Evidence page say that results may not reflect the latest collection.
- **Evidence page:** shows both statuses, the oldest successful collection across projects, groups and capabilities, and the last successful evaluation. It shows no time when any part has never succeeded or the status is unknown.

## Dashboard

`/` shows compliance posture from the evaluation API (`src/api/evaluations.ts`):

- **Evaluation status:** the evaluation freshness, with failed and stale announced as alerts, whether a run is running or queued, results behind collection, and the last run's error.
- **Rule results:** totals by outcome from the latest *successful* run in `GET /api/v1/evaluations/runs` (its `counts.outcomes`). If a newer run has not succeeded, the page says the totals come from an earlier run. The pass share is "X of N rule results", where N includes `unknown` and `unsupported`. Neither is ever counted as passing; each outcome is shown with its meaning. The share is floored, so it never reads 100% unless every result passed.
- **Open findings:** counted by severity and by policy and rule from `GET /api/v1/findings?state=open`, following `next_after_id` for at most 5 pages of 200. When more remain, every count reads "at least". Open findings whose latest result is unknown or unsupported are called out, since they could not be re-checked. "No open findings" says that this is not compliance, because unknown results never open a finding.
- **Links:** counts link to `/findings` with the API's own filter names (`state`, `outcome`, `severity`, `policy`, `rule`, `project`), which the Findings page reads.

## Findings

`/findings` lists findings from `GET /api/v1/findings`; `/findings/:id` shows one.

- **List:** project, rule, policy and pinned version, severity, state, latest outcome, and first and last seen (UTC). Each project links to the finding.
- **Filters:** `state`, `outcome`, `severity`, `project` (GitLab ID), `policy` and `rule` are read from and written to the URL in the API's own names, so the dashboard's links and shared URLs open the same view. Values the API would reject are dropped. "Load more" follows `next_after_id`.
- **Not compliance:** an empty list says that no findings is not the same as every rule passing. An unknown or unsupported latest outcome carries its meaning for screen readers. The policy evaluation status is shown on both pages, and a failed or stale evaluation says the results are not current.
- **Expected versus actual:** the latest evaluation's expected value beside the observed one. When the outcome is not a pass or fail, or nothing was observed, the observed side reads "Unknown — <reason>" and never shows a value. Every unknown reason code is listed with its meaning, the first one explaining the result. The page also shows the capability the rule depends on, the collection status at evaluation, whether policy inheritance was complete, and the source.
- **Evidence:** the observation the result judged, from the project's observations (`GET /api/v1/gitlab/projects/:id/observations?before_id=<id+1>&limit=1`, since there is no single-observation endpoint): source, reference, collection time and status, SHA-256 digest, and whether `digest_valid` holds. A failed digest check, an `evidence_digest_mismatch` reason, or a linked record that cannot be found is announced as an alert. A result with no linked evidence says so; for a pass or fail that is also an alert.
- **History:** every evaluation of the rule for the project, newest first and across policy versions, with "Load older evaluations" following `next_before_id`.
- **Not found:** an unknown or malformed finding ID shows "Finding not found" without calling the API for a malformed one.

`src/findings/` holds the labels, reason explanations, URL filters and the evaluation note. `src/api/usePaged.ts` and `src/components/ErrorMessage.tsx` are shared with the Inventory page.

## Evidence

`/evidence` exports scoped evidence through `POST /api/v1/evidence/exports`:

- **Who sees the form:** only users whose `/auth/me` permissions include `evidence:export` (admin and auditor). Others see why; the API enforces it either way. The Findings list ("Export evidence for this view") and a finding's detail ("Export evidence for this project and rule") link to the form, also only for those users.
- **Scope:** group path, project GitLab IDs, policy, rule, outcome, finding state and a UTC time window, or "Export everything", which disables the other fields and sends `{"all": true}` alone. A scope is required, as the API requires. The form checks the API's limits before sending and lists every problem. The request body holds only the fields that were set.
- **Links:** the form is prefilled from the URL (`group`, `projects=105,230`, `policy`, `rule`, `outcome`, `finding_state`, `since`, `until`, `format=csv`). Findings filters map onto these, except severity, which the export cannot filter by; the link says so.
- **Download:** the request sends the `X-CSRF-Token` header. The response is read to the end, with the bytes received shown and a cancel button, and only then saved, under the `Content-Disposition` name reduced to letters, digits, `.`, `-` and `_` with the format's extension. A cancelled or interrupted export saves nothing. 401 returns to sign-in; 403, 409 and 422 show the API's reason.
- **Verification:** the page explains the manifest and `SHA256SUMS`, that `manifest_sha256` is only in the `evidence.export_completed` audit event (naming the export ID from `X-Baluarte-Export-Id`), and the `baluarte-verify-export <file> --manifest-sha256 <value>` command. It points admins and auditors to `GET /api/v1/audit-events`, which the Audit log page reads.

## Exceptions

An exception waives one rule for one project for a limited time (`GET/POST /api/v1/exceptions`, docs/POLICY_ENGINE.md#exceptions):

- **Requesting:** the finding detail's Exception section shows any requested or approved exception for its project and rule. Users with `exception:request` (admins and analysts) can request one when the latest outcome is a known `fail`. For `unknown` or `unsupported` the page explains why nothing can be waived. The form needs a justification of 10 to 4,000 characters and an expiry day; the exception ends at 23:59:59 UTC on that day, at most 365 days ahead.
- **Deciding:** `/exceptions` lists exceptions, with filters in the URL (`state`, `project`, `policy`, `rule`). `expired` counts any exception past its expiry, before the next evaluation records it. `/exceptions/:id` shows the details and the history, oldest first.
  - Users with `exception:approve` (admins) can approve, reject or revoke after a confirmation step. Rejecting and revoking need a reason.
  - Every change sends the `X-CSRF-Token` header and is audited.
- **Separation of duties:** the page hides approve and reject when the request's `requested_by.user_id` is the signed-in user's `user_id` from `/auth/me`. The API refuses self-approval regardless, and its reason is shown.
- **Effect:** an excepted finding shows its expiry in the findings list and on its detail page, and is never counted as passing. The dashboard keeps `excepted` out of the pass share.

## Governance

`/governance` holds what Baluarte checks and, kept apart, what GitLab enforces ([docs/POLICY_ENGINE.md](../docs/POLICY_ENGINE.md)). The UI says **baseline** for what the API calls a policy, **apply** for an assignment and **in effect** for a resolved policy, so baselines are never confused with GitLab's own security policies. Every role can read it; changes need `policy:write` (admins and analysts), and the pages hide those actions from others. The API enforces it either way. The old `/policies`, `/policies/:name` and `/policy-versions/new` links redirect here.

- **Tabs:** Baselines, Where baselines apply, GitLab security policies, and Changes.
- **Review status:** every Governance view warns while baseline changes take effect without review (`review_required` from `GET /api/v1/policy-changes`), and says how many changes wait for review when it is required. It shows nothing when that read fails.
- **Changes:** `/governance/changes` lists the changes waiting for review, each with what it does and who proposed it; a new version shows a line diff against the version in force. Writers approve (optional note) or reject (note required); for the proposer both buttons are disabled with an explanation. Below, decided changes with who approved them, or that they were not reviewed because review was off. While review is required, saving, applying and removing report that the change was proposed (the editor then opens this page) instead of applying it. Version pages and the "Applied to" table show how each was reviewed.
- **Baselines:** each baseline with its latest version, where it is applied (and whether a newer version isn't applied everywhere yet), and how many projects fail, pass or are unknown in the latest successful evaluation (`GET /api/v1/evaluations/summary`), with the failing count linked to its open findings. Without counts it links to open findings. With no baselines, the page explains what one is and offers the recommended baseline (private or internal visibility; every default branch protected).
- **Editor:** `/governance/baselines/new` is a form: name, rules as switches with their settings and severity, and the groups the baseline covers. "Add rule" offers the two GitLab security policy rules ([docs/EVALUATION.md](../docs/EVALUATION.md)), which are never in the recommended baseline: required scans (scan checkboxes, and whether a policy that lets pipelines skip CI counts) and required approvals (1 to 20 on every merge request, and whether the policy must stop the author or committers approving). The form explains what counts: scans on the default branch, not merge-request-only rules; unconditional approval rules, not those triggered by scan or license findings. `?start=recommended` prefills the recommended baseline and `?from=<name>&version=<n>` starts a new version of an existing one, with the name fixed. "Edit as YAML (advanced)" shows the form's YAML for editing, with "Check YAML" (`POST /api/v1/policies/validate`) listing every error with its path; `?mode=yaml` opens straight into YAML, and a saved version the form can't show completely opens as YAML too. A new baseline is saved with "Save and apply" (save, then apply to the whole instance or a group or project found by path) or "Save without applying"; both report the result on the baseline's page. An identical source says that no new version was saved, and an assignment that was already in place says that nothing changed.
- **Baseline page:** `/governance/baselines/:name` shows the selected version's rules as sentences with their severity and their pass, fail and unknown counts (`GET /api/v1/evaluations/summary?policy=`, each failing count linked to its findings), the groups it covers, its YAML and SHA-256 under "Show YAML", where it is applied, and every version (`?version=N` selects one). "Apply…" applies a version to a target; "Remove from here" stops checking it on a target after a confirmation. When places still use an older version, a banner offers to apply the latest one to each of them in turn.
- **Where baselines apply:** `/governance/where-baselines-apply` shows the instance, its groups and subgroups as a tree, with what is set at each level, what is inherited and from where, what a closer setting replaces, and versions whose scope leaves a group out. The API resolves every group in one read (`GET /api/v1/policy-assignments/tree`); the browser only nests it and puts it into words. Groups and projects link to their pages. Groups whose path can't be confirmed are marked partly unknown. Project assignments are listed under their group. Rows offer "Apply here…" and removal to writers. "Check a project or group" shows the API's own resolution (`GET /api/v1/gitlab/{groups|projects}/:id/effective-policies`) as an inheritance chain, the baselines in effect, and those replaced or out of scope.
- **GitLab security policies:** `/governance/gitlab-security-policies` lists the policies Baluarte discovered (`GET /api/v1/gitlab/security-policies`), read-only and always named with the "GitLab" prefix: type, enabled or disabled, policy source project, the groups it is linked to and how many groups and projects it reaches, filtered by type and optionally including policies no longer found. Admins (`settings:manage`) get "Collect now" (`POST /api/v1/gitlab/security-policies/discover`). When policies couldn't be read (not collected yet, a type GitLab doesn't support, the token lacks access, or a failed run) a notice says so and that coverage is unknown, not empty; an empty list never claims there are none when they couldn't be read.
- **GitLab security policy page:** `/governance/gitlab-security-policies/:id` shows the policy source project, links, scope and exclusions, a plain-language summary of its YAML (scans and when they run; approvals, approvers, rules and settings) with notable settings flagged (skip_ci allowed, fail open, authors approving their own merge requests), the groups and projects it reaches, and the verbatim YAML with its SHA-256. The summary comes from a small reader for GitLab's block YAML (`src/security-policies/yaml.ts`); YAML it doesn't handle shows without a summary. Admins also get an "Open in GitLab" link built from the configured GitLab URL; others don't, because reading settings needs `settings:manage`.
- **Coverage:** `/governance/gitlab-security-policies/coverage` (`GET /api/v1/gitlab/security-policies/coverage`) counts covered, not covered and unknown groups and projects, and lists them with the enabled policy of each type and why: the policies that reach it, only disabled ones, its group covered but the scope excluding it, a source project linked but none of its policies reaching it, or no source project linked at all. Unknown rows give the API's reason and are never counted as not covered. `?kind=projects` switches to projects; "Only gaps" and "Only unknown" filter them.

## Audit log

`/audit-log` is for users with `audit_log:read` (admins and auditors); the navigation hides it from everyone else and the API refuses it. It lists `GET /api/v1/audit-events` newest first: time, actor, action, target, a one-line summary of the details (the full JSON and its SHA-256 on expanding), and whether the digest still verifies (`digest_valid`). A digest that no longer matches is flagged in red with a count above the table. Filters for action (`GET /api/v1/audit-events/actions`), actor and UTC dates are kept in the URL; "Load older events" pages with `before_id`. A note says the page reads the database, and that the `baluarte.audit` log stream is the independent copy.

## Compliance

`/compliance` is for users with `compliance:read` (admins and auditors). It shows how Baluarte itself meets its operating profile (`GET /api/v1/compliance/posture`, `/catalogue` and `/exceptions`), separate from findings about GitLab.

- **Posture:** the frameworks, counts by status, and each control with its status, the selected frameworks' references and the API's detail, problems first. "Details and how to fix" shows the description, how to meet it, the evidence, and the exception or attestation in force. Admins can attest an attestation control (`POST /api/v1/compliance/attestations`).
- **No profile:** the page says so and sends admins to Settings; nothing is enforced until a profile is chosen.
- **Exceptions:** every request with its state. Admins request one (control, justification, expiry up to 365 days), approve or reject someone else's (the requester only sees that another administrator must decide), and revoke one in force after a confirmation.
- **Banner:** on every other page, admins and auditors see a banner while a configured profile has controls not met or awaiting attestation. It reads the posture on load and every five minutes, and stays quiet if that fails.

## Remediation

Dry-run plans of the GitLab changes that would fix failing findings (`/api/v1/remediation/plans`, docs/REMEDIATION.md). Every remediation screen carries a notice that Baluarte makes no change to GitLab: execution is not available, so approving a plan changes nothing.

- **Listing:** `/remediation` lists plans, filtered by `state` in the URL. `expired` counts any plan past its expiry.
- **Creating:** `/remediation/new` takes finding IDs, or a project GitLab ID, policy and rule. It's only for users with `remediation:plan` (admins and analysts).
  - The form is prefilled from `?finding=5&finding=6` or `?project=105&policy=…&rule=…`.
  - "Plan a dry-run fix" links on the findings list (when it filters by project, policy or rule) and on an open, failing finding's detail page lead there. The findings list link says which of its filters a plan ignores.
  - The form checks the API's limits first: one kind of scope, at most 100 findings, and 1 to 72 hours.
  - When nothing can be planned, the API's reason for each finding is listed.
- **Reviewing:** `/remediation/:id` shows the plan.
  - **Changes:** each step as current → intended, the policy's expected value, and the exact request that would be sent, in a labelled code block. It also shows the verification read, the preconditions, the blast radius, and the finding, evaluation and evidence it rests on.
  - **What isn't planned:** the skipped findings with their reasons, and the actions remediation never takes.
  - **Integrity:** the plan digest, with a warning when the stored plan no longer matches it. Stale reasons are shown before anyone tries to approve.
- **Deciding:**
  - **Submitting:** the creator submits a draft.
  - **Approving and rejecting:** users with `remediation:approve` (admins) approve or reject a submitted plan after a confirmation step. The step states the digest the approval binds to, and approval sends that `plan_digest`. Rejecting needs a reason.
  - **Refusals:** a 409 lists the API's staleness reasons.
  - **The creator:** the page compares the plan's `created_by_user_id` with `user_id` from `/auth/me`. It offers submitting only to the creator and hides the decision from them. The API refuses the creator's decision regardless, and its reason is shown.
