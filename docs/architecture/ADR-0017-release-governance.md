# ADR-0017: Release governance

- Status: Accepted
- Date: 2026-11-25

## Context

Change evidence (docs/CHANGE_EVIDENCE.md) judges the merge-request path: each change to the default branch reviewed by someone other than its author, its pipeline passing, and production deployments shipping reviewed changes. Many organizations gate production differently. A common GitLab practice is a protected release tag (such as `*-release`) whose production deployment needs an approval on a protected environment, where the person who created the tag may not approve it and the deployer is not the approver. That separation of duties, with the approval as the formal change approval, is what SOC 2 (CC8.1) and ISO/IEC 27001:2022 (A.8.32, A.5.3) auditors test.

Baluarte did not read protected tags, protected environments, deployment approvals or who created and deployed a release, and it judged a deployment of a tag whose commit was not among the collected changes as a failure. A read-only check against a real GitLab 19.5 Enterprise instance showed that:

- the approval rule can live on the group's protected environment rather than the project's;
- protected tag patterns differ by project;
- deploy jobs usually run automatically after approval, so GitLab records the tag creator as the deployer;
- GitLab's "Allow pipeline triggerer to approve deployment" project setting lets the tag creator approve their own release.

## Decision

- **Release practices are declared, presets plus custom.** Three presets with parameters:
  - **merge request to the default branch**;
  - **protected release tag with deployment approval** (tag patterns, environment, required approvals);
  - **release branch** (branch patterns, environment).

  **Custom** is built from the same blocks. Any practice can also require merge-request review into the default branch. A practice is assigned to the connection, a group or a project, and the nearest assignment wins, as with baselines and templates.
- **Who did what is what GitLab records.**
  - The **tag creator** is the user of the tag's pipeline.
  - The **approvers** are the deployment's approvals with status `approved`.
  - The **deployer** is the deployment's `user`.

  With deploy jobs that run automatically, deployer ≠ approver therefore means creator ≠ approver. A practice does not require a separately recorded deployer.
- **Collection stays read-only:**
  - protected tags, protected environments of the project and its groups, and the self-approval setting;
  - for each production deployment: its tag, its approvals, the deployer, the tag pipeline's user and source, and its GitLab release.

  Usernames only, never email addresses or display names.
- **Releases are judged per practice.** For the tag preset, a production release must:
  - come from a tag matching a protected pattern;
  - have at least the required approvals, given before the deployment finished;
  - not be approved by the tag creator;
  - not be deployed by an approver.

  A missing read is `unknown`, never `pass`. Deploying a tag is no longer a failure in itself.
- **An audit release is exported per production release.** It never contains source code. It holds:
  - the release record, its notes and GitLab's release evidence;
  - the merge requests it contains, with approvals, pipelines and tests;
  - the three people and their times;
  - the protections in force;
  - small report files from the release pipeline (test reports, SBOM, security scans), each within a size limit and with its checksum.

  It is verifiable offline like other exports.
- **The self-approval setting becomes a template setting**, so it can be aligned through reviewed change sets like other settings (ADR-0016).

## Consequences

- A project's declared practice and its observed releases can disagree; that is reported, never silently reconciled.
- Projects on GitLab Free have no deployment approvals: the tag preset gives `unsupported` for the approval checks there, not `pass`.
- GitHub releases (environments with required reviewers) follow the same model later through the provider layer (ADR-0014).

## Revisit when

Releases are gated outside GitLab (a ticketing or change-board system) and that record must be collected, or a separately recorded deployer is required.
