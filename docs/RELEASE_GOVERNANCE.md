# Release governance

Roadmap milestone **Release governance** ([ROADMAP.md](ROADMAP.md#release-governance)), decided in [ADR-0017](architecture/ADR-0017-release-governance.md). Baluarte records how each project is meant to reach production, checks every production release against it, and exports each release with its evidence for auditors. **Collection is read-only.** Releases are judged from evidence; a release that cannot be judged is `unknown`, never `pass`.

## Status

| Step | What it delivers | State |
|---|---|---|
| RG.1 Release evidence | Protected tags, protected environments (project and groups) and the self-approval setting per project; each production deployment with its tag, approvals, deployer, tag pipeline user and GitLab release; a tag deployment no longer fails because its ref is not the default branch | Done |
| RG.2 Release practices | Presets and custom practices with parameters, assigned to the connection, a group or a project (nearest wins); a Release practices page listing which groups and projects follow which practice, and what their releases actually show | Done |
| RG.3 Release checks | Each production release judged against its practice: protected ref, approvals required and given before deploying, tag creator not an approver, deployer not an approver, optional merge-request review; findings, the auditor report and evidence packages | Done |
| RG.4 Audit release export | One package per production release: the release, its notes and GitLab release evidence, its merge requests with approvals, pipelines and tests, who created, approved and deployed it, the protections in force, and small pipeline reports (test reports, SBOM, security scans) with checksums; verified offline | Planned |
| RG.5 Settings | `deployments.forbid_pipeline_triggerer_approval` as a template setting, aligned by change sets; templates for merge-request review into the default branch documented as the way to close that gap | Planned |

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

## Audit release export (RG.4)

A package per production release, never source code:

- `release.json`: tag, commit, environment, deployment, times, the three roles, and the protections in force at the time;
- release notes and GitLab's release evidence file;
- the merge requests the release contains since the previous production release, with approvals, pipelines and test results;
- reports from the release pipeline's jobs, fetched by type (`junit`, `cyclonedx`, `sast`, `dependency_scanning`, `secret_detection`, `container_scanning`): each at most 10 MB and 50 MB per release, anything larger listed with its size and not fetched;
- a manifest with SHA-256 checksums, verified offline with `baluarte-verify-export`.
