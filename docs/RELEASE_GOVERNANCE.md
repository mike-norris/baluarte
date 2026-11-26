# Release governance

Roadmap milestone **Release governance** ([ROADMAP.md](ROADMAP.md#release-governance)), decided in [ADR-0017](architecture/ADR-0017-release-governance.md). Baluarte records how each project is meant to reach production, checks every production release against it, and exports each release with its evidence for auditors. **Collection is read-only.** Releases are judged from evidence; a release that cannot be judged is `unknown`, never `pass`.

## Status

| Step | What it delivers | State |
|---|---|---|
| RG.1 Release evidence | Protected tags, protected environments (project and groups) and the self-approval setting per project; each production deployment with its tag, approvals, deployer, tag pipeline user and GitLab release; a tag deployment no longer fails because its ref is not the default branch | Done |
| RG.2 Release practices | Presets and custom practices with parameters, assigned to the connection, a group or a project (nearest wins); a Release practices page listing which groups and projects follow which practice, and what their releases actually show | Planned |
| RG.3 Release checks | Each production release judged against its practice: protected ref, approvals required and given before deploying, tag creator not an approver, deployer not an approver, optional merge-request review; findings, the auditor report and evidence packages | Planned |
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
