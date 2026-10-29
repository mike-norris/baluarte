# Rule evaluation

How a rule in an effective policy is checked against the GitLab inventory. Implemented in `backend/src/baluarte/policy/evaluation/` (BAL-019, BAL-020). The policy format and how policies are resolved for a project are in [POLICY_ENGINE.md](POLICY_ENGINE.md).

Status:

- **Implemented:** the result model, the shared engine, and the `gitlab.project.visibility` and `gitlab.branch.default_protected` evaluators.
- **Not yet implemented:** results are computed but not stored or served. Persistence is BAL-021.

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

Policy-version and observation IDs will be linked when observations are stored (BAL-021 and BAL-026). Until then, `observed_at` and `collection` record provenance.

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

## Deferred

| Item | Issue |
| --- | --- |
| Storing results and findings, linking observation IDs, an API | BAL-021 |
| Tests across unknown, unsupported and stale outcomes end to end | BAL-022 |
| Exceptions (`excepted`) | BAL-033 |
| Group-level rules. All current rule types target projects. | |
