"""In-process OpenID provider for tests, served through httpx.MockTransport."""

import base64
import hashlib
import json
import time
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from baluarte.auth.oidc import OidcClient
from baluarte.auth.roles import Role
from baluarte.auth.settings import AuthSettings

ISSUER = "https://idp.example.test"
CLIENT_ID = "baluarte"
PUBLIC_URL = "https://baluarte.example.test"


def make_settings(**overrides: Any) -> AuthSettings:
    values: dict[str, Any] = {
        "public_url": PUBLIC_URL,
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret": "test-client-secret",
        "scopes": "openid profile email",
        "roles_claim": "groups",
        "group_roles": {
            Role.ADMIN: frozenset({"baluarte-admins"}),
            Role.ANALYST: frozenset({"baluarte-analysts"}),
            Role.AUDITOR: frozenset({"baluarte-auditors"}),
        },
        "session_idle": timedelta(minutes=60),
        "session_lifetime": timedelta(hours=8),
    }
    values.update(overrides)
    return AuthSettings(**values)


def new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    jwk: dict[str, Any] = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}


class FakeIdP:
    """Records requests and answers discovery, JWKS, token and userinfo calls."""

    def __init__(self) -> None:
        self.key = new_key()
        self.kid = "key-1"
        self.jwks_keys = [public_jwk(self.key, self.kid)]
        self.discovery: dict[str, Any] = {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/jwks",
            "userinfo_endpoint": f"{ISSUER}/userinfo",
            "id_token_signing_alg_values_supported": ["RS256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
        }
        self.subject = "user-1"
        self.claims: dict[str, Any] = {
            "email": "ada@example.test",
            "name": "Ada Lovelace",
            "groups": ["baluarte-analysts"],
        }
        self.userinfo_claims: dict[str, Any] | None = None
        self.code = "auth-code-1"
        self.nonce: str | None = None
        self.challenge: str | None = None
        self.token_status = 200
        self.fail_with: Exception | None = None
        self.requests: list[httpx.Request] = []

    def client(self, settings: AuthSettings, clock: Any = time.time) -> OidcClient:
        return OidcClient(settings, httpx.Client(transport=httpx.MockTransport(self)), clock)

    def authorize(self, location: str) -> dict[str, str]:
        """Act as the browser hitting the authorization endpoint; remember nonce and PKCE."""
        parts = urlsplit(location)
        assert (
            f"{parts.scheme}://{parts.netloc}{parts.path}"
            == self.discovery["authorization_endpoint"]
        )
        query = {name: values[0] for name, values in parse_qs(parts.query).items()}
        self.nonce = query["nonce"]
        self.challenge = query["code_challenge"]
        return query

    def id_token(
        self, key: Any = None, headers: dict[str, Any] | None = None, **claims: Any
    ) -> str:
        now = int(time.time())
        payload = {
            "iss": ISSUER,
            "sub": self.subject,
            "aud": CLIENT_ID,
            "iat": now,
            "exp": now + 300,
            "nonce": self.nonce,
            **self.claims,
            **claims,
        }
        payload = {name: value for name, value in payload.items() if value is not None}
        return jwt.encode(
            payload,
            key or self.key,
            algorithm="RS256",
            headers={"kid": self.kid, **(headers or {})},
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json=self.discovery)
        if path == "/jwks":
            return httpx.Response(200, json={"keys": self.jwks_keys})
        if path == "/token":
            return self._token(request)
        if path == "/userinfo":
            body = self.userinfo_claims if self.userinfo_claims is not None else {}
            return httpx.Response(200, json={"sub": self.subject, **body})
        return httpx.Response(404)

    def _token(self, request: httpx.Request) -> httpx.Response:
        if self.token_status != 200:
            return httpx.Response(self.token_status, json={"error": "invalid_grant"})
        form = {name: values[0] for name, values in parse_qs(request.content.decode()).items()}
        verifier = form.get("code_verifier", "")
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        if form.get("code") != self.code or challenge != self.challenge:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(
            200,
            json={"access_token": "access-1", "token_type": "Bearer", "id_token": self.id_token()},
        )
