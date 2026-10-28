# ADR-0006: The API is the OIDC client and keeps sessions in PostgreSQL

- Status: Accepted
- Date: 2026-10-28

## Context

Baluarte needs OIDC sign-in and server-side role checks (FR-011). The browser could hold tokens itself, as a single-page OIDC client, or the API could complete sign-in and give the browser only a session cookie. Tokens held in browser JavaScript are exposed to any script injection, and every API request would need to validate a JWT, so roles could not be withdrawn before the token expires. ADR-0001 and ADR-0002 rule out adding Redis or another session store.

## Decision

- The API is a confidential (or PKCE-only public) OIDC client using the authorization code flow with PKCE (`S256`). It validates the ID token's signature (asymmetric algorithms only), issuer, audience, `azp`, expiry, issued-at and nonce.
- After sign-in the browser receives one opaque, random session cookie: `HttpOnly`, `Secure` (except on loopback development origins), `SameSite=Lax`, host-only and named with the `__Host-` prefix. Provider tokens are discarded and never reach the browser.
- Sessions live in PostgreSQL (`user_session`), keyed by the SHA-256 digest of the cookie value, with idle and absolute expiry. Sign-out revokes the row.
- Roles (`admin`, `analyst`, `auditor`) are mapped from an identity provider groups claim at sign-in and fixed for the session. Users with no mapped role cannot sign in. Endpoints check permissions, never role names.
- State-changing requests need an `X-CSRF-Token` header derived from the session and, when present, a matching `Origin`.
- Routers are mounted behind authentication by default. Only health, readiness and the two sign-in redirects are public, and a test fails if any other route answers an anonymous request.

## Consequences

- No new infrastructure: sessions add one table and one indexed lookup per request.
- Role changes in the identity provider take effect at the next sign-in, at most one session lifetime later (8 hours by default). Revoking a user's sessions immediately needs an administrative action, not yet built.
- The identity provider must be reachable from the API, which ADR-0005 already allows, and must publish OIDC discovery over https.
- Signing out of Baluarte does not sign the user out of the identity provider (no RP-initiated logout yet).
- Non-browser API clients (service accounts, tokens) are not supported yet and need their own decision.

## Revisit when

Programmatic API access is needed, sessions must be revoked from the identity provider (back-channel logout), or roles must be assigned inside Baluarte rather than from provider groups.
