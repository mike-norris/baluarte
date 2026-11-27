# Release governance

Roadmap milestone **Release governance** ([ROADMAP.md](ROADMAP.md#release-governance)), decided in [ADR-0017](architecture/ADR-0017-release-governance.md). Baluarte records how each project is meant to reach production, checks every production release against it, and exports each release with its evidence for auditors. **Collection is read-only.** Releases are judged from evidence; a release that cannot be judged is `unknown`, never `pass`.

## Status

| Step | What it delivers | State |
|---|---|---|
| RG.1 Release evidence | Protected tags, protected environments (project and groups) and the self-approval setting per project; each production deployment with its tag, approvals, deployer, tag pipeline user and GitLab release; a tag deployment no longer fails because its ref is not the default branch | Done |
| RG.2 Release practices | Presets and custom practices with parameters, assigned to the connection, a group or a project (nearest wins); a Release practices page listing which groups and projects follow which practice, and what their releases actually show | Done |
| RG.3 Release checks | Each production release judged against its practice: protected ref, approvals required and given before deploying, tag creator not an approver, deployer not an approver, optional merge-request review; findings, the auditor report and evidence packages | Done |
| RG.4 Audit release export | One package per production release: the release, its notes and GitLab release evidence, its merge requests with approvals, pipelines and tests, who created, approved and deployed it, the protections in force, and small pipeline reports (test reports, SBOM, security scans) with checksums; verified offline | Done |
| RG.5 Settings | `deployments.forbid_pipeline_triggerer_approval` as a template setting, aligned by change sets; templates for merge-request review into the default branch documented as the way to close that gap | Done |

## Practices

| Practice | Production comes from | Checks |
|---|---|---|
| Merge request to the default branch | The default branch | Each change reviewed by someone other than its author, pipeline passed (the existing change rules); the deployment shipped a reviewed change |
| Protected release tag with deployment approval | A tag matching one of the protected patterns, such as `*-release` | The tag is protected; the deployment has the required approvals, given before it finished; the tag creator did not approve; the deployer is not an approver |
| Release branch | A branch matching one of the protected patterns, such as `release/*` | The branch is protected; changes reached it through reviewed merge requests |
| Custom | Any of the above blocks | As chosen |

Parameters: ref patterns (several per practice), production environments, required approvals, and whether merge-request review into the default branch is also required. Assignments work like template assignments: the connection, a group or a project, nearest wins, with the decision shown per project.

| Parameter | Meaning | Default |
|---|---|---|
| `production_from` | `default_branch`, `tag` or `branch`; fixed by a preset, chosen by a custom practice | By preset |
| `ref_patterns` | Tag or branch patterns production deploys, GitLab wildcards (`*`); none for the default branch | `*-release` (tag), `release/*` (branch) |
| `environments` | Production environment names | `production` |
| `required_approvals` | Deployment approvals each release needs, 0 to 10 | 1 for the tag preset, else 0 |
| `creator_may_approve` | Whoever started the release's pipeline (for a tag, its creator) may approve it | `false` |
| `deployer_may_approve` | Whoever GitLab records as the deployer may also approve | `false` |
| `require_merge_request_review` | Each change to the default branch is reviewed by someone other than its author | `true` |

A practice is edited in place, not versioned; creating, changing (with the values before and after), deleting, assigning and unassigning are audited (`release_practice.*`). A practice that is still assigned cannot be deleted.

### What releases show

**Governance, Release practices** lists each active project in monitoring scope with its practice, the connection, group or project that decided it, and its production deployments of the last 90 days to the practice's environments, by what they deployed: the default branch, a tag, another branch, or not known (recorded before tags were, or the project's default branch is not known). A project:

- **follows** its practice when every deployment is of a ref it expects;
- **differs** when one is not, with the refs it did not expect;
- is **not known** when one could not be classified;
- has **no releases**, or **no practice**.

This compares refs only; approvals and people are judged by the release checks (RG.3).

### API

| Method and path | Permission | What it does |
|---|---|---|
| `GET /release-practices` | `findings:read` | Practices with their assignment counts, the presets and their defaults |
| `POST /release-practices` | `policy:write` | Create: `name`, `preset`, optional `description` and `settings` over the preset's defaults |
| `PUT /release-practices/{name}` | `policy:write` | Change `settings` and `description`; the preset stays |
| `DELETE /release-practices/{name}` | `policy:write` | Delete a practice that is not assigned (409 otherwise) |
| `GET /release-practice-assignments` | `findings:read` | Where practices are assigned |
| `POST /release-practice-assignments` | `policy:write` | Assign: `name`, `target_type`, `target_id` (omitted for the connection); replaces the target's practice |
| `DELETE /release-practice-assignments/{id}` | `policy:write` | Remove an assignment |
| `GET /release-practices/projects?status=&practice=` | `findings:read` | Each project's practice, who decided it, and what its releases show |

Writes need the `X-CSRF-Token` header.

## Release checks (RG.3)

The baseline rule `gitlab.release.follows_practice` (option `window_days`, 1–365, default 90) judges every production deployment in the window to the practice's environments against the practice in force for the project. Each release is checked for:

| Check | Fails when | Unknown when |
|---|---|---|
| Ref | The practice does not expect the ref deployed (for example a branch under a tag practice) | GitLab did not say whether a tag or a branch was deployed |
| Protected tag (tag practices) | The tag matches no protected tag pattern | The protected tags were not read |
| Approvals (when `required_approvals` > 0) | Fewer distinct approvers than required gave approval before the deployment finished | The approvals were not read, or GitLab did not report them (Premium or Ultimate) |
| Creator not an approver (unless `creator_may_approve`) | Whoever started the release approved it | GitLab did not record who started it |
| Deployer not an approver (unless `deployer_may_approve`) | The deployer approved it | GitLab did not record the deployer |
| Merge-request review (when `require_merge_request_review`) | The change shipped was not reviewed by someone other than its author | The change shipped is not recorded |

A project with no practice assigned is `unknown`, never `pass`; a project with no production releases in the window passes. A failing result lists each failing release with its ref and the people GitLab records: who started it, who approved it and who deployed it.

The result records the practice in force (name, preset, settings and where it was assigned) and the release protections observation it used. Before a `pass` or `fail` is stored, every deployment and change observation it rests on and that protections observation have their digests checked and the value is rebuilt from them, as for the change rules. Findings, the auditor report and evidence packages include it like any other rule. It maps to SOC 2 CC8.1, ISO/IEC 27001 A.8.32 and NIST CM-3 and AC-5 (mapping version `2026.11.2`). The rule is GitLab only; on GitHub it is `unsupported`.

The baseline editor offers it under the change rules; the recommended baseline leaves it off, since it needs a practice assigned first.

## Who did what

As GitLab records it:

| Role | Source |
|---|---|
| Tag creator | The user of the tag's pipeline (`GET /projects/:id/pipelines/:id`), with the pipeline `source` (`push`, `api`, ...) |
| Approvers | The deployment's `approvals` with status `approved`, with their times and comments (GitLab Premium or Ultimate) |
| Deployer | The deployment's `user` |

Deploy jobs that run automatically after approval run as the pipeline's user, so GitLab records the tag creator as the deployer; deployer ≠ approver then means creator ≠ approver. A manual deploy job records whoever runs it.

## Collection (RG.1)

Read with the read-only token, after project discovery:

- `GET /projects/:id/protected_tags`: names (patterns) and who may create them.
- `GET /projects/:id/protected_environments` and `GET /groups/:id/protected_environments` for each group above the project: required approvals, approval rules and deploy access. The approval rule is often on the group.
- `GET /projects/:id`: `allow_pipeline_trigger_approve_deployment`.
- For each production deployment (already collected): `GET /projects/:id/deployments/:id` for `approvals`, `approval_summary` and `user`, then the tag pipeline's `user` and `source`, and `GET /projects/:id/releases/:tag` for the release (notes, author, evidences, asset links).

Only usernames are stored.

## Settings (RG.5)

GitLab's project setting **Allow pipeline triggerer to approve deployment** (`allow_pipeline_trigger_approve_deployment`) lets whoever started a pipeline approve its deployments. For a tag pipeline that is the tag's creator, so with the setting on a release's creator can approve their own release. RG.3 catches that after the fact; RG.5 keeps it from happening.

**The rule.** `gitlab.deployment.triggerer_cannot_approve` (no options) passes when the setting is off and fails when it is on. The value comes from the `pipeline_trigger_approval` part of the project's release protections observation (RG.1), read with change evidence, so the rule follows the `changes` collection's freshness. It is `unknown` when the protections were never read or that part could not be read, and `unsupported` when GitLab answered without the setting: deployment approvals need GitLab Premium or Ultimate. A result rests on that observation, whose digest is checked and whose value is rebuilt before a `pass` or `fail` is stored; the observation counts even when another of its parts was not read (`partial`). It maps to SOC 2 CC8.1, ISO/IEC 27001 A.8.32 and NIST AC-5 (mapping version `2026.11.3`). The baseline editor offers it under the change rules, off by default; it is GitLab only.

**The template setting.** `deployments.forbid_pipeline_triggerer_approval: true` in a configuration template ([CONFIGURATION_TEMPLATES.md](CONFIGURATION_TEMPLATES.md)) says the setting should be off. Drift is judged by the rule above. A change set shows the setting's value before and after from that evidence and warns that turning it off makes someone else approve each release (or, the other way, that it lets a release's creator approve it). A baseline that uses the rule blocks a change set that would allow it again. Applying writes `PUT /projects/:id` with `allow_pipeline_trigger_approve_deployment`, after reading it again, with the template write token (Maintainer); rolling back writes the previous value. Template writes stay off unless `BALUARTE_TEMPLATE_WRITES` is on.

### Closing the merge-request gap

A tag practice judges the release, not how its changes reached the default branch: a tag can be cut from a commit that was pushed straight to the default branch or merged without review. The practice's `require_merge_request_review` judges that per release from evidence; a template keeps it from happening, by setting what GitLab enforces on merge requests into the default branch:

```yaml
apiVersion: baluarte.io/v1alpha1
kind: ConfigurationTemplate
metadata:
  name: tag-releases
  description: Reviewed changes, released by protected tag with a separate approver
spec:
  settings:
    default_branch_protected: true       # no direct pushes
    merge_requests:
      min_approvals: 1                   # every merge request needs an approval
      forbid_author_approval: true       # not by its author
      forbid_committer_approval: true    # nor by anyone who committed to it
      reset_approvals_on_push: true      # a later push needs approval again
      forbid_override: true              # a merge request cannot lower its own rules
    deployments:
      forbid_pipeline_triggerer_approval: true   # the tag's creator cannot approve the release
```

Assign it where the tag practice is assigned, and drift shows each project that differs; a change set aligns them. The merge request settings need GitLab Premium or Ultimate for approval rules. Protected tags and protected environments (who may create a release tag, how many approvals a deployment needs) are not template settings yet: Baluarte reads them (RG.1) and the release checks judge them (RG.3), but they are still set in GitLab.

## Audit release export (RG.4)

**Governance, Releases** lists recorded production releases, newest first, with who started, approved and deployed each as GitLab records them. **Export audit release** builds one ZIP for one release, for an auditor. It never holds source code.

| File | What it holds |
|---|---|
| `README.txt` | The release, who did what, the practice and judgment, counts, gaps and how to verify |
| `release.json` | The deployment (environment, ref, tag, commit, pipeline, times), the roles (who started it, each approval with its time and comment, the deployer), the GitLab release (name, author, times, release evidence SHA-256 and links), the practice in force and the release judged against it as the release checks judge it, the release protections in force, the previous production release, and the range of changes |
| `release_notes.md` | The GitLab release's notes, when it has some |
| `merge_requests.csv` | The merge requests and direct pushes since the previous production release of the same environment: author, who merged, who approved, self-approval, review and overall outcome, pipeline and tests, and which one the release shipped |
| `observations.jsonl` | Every observation the package rests on (the deployment, each change, the protections), with its `payload_digest` |
| `reports.csv`, `reports/<sha256>` | The release pipeline's reports, each stored once by its SHA-256, or listed with why it was not fetched |
| `gaps.csv` | What is missing or could not be judged |
| `manifest.json`, `SHA256SUMS` | Every file's SHA-256, bytes and rows; the summary; limitations |

**The range of changes.** Baluarte does not read git history. The changes are those merged into the default branch after the change the previous production release shipped, up to the one this release shipped, by merge time (`changes.basis` `shipped_commits`). When either shipped change is not recorded, the range uses deployment times instead (`deployment_times`) and a gap says so. The first recorded release starts from the project's recorded history. A redeploy of what an earlier release shipped lists the change it shipped, with the gap `nothing_new`. At most 500 changes are listed.

**Reports.** The release pipeline's jobs are read from GitLab, read-only, when the package is built (unless `include_reports` is false). Every report artifact of type `junit`, `cyclonedx`, `sast`, `dependency_scanning`, `secret_detection` or `container_scanning` is listed with its size:

- `junit`: GitLab's parsed test report for the pipeline, fetched once as `test_report.json`.
- Other types: GitLab's REST API does not serve a report artifact by type, so the file is read out of the job's artifacts archive when the job also keeps it there (`artifacts:paths`). Only the file named like the report is read; nothing else in the archive is copied.
- Each report is at most 10 MB, and a release at most 50 MB in all. Anything larger, expired, served by redirect (object storage), or kept only as a report artifact is listed in `reports.csv` and `gaps.csv` with why, never silently left out.

GitLab's release evidence file is served only through its web interface, so the package records its SHA-256 and collection time from the release instead of the file.

**Verifying.** `baluarte-verify-export <file> --manifest-sha256 <value>` checks it offline like any export. It also checks each `reports/<sha256>` against its name and `reports.csv`, and that every observation `release.json` and `merge_requests.csv` cite is in the package. The manifest hash is in the `release.export_completed` audit event, and `GET /api/v1/releases/exports/{export_id}` returns it.

| Method and path | Permission | What it does |
|---|---|---|
| `GET /releases?connection_id=&project_id=&limit=` | `findings:read` | Recorded production releases, newest first, with who started, approved and deployed each |
| `POST /releases/exports` | `evidence:package` | Build the audit release of `change_id` (a production deployment); `include_reports` (default true). 422 `release_unknown` or `not_a_release`. Audited as `release.export_created` and `release.export_completed` |
| `GET /releases/exports/{export_id}` | `evidence:package` | The export's sizes and hashes, from the audit log |
