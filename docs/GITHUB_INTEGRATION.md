# GitHub integration contract

Roadmap milestone M13 ([ROADMAP.md](ROADMAP.md#m13--github)), through the common provider layer of [ADR-0014](architecture/ADR-0014-provider-integrations.md). This page starts as the feasibility spike: what GitHub's official REST documentation says about each collection and rule Baluarte needs, and what is still to be confirmed against a live organization. Nothing here is assumed from GitLab, and nothing is implemented yet.

Each fact names its source page on docs.github.com (REST API version `2022-11-28`). **To verify** marks what the documentation did not state, or stated only in part. Each such row is confirmed by a live probe and a recorded response before a rule depends on it. As with GitLab, run-time capability detection decides in the end, not this table.

## Status

| Step | What it delivers | State |
|---|---|---|
| M13.1 Feasibility spike | This page: authentication, versions, endpoints, permissions, plans and rate limits per collection and rule, and the step plan | Done |
| M13.2 Provider layer and connection | Provider on connections and normalized records; GitHub App and fine-grained token connections for GitHub.com (Enterprise Server later); capability probes; rate limits and retries; organization, team, repository and membership discovery | Planned |
| M13.3 Settings and rules | Repository settings, branch protection and rulesets, required reviews, visibility, security features; each rule type declares the providers it supports, and GitHub results carry their evidence | Planned |
| M13.4 Change evidence and webhooks | Pull requests, reviews, workflow runs, deployments and releases; signed webhooks processed once per delivery and de-duplicated against scheduled collection | Planned |
| M13.5 Issue register and acceptance | GitHub issues in the register (pull requests left out); one workspace with a GitLab and a GitHub connection evaluated by one baseline | Planned |

## Authentication

- **GitHub App installation, preferred** ([ADR-0014](architecture/ADR-0014-provider-integrations.md)). Permissions are requested per resource, each as read or write. The ones Baluarte reads with are: repository `administration`, `actions`, `deployments`, `issues`, `pull_requests`, `secret_scanning_alerts` and `security_events`, plus organization `members` and organization `administration` (the organization rulesets endpoints). Collection asks for read only. Writes keep ADR-0007's separate credential and switch. *Source: Permissions required for GitHub Apps; the installation permissions schema in Organizations.*
- **Fine-grained personal access token, fallback**, with the same permissions under their display names ("Administration", "Actions", "Members", …). *Source: Permissions required for fine-grained personal access tokens.* **To verify:** availability and minimum version on GitHub Enterprise Server, which that page does not state.
- **Classic tokens** are not supported. They grant broad scopes (`repo`, `admin:org`, `security_events`), which conflicts with least privilege.

## Versions

- **GitHub.com**, with API version header `X-GitHub-Api-Version: 2022-11-28`.
- **GitHub Enterprise Server** comes later: M13 targets GitHub.com first (decided 2026-11-21). GitHub supports at least the four most recent feature releases; on 2026-11-21 these are 3.19 to 3.23, and 3.19 stops being supported on 2026-12-09. Once a test instance is available, Baluarte will support the releases GitHub supports then, and recheck the minimum at each Baluarte release. *Source: Enterprise Server releases.*

## Rate limits

*Source: Rate limits for the REST API.*

| Credential | Primary limit |
|---|---|
| Fine-grained token (authenticated user) | 5,000 requests an hour |
| GitHub App installation | 5,000 an hour at least. It grows by 50 per repository beyond 20 and per organization user beyond 20, up to 12,500. An installation on a GitHub Enterprise Cloud organization gets 15,000 |

- **Secondary limits:**
  - at most 100 concurrent requests;
  - 900 points a minute per REST endpoint (a GET costs 1, a write 5);
  - 90 seconds of CPU per minute of real time;
  - about 80 content-creating requests a minute and 500 an hour.
- **Headers:** `x-ratelimit-limit`, `-remaining`, `-used`, `-reset` (UTC epoch seconds) and `-resource`.
- **When limited:**
  - The primary limit gives 403 or 429 with `x-ratelimit-remaining: 0`. Baluarte waits until `x-ratelimit-reset`.
  - A secondary limit: Baluarte honours `retry-after`. Without it, it waits until the reset when nothing remains, and otherwise waits at least a minute and backs off exponentially.
- **Collection design:** one request at a time per connection, so Baluarte never comes near the concurrency limit.
- **To verify:** whether conditional requests (`ETag`, `304`) count against the limit. The page does not say.

## Webhooks

*Source: Validating webhook deliveries.*

- **Signature:** `X-Hub-Signature-256: sha256=<hex>`, the HMAC-SHA256 of the raw body under the webhook secret, compared in constant time. The legacy `X-Hub-Signature` (SHA-1) is ignored.
- **Implementation test:** the documented vector (secret `It's a Secret to Everybody`, body `Hello, World!`, signature `757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17`) becomes a test.
- **To verify:** the delivery ID header (`X-GitHub-Delivery`) and redelivery behaviour, from "Webhook events and payloads" and "Handling webhook deliveries". Deliveries will be processed once per delivery ID, and keyed by provider IDs so they never duplicate scheduled reads.

## Collections

| Collection | Endpoint | Permission | Plan and notes |
|---|---|---|---|
| Organization | `GET /orgs/{org}` | Organization owner for full details | `two_factor_requirement_enabled` (true, false or null). **To verify** whether a non-owner sees it, or sees null. *Source: Organizations* |
| Members | `GET /orgs/{org}/members` | organization `members` read | **To verify:** the `filter=2fa_disabled` filter and who may use it |
| Repositories, teams, collaborators | — | — | **To verify** in M13.2: listing endpoints and pagination, and the `visibility` and `security_and_analysis` fields of a repository |
| Branch protection | `GET /repos/{owner}/{repo}/branches/{branch}/protection` | **To verify:** the App permission (the page says only that admin or owner rights are needed to change it) | Available on public repositories with GitHub Free; on private repositories it needs Pro, Team, Enterprise Cloud or Enterprise Server. Gives `required_pull_request_reviews` (`required_approving_review_count` 0 to 6, `dismiss_stale_reviews`, `require_code_owner_reviews`, `require_last_push_approval`), `required_status_checks` (`strict`, `checks`; `contexts` is closing down), `enforce_admins` and `restrictions`. *Source: Protected branches* |
| Rules for a branch | `GET /repos/{owner}/{repo}/rules/branches/{branch}` | **To verify** | Every active rule that applies to the branch, from repository or organization rulesets. Rules set to `evaluate` (Enterprise only) or `disabled` are left out. **To verify:** plan availability. *Source: Rules* |
| Organization rulesets | `GET /orgs/{org}/rulesets` | organization `administration` | **To verify:** plan availability |
| Issues | `GET /repos/{owner}/{repo}/issues?state=all&since=…` | repository `issues` read | Pull requests come back too and carry a `pull_request` key; Baluarte drops them from the register. `since` filters on last update, which suits incremental reads. The page is unclear on the default `state`, so Baluarte always sends `state=all`. *Source: Issues* |
| Pull requests, reviews | — | repository `pull_requests` read | **To verify** in M13.4 |
| Workflow runs | — | repository `actions` read | **To verify** in M13.4 |
| Deployments, releases | — | repository `deployments` read; releases **to verify** | **To verify** in M13.4 |
| Secret scanning alerts | `GET /repos/{owner}/{repo}/secret-scanning/alerts` | Repository or organization administrator; App `secret_scanning_alerts` | Gives 404 when the repository is public or secret scanning is off. **To verify:** licensing for private repositories. *Source: Secret scanning* |
| Code scanning alerts | `GET /repos/{owner}/{repo}/code-scanning/alerts` | App `security_events` | Gives 403 when GitHub Advanced Security is not enabled for the repository. *Source: Code scanning* |

A 403 or 404 caused by plan or licensing is recorded as a capability (`unsupported`), never as a pass. A refusal Baluarte cannot explain is `unknown`.

## Rule types

The current rule types are GitLab's. During M13.3 each one declares the providers it supports, and GitHub equivalents are added with their own names where the concepts differ.

| GitLab rule type | GitHub equivalent | Basis |
|---|---|---|
| `gitlab.project.visibility` | Repository `visibility` | **To verify** the field in M13.2 |
| `gitlab.branch.default_protected` | Default branch protected by branch protection or by a ruleset rule | Protected branches and Rules pages. `unsupported` for private repositories on GitHub Free |
| `gitlab.project.merge_request_approvals` | `required_approving_review_count` from branch protection, or the `pull_request` rule's parameters in the branch's rules | Protected branches. The rule parameters are **to verify** |
| `gitlab.group.two_factor_required` | Organization `two_factor_requirement_enabled` | Organizations. Owner visibility is **to verify**; null gives `unknown` |
| `gitlab.project.privileged_members` | Collaborators with admin or maintain permission | **To verify** |
| `gitlab.project.access_tokens` | Fine-grained tokens with access to the organization | **To verify**; `unsupported` until confirmed |
| `gitlab.security_policy.scan_execution` | Secret scanning and code scanning enabled | Secret and Code scanning pages; GitHub Advanced Security licensing applies |
| `gitlab.security_policy.approval` | None | GitLab only |
| `gitlab.change.reviewed`, `gitlab.change.pipeline_passed`, `gitlab.change.deployed_after_review` | Pull request reviews, workflow runs or checks, deployments | **To verify** in M13.4 |
| `gitlab.labels.consistent` | Repository labels | **To verify** in M13.3 |

## Open questions for M13.2

- A live GitHub organization and test repositories, on GitHub Free and on a paid plan. Plan-dependent behaviour has to be recorded from real responses.
- Where the GitHub App's private key is kept: sealed in `app_secret` like other secrets.
