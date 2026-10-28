# GitLab integration contract

## Initial scope

Self-managed GitLab REST API, one instance, read-only credential, group/subgroup/project enumeration, default-branch protections, project visibility, and capability detection.

## Reliability

Handle pagination, rate limits, 401/403, 404, 429, 5xx, timeouts, network partitions, and deleted resources. Do not conflate inaccessible with deleted. Reconcile periodically; system hooks/webhooks are optional accelerators, not the only source of truth. Verify signatures/secrets on any incoming hooks.

## Compatibility

GitLab version, license tier, and token privileges determine endpoint behavior. Create a tested capability matrix from actual responses. Avoid assuming that approval, security scanning, or compliance APIs are available in all tiers. Prefer feature detection over version-only assumptions.

## Data freshness

Every resource and finding must show `last_collected_at`, status, and collection errors. A failed scan cannot silently preserve a green dashboard.

## Initial rules

Visibility and default-branch protection are the first two rules. Approval rules, push restrictions, security scans, and deployments require separate endpoint/tier investigations before committing to implementation.
