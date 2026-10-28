"""OIDC sign-in, server-side sessions and role-based access control.

The API is the OIDC client (authorization code flow with PKCE). Browsers hold only an opaque
session cookie; roles come from identity provider claims and are enforced on the server.
"""
