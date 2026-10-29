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
- **`excepted`:** reserved for approved exceptions (BAL-033). Nothing produces it yet.

## Deciding a rule

Every rule type goes through the same engine (`engine.evaluate_rule`):

1. **Collect every unknown reason.** If any reason in the target or collection group (rows 1 to 9 below) applies, the result is `unknown`, whatever the value says. This holds even when the stored value would pass.
2. **Check the capability.** If the rule needs a capability that the instance does not support, the result is `unsupported`.
3. **Check what remains.** An undetected capability, or a value GitLab did not report, makes the result `unknown`.
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
| 11 | `value_unknown` | The project is otherwise fine, but GitLab did not report the value. |
| 12 | `evidence_missing` | Set when storing: no successful observation of the value's source exists for the project. |
| 13 | `evidence_digest_mismatch` | Set when storing: the observation's SHA-256 digest no longer matches its payload, so the row was changed after it was written. |
| 14 | `evidence_mismatch` | Set when storing: the observation's payload does not hold the value judged (the inventory and its evidence disagree). |

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

Archived projects are evaluated like any other: an archived public project is still public.

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

## Running an evaluation

`evaluation.project` evaluates the policies in force for one project. It only reads:

```python
collections = collection_contexts(db, instance.id, settings)  # once per batch
capabilities = capability_statuses(db, instance.id)  # once per batch
resolution = resolve_project(db, instance, project)
results = evaluate_project(db, project, resolution, collections, capabilities)
```

`capabilities` defaults to the instance's snapshot when omitted. Each project's default-branch protection observation is read with the project.

- **Which policies:** only the resolution's `effective` policies are evaluated. Overridden and out-of-scope versions are not.
- **Which rules:** a rule is evaluated only if its type has an evaluator. Today that is `gitlab.project.visibility` and `gitlab.branch.default_protected`. Rules of other types produce no result until their evaluator exists; they never produce a guessed one.
- **Order:** by policy name, then by the rule's position in the policy.

## Adding a rule type

A rule type is one `RuleEvaluator`, registered in `EVALUATORS` (`evaluation/project.py`). It declares:

- the collection its value comes from, and its `source`
- the capability probe it needs, if any
- how to state the expectation, read the value and judge it
- why a missing value is missing

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

Where a scenario leaves results undecided, the tests also check that no `pass` appears in the latest results, in the project evaluations API (with `observed` empty), or among findings resolved by a pass, and, where every project is affected, in the run's outcome counts. The unit tests in `test_evaluation_visibility.py`, `test_evaluation_default_branch.py`, `test_evaluation_project.py` and `test_evaluation_store.py` cover each reason and transition on its own.

## Deferred

| Item | Issue |
| --- | --- |
| Exceptions (`excepted`) | BAL-033 |
| Group-level rules. All current rule types target projects. | |
