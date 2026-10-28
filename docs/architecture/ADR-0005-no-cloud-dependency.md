# ADR-0005: No cloud or internet dependency at runtime

- Status: Accepted
- Date: 2026-10-28

## Context

Target users run self-managed GitLab, often in restricted or air-gapped networks, and audit evidence may be sensitive. A runtime dependency on a vendor cloud, SaaS, or public internet endpoint would block these deployments and move evidence outside the organization's control.

## Decision

Baluarte runs fully on infrastructure the operator controls. Runtime network access is limited to the configured GitLab instance, the configured OIDC provider, and PostgreSQL. No telemetry, license checks, update checks, or CDN-hosted assets. Images can be loaded from a private registry, and the web image bundles all frontend assets.

## Consequences

- Air-gapped installs are supported, with images mirrored to a private registry.
- Features that need external data (for example vulnerability feeds) must be optional and off by default, and require an ADR.
- No built-in usage analytics; product feedback comes through GitHub issues.

## Revisit when

An optional integration is proposed that needs outbound access; it must remain opt-in.
