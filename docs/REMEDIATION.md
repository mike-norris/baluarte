# Remediation plans

Implemented in `baluarte.remediation.plans` and `/api/v1/remediation/plans` (BAL-031). **A plan is a dry run: it describes the GitLab changes that would fix open findings, and nothing in Baluarte sends them.** Baluarte still makes only `GET` requests to GitLab with a `read_api` token ([ADR-0003](architecture/ADR-0003-read-only-v0-1.md)). Executing plans from Baluarte (BAL-032) is deferred past v0.1: in v0.1 an approved plan is applied by hand (see [Applying an approved plan](#applying-an-approved-plan-in-v01)).

The design follows the [remediation research](REMEDIATION_RESEARCH.md) and [ADR-0007](architecture/ADR-0007-remediation-constraints.md).

## What gets planned

A plan covers a scope of findings: explicit `finding_ids`, or every unresolved finding matching `policy`, `rule` and `project_gitlab_id`. A scope holds at most 100 findings.

A finding becomes a step only when all of these hold. Otherwise it is listed under `skipped` with a `reason_code`:

| Condition | Skip reason |
|---|---|
| The finding exists for this instance | `not_found` |
| It is `open`, not `resolved` or `excepted` | `resolved`, `excepted` |
| Its latest result is `fail`; `unknown` and `unsupported` are never planned | `latest_unknown`, `latest_unsupported`, ... |
| The project is `active` in the inventory and not archived | `project_missing`, `project_inaccessible`, `project_archived` |
| The failure has evidence that was read successfully | `evidence_missing` |
| The evidence still matches its SHA-256 digest | `evidence_digest_mismatch` |
| The evidence is still the latest of its kind for the project | `evidence_not_latest` |
| An allowlisted action fixes it without widening access | `would_widen_access`, `not_required`, `branch_changed`, `value_unknown` |

Plans are built from stored evidence only, never from a call to GitLab. Creating or approving a plan also needs the projects collection to be `fresh` or `partial`; when it is `failed`, `stale` or `never`, both are refused (409).

## Actions

Only these two actions exist:

| Action | Request | Chosen value | Needs |
|---|---|---|---|
| `restrict_project_visibility` | `PUT /projects/:id` with `{"visibility": ...}` | The least exposed level the rule allows. A change that would make the project more visible is skipped | Owner |
| `protect_default_branch` | `POST /projects/:id/protected_branches` with the branch name and `push_access_level`, `merge_access_level` and `unprotect_access_level` all 40 (Maintainer), stated explicitly | GitLab's own default for a new protection | Maintainer expected (UNVERIFIED) |

Excluded, and recorded in every plan under `excluded_actions`:
- **Group visibility:** lowering it needs every subgroup and project changed first, bottom up.
- **Unprotecting, or deleting and recreating, a protection:** the branch would be unprotected between the calls.
- **Approval rules:** a repeat `POST` duplicates a rule, and `PUT` drops approvers it doesn't list.
- **Push rules:** `DELETE` removes all of a project's push rules.

## A step

Each step records:
- **Target:** the project's GitLab ID and path.
- **Provenance:** the finding, evaluation and observation IDs, the observation's source, digest and time, and the policy name, version and rule.
- **Values:** `current`, `intended` and the rule's `expected` value.
- **`request`:** the exact intended call: method, API-relative path and body.
- **`verify`:** the read that should show the intended value afterwards.
- **`preconditions`:** what must still be true when executing. For example: visibility is still `public`; the default branch is still `main` and no rule, wildcards included, protects it.
- **`effects`:** the blast radius from the research. For example: non-members lose access at once; Developers can no longer push directly; on Free, GitLab keeps no audit record of the change.
- **`idempotency_key`:** the same intended change on the same project always gets the same key, so execution can deduplicate it.

`GET /api/v1/remediation/plans/{id}` also returns `summary`, one diff-style line per step and skip, for example:

```
~ platform/api: visibility public -> private (PUT /projects/42; finding 7)
= platform/legacy: skipped (latest_unknown): The latest result is unknown, not fail; ...
```

## Lifecycle and approval

| From | To | Who |
|---|---|---|
| (new) | `draft` | `remediation:plan` (admin, analyst) |
| `draft` | `submitted` | The plan's creator |
| `submitted` | `approved` | `remediation:approve` (admin), not the creator, naming the `plan_digest` they reviewed |
| `submitted` | `rejected` | `remediation:approve`, not the creator, with a note |
| `draft`, `submitted`, `approved` | `expired` | At `expires_at`, at most 72 hours after creation (default 24) |

- **Immutable:** the plan document and `plan_digest` (SHA-256 of its canonical JSON) never change. A trigger rejects any other change and `DELETE`, and CHECK constraints require the approver to differ from the creator and `approved_digest` to equal `plan_digest`.
- **Staleness:** approval is refused (409, with reasons) when the plan no longer matches what it was built from:
  - a finding is no longer open and failing
  - it was judged again on newer evidence
  - newer evidence exists for the project
  - the stored plan no longer matches its digest

  `stale_reasons` on a draft or submitted plan shows this before anyone tries to approve. A stale plan can still be rejected; create a new one instead.
- **Expiry:** expiry takes effect at `expires_at` in every response, and `expire_due` marks rows `expired` when a plan is created.
- **Audit:** every transition is audited as `remediation_plan.created`, `.submitted`, `.approved`, `.rejected` or `.expired`, with the plan digest.
- **Approval changes nothing in GitLab.** In v0.1 someone applies an approved plan by hand; see below.

## Applying an approved plan in v0.1

Baluarte does not execute plans in v0.1. An approved plan is a reviewed, audited instruction list:

1. **Who applies it:** someone with the GitLab role each step names (the plan records the role and tier), using their own GitLab account, in the GitLab UI or with their own token. Baluarte's `read_api` token is never used for this.
2. **Before each step:** check its `preconditions` in GitLab. If one no longer holds, skip the step: the plan is out of date. Make each change exactly as the step's request describes, no wider.
3. **Afterwards:** run project discovery, which queues an evaluation (or wait for the next scheduled collection). A fixed finding resolves only when fresh evidence shows the expected value, so verification goes through the normal read path.
4. **Records:** the approval is in Baluarte's audit log as `remediation_plan.approved`, with the digest. The change itself is recorded by GitLab only on Premium and Ultimate. On Free, keep your own record, for example a ticket that names the plan ID and digest.

Apply a plan before it expires (24 hours by default, at most 72). After that, create a new plan from current evidence.

## Guarantees and their tests

All in `backend/tests/test_remediation_plans.py`.

| Guarantee | Test |
|---|---|
| No code path in planning or its API makes an HTTP request. Every test runs with the real network transport replaced by one that fails the test | `no_http` fixture, `test_the_no_http_guard_catches_a_gitlab_call`, `test_create_submit_and_approve_over_the_api` |
| Planning code uses no HTTP client, and the GitLab client can only send `GET` | `test_planning_code_uses_no_http_client`, `test_the_gitlab_client_can_only_send_get` |
| There is no execution endpoint | `test_there_is_no_execution_endpoint` |
| `unknown`, `excepted` and resolved findings are never planned | `test_unknown_excepted_and_resolved_findings_are_never_planned` |
| No change widens access | `test_a_change_that_would_widen_access_is_skipped` |
| Tampered or superseded evidence is never planned | `test_tampered_evidence_is_never_planned`, `test_a_failure_judged_on_superseded_evidence_is_skipped` |
| Plans can't be changed or deleted, and approval is bound to the digest | `test_a_plan_cannot_be_changed_or_deleted`, `test_the_database_requires_another_approver_and_a_bound_digest`, `test_approval_must_name_the_reviewed_digest` |
| Separation of duties | `test_only_the_creator_submits_and_never_approves_their_own_plan`, `test_reject_needs_a_note_and_someone_else` |
| Stale plans and stale collections are not approved | `test_a_stale_plan_cannot_be_approved`, `test_newer_evidence_makes_a_plan_stale`, `test_plans_need_a_current_projects_collection` |
| Expiry | `test_expiry_takes_effect_at_expires_at_and_is_audited` |

## Not yet

- **Execution from Baluarte (BAL-032), deferred past v0.1:**
  - a separate write credential, mounted only into the worker that executes
  - re-checking `preconditions` with a fresh read
  - one request per step, with no blind retries of writes
  - verifying each step through the read path
  - auditing every request and result
- **Rollback plans:** each step records the prior value (`current`) so a reverse plan can be generated later.
- **More actions:** for example tightening an existing protection with `PATCH`.
