# ADR-0014: A common provider layer for GitLab and GitHub

- Status: Accepted
- Date: 2026-11-21

## Context

Baluarte was built for GitLab only, and its rules, observations, changes and labels use GitLab concepts. Organizations also use GitHub, and need one baseline, one evidence trail and one set of reports across both. The two providers model similar things differently (groups and organizations, protected branches and rulesets, merge requests and pull requests), and their features depend on version, edition and plan.

## Decision

- **Adapters per provider.** Each provider has an adapter under `integrations/<provider>` that owns authentication, requests, pagination, rate limits, retries, incremental synchronization, webhook validation and capability detection. Nothing outside an adapter calls a provider API.
- **Normalized concepts, with the original kept.** Shared concepts (organization or group, repository, team, membership, branch protection, review, pipeline or workflow run, deployment, release, issue, epic or equivalent, time record) are normalized into common records. Each record keeps the provider, the connection, the provider's IDs and URLs, original timestamps and the provider-specific payload as evidence.
- **Capabilities are explicit.** Each rule type and template setting declares which providers and capabilities it needs. A provider, version or plan that cannot answer gives `unsupported`; a refusal, a stale collection or missing data gives `unknown`; neither is ever `pass`. Nothing is assumed missing on either provider: each capability is verified against the provider's official documentation and detected at run time.
- **GitHub authentication.** A GitHub App installation is preferred, with a fine-grained personal access token as the fallback. GitHub.com and GitHub Enterprise Server are both targets, with documented minimum versions.
- **Webhooks and polling together.** Webhooks are validated by signature and processed idempotently by delivery ID. Scheduled collection stays the source of completeness; records are keyed by provider IDs so a webhook and a scheduled read never create duplicates.
- **Writes keep ADR-0007's constraints.** Every provider write path keeps a separate token or permission, a deployment switch, an allowlist, a send-once request and a read-back.
- **GitLab first, unchanged.** The GitLab integration moves onto the layer without changing results, evidence or exports.

## Consequences

- Rules and templates are written against normalized records, so one baseline can govern both providers.
- Evidence exports show the provider and connection of every record.
- Each provider has a capability matrix in the docs, like the GitLab one.
- Some rules are provider-specific and say so.

## Revisit when

A third provider is proposed, or a provider changes its authentication model.
