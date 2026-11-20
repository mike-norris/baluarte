# Remediation plans

Implemented in `baluarte.remediation` and `/api/v1/remediation/plans` (BAL-031, BAL-032). **A plan describes the GitLab changes that would fix open findings. Creating, submitting and approving it change nothing in GitLab.** An approved plan changes GitLab only when someone applies it ([Applying a plan](#applying-a-plan)). Applying needs `BALUARTE_REMEDIATION_WRITES=true` and a remediation write token, separate from the read-only token. Without them, Baluarte makes only `GET` requests to GitLab, and approved plans are [applied by hand](#applying-a-plan-by-hand).

The design follows the [remediation research](REMEDIATION_RESEARCH.md), [ADR-0007](architecture/ADR-0007-remediation-constraints.md) and [ADR-0012](architecture/ADR-0012-remediation-writes.md).

## What gets planned

A plan covers a scope of findings: explicit `finding_ids`, or every unresolved finding matching `policy`, `rule` and `project_gitlab_id`. A scope holds at most 100 findings.

A finding becomes a step only when all of these hold. Otherwise it is listed under `skipped` with a `reason_code`:

| Condition | Skip reason |
|---|---|
| The finding exists for this instance | `not_found` |
| It is `open`, not `resolved` or `excepted` | `resolved`, `excepted` |
| Its latest result is `fail`; `unknown` and `unsupported` are never planned | `latest_unknown`, `latest_unsupported`, ... |
| The project is `active` in the inventory and not archived | `project_missing`, `project_inaccessible`, `project_archived` |
| The project is in monitoring scope ([MONITORING_SCOPE.md](MONITORING_SCOPE.md)) | `project_excluded` |
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
| `approved` | `applying` | `remediation:plan`, before `expires_at` ([Applying a plan](#applying-a-plan)) |
| `applying` | `applied` or `failed` | The worker |

- **Immutable:** the plan document, `plan_digest` (SHA-256 of its canonical JSON) and the decision never change. A trigger allows only the transitions above, and progress updates while `applying`. It rejects `DELETE`. CHECK constraints require the approver to differ from the creator and `approved_digest` to equal `plan_digest`.
- **Staleness:** approval is refused (409, with reasons) when the plan no longer matches what it was built from:
  - a finding is no longer open and failing
  - it was judged again on newer evidence
  - newer evidence exists for the project
  - the stored plan no longer matches its digest

  `stale_reasons` on a draft or submitted plan shows this before anyone tries to approve. A stale plan can still be rejected; create a new one instead.
- **Expiry:** expiry takes effect at `expires_at` in every response, and `expire_due` marks rows `expired` when a plan is created.
- **Audit:** every transition is audited as `remediation_plan.created`, `.submitted`, `.approved`, `.rejected`, `.expired`, `.applying`, `.applied` or `.failed`, with the plan digest.
- **Approval changes nothing in GitLab.** Applying is a separate step, below.

## Applying a plan

`POST /api/v1/remediation/plans/{id}/apply` (`remediation:plan`) marks an approved plan `applying` and queues the `gitlab.apply_remediation_plan` job. The plan's `can_apply` lists, before anyone tries, every reason it would be refused now (409, `reasons`):

| Reason | Meaning |
|---|---|
| `remediation_writes_disabled` | `BALUARTE_REMEDIATION_WRITES` is not `true` for this deployment |
| `write_token_missing` | No remediation write token is stored |
| `not_approved`, `expired` | Only an approved, unexpired plan is applied |
| `stale` | The plan no longer matches its findings and evidence (`stale` lists why) |
| `projects_not_current` | The projects collection is not `fresh` or `partial` |
| `already_applying` | Another plan is applying a step with the same idempotency key |

The job applies the steps in order. For each step:

1. **Read** the project with the read-only token: `GET /projects/:id`, and for a protection `GET /projects/:id/protected_branches`, with wildcards matched in Baluarte.
2. **Already as intended:** `skipped`, nothing written. Applying the same change twice, from one plan or two, writes it once.
3. **Out of date:** a precondition no longer holds, so the step is `out_of_date` and nothing is written. Examples: the visibility is no longer the planned `current` value, the default branch changed, a rule now covers the branch, or the project is archived. The next step still runs; a new plan from fresh evidence decides what is still needed.
4. **Write once** with the remediation write token. It is never retried, whatever GitLab answers.
5. **Verify** by reading again with the read-only token. The step is `applied` only when that read shows the intended value: the visibility, or a rule named exactly the branch with push and merge (and unprotect, when GitLab reports it) at Maintainer only. A refused write, such as a 409 because someone protected the branch meanwhile, is resolved by this read. It is never resolved by deleting and recreating anything.

The plan stops at the first `failed` step: a project that cannot be read, or a write that did not take effect. Later steps are `not_run`, and `failed_step` and `failure` say where and why. A failed plan is not applied again; build a new one. `progress` holds each step's status, the write's HTTP status, whether the read-back verified it, and what that read showed.

Afterwards project discovery is queued. A finding resolves only when fresh evidence shows the expected value, never because a write succeeded.

Audit events: `remediation_plan.applying` (with the idempotency keys), `remediation_plan.write` per request (method, path, status, step and key), `remediation_plan.step` per step (status, HTTP status, verification and what was read), then `remediation_plan.applied` or `.failed` with the count of steps by status.

### The write token

An administrator enters it under Settings, Remediation write token (`/api/v1/settings/remediation-write-token`). It is sealed like the other secrets, never returned or logged, and checked with `GET /personal_access_tokens/self`. Setting, replacing and removing it are audited as `settings.remediation_write_token_*`. It is separate from the [label write token](LABELS.md#the-write-token): the label switches and token do not allow remediation.

Use an identity with the least access that can make these changes:
- the `api` scope (no narrower classic scope allows these writes);
- Owner on the projects whose visibility it restricts, and Maintainer for protections (UNVERIFIED; see the research);
- limited to the groups it should fix, with an expiry date.

A role or tier GitLab refuses shows as a failed step with GitLab's status.

## Applying a plan by hand

Without the switch or the token, an approved plan is a reviewed, audited instruction list:

1. **Who applies it:** someone with the GitLab role each step names (the plan records the role and tier), using their own GitLab account, in the GitLab UI or with their own token. Baluarte's `read_api` token is never used for this.
2. **Before each step:** check its `preconditions` in GitLab. If one no longer holds, skip the step: the plan is out of date. Make each change exactly as the step's request describes, no wider.
3. **Afterwards:** run project discovery, which queues an evaluation (or wait for the next scheduled collection). A fixed finding resolves only when fresh evidence shows the expected value, so verification goes through the normal read path.
4. **Records:** the approval is in Baluarte's audit log as `remediation_plan.approved`, with the digest. The change itself is recorded by GitLab only on Premium and Ultimate. On Free, keep your own record, for example a ticket that names the plan ID and digest.

Apply a plan before it expires (24 hours by default, at most 72). After that, create a new plan from current evidence.

## Applying: guarantees and their tests

All in `backend/tests/test_remediation_execution.py`, against a fake GitLab.

| Guarantee | Test |
|---|---|
| Read with the read token, written once with the write token, read back with the read token; findings resolve only through fresh evidence | `test_a_public_project_is_restricted_and_verified`, `test_an_unprotected_default_branch_is_protected_and_verified` |
| A change already made is skipped without a write | `test_a_change_already_made_is_skipped_without_writing`, `test_two_plans_never_apply_the_same_change_at_once` |
| A changed project is left alone and the rest still run | `test_an_out_of_date_step_is_not_written_and_the_rest_still_run` |
| A refused write is sent once, never retried, and stops the plan | `test_a_refused_write_is_sent_once_and_stops_the_plan` |
| A conflict is resolved by reading back | `test_a_conflict_is_resolved_by_reading_back` |
| A write that does not read back fails | `test_a_write_that_does_not_read_back_fails`, `test_an_unreadable_project_fails_without_writing` |
| Both switches are needed, the label token does not count, and turning writes off stops a queued plan | `test_applying_needs_the_switch_the_token_and_an_approved_plan`, `test_turning_writes_off_before_the_job_runs_fails_the_plan` |
| Stale plans and stale collections are not applied | `test_a_stale_plan_or_stale_collection_is_not_applied` |
| Only planners apply; only administrators manage the token, which is sealed, audited and never shown | `test_only_remediation_planners_apply`, `test_the_write_token_is_sealed_checked_audited_and_never_shown` |
| The writer can only restrict visibility and protect a branch | `test_the_writer_can_only_restrict_visibility_and_protect_a_branch`, `test_only_an_exact_maintainer_rule_counts_as_protected_as_intended` |
| The database keeps applying records | `test_the_database_keeps_applying_records` |

## Planning: guarantees and their tests

All in `backend/tests/test_remediation_plans.py`.

| Guarantee | Test |
|---|---|
| No code path in planning or its API makes an HTTP request. Every test runs with the real network transport replaced by one that fails the test | `no_http` fixture, `test_the_no_http_guard_catches_a_gitlab_call`, `test_create_submit_and_approve_over_the_api` |
| Planning code uses no HTTP client, and the GitLab client can only send `GET` | `test_planning_code_uses_no_http_client`, `test_the_gitlab_client_can_only_send_get` |
| Applying is the only endpoint that leads to a write | `test_the_only_remediation_endpoints` |
| `unknown`, `excepted` and resolved findings are never planned | `test_unknown_excepted_and_resolved_findings_are_never_planned` |
| No change widens access | `test_a_change_that_would_widen_access_is_skipped` |
| Tampered or superseded evidence is never planned | `test_tampered_evidence_is_never_planned`, `test_a_failure_judged_on_superseded_evidence_is_skipped` |
| Plans can't be changed or deleted, and approval is bound to the digest | `test_a_plan_cannot_be_changed_or_deleted`, `test_the_database_requires_another_approver_and_a_bound_digest`, `test_approval_must_name_the_reviewed_digest` |
| Separation of duties | `test_only_the_creator_submits_and_never_approves_their_own_plan`, `test_reject_needs_a_note_and_someone_else` |
| Stale plans and stale collections are not approved | `test_a_stale_plan_cannot_be_approved`, `test_newer_evidence_makes_a_plan_stale`, `test_plans_need_a_current_projects_collection` |
| Expiry | `test_expiry_takes_effect_at_expires_at_and_is_audited` |

## Not yet

- **Live verification:** the [open questions](REMEDIATION_RESEARCH.md#open-questions) still need tests against real GitLab versions and tiers. Applying is built to fail safe on them (see [ADR-0012](architecture/ADR-0012-remediation-writes.md#consequences)).
- **Rollback plans:** each step records the prior value (`current`) so a reverse plan can be generated later.
- **More actions:** for example tightening an existing protection with `PATCH`.
