"""OpenID Connect relying party: discovery, authorization URL, code exchange and ID tokens.

Errors carry a short reason code that is safe to log and record in the audit trail. Provider
responses, tokens and secrets are never included in an error or a log line.
"""

import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

from baluarte.auth.settings import AuthSettings

logger = logging.getLogger(__name__)

# Asymmetric algorithms only: "none" and HMAC algorithms are never accepted for ID tokens.
ALLOWED_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)
CLOCK_SKEW_SECONDS = 60
METADATA_TTL_SECONDS = 3600
JWKS_REFRESH_INTERVAL_SECONDS = 60
# Token endpoint client authentication methods (OIDC Core section 9). Names, not secrets.
BASIC_AUTH = "client_secret_basic"
POST_AUTH = "client_secret_post"


class OidcError(Exception):
    """Sign-in failed. ``reason`` is a short code, safe for logs and the audit trail."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ProviderMetadata:
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str | None
    algorithms: frozenset[str]
    token_auth_method: str


@dataclass(frozen=True)
class PendingLogin:
    state: str
    nonce: str
    code_verifier: str


def new_pending_login() -> PendingLogin:
    return PendingLogin(
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        code_verifier=secrets.token_urlsafe(64),
    )


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _https(url: object, field: str) -> str:
    if not isinstance(url, str) or urlsplit(url).scheme != "https":
        raise OidcError(f"discovery_invalid_{field}")
    return url


class OidcClient:
    def __init__(
        self,
        settings: AuthSettings,
        http: httpx.Client,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._http = http
        self._clock = clock
        self._lock = threading.Lock()
        self._metadata: ProviderMetadata | None = None
        self._metadata_at = 0.0
        self._jwks: jwt.PyJWKSet | None = None
        self._jwks_at = 0.0

    def _get_json(self, url: str, reason: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._http.get(url, **kwargs)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("oidc: %s (%s)", reason, type(exc).__name__)
            raise OidcError(reason) from exc
        if not isinstance(body, dict):
            raise OidcError(reason)
        return body

    def metadata(self) -> ProviderMetadata:
        with self._lock:
            now = self._clock()
            if self._metadata is None or now - self._metadata_at > METADATA_TTL_SECONDS:
                self._metadata = self._discover()
                self._metadata_at = now
            return self._metadata

    def _discover(self) -> ProviderMetadata:
        document = self._get_json(
            self._settings.issuer + "/.well-known/openid-configuration", "discovery_failed"
        )
        # OIDC Discovery 1.0 section 4.3: the issuer must match exactly.
        if document.get("issuer") != self._settings.issuer:
            raise OidcError("discovery_issuer_mismatch")
        advertised = document.get("id_token_signing_alg_values_supported", ["RS256"])
        algorithms = ALLOWED_ALGORITHMS & set(advertised if isinstance(advertised, list) else [])
        if not algorithms:
            raise OidcError("discovery_no_supported_algorithm")
        methods = document.get("token_endpoint_auth_methods_supported", [BASIC_AUTH])
        if self._settings.client_secret is None:
            method = "none"
        elif BASIC_AUTH in methods:
            method = BASIC_AUTH
        elif POST_AUTH in methods:
            method = POST_AUTH
        else:
            raise OidcError("discovery_no_supported_client_auth")
        userinfo = document.get("userinfo_endpoint")
        return ProviderMetadata(
            authorization_endpoint=_https(
                document.get("authorization_endpoint"), "authorization_endpoint"
            ),
            token_endpoint=_https(document.get("token_endpoint"), "token_endpoint"),
            jwks_uri=_https(document.get("jwks_uri"), "jwks_uri"),
            userinfo_endpoint=_https(userinfo, "userinfo_endpoint") if userinfo else None,
            algorithms=frozenset(algorithms),
            token_auth_method=method,
        )

    def authorization_url(self, pending: PendingLogin) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._settings.client_id,
                "redirect_uri": self._settings.redirect_uri,
                "scope": self._settings.scopes,
                "state": pending.state,
                "nonce": pending.nonce,
                "code_challenge": code_challenge(pending.code_verifier),
                "code_challenge_method": "S256",
            }
        )
        return f"{self.metadata().authorization_endpoint}?{query}"

    def exchange_code(self, code: str, code_verifier: str) -> dict[str, Any]:
        metadata = self.metadata()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._settings.redirect_uri,
            "code_verifier": code_verifier,
        }
        auth = httpx.Auth()  # no-op unless the provider takes HTTP Basic client credentials
        secret = self._settings.client_secret
        if metadata.token_auth_method == BASIC_AUTH and secret is not None:
            auth = httpx.BasicAuth(self._settings.client_id, secret)
        else:
            form["client_id"] = self._settings.client_id
            if metadata.token_auth_method == POST_AUTH and secret is not None:
                form["client_secret"] = secret
        try:
            response = self._http.post(
                metadata.token_endpoint,
                data=form,
                auth=auth,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("oidc: token exchange failed (%s)", type(exc).__name__)
            raise OidcError("token_exchange_failed") from exc
        if not isinstance(body, dict) or not isinstance(body.get("id_token"), str):
            raise OidcError("token_response_invalid")
        return body

    def _signing_key(self, token: str) -> tuple[jwt.PyJWK, str]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise OidcError("id_token_malformed") from exc
        algorithm = header.get("alg")
        if algorithm not in self.metadata().algorithms:
            raise OidcError("id_token_algorithm_rejected")
        kid = header.get("kid")
        key = self._find_key(kid, refresh=False) or self._find_key(kid, refresh=True)
        if key is None:
            raise OidcError("id_token_key_unknown")
        return key, algorithm

    def _find_key(self, kid: object, *, refresh: bool) -> jwt.PyJWK | None:
        with self._lock:
            now = self._clock()
            stale = self._jwks is None or now - self._jwks_at > METADATA_TTL_SECONDS
            # A token signed with an unknown key may follow a key rotation. Refetch, but no
            # more than once a minute so forged tokens cannot hammer the provider.
            if stale or (refresh and now - self._jwks_at > JWKS_REFRESH_INTERVAL_SECONDS):
                if self._metadata is None:
                    raise OidcError("discovery_failed")
                document = self._get_json(self._metadata.jwks_uri, "jwks_failed")
                try:
                    self._jwks = jwt.PyJWKSet.from_dict(document)
                except jwt.PyJWTError as exc:
                    raise OidcError("jwks_invalid") from exc
                self._jwks_at = now
            if self._jwks is None:
                raise OidcError("jwks_failed")
            keys = [key for key in self._jwks.keys if key.public_key_use in (None, "sig")]
        if kid is None:
            return keys[0] if len(keys) == 1 else None
        return next((key for key in keys if key.key_id == kid), None)

    def validate_id_token(self, token: str, nonce: str) -> dict[str, Any]:
        key, algorithm = self._signing_key(token)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=[algorithm],
                audience=self._settings.client_id,
                issuer=self._settings.issuer,
                leeway=CLOCK_SKEW_SECONDS,
                options={"require": ["iss", "sub", "aud", "exp", "iat"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise OidcError("id_token_expired") from exc
        except jwt.PyJWTError as exc:
            logger.warning("oidc: ID token rejected (%s)", type(exc).__name__)
            raise OidcError("id_token_invalid") from exc
        audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        if len(audiences) > 1 and claims.get("azp") != self._settings.client_id:
            raise OidcError("id_token_invalid_azp")
        claimed_nonce = claims.get("nonce")
        if not isinstance(claimed_nonce, str) or not hmac.compare_digest(claimed_nonce, nonce):
            raise OidcError("id_token_nonce_mismatch")
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            raise OidcError("id_token_invalid")
        return claims

    def userinfo(self, access_token: str, subject: str) -> dict[str, Any]:
        endpoint = self.metadata().userinfo_endpoint
        if endpoint is None:
            return {}
        claims = self._get_json(
            endpoint, "userinfo_failed", headers={"Authorization": f"Bearer {access_token}"}
        )
        # OIDC Core 5.3.2: userinfo must describe the same subject as the ID token.
        if claims.get("sub") != subject:
            raise OidcError("userinfo_subject_mismatch")
        return claims
