# Change evidence

Configuration rules say how a project is set up today. An auditor testing change management asks something else: for each change that reached the default branch in the period, who approved it, did its pipeline pass, and what was deployed. Baluarte records that evidence per change, judges it with three change rules, reads any result as it stood at a past moment, and builds an evidence package for an audit period with a reproducible sample of changes.

Implemented in `backend/src/baluarte/integrations/gitlab/changes.py` (collection), `backend/src/baluarte/changes/` (judging a change and the settings), `backend/src/baluarte/policy/evaluation/changes.py` (the rules), `backend/src/baluarte/evidence/as_of.py` (reading as of a moment), `backend/src/baluarte/evidence/package.py` (the package) and migration `0021`.

## What a change is

| Kind | What it is | Identified by |
|---|---|---|
| `merge_request` | A merge request merged into the project's default branch. | Project and `iid`. |
| `direct_push` | A first-parent commit on the default branch that no merged merge request into that branch brought. | Project and commit SHA. |
| `deployment` | A successful deployment to one of the production environments. | Project and deployment ID. |

A merge request is identified by every commit that can stand for it (`shas`): its merge commit, its squash commit, and its last commit, which is what lands on the branch when it is merged by fast-forward. `sha` is the commit on the default branch: the merge or squash commit, else the last commit.

## Collection

The `gitlab.collect_changes` job runs after every project discovery, once per discovery run, as a collection run of kind `changes`. For each active, unarchived project in monitoring scope that has a default branch, it calls only `GET`:

1. **Deployments.** `GET /projects/:id/deployments?environment=<name>&status=success&finished_after=<t>&order_by=finished_at`, for each production environment.
2. **Merge requests.** `GET /projects/:id/merge_requests?state=merged&target_branch=<default>&updated_after=<t>&order_by=updated_at`, then for each one `GET .../merge_requests/:iid/approvals`, `GET .../merge_requests/:iid/pipelines` (the newest pipeline for its last commit), `GET /projects/:id/pipelines/:id` and, once the pipeline has finished, `GET /projects/:id/pipelines/:id/test_report_summary`.
3. **Direct pushes.** `GET /projects/:id/repository/commits?ref_name=<default>&first_parent=true&since=<t>`. A commit that is not one of the merge requests' commits is looked up with `GET /projects/:id/repository/commits/:sha/merge_requests`; if no merged merge request into the default branch brought it, it is a direct push, read with `GET /projects/:id/pipelines?sha=&ref=` and the pipeline reads above.

### History and the cursor

A project's first collection reads back `lookback_days` (default 90). That moment is the project's `history_since`, kept in `change_cursor`, and nothing earlier is ever read. Later collections start from the cursor:

- **Merge requests:** from the latest merge seen, or from `redeploy_days` ago (default 7), whichever is earlier. Merge requests merged in the last `redeploy_days` are read again on every collection, so a deployment that comes later is recorded against them.
- **Commits:** from the latest commit seen, less one day.
- **Deployments:** from the latest deployment seen, less one hour.

The commit cursor advances only when every commit was decided. A commit whose merge requests could not be read holds it, and the commit is read again next time.

### Evidence and versions

Every change is an observation (sources `GET merge request change`, `GET default branch commit` and `GET production deployment`), with `source_ref` naming the change (`/projects/:id/merge_requests/:iid`, `.../repository/commits/:sha`, `.../deployments/:id`). A new observation is written only when what was read differs from the change's current one. A late deployment, or a part that could now be read, therefore adds a **version**; earlier versions are never rewritten. `change_record.observation_id` names the current version.

Each project's collection also records a read observation (source `GET /projects/:id/merge_requests (change evidence)`) holding `history_since` and whether merge requests, commits and deployments could be listed (`ok`, or the status). The change rules read it first.

A merge request's payload holds `iid`, `title`, `web_path`, `author`, `merged_by` (usernames only), `created_at`, `merged_at`, `target_branch`, `sha`, `merge_commit_sha`, `squash_commit_sha`, and three parts:

| Part | Holds |
|---|---|
| `approvals` | `approved_by` (`username`, `approved_at`), `approvals_required` and `approvals_left` (Enterprise Edition only, else null), `self_approved`, `committer_approved` (always null, see below). |
| `pipeline` | `id`, `pipeline_status`, `source`, `finished_at`, `tests` (`total`, `success`, `failed`, `skipped`, `error`, or null without a test report), `tests_status`. `{"status": "none"}` when no pipeline ran. |
| `deployments` | `items`: the successful production deployments of its commits (`id`, `environment`, `status`, `finished_at`). |

A direct push holds `sha`, `title`, `author_name`, `committed_at`, `branch` and `pipeline`. Email addresses and display names are never stored.

Each part records what GitLab returned. When a read fails, the part is `{"status": "forbidden" | "not_found" | "failed", "error": ...}` instead. A part that keeps failing across projects (20 times in a row) is not called again in that collection. A 401 stops the collection: every remaining project is recorded as not read, and the run fails.

### GitLab editions and versions

| Read | Availability | Without it |
|---|---|---|
| Merge request approvals (`/approvals`) | Every tier. `approvals_required` and `approvals_left` are Enterprise Edition only. `/approval_state` (Premium) is not used. | Approvals are `unknown`. |
| Who committed a change | Commits carry `author_name` and `author_email`, never a username. | `committer_approved` is always null; whether a committer approved is not established. |
| `merge_user` | GitLab 14.7 and later; `merged_by` is deprecated and read only as a fallback. | `merged_by` is null. |
| `state` filter on `/repository/commits/:sha/merge_requests` | GitLab 18.2 and later. | Filtered by Baluarte instead, on every version. |
| Deployments `finished_after` | Needs `order_by=finished_at` or `status=success`; cannot be combined with `updated_after`. | Not applicable: both are sent. |
| `finished_at` of a pipeline | Only in the single pipeline read, not in lists. | Read one by one. |
| Test report summary | Every tier; empty when the pipeline produced no report. | `tests` is null and `tests_status` says why. |

Capability detection adds probes for these reads (`merge_requests.read`, `merge_request_approvals_state.read`, `pipelines.read`, `test_reports.read`, `deployments.read`, `commits.read`). They say what to expect; the rules do not depend on them, because every part of every change records what GitLab returned.

## Judging a change

`baluarte.changes.assess` judges a change the same way for the stored outcome, the rules and the API. A change's `outcome` is the default checks: reviewed by someone other than its author, and its pipeline passed.

| Check | Pass | Fail | Unknown |
|---|---|---|---|
| Review | At least `min_approvals` approvals by people other than the author. | The author approved it (when forbidden), too few approvals, or a direct push (unless allowed). | Approvals not read, or the author not known. |
| Pipeline | The newest pipeline for the change's last commit succeeded (and, with `require_tests`, has a test report with no failed or erroring tests). | It failed or was canceled, no pipeline ran, or required tests are missing or failed. | The pipeline or a required test report was not read. |
| Deployment | It deployed a commit of a change that passes review. | It deployed a change that fails review, or a ref other than the default branch. | It deployed a default-branch commit Baluarte has not recorded (it may predate the history), or the change cannot be judged. |

A failure outranks unknown, and unknown outranks pass. `reasons` explains the deciding checks.

## Change rules

| Rule type | Passes when |
|---|---|
| `gitlab.change.reviewed` | Every merge request and direct push in the window passes review. Options: `window_days` (1-365, default 90), `min_approvals` (1-20, default 1), `forbid_self_approval` (default true), `allow_direct_push` (default false). |
| `gitlab.change.pipeline_passed` | Every merge request and direct push in the window passes the pipeline check. Options: `window_days`, `require_tests` (default false). |
| `gitlab.change.deployed_after_review` | Every production deployment in the window deployed a reviewed change. Option: `window_days`. |

The window is the `window_days` up to the evaluation. The rules are judged per project, from its stored change observations; they never call GitLab.

- **Fail:** any change in the window fails. The reason names up to three.
- **Unknown:** no change fails, but one cannot be judged, or a listing the rule needs (merge requests and commits, or deployments) could not be read, so a change Baluarte cannot see might be in the window. Also unknown when the project's changes were never read or could not be listed (the collection kind is `changes`), and when the window starts before the project's `history_since`.
- **Pass:** every change in the window passes. No changes in the window passes, with a count of 0.

The `observed` value is `{window_days, from, to, changes, passed, failed, unknown, failing: [{id, kind, iid, sha, title}], failing_total, gaps, reasons}`, with at most 50 failing changes listed.

### Evidence

A change-rule result rests on many observations. The stored row links the project's read observation (`observation_id`) and lists every change observation in the window (`observation_ids`; for deployments, also the changes they deployed), with `evidence_digest`, the SHA-256 of the canonical JSON `{"observations": [[id, payload_digest], ...]}` in ID order. Before a `pass` or `fail` is stored, every one of those observations is checked against its digest and the value is rebuilt from them; a mismatch stores `unknown` (`evidence_digest_mismatch` or `evidence_mismatch`). Retention keeps every observation and change a stored result refers to.

The default control mapping pairs the change rules with SOC 2 CC8.1, ISO/IEC 27001:2022 A.8.32 and A.8.29, and NIST SP 800-53 CM-3, AC-5 and SA-11 ([CONTROL_MAPPING.md](CONTROL_MAPPING.md)).

## Settings

`GET` and `PUT /api/v1/settings/change-evidence` (`settings:manage`):

```json
{"lookback_days": 90, "redeploy_days": 7, "production_environments": ["production"]}
```

`lookback_days` is 1-730, `redeploy_days` 1-30, and `production_environments` 1-20 distinct names. A change is audited as `settings.change_evidence_updated` with the previous values. A shorter look-back keeps the history already collected; a longer one applies only to projects collected for the first time.

## Changes API

All under `/api/v1`, with `findings:read` (every role).

`GET /changes?project=&kind=&outcome=&from=&to=&before_id=&limit=&as_of=` lists merge requests and direct pushes, newest first by when they landed (`kind=deployment` lists deployments instead). `from` and `to` bound when the change landed; `limit` is 1-200 (default 50); `before_id` is the previous page's `next_before_id`.

```json
{
  "items": [
    {
      "id": 41,
      "project": {"gitlab_id": 7, "path": "acme/web", "state": "active"},
      "kind": "merge_request",
      "iid": 12,
      "sha": "9f2c…",
      "title": "Rotate the signing key",
      "web_path": "/acme/web/-/merge_requests/12",
      "author": "dev",
      "merged_by": "lead",
      "changed_at": "2026-10-30T11:00:00Z",
      "outcome": "pass",
      "reasons": ["!12 was approved by reviewer", "the pipeline of !12 succeeded"],
      "observation": {"id": 902, "payload_digest": "5be1…", "collected_at": "2026-10-30T11:05:00Z"},
      "approvals": {
        "status": "known",
        "approved_by": [{"username": "reviewer", "approved_at": "2026-10-30T10:40:00Z"}],
        "required": null,
        "left": null,
        "self_approved": false,
        "committer_approved": null
      },
      "pipeline": {
        "status": "known",
        "id": 3301,
        "pipeline_status": "success",
        "source": "merge_request_event",
        "finished_at": "2026-10-30T10:30:00Z",
        "tests": {"total": 412, "success": 410, "failed": 0, "skipped": 2, "error": 0}
      },
      "deployments": {
        "status": "known",
        "items": [{"environment": "production", "status": "success", "finished_at": "2026-10-31T09:00:00Z"}]
      },
      "shas": ["9f2c…", "41aa…"],
      "complete": true
    }
  ],
  "next_before_id": null,
  "as_of": null,
  "evaluation_run": null
}
```

- `approvals` is `{"status": "unknown", "reason": "..."}` when it could not be read. A direct push is `known` with no approvals and nulls; a deployment has `null`.
- `pipeline` is `{"status": "none"}` when no pipeline ran, or `unknown` with a reason; `null` for a deployment.
- `deployments` lists the change's production deployments, or is `unknown`; a deployment lists itself.
- `complete` is false while a part failed in a way the next collection may fix.

`GET /changes/{id}?as_of=` returns the same object plus every version, newest first:

```json
{
  "id": 41,
  "...": "as above",
  "versions": [
    {
      "observation_id": 955,
      "collected_at": "2026-10-31T09:10:00Z",
      "source": "GET merge request change",
      "payload_digest": "c03d…",
      "digest_valid": true,
      "payload": {"kind": "merge_request", "iid": 12, "approvals": {"status": "ok", "...": "..."}}
    },
    {"observation_id": 902, "collected_at": "2026-10-30T11:05:00Z", "...": "..."}
  ],
  "as_of": null,
  "evaluation_run": {"id": 88, "finished_at": "2026-10-31T09:15:00Z"}
}
```

`digest_valid` recomputes the payload's SHA-256 now.

## Reading as of a moment

`as_of` (ISO 8601 with a time zone) reads results as they stood at that moment. It is accepted by `GET /findings`, `GET /findings/{id}`, `GET /findings/{id}/evaluations`, `GET /controls`, `GET /controls/{framework}/{id}`, `GET /evaluations/summary`, `GET /evaluations/runs`, `GET /gitlab/projects/{gitlab_id}`, `GET /gitlab/projects/{gitlab_id}/evaluations`, `GET /changes` and `GET /changes/{id}`. Each response adds:

```json
{"as_of": "2026-09-30T23:59:59Z", "evaluation_run": {"id": 61, "finished_at": "2026-09-30T22:04:11Z"}}
```

`evaluation_run` is the latest successful evaluation run that finished at or before `as_of` (without `as_of`, the latest successful run, and `as_of` is null). When no run had finished by then, the response is `200` with `evaluation_run: null` and empty results; a single finding or change read by ID is `404`.

| Read | As of `T` |
|---|---|
| Result counts, control status, a project's results | The run in force at `T`, with the monitoring scope and control mapping in force at `T`. |
| Findings | Those first seen by `T`, each in the state its last decided evaluation by `T` gave it: `fail` open, `excepted` excepted (open if that exception was no longer in force at `T`), `pass` resolved. A finding the run in force did not evaluate was resolved: `project_excluded` if its project was out of scope at `T`, else its later resolution if that happened by `T`, else `no_longer_applies`. Filters apply to the state at `T`. |
| A finding's evaluations | Those evaluated by `T`. |
| Evaluation runs | Those finished by `T`. |
| Monitoring scope | Exclusions decided by `T` and not ended by `T`. |
| Control mapping | Changes decided by `T` and not superseded by a change decided by `T`. A control's change history shows each change as it stood at `T`. |
| Changes | Those recorded by `T`, each from its newest version collected by `T`, judged from that version. |

Inventory fields (paths, states) and a finding's severity are today's: the inventory keeps no history of them.

## Evidence package

`POST /api/v1/evidence/packages` (`evidence:package`: admin, analyst and auditor; `X-CSRF-Token`) builds a ZIP for an audit period:

```json
{
  "from": "2026-07-01T00:00:00Z",
  "to": "2026-10-01T00:00:00Z",
  "framework": "soc2",
  "control_id": "CC8.1",
  "groups": ["acme/payments"],
  "sample": {"size": 25, "seed": 1234567}
}
```

`from` is inclusive and `to` exclusive. `framework` with `control_id`, and `groups`, are optional and narrow it. `sample.size` is 1-500; without `seed`, Baluarte chooses one. The response streams the ZIP with headers `X-Baluarte-Export-Id` and `X-Baluarte-Sample-Seed`.

| File | Holds |
|---|---|
| `README.txt` | The period, run, scope and sample, the files, and how to verify the package and redraw the sample. |
| `export.json` | Metadata: the period, scope, sample and freshness at export time. |
| `inventory.csv` | Projects in scope, flagged when out of monitoring scope at `to`. |
| `evaluations.csv` | The results of the run in force at `to`, with `observation_ids` and `evidence_digest` for change rules. |
| `findings.csv` | Findings in their state at `to`. |
| `changes.csv` | Every change that landed in the period on a monitored project, with its outcome and `sampled`. |
| `change_sample.csv` | The sampled merge requests and direct pushes, in ID order. |
| `change_evidence.jsonl` | Every version of each sampled change, oldest first, with `digest_valid`. |
| `observations.jsonl`, `policies.csv`, `exceptions.csv`, `collection_errors.csv`, `collection_runs.csv` | As in the scoped export, for the period. |
| `manifest.json`, `SHA256SUMS` | As in the scoped export; the manifest adds `evidence_package`. |

**The sample** is drawn from every merge request and direct push in `changes.csv` with Python's `random.Random(seed).sample(sorted(ids), size)`, then sorted. The same seed and the same changes always give the same sample; a size at or above the population takes every change.

The manifest's `evidence_package`:

```json
{
  "format": "baluarte.evidence-package/v1",
  "period": {"from": "2026-07-01T00:00:00Z", "to": "2026-10-01T00:00:00Z"},
  "evaluation_run": {"id": 61, "finished_at": "2026-09-30T22:04:11Z"},
  "scope": {"from": "...", "to": "...", "framework": "soc2", "control_id": "CC8.1", "groups": ["acme/payments"]},
  "sample": {
    "requested_size": 25,
    "size": 25,
    "seed": 1234567,
    "seed_chosen_by": "requester",
    "algorithm": "Python random.Random(seed).sample(population, size) over the change IDs sorted ascending; ...",
    "kinds": ["merge_request", "direct_push"],
    "population": 318,
    "change_ids": [12, 40, 41]
  },
  "change_history": {
    "projects_starting_later": 1,
    "projects": [{"project_path": "acme/payments/new-service", "history_since": "2026-08-14T10:00:00Z"}],
    "projects_truncated": false,
    "projects_never_collected": 0
  },
  "limitations": ["..."]
}
```

`change_history` lists the projects whose change history starts after the period does: their changes before `history_since` were never collected.

`GET /api/v1/evidence/packages/{export_id}` (`evidence:package`) returns what the audit log recorded:

```json
{
  "export_id": "3f0c9a5e6b7d4c2a9e8f1b0a7c6d5e4f",
  "from": "2026-07-01T00:00:00Z",
  "to": "2026-10-01T00:00:00Z",
  "seed": 1234567,
  "sample_size": 25,
  "manifest_sha256": "a41f…",
  "created_by": "user:3",
  "created_at": "2026-10-02T08:00:00Z",
  "completed_at": "2026-10-02T08:00:04Z"
}
```

`manifest_sha256` is null until every byte has been produced. A package that cannot be built is `409` with `{code, message, reasons}`: `not_configured`, `nothing_collected`, `period_not_over` (`to` is in the future) or `no_evaluation_run` (no run finished by `to`). An invalid request, unknown group or control, or too many rows is `422`.

Packages are audited as `evidence.package_created` (period, scope, seed, the sampled IDs, the run and the planned rows) and `evidence.package_completed` (the hashes of the response, every file and the manifest).

`baluarte-verify-export` checks a package like any export and also checks every observation in `change_evidence.jsonl` against its digest and redraws the sample from the manifest's seed over `changes.csv`.

## Limits

- Changes are judged only from the collected history. Changes before a project's `history_since` are not collected, and a rule whose window starts earlier is `unknown`.
- A direct push is detected from the first-parent history of the default branch. A force-push that rewrote the branch is not recorded as such.
- `committer_approved` is never known, because GitLab's commit API has no usernames.
- A deployment of a commit outside the collected history is `unknown`, never a pass.
- A merge request merged before `redeploy_days` ago is not read again, so a deployment of it after that is recorded as a deployment (and judged) but not added to the merge request's deployments.
