# Labels

Organizations with many groups want one set of labels everywhere: the same names, colours and descriptions, defined once at the top of each group hierarchy so every project inherits them. Baluarte reads the labels every group and project defines, compares them with a reviewed **label taxonomy**, reports the **drift**, judges it per project with the `gitlab.labels.consistent` rule, and can apply a reviewed **label plan** that fixes what can be fixed safely. Applying a plan is the only way Baluarte writes to GitLab ([ADR-0009](architecture/ADR-0009-label-writes.md)).

Implemented in `backend/src/baluarte/integrations/gitlab/labels.py` (collection), `backend/src/baluarte/labels/` (taxonomy, drift, plans, write token), `backend/src/baluarte/integrations/gitlab/label_writer.py` (the only GitLab writes), `backend/src/baluarte/policy/evaluation/labels.py` (the rule), `backend/src/baluarte/api/v1/labels.py` (the API) and migration `0022`.

## The taxonomy

```yaml
apiVersion: baluarte.io/v1alpha1
kind: LabelTaxonomy
metadata:
  name: acme-labels
spec:
  scope:
    groups: ["acme"]          # full paths; a group covers its subgroups
  placement: top_level_group   # where every label is defined (the only placement)
  strict: false                # true also reports labels the taxonomy does not name
  labels:
    - name: "priority::high"
      color: "#D9534F"         # #RRGGBB
      description: "Must be fixed this sprint"
      aliases: ["P1"]          # old names to rename to this one
    - name: bug
      color: "#FF0000"
```

Names are unique ignoring case and repeated spaces; an alias may not name another label. A version is proposed with `policy:write`. While baseline review is required (`/settings/governance`) it waits until someone else with `policy:write` approves it, as baseline changes do; otherwise it is in force at once and recorded as `self_approved`. Only one version waits at a time. Each version keeps its YAML, digest, justification and decision; a trigger refuses any change to a decided version and any `DELETE`. Audited as `labels.taxonomy_proposed`, `_approved` and `_rejected`.

The taxonomy in force at a moment is the latest version approved by then, so `as_of` reads judge drift against the taxonomy that applied at the time.

## Collection

The `gitlab.collect_labels` job runs after each project discovery, as a collection run of kind `labels`, with the read-only token and only `GET`:

| Owner | Request |
|---|---|
| Every active group | `GET /groups/:id/labels?only_group_labels=true&include_ancestor_groups=false&include_descendant_groups=false` |
| Every active, unarchived project in monitoring scope | `GET /projects/:id/labels?include_ancestor_groups=false` |

So each label is recorded once, on the group or project that defines it; inherited labels are worked out from the group tree. A project item with `is_project_label: false` is never counted as the project's own. Each owner gets one observation (sources `GET /groups/:id/labels` and `GET /projects/:id/labels`) of its labels sorted by name: `id`, `name`, `color`, `description`, a project label's `priority`, and `archived` when GitLab says so. A new observation is written only when the list changed. A 403 or 404 is recorded as `forbidden` or `not_found` and judged unknown, never as "no labels"; a 401 stops the run and the rest are recorded as not read. The capability probe `labels.read` reads one group's labels.

Archived labels count as defined: archiving hides a label from pickers but it still exists, and a plan never creates a second one.

## Drift

For each hierarchy (the topmost group in the taxonomy's scope, its subgroups and their projects) every taxonomy label is checked:

| Kind | Meaning | On |
|---|---|---|
| `missing` | Defined nowhere in the hierarchy under its name or a variant. | The top-level group, once. |
| `misnamed` | Defined under an alias or a case or spacing variant of its name. | Where the variant is defined. |
| `color` | Its colour differs (compared case-insensitively). | The top-level group. |
| `description` | Its description differs. | The top-level group. |
| `misplaced` | Defined below the top-level group, and not on it. | Where it is defined. |
| `duplicate` | Defined below the top-level group as well as on it, shadowing it. | Where it is defined. |
| `unknown_label` | Defined but not named by the taxonomy (`strict: true` only). | Where it is defined. |
| `unknown` | The owner's labels could not be read, or its stored evidence fails its digest check. | That owner. |

When the top-level group could not be read, its hierarchy reports only `unknown`: nothing is claimed missing. Each item lists the observations it was judged from. Personal namespaces are not covered.

## The rule

```yaml
- id: labels-consistent
  type: gitlab.labels.consistent
  severity: low
  ignore: [unknown_label]   # drift kinds that do not count; never `unknown`
```

A project passes when no counted drift is on it or on a group above it. It is `unknown` when no taxonomy is in force or the labels of the project or a group above it could not be read, and passes with nothing to check when it is outside the taxonomy's scope. Its result is backed by every label observation it was judged from: if one is gone, changed or fails its digest check, the result is stored as `unknown`. The rule has no default control mapping; map it to the control your organization uses it for ([CONTROL_MAPPING.md](CONTROL_MAPPING.md)). Findings can be listed with `GET /findings?rule_type=gitlab.labels.consistent`.

## Plans

A plan is built (`remediation:plan`) from a fresh read: the labels of every group and project in its scope are read with the read-only token, recorded as evidence, and drift is computed from that read. At most 500 groups and projects are read; name fewer groups otherwise. Three kinds of step are planned, all on the top-level group:

| Step | Request | When |
|---|---|---|
| `create` | `POST /groups/:id/labels` with `name`, `color`, `description` | A label is `missing`. |
| `rename` | `PUT /groups/:id/labels/:label_id` with `new_name`, `color`, `description` | A label on the top-level group is `misnamed` and the canonical name is not already defined there. Issues and merge requests keep the renamed label. |
| `update` | `PUT /groups/:id/labels/:label_id` with `color`, `description` | Its colour or description differs. |

Everything else is a **manual** item with the reason: duplicates and misplaced labels (merge them into the top-level label in GitLab), misnamed labels below the top-level group, a rename whose canonical name already exists, labels not in the taxonomy, and owners that could not be read. **Nothing is ever deleted.** Each step carries the label as read (`prior`), what it will be (`intended`) and an idempotency key.

Lifecycle, enforced again by a trigger: `draft` → `submitted` by its creator → `approved` or `rejected` by someone else with `remediation:approve`, naming the `plan_digest` they reviewed. While review is not required, submitting approves the plan at once (`self_approved`). An approved plan → `applying` → `applied` or `failed`. A plan not applied within 24 hours expires. Approval is refused when the taxonomy in force has changed since the plan was built.

### Applying

`POST /labels/plans/:id/apply` (`remediation:plan`) is refused, with every reason, unless:

- the deployment sets `BALUARTE_LABEL_WRITES=true` (default `false`);
- a label write token is stored;
- the plan is approved, unexpired and its taxonomy is still in force.

The `gitlab.apply_label_plan` job (one attempt, never retried) applies the steps in order. For each step it reads the owner's labels with the read-only token. A label already as intended is `skipped`; a label that changed since the plan was built stops the plan. Otherwise the write is sent once with the write token, then the label is read back with the read-only token. The plan stops at the first write GitLab refuses (any 4xx or 5xx, or no answer) or that does not read back as intended; later steps are `not_run`. Each write is audited as `labels.write` with method, path and status; the plan's start and end as `labels.plan_applying` and `labels.plan_applied` or `_failed`. A label collection is queued afterwards so drift shows the result.

## The write token

An administrator stores it at `PUT /settings/label-write-token` (`settings:manage`). Use a token with the `api` scope that reaches only the groups in the taxonomy's scope and expires, ideally a group access token on a bot user. It is sealed like other secrets ([ADR-0008](architecture/ADR-0008-settings-in-the-database.md)), so `BALUARTE_SECRET_KEY_FILE` must be set. On save it is checked with `GET /personal_access_tokens/self`, and its name, scopes and expiry are shown; a failed check is shown as `check_error` and the token is stored anyway. The token is never returned, logged or audited. Only the applier decrypts it; collection never sees it, and the read-only client has no write method. `DELETE /settings/label-write-token` removes it. Storing, replacing and removing it are audited as `settings.label_write_token_set`, `_replaced` and `_removed`.

## API

| Endpoint | Permission |
|---|---|
| `GET /labels/taxonomy?as_of=` | `findings:read` |
| `POST /labels/taxonomy/validate`, `POST /labels/taxonomy`, `POST /labels/taxonomy/:id/approve`, `/reject` | `policy:write` |
| `GET /labels/drift?group=&kind=&label=&after=&limit=&as_of=` | `findings:read` |
| `GET /labels/owners/{group,project}/:gitlab_id?as_of=` | `findings:read` |
| `GET /labels/plans`, `GET /labels/plans/:id` | `findings:read` |
| `POST /labels/plans`, `/labels/plans/:id/submit`, `/labels/plans/:id/apply` | `remediation:plan` |
| `POST /labels/plans/:id/approve`, `/reject` | `remediation:approve` |
| `GET`, `PUT`, `DELETE /settings/label-write-token` | `settings:manage` |

Refusals are `{"detail": {"code", "message", "reasons"}}`: `own_change` (403), and with 409 `label_writes_disabled`, `write_token_missing`, `not_approved`, `plan_expired`, `digest_mismatch`, `taxonomy_changed`, `scope_too_large`, `nothing_to_plan`, `no_taxonomy`, `taxonomy_pending`, `gitlab_not_configured`. An invalid taxonomy is 422 `invalid_taxonomy` with every error.
