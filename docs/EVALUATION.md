# Rule evaluation

How a rule in an effective policy is checked against the GitLab inventory. Implemented in `backend/src/baluarte/policy/evaluation/` (BAL-019, BAL-020, BAL-021). The policy format and how policies are resolved for a project are in [POLICY_ENGINE.md](POLICY_ENGINE.md).

Status:

- **Implemented:** the result model, the shared engine, and the `gitlab.project.visibility` and `gitlab.branch.default_protected` evaluators.
- **Stored and served:** the `policy.evaluate` job stores every result as an immutable `evaluation` row linked to the observation it judged, and follows findings. See [Evaluations and findings](DATA_MODEL.md#evaluations-and-findings) and the findings API in `backend/README.md`.

## Results

Each rule, in each effective policy, for each project produces one result:

| Field | Meaning |
| --- | --- |
| `policy` | The pinned version: `policy_id`, `name`, `version` and `yaml_digest`. |
| `rule_id`, `rule_type`, `severity` | As written in the policy. |
| `target` | `target_type`, `gitlab_id` and `path`. |
| `outcome` | `pass`, `fail`, `unknown`, `unsupported` or `excepted` (see below). |
| `expected` | What the rule requires, e.g. `{"allowed": ["private"]}`. |
| `observed` | The value judged. `null` whenever the outcome is `unknown` or `unsupported`, so an untrusted value is never shown as current. |
| `source` | Where the value is read from, e.g. `gitlab_project.visibility` or `GET /projects/:id/protected_branches`. |
| `observed_at` | When GitLab last confirmed the value (`last_seen_at`); `null` when `observed` is. |
| `evaluated_at` | The database clock at evaluation. |
| `reason` | A human-readable explanation built from fixed text and inventory paths. It never includes credentials or the raw collection error. |
| `unknown_reasons` | Every reason the rule could not be decided, in precedence order. Empty unless `unknown`. |
| `ancestry` | `complete` or `incomplete`, from the policy resolution. |
| `capability` | The GitLab capability probe the rule depends on (`name`, or `null`) and its status: `not_required`, `supported`, `unsupported` or `unknown`. |
| `collection` | The collection the value came from: `kind`, its freshness `status` and `last_success_at`. |

When a result is stored, its row also records the policy version ID and the ID of the observation it judged (BAL-021, BAL-026). A `pass` or `fail` is stored only when that observation backs it; see [Stored results](#stored-results).

## Outcomes

- **`pass` and `fail`:** only when the value can be trusted. See the next section.
- **`unknown`:** the rule could not be decided. It is never compliant.
- **`unsupported`:** the GitLab instance lacks a capability the rule needs.
- **`excepted`:** a `fail` waived by an approved, unexpired exception (BAL-033). It is applied when the result is stored, after the evidence check, so only a backed failure can be waived; `unknown` and `unsupported` never become `excepted`. See [POLICY_ENGINE.md](POLICY_ENGINE.md#exceptions).

## Deciding a rule

Every rule type goes through the same engine (`engine.evaluate_rule`):

1. **Collect every unknown reason.** If any reason in the target or collection group (rows 1 to 9 below) applies, the result is `unknown`, whatever the value says. This holds even when the stored value would pass.
2. **Check the capability.** If the rule needs a capability that the instance does not support, the result is `unsupported`.
3. **Check what remains.** An undetected capability, or a value GitLab did not report, makes the result `unknown`. So does a part of the value that only this rule checks, such as an approval setting the rule requires that GitLab left out (the rule type's `undecided` hook, reported as `value_unknown` with its own reason).
4. **Judge the value.** Only now does the rule type decide `pass` or `fail`.

### Unknown reasons and precedence

All reasons that apply are kept in `unknown_reasons`. The first one, in this order, gives the `reason` text:

| # | Reason | When |
| --- | --- | --- |
| 1 | `target_not_in_inventory` | The project has not been discovered. |
| 2 | `target_missing` | The last discovery did not find the project: it was deleted, or the token can no longer see it. |
| 3 | `target_inaccessible` | GitLab denied access to the project at the last discovery. |
| 4 | `collection_not_configured` | GitLab is not configured. |
| 5 | `collection_failed` | The latest collection of the value's kind failed. An older success does not count. |
| 6 | `collection_never` | That collection has never completed. |
| 7 | `collection_stale` | Its last success is older than the stale limit: twice the sync interval, or 24 hours when scheduling is off. |
| 8 | `target_collection_error` | The project has a `last_error`: the last collection could not confirm it. |
| 9 | `ancestry_incomplete` | Policy resolution could not follow every group above the project. A policy, or a nearer version overriding one that was found, may be missing. |
| 10 | `capability_unknown` | The rule needs a capability probe that has not run. |
| 11 | `security_policies_unread` | Security policy rules: the project's security policies have never been read, the last read failed (`forbidden`, `not_found`, `failed`), or the rule's policy type was not read (`unknown`). |
| 12 | `security_policy_type_unsupported` | Security policy rules: this GitLab cannot list the rule's policy type. |
| 13 | `security_policy_truncated` | Security policy rules: GitLab listed more policies of the type than were read. |
| 14 | `security_policy_yaml_invalid` | Security policy rules: a policy of the type that reaches the project has YAML the strict loader rejects, or no known `enabled` flag. |
| 15 | `value_unknown` | The project is otherwise fine, but GitLab did not report the value. |
| 16 | `evidence_missing` | Set when storing: no successful observation of the value's source exists for the project. |
| 17 | `evidence_digest_mismatch` | Set when storing: the observation's SHA-256 digest no longer matches its payload, so the row was changed after it was written. |
| 18 | `evidence_mismatch` | Set when storing: the observation's payload does not hold the value judged (the inventory and its evidence disagree). |

The order puts the most fundamental problem first:

- A project that no longer exists makes its data questions moot.
- A failed or stale collection affects every project.
- One project's collection error affects only that project.
- Incomplete ancestry questions which rule applies, not the value.

### Partial collections

A `partial` collection run finished, but some resources could not be confirmed.

- **Confirmed projects:** projects without a `last_error` were confirmed by that run and are evaluated normally.
- **Unconfirmed projects:** a project that could not be confirmed keeps its `last_error` until it is next seen. It is `unknown` with `target_collection_error`, whatever the run's status.

### Collections

Each rule type names the collection its value comes from. Only that collection's freshness matters. A failed group or capability collection does not make visibility results unknown. Ancestry problems are reported separately, through `ancestry_incomplete`.

## `gitlab.project.visibility`

| | |
| --- | --- |
| Value | `gitlab_project.visibility`, from the projects collection. |
| Passes when | The visibility is one of `allowed`. |
| Fails when | The visibility is any other known value. |
| Capability | None. Every supported GitLab version and edition reports project visibility, so this rule is never `unsupported`. |
| Unknown when | Any reason above applies, including a `NULL` visibility (`value_unknown`). |

The rule itself ignores the archived flag, but archived projects are not evaluated at all (see [Archived projects](#archived-projects)).

## `gitlab.branch.default_protected`

| | |
| --- | --- |
| Value | The project's latest default-branch protection observation (source `GET /projects/:id/protected_branches`), read during project discovery. It shares the projects collection's freshness. |
| Passes when | `required: true` and a protected-branch rule covers the default branch. |
| Fails when | `required: true` and GitLab listed the project's protected branches but none covers the default branch. |
| `required: false` | Asserts nothing. It passes whenever the protection is known, recording what was observed, and is `unknown` when it is not. |
| Capability | `protected_branches.read`. |
| Unknown when | Any reason above applies, or (`value_unknown`): |

- **No default branch:** the project is an empty repository, so there is no branch to check. GitLab's protection for the first push is set by instance and group defaults, which are not read. An empty project is not called compliant.
- **Never read:** the project's protection has not been read yet, for example a project discovered before this evaluator existed.
- **Not read:** the last read was denied (`forbidden`), the project was not found (`not_found`), or it failed (`failed`). A read that fails after an earlier success replaces it, so an old "protected" never stands in for a read that did not happen.
- **Branch changed:** the last read was for a branch that is no longer the default. It is read again at the next project discovery.

How protection is read and matched is described in [GITLAB_INTEGRATION.md](GITLAB_INTEGRATION.md#default-branch-protection). In short:

- **Matching:** the default branch is matched against every protected-branch rule, including wildcards (`*`, `release/*`).
- **Recorded:** only the matching rules are kept, with their access-level summary, force push and code owner approval. Whether a branch counts as protected does not depend on those settings.
- **Not judged:** who may push or merge is not judged by this rule.

## Access-control and merge request approval rules

These four rules judge observations recorded after each project discovery ([GITLAB_INTEGRATION.md](GITLAB_INTEGRATION.md#access-controls)). Each rule's value is its observation's whole payload, so the evidence check compares the payload with `observed` and a result is backed by exactly one observation. They share the projects collection's freshness. As audit evidence they speak to change approval (SOC 2 CC8.1, ISO/IEC 27001 A.8.32: merge request approvals), access provisioning and review (CC6.1 to CC6.3, A.5.15 to A.5.18: privileged members, access tokens) and authentication (CC6.1, A.8.5: two-factor); a baseline's own control mapping is the auditor's to make. A read that was denied (`forbidden`), not found or failed, or never happened, is `unknown` (`value_unknown`) with the reason; so is any part a rule needs that GitLab did not report or that cannot be seen.

### `gitlab.project.merge_request_approvals`

| | |
| --- | --- |
| Value | The project's approval settings and approval rules (source `GET /projects/:id/approvals`), with the default branch and whether it is protected. |
| Required approvals | The largest `approvals_required` among `regular` and `any_approver` rules that cover the default branch. A rule with no protected branches covers every branch; `applies_to_all_protected_branches` covers it when it is protected; otherwise one of its protected branch names must match it (wildcards as GitLab matches them). `code_owner` and `report_approver` rules apply only to some changes and never count. |
| Passes when | The required approvals are at least `min_approvals`, and each flag set to `true` is met: `forbid_author_approval` by `merge_requests_author_approval: false`, `forbid_committer_approval` by `merge_requests_disable_committers_approval: true`, `reset_approvals_on_push` by `reset_approvals_on_push: true`, `forbid_override` by `disable_overriding_approvers_per_merge_request: true`. |
| Capability | `merge_request_approvals.read`. Project approval rules need GitLab Premium or Ultimate; elsewhere the probe does not answer and the rule is `unknown` (`capability_unknown`). |
| Unknown when | The project has no default branch; the approvals were read for a different default branch; which rules cover the branch is not known (an unknown rule type, a missing count, or `applies_to_all_protected_branches` while the branch's protection could not be read) and `min_approvals` is above 0; or a setting a flag requires was not reported. |

Approval policies (`gitlab.security_policy.approval`) are judged separately: a project can meet the policy rule while its own settings do not, and the reverse.

### `gitlab.project.privileged_members`

| | |
| --- | --- |
| Value | The project's direct and inherited members (source `GET /projects/:id/members/all`): counts by access level and state, and the usernames of members with Maintainer or Owner access and of members whose account is not active. |
| Passes when | At most `max_privileged` members hold Maintainer (40) or Owner (50) access, and, with `forbid_inactive_accounts`, no member's account is in a state other than `active` (for example `blocked` or `deactivated`). |
| Capability | None: every edition lists members. |
| Unknown when | Within the limit but some members' access level was not reported; or, with `forbid_inactive_accounts`, some members' state was not reported and none is known to be inactive. Too many known privileged members fail regardless. |

Bot users of project and group access tokens are members and count like anyone else.

### `gitlab.group.two_factor_required`

| | |
| --- | --- |
| Value | The two-factor settings of each group above the project, top-down, from group discovery (source `GET /groups (two-factor settings of the project's groups)`), whether that chain is complete, and the instance setting when the token has administrator access. |
| Passes when | A group above the project or the instance requires two-factor authentication and, with `max_grace_hours`, the shortest grace period among those that require it is at most that. |
| Fails when | Every group above the project and the instance are seen not to require it, or the grace period is too long and nothing unseen could set a shorter one. |
| Capability | `groups.two_factor` (the group listing carries `require_two_factor_authentication`). |
| Unknown when | Nothing seen requires it, but something cannot be seen: a group above the project the token cannot see (incomplete chain), a group whose setting was not reported, or the instance setting, which needs an administrator token. Without one, a project in no requiring group is `unknown`, never `fail`. |

A personal project has no groups above it, so only the instance setting decides it.

### `gitlab.project.access_tokens`

| | |
| --- | --- |
| Value | The project's active access tokens (source `GET /projects/:id/access_tokens`) with their creation, expiry and last-used dates, and the date they were read (`as_of`). |
| Passes when | Every active token expires no more than `max_lifetime_days` after it was created and, with `max_unused_days`, was used (or, if never used, created) within that many days of `as_of`. A project without active tokens passes. |
| Fails when | A token never expires, lives too long, or has gone unused too long. |
| Capability | None: every edition has project access tokens. Reading them needs the Maintainer role, so with a lower role each project is `unknown` with the 403. |
| Unknown when | A token's creation date was not reported, or `as_of` is missing while `max_unused_days` is set. |

## GitLab security policy rules

`gitlab.security_policy.scan_execution` and `gitlab.security_policy.approval` judge the GitLab security policies that reach a project. They read only the project's latest stored observation from security policy discovery (source `GET /api/graphql project security policies`, see [GITLAB_INTEGRATION.md](GITLAB_INTEGRATION.md#security-policies)) and never call GitLab. Each policy's YAML is parsed with the same strict loader as Baluarte policies (no tags, anchors or duplicate keys, 256 KiB at most).

| | |
| --- | --- |
| Value | A summary rebuilt from the observation: `{"policies": [...], "disabled": n}`, one entry per enabled policy of the rule's type that reaches the project, sorted by source and name. `disabled` counts the disabled ones. |
| Collection | `security_policies`. Its freshness is that of security policy discovery, so a failed or stale discovery makes these rules `unknown`. |
| Capability | `security_policies.read`. Detection never reports it `unsupported`; a type this GitLab cannot list is `unknown` (`security_policy_type_unsupported`), never `unsupported` or `fail`. |
| Unknown when | Any reason above applies, or one of reasons 11 to 14. |

**Which policies reach a project.** Discovery asks GitLab for the project's policies with `includeUnscoped: false`, so GitLab has already applied each policy's `policy_scope`. A policy whose `scope.excluding_projects` still lists the project is skipped as well. A disabled policy never counts. A partial observation (another type or target not read) is still judged: only the rule's own type must have been read in full.

### `gitlab.security_policy.scan_execution`

The summary of each policy holds its `scans` (from `actions[].scan`), `runs_on_default_branch` and `skip_ci_allowed`.

- **Runs on the default branch:** a rule of type `pipeline` or `schedule` with `branch_type` `default`, `protected` or `all`, or with `"*"` in `branches`. `protected` assumes the default branch is protected, which the `gitlab.branch.default_protected` rule checks. Rules that run only for merge requests (`branch_type: target_default`) do not count.
- **`[skip ci]`:** a policy without `skip_ci` can be skipped, as in GitLab. With `allow_skip_ci: false`, only policies with `skip_ci.allowed: false` count.
- **Passes when** every scan in `scans` runs on the default branch in at least one counted policy. Scans may come from different policies.
- **Fails when** no enabled policy reaches the project, or a scan is missing. The reason names the missing scans and the policies that did not count.

### `gitlab.security_policy.approval`

The summary of each policy holds `approvals_required` (the largest `require_approval` action, 0 when none), `every_merge_request`, `prevent_approval_by_author` and `prevent_approval_by_commit_author` (from `approval_settings`).

- **Every merge request:** a rule of type `any_merge_request` that covers protected branches: `branch_type` `protected`, `default` or `all`, or an empty `branches` list. Conditional rules (`scan_finding`, `license_finding`) do not count: they require approval only when a finding matches, so a license approval policy does not satisfy this rule.
- **Passes when** one policy requires approval on every merge request, with at least `min_approvals` approvals, and prevents approval by the author and by committers when `forbid_author_approval` and `forbid_committer_approval` ask for it.
- **Fails when** no enabled policy reaches the project, or none qualifies. The reason says what the policies that reach the project lack.

### Evidence

The stored `observed` value is the summary, so the evidence check rebuilds it from the linked observation's payload. An observation with status `ok` or `partial` can back a result; a payload changed after it was written gives `evidence_digest_mismatch`. A finished security policy discovery (`succeeded` or `partial`) queues one `policy.evaluate`, as a project discovery does.

### Capabilities

The capability status comes from the instance's latest capability snapshot (`capability_statuses`):

- **`supported`:** the probe answered.
- **`unknown`:** every other probe status (`forbidden`, `not_found`, `error`, `skipped`), a missing probe, a missing snapshot, or a failed detection. Detection cannot prove a feature is missing: a 404 may mean a hidden sample project and a 403 a token limit.
- **`unsupported`:** the engine supports it, but no probe produces it today. Protected branches exist in every edition.

## Stored results

A result is decided from the inventory, then checked against its evidence before it is stored (`evaluation/store.py`). A `pass` or `fail` is stored only when the project's latest observation of the value's source:

1. exists and was read successfully (`ok`, with a payload);
2. still matches its SHA-256 digest, recomputed at evaluation time;
3. holds exactly the value judged.

Otherwise the result is stored as `unknown` with reason 12, 13 or 14 above, and changes no finding. A digest failure is also logged as a warning (observation ID and project path only). A row rewritten together with a recomputed digest is not detected here; see the audit limitations in [AUDIT_EVIDENCE.md](AUDIT_EVIDENCE.md).

### Result counts

`GET /api/v1/evaluations/summary?policy=` (`findings:read`) counts results per baseline, version and rule. The counts come from the latest **successful** evaluation run only. A successful run judges every project once per rule in force, so the counts are one consistent snapshot; a run that failed or is still running is not counted, even when it stored some pages.

```text
EvaluationSummary {
  evaluation_run: EvaluationRun | null   # the run counted; null when none has succeeded
  freshness: EvaluationFreshness         # same as /evaluations/freshness
  baselines: [{
    name
    projects: Counts                     # summed over its versions
    versions: [{
      policy_id, version, yaml_digest
      projects: Counts                   # each project once, by its worst result
      rules: [{ rule_id, rule_type, severity, results: Counts }]   # one result per project
    }]
  }]
}
Counts { pass, fail, unknown, unsupported, excepted }
```

- **Worst result:** a project counts as `fail` if any rule failed, then `unknown`, `unsupported`, `excepted`; it counts as `pass` only when every rule passed. `unknown` and `unsupported` are never counted as passing.
- **Versions add up:** one run judges a project under at most one version of each baseline, so a baseline's `projects` is the sum over its versions.
- **Order:** baselines by name, versions newest first, rules by ID.
- Read the counts with `freshness`: counts from an old run are not current compliance.

## Running an evaluation

`evaluation.project` evaluates the policies in force for one project. It only reads:

```python
collections = collection_contexts(db, instance.id, settings)  # once per batch
capabilities = capability_statuses(db, instance.id)  # once per batch
resolution = resolve_project(db, instance, project)
results = evaluate_project(db, project, resolution, collections, capabilities)
```

`capabilities` defaults to the instance's snapshot when omitted. Each project's default-branch protection and security policy observations are read with the project.

- **Which policies:** only the resolution's `effective` policies are evaluated. Overridden and out-of-scope versions are not.
- **Which rules:** a rule is evaluated only if its type has an evaluator. Today that is every rule type in the schema: `gitlab.project.visibility`, `gitlab.branch.default_protected`, `gitlab.security_policy.scan_execution`, `gitlab.security_policy.approval`, `gitlab.project.merge_request_approvals`, `gitlab.project.privileged_members`, `gitlab.group.two_factor_required` and `gitlab.project.access_tokens`. Rules of other types produce no result until their evaluator exists; they never produce a guessed one.
- **Order:** by policy name, then by the rule's position in the policy.

### Archived projects

Archived projects are not evaluated (decided in UAT, 2026-11-01). GitLab makes an archived project read-only, so nobody can act on a baseline result for it, and its findings would only be noise.

- **No results:** the run writes no evaluation rows for an active, archived project, and does not count it in the run's `projects`. The result counts and `GET /gitlab/projects/{id}/evaluations` therefore leave it out.
- **Findings resolve:** its `open` and `excepted` findings resolve with `resolution = project_archived` (an exception's link is cleared, as for `no_longer_applies`). The run counts them under `findings.project_archived`.
- **Unarchived:** the next run judges the project again, and a `fail` reopens the same finding.
- **Missing or inaccessible:** these keep their existing handling. A project that left the inventory with an open finding is still judged `unknown` with `target_missing` or `target_inaccessible`, whatever its last-seen archived flag, so a finding is never closed on stale information.
- **Reads say why:** `GET /gitlab/projects/{gitlab_id}` and `GET /gitlab/projects/{gitlab_id}/effective-policies` return `not_evaluated: "archived"` for such a project (`null` otherwise). The policies still resolve, so the page can show what would apply.

`runner.not_evaluated(project)` is the one place that decides this.

### Projects out of monitoring scope

Projects an organization takes out of monitoring ([MONITORING_SCOPE.md](MONITORING_SCOPE.md)) are not evaluated either. The run writes no results for them and does not count them in `projects`; their `open` and `excepted` findings resolve with `resolution = project_excluded`, counted under `findings.project_excluded`. Result counts and control posture leave them out even before the next run. When an exclusion ends, the next run judges the projects again and a `fail` reopens the same finding. An archived project that is also excluded resolves as `project_archived`.

## Controls

Findings list the framework controls their rule type supports (`controls`), and each control's status is derived from the latest successful run ([CONTROL_MAPPING.md](CONTROL_MAPPING.md#status)). Changing the mapping changes neither stored results nor findings.

## Adding a rule type

A rule type is one `RuleEvaluator`, registered in `EVALUATORS` (`evaluation/project.py`). A new rule type also needs default pairs in `baluarte.controls.catalogue`; a test fails until every rule type supports a control in each framework. It declares:

- the collection its value comes from, and its `source`
- the capability probe it needs, if any
- how to state the expectation, read the value and judge it
- why a missing value is missing, and which unknown reason that is (`value_unknown` unless it says otherwise)
- when its value was observed (the project's `last_seen_at` unless it says otherwise)
- why a known value cannot decide a particular rule (`undecided`), if that can happen

The engine applies the unknown and unsupported handling above, so each rule type decides only `pass` or `fail`.

## Security guarantees and their tests

Baluarte never reports unknown, unsupported or stale state as compliant. The end-to-end tests in `backend/tests/test_outcome_guarantees.py` drive the real jobs (capability detection, group and project discovery with protection reads, and `policy.evaluate`) against a scripted GitLab and a real database, then check stored evaluations, findings, run counts and the API. Time is moved by shifting stored timestamps back against the database clock.

| Guarantee | Test |
| --- | --- |
| A healthy collection decides every rule, and findings open on `fail`. | `test_a_healthy_collection_decides_every_rule` |
| A failed collection (token rejected with 401, or a GitLab error) after a success makes every result `unknown` (`collection_failed`), resolves no finding, and `/gitlab/freshness` reports `failed`. | `test_a_failed_collection_after_a_success_decides_nothing` |
| A token revoked during protection reads leaves protection `unknown` for every project, keeps the branch finding open, stops calling GitLab, and makes the run `partial`. | `test_a_token_revoked_during_protection_reads_leaves_protection_unknown` |
| A denied (403) protection read is `unknown` with its reason, linked to the failed read as evidence. | `test_forbidden_protection_reads_are_unknown_with_their_reason` |
| An undetected capability (probe denied, or detection failed) makes the rules that need it `unknown` (`capability_unknown`). | `test_an_undetected_capability_makes_its_rule_unknown`, `test_a_failed_capability_detection_leaves_every_capability_unknown` |
| An unsupported capability is stored as `unsupported`: never `pass`, and it neither opens nor resolves a finding. No probe reports `unsupported` today, so the test stands in a snapshot that does. | `test_an_unsupported_capability_is_unsupported_not_compliant` |
| A stale collection is `unknown` (`collection_stale`) even when the stored evidence says compliant. An open finding stays open and a resolved one is not reopened. | `test_a_stale_collection_is_unknown_even_when_the_evidence_said_pass` |
| Old evaluation results are reported `stale` by `/evaluations/freshness`. | `test_old_evaluation_results_are_reported_stale` |
| A project a partial discovery could not confirm is `unknown` (`target_collection_error`) and keeps its findings open. | `test_an_unconfirmed_project_in_a_partial_discovery_is_unknown` |
| A missing or inaccessible project is still evaluated while it has open findings, as `unknown`, and they stay open. | `test_a_missing_or_inaccessible_project_keeps_its_findings_open` |
| A gap in the group chain makes the project's rules `unknown` (`ancestry_incomplete`) and resolves nothing. | `test_a_gap_in_the_group_chain_is_unknown` |
| Evidence changed after it was written (digest mismatch) never backs a `pass`, even when the inventory was changed to agree. | `test_tampered_evidence_never_backs_a_pass` |
| An inventory that disagrees with its evidence is `unknown` (`evidence_mismatch`). | `test_an_inventory_that_disagrees_with_its_evidence_is_unknown` |
| A new policy version continues the same finding. | `test_a_new_policy_version_continues_the_same_finding` |
| An exception waives only a backed `fail`: `unknown` (including a stale collection) is never `excepted`, and requested or rejected exceptions waive nothing. | `test_exceptions.py`: `test_unknown_is_never_excepted`, `test_stale_collection_is_not_excepted_but_keeps_a_waived_finding`, `test_only_an_approved_exception_waives` |
| An exception that expired or was revoked waives nothing: the API reads it as ended at once, and the next run reopens or settles its finding. | `test_expiry_ends_the_waiver_on_the_next_run`, `test_an_exception_past_expiry_reads_as_expired_before_the_next_run`, `test_revoking_reopens_the_finding_at_once`, `test_an_unknown_result_after_the_exception_ended_reopens_the_finding` |
| Nobody approves or rejects their own exception. | `test_the_requester_cannot_approve_their_own_request`, `test_the_requester_cannot_decide_their_own_request` |

Where a scenario leaves results undecided, the tests also check that no `pass` appears in the latest results, in the project evaluations API (with `observed` empty), or among findings resolved by a pass, and, where every project is affected, in the run's outcome counts. The unit tests in `test_evaluation_visibility.py`, `test_evaluation_default_branch.py`, `test_evaluation_security_policy.py`, `test_evaluation_access.py`, `test_evaluation_project.py` and `test_evaluation_store.py` cover each reason and transition on its own; `test_evaluation_access_store.py` checks that the access-control results are stored with their evidence.

## Deferred

| Item | Issue |
| --- | --- |
| Exceptions for a group subtree or a whole policy | |
| Executing remediation plans. Plans are built only from `fail` results and never from `unknown`, `unsupported` or `excepted` ones ([REMEDIATION.md](REMEDIATION.md)) | BAL-032 |
| Group-level rules. All current rule types target projects. | |
