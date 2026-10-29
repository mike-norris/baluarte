# GitLab remediation API research (BAL-030)

Research input for milestone M6 (BAL-031 dry-run plans and approvals, BAL-032 idempotent remediation with verification). **v0.1 stays read-only ([ADR-0003](architecture/ADR-0003-read-only-v0-1.md)); nothing here changes that.** The design constraints that follow from this research are proposed in [ADR-0007](architecture/ADR-0007-remediation-constraints.md).

**Method and limits:**
- Every statement comes from GitLab's published documentation, read on 2026-10-29, and is cited. GitLab issue reports are used only where the documentation is silent, and are marked as issue reports.
- Statements that could not be confirmed are marked **UNVERIFIED**.
- **Nothing was tested against a live GitLab instance.** Behaviour must be confirmed per version and tier before any write path ships; see [Open questions](#open-questions).

## Token scope: the core trade-off

| Scope | Documented meaning |
|---|---|
| `read_api` (what Baluarte uses today) | "Grants read access to the API for the token's scope." |
| `api` | "Grants complete read and write access to the API for the token's scope." |
| `write_repository` | Git push over HTTP. It "Does not support API authentication." |

Source: [Access token scopes](https://docs.gitlab.com/security/tokens/access_token_scopes/)

**No classic scope narrower than `api` grants REST write access to project settings or protected branches** ([source](https://docs.gitlab.com/security/tokens/access_token_scopes/)). Any remediation with a classic token therefore needs a full read-write API token, bounded only by the token user's role. For contrast, `read_api` cannot change anything.

**Fine-grained personal access tokens** are documented as follows ([source](https://docs.gitlab.com/auth/tokens/fine_grained_access_tokens/)):
- Generally available in GitLab 19.2 (beta in 18.10), in all tiers, on Self-Managed.
- Scoped to resources and actions, and limited to specific groups or projects.
- Each request must pass both the token's permission and the owner's own role.

Whether they cover editing project visibility or protected branches is **UNVERIFIED**: the per-endpoint permission lists were not part of the page reviewed. If they do, they are the preferred write credential on 19.2+.

**Project and group access tokens** create a bot user per token, need Maintainer or Owner to create, and have a required expiry. The default maximum lifetime is 365 days, and administrators can change it from 17.6. They are available with any licence on Self-Managed ([source](https://docs.gitlab.com/user/project/settings/project_access_tokens/)). Whether a project access token can hold the Owner role, which project visibility changes need, is **UNVERIFIED** (the page does not list the roles).

## Capability matrix

| Action | Write request | Read-back | Role needed | Tier | Repeat-safe? | GitLab audit event |
|---|---|---|---|---|---|---|
| Set project visibility | `PUT /projects/:id` with `visibility` ([Projects API](https://docs.gitlab.com/api/projects/)) | `GET /projects/:id` (`visibility`) | Owner ([Project visibility](https://docs.gitlab.com/user/public_access/)) | All | Yes in principle: PUT sets a value. Repeating it unchanged is **UNVERIFIED** for side effects | `project_visibility_level_updated` (15.7, Premium/Ultimate) ([audit types](https://docs.gitlab.com/user/compliance/audit_event_types/)) |
| Set group visibility | `PUT /groups/:id` with `visibility` ([Groups API](https://docs.gitlab.com/api/groups/)) | `GET /groups/:id` | Owner or administrator ([Groups API](https://docs.gitlab.com/api/groups/)) | All | Yes in principle (PUT) | None found in the audit event list (**UNVERIFIED** that none exists) |
| Protect the default branch | `POST /projects/:id/protected_branches` (201) ([Protected branches API](https://docs.gitlab.com/api/protected_branches/)) | `GET /projects/:id/protected_branches/:name` | Not stated in the API page (**UNVERIFIED**; Maintainer expected) | All. `allowed_to_*` user/group/access-level entries are Premium/Ultimate; `code_owner_approval_required` is Premium/Ultimate | **No.** Not documented. Issue reports say a repeat POST returns `409 Protected branch '…' already exists` ([#241605](https://gitlab.com/gitlab-org/gitlab/-/issues/241605), [#37315](https://gitlab.com/gitlab-org/gitlab-foss/-/issues/37315)) | `protected_branch_created` (15.2) |
| Change existing protection | `PATCH /projects/:id/protected_branches/:name` (200) | as above | Callers changing `allowed_to_unprotect` must themselves be in it ([source](https://docs.gitlab.com/api/protected_branches/)) | As above | Mostly. Access-level records are edited by `id`, and unknown ids give 404. An issue report notes PATCH can produce conflicting push settings ([#551595](https://gitlab.com/gitlab-org/gitlab/-/issues/551595)) | `protected_branch_updated` (15.8) |
| Unprotect | `DELETE /projects/:id/protected_branches/:name` (204) | GET returns 404 afterwards (**UNVERIFIED**) | Must be in `allowed_to_unprotect` | All | Behaviour when already unprotected is not documented | `protected_branch_removed` (15.2) |
| Project approval configuration | `POST /projects/:id/approvals` ([MR approvals API](https://docs.gitlab.com/api/merge_request_approvals/)) | `GET /projects/:id/approvals` | Not named. The caller "must be an eligible approver" for configuration updates | Premium/Ultimate | POST updates the configuration (**UNVERIFIED** as fully idempotent) | `update_approval_rules` (15.2) |
| Approval rules | `POST /projects/:id/approval_rules`, `PUT …/:approval_rule_id`, `DELETE …/:approval_rule_id` | `GET /projects/:id/approval_rules[/:id]` | Not named | Premium/Ultimate | POST creates a new rule each time (**not** repeat-safe). PUT "removes any approvers and groups not defined" in the request | `approval_rule_created` (15.2), `approval_rule_deleted` (14.9) |
| Push rules | `POST` / `PUT` / `DELETE /projects/:id/push_rule` ([Push rules API](https://docs.gitlab.com/api/project_push_rules/)) | `GET /projects/:id/push_rule`. The literal string `"null"` means none are configured | Not stated | Premium/Ultimate | PUT updates. POST when rules exist is not documented. DELETE removes **all** push rules | `project_push_rules_*_updated` (16.5–18.6, per attribute) |

Deprecated fields to avoid:
- `approvals_before_merge` (deprecated in 12.3; use approval rules).
- `require_password_to_approve` (deprecated in 16.9; use `require_reauthentication_to_approve` from 17.1) ([source](https://docs.gitlab.com/api/merge_request_approvals/)).

## Safety notes per action

**Project visibility**
- A project "must be at least as restrictive as the visibility of its parent group", and a fork at least as restrictive as its upstream ([source](https://docs.gitlab.com/user/public_access/)). Making a project *more* restrictive is always allowed by this rule. Making it less restrictive can be refused.
- Administrators can restrict which levels may be chosen ([source](https://docs.gitlab.com/user/public_access/)).
- **Blast radius:** lowering visibility immediately removes clone and view access for non-members. Internal projects become unreachable to authenticated non-members, and public ones to anonymous users (and pipelines or integrations relying on them).
- This is the safest direction for Baluarte's only allowed value set (`allowed: [private]` restricts, never widens), but it can still break consumers.
- **Needs:** the Owner role, which is a large privilege for a service identity.

**Group visibility**
- Lowering is refused while any subgroup or project is more visible: "you cannot set a group to private if a project or subgroup in that group is public" ([source](https://docs.gitlab.com/user/public_access/)).
- A remediation would therefore have to change descendants first, bottom-up. That is a multi-step plan with a wide blast radius. **Recommend excluding it from automated remediation** and leaving it as a manual action.

**Protected default branch**
- **Ordering:** protect with POST if absent, or update with PATCH if present. Never use DELETE-then-POST to "replace" a protection: between the two calls the branch is unprotected, and pushes or force-pushes can land.
- **Defaults:** POST without `push_access_level`, `merge_access_level` or `unprotect_access_level` uses `40` (Maintainer) for each ([source](https://docs.gitlab.com/api/protected_branches/)). That may be stricter than the team's practice (for example, Developers merging), so the plan must show the effective levels.
- **Existing branch:** POST on an already-protected name is reported to fail with 409 rather than overwrite (issue reports above). Treat 409 as "exists, read it back and compare".
- **Wildcards:** a wildcard protection (`release/*`) can already cover the default branch. The read path must evaluate wildcards before deciding a protection is missing (**UNVERIFIED** whether `GET …/protected_branches/:name` resolves wildcards for a concrete name).

**Approval rules**
- **Tier:** Premium/Ultimate only.
- **Not repeat-safe:** POST creates a duplicate rule on repeat, so creation needs a read-before-write match on rule name and type.
- **PUT replaces:** PUT drops approvers not listed, so updates must send the full intended set.

**Push rules**
- **Tier:** Premium/Ultimate only.
- **DELETE wipes everything:** it removes all of a project's push rules, so it must never be used to change one attribute.
- **Null fields:** several boolean fields read back as `null` when disabled, so verification must treat `null` as `false` ([source](https://docs.gitlab.com/api/project_push_rules/)).

**Rate limits**
- The authenticated API limit on Self-Managed is off by default. When an administrator enables it, the default is 7200 requests per user per 3600 seconds ([source](https://docs.gitlab.com/administration/settings/user_and_ip_rate_limits/)).
- Throttled requests return `429` with `Retry-After`, and every response carries `RateLimit-*` headers ([source](https://docs.gitlab.com/administration/settings/user_and_ip_rate_limits/)).
- The existing client already retries 429 and honours `Retry-After`. Writes must not be retried blindly: see the constraints below.

**GitLab-side audit trail**
- Audit events are stored in Premium and Ultimate; streaming requires Ultimate ([source](https://docs.gitlab.com/user/compliance/audit_event_types/)).
- On Free there is no GitLab-side audit record of a Baluarte change, so Baluarte's own audit trail is the only record.
- No group visibility event was found.

## Recommended constraints for BAL-031/032

Proposed formally in [ADR-0007](architecture/ADR-0007-remediation-constraints.md):

1. **Never automatic.** Every change comes from a plan approved by a person. Evaluations never trigger writes.
2. **Dry-run plan first.** A plan is computed from a fresh read, not stored data. It records, per target:
   - current and intended values
   - the exact request (method, path, body)
   - expected side effects (for example, "non-members lose clone access")
   - prerequisites (role, tier)

   Plans expire, and a plan whose read-back no longer matches at execution time is refused.
3. **Explicit approval with separation of duties.** The approver is an admin who is not the requester (or a configurable two-person rule). Approval is audited and bound to the plan's digest.
4. **Separate write credential.** A separate token, secret file and setting from the read token, mounted only into the worker that executes approved plans, and absent unless remediation is enabled.
   - Prefer a fine-grained token limited to the needed actions and projects (if it covers them; UNVERIFIED), else a project or group access token on a bot user.
   - The read path keeps `read_api`.
5. **Per-action allowlist.** Only listed actions can execute. This starts with "restrict project visibility" and "protect or tighten default-branch protection". Group visibility, DELETE operations, approval-rule PUT and push-rule DELETE are excluded.
6. **Idempotent execution.**
   - Each plan step has an idempotency key and is a no-op if the read-back already matches.
   - Use set-style requests (PUT, PATCH).
   - A POST that answers 409 is resolved by reading back and comparing, never by deleting and recreating.
   - Retry only reads; a failed write is re-planned, not replayed.
7. **Verify after every write via the read path.** A step succeeds only when a subsequent read with the read token shows the intended value. Otherwise it fails with the observed value recorded.
8. **Audit both sides.**
   - Baluarte records plan, approval, each request and response status, and the verification result in `audit_event`.
   - GitLab's own events (Premium/Ultimate) are referenced where available.
   - On Free, the docs must state that GitLab keeps no record.
9. **Bounded blast radius.** Cap targets per plan, execute sequentially, stop on the first unexpected result, and never widen access (no change may make anything more visible or less protected).
10. **Rollback is a new plan.** Each step records the prior value, so a reverse plan can be generated and approved like any other. Nothing reverts automatically.

## Open questions

These need tests against real GitLab versions and tiers (proposed: the latest two minor releases of the two most recent majors, in CE and EE without a licence, Premium and Ultimate):

1. Do fine-grained tokens (19.2+) cover `PUT /projects/:id` visibility and protected-branch POST/PATCH, and with what permission names?
2. Can a project or group access token hold the Owner role needed for visibility changes? If not, what is the least-privileged identity that can?
3. What is the exact status and body of POST `protected_branches` for an existing name on current versions (409?), and of DELETE for an unprotected name?
4. Does `GET /projects/:id/protected_branches/:name` match a concrete branch against wildcard rules, or only exact names?
5. Does a `PUT /projects/:id` that repeats the current visibility emit an audit event or other side effects?
6. What error does `PUT /projects/:id` return when the requested visibility is restricted by an administrator or exceeds the parent group's?
7. Which role is required for protected-branch, approval-rule and push-rule writes? The API pages don't state it.
8. Does `POST /projects/:id/push_rule` fail or overwrite when rules already exist?
9. Is there a group visibility audit event under another name?
