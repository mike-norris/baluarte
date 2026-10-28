import base64
import json
import time
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from oidc_fake import CLIENT_ID, ISSUER, FakeIdP, make_settings, new_key, public_jwk

from baluarte.auth.oidc import OidcClient, OidcError, code_challenge, new_pending_login


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def idp() -> FakeIdP:
    fake = FakeIdP()
    fake.nonce = "expected-nonce"
    return fake


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(idp: FakeIdP, clock: Clock) -> OidcClient:
    return idp.client(make_settings(), clock)


def paths(idp: FakeIdP) -> list[str]:
    return [request.url.path for request in idp.requests]


def test_code_challenge_matches_rfc7636_example() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_authorization_url(client: OidcClient, idp: FakeIdP) -> None:
    pending = new_pending_login()

    query = idp.authorize(client.authorization_url(pending))

    assert query == {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": "https://baluarte.example.test/api/v1/auth/callback",
        "scope": "openid profile email",
        "state": pending.state,
        "nonce": pending.nonce,
        "code_challenge": code_challenge(pending.code_verifier),
        "code_challenge_method": "S256",
    }


def test_metadata_is_cached_then_refreshed(client: OidcClient, idp: FakeIdP, clock: Clock) -> None:
    client.metadata()
    client.metadata()
    assert paths(idp) == ["/.well-known/openid-configuration"]

    clock.now += 3601
    client.metadata()
    assert len(idp.requests) == 2


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"issuer": "https://evil.example.test"}, "discovery_issuer_mismatch"),
        ({"token_endpoint": "http://idp.example.test/token"}, "discovery_invalid_token_endpoint"),
        ({"jwks_uri": None}, "discovery_invalid_jwks_uri"),
        ({"id_token_signing_alg_values_supported": ["none", "HS256"]}, "discovery_no_supported"),
        ({"token_endpoint_auth_methods_supported": ["private_key_jwt"]}, "no_supported_client"),
    ],
)
def test_discovery_is_validated(
    client: OidcClient, idp: FakeIdP, change: dict[str, object], reason: str
) -> None:
    idp.discovery.update(change)

    with pytest.raises(OidcError) as raised:
        client.metadata()
    assert reason in raised.value.reason


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectTimeout("timed out"), httpx.ConnectError("refused")],
)
def test_unreachable_provider(client: OidcClient, idp: FakeIdP, failure: Exception) -> None:
    idp.fail_with = failure

    with pytest.raises(OidcError, match="discovery_failed"):
        client.metadata()


def test_valid_id_token(client: OidcClient, idp: FakeIdP) -> None:
    claims = client.validate_id_token(idp.id_token(), "expected-nonce")

    assert claims["sub"] == "user-1"
    assert claims["groups"] == ["baluarte-analysts"]


@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"exp": int(time.time()) - 120}, "id_token_expired"),
        ({"aud": "someone-else"}, "id_token_invalid"),
        ({"iss": "https://evil.example.test"}, "id_token_invalid"),
        ({"nonce": "replayed-nonce"}, "id_token_nonce_mismatch"),
        ({"nonce": None}, "id_token_nonce_mismatch"),
        ({"iat": None}, "id_token_invalid"),
        ({"aud": [CLIENT_ID, "other"]}, "id_token_invalid_azp"),
    ],
)
def test_invalid_claims_are_rejected(
    client: OidcClient, idp: FakeIdP, claims: dict[str, object], reason: str
) -> None:
    token = idp.id_token(**claims)

    with pytest.raises(OidcError) as raised:
        client.validate_id_token(token, "expected-nonce")
    assert raised.value.reason == reason


def test_multiple_audiences_with_matching_azp(client: OidcClient, idp: FakeIdP) -> None:
    token = idp.id_token(aud=[CLIENT_ID, "other"], azp=CLIENT_ID)

    assert client.validate_id_token(token, "expected-nonce")["sub"] == "user-1"


def test_token_signed_by_another_key_is_rejected(client: OidcClient, idp: FakeIdP) -> None:
    with pytest.raises(OidcError, match="id_token_invalid"):
        client.validate_id_token(idp.id_token(key=new_key()), "expected-nonce")


def _unsigned(payload: dict[str, object]) -> str:
    def encode(part: bytes) -> str:
        return base64.urlsafe_b64encode(part).rstrip(b"=").decode()

    header = encode(b'{"alg":"none","typ":"JWT","kid":"key-1"}')
    return f"{header}.{encode(json.dumps(payload).encode())}."


def test_alg_none_is_rejected(client: OidcClient, idp: FakeIdP) -> None:
    payload = jwt.decode(idp.id_token(), options={"verify_signature": False})

    with pytest.raises(OidcError, match="id_token_algorithm_rejected"):
        client.validate_id_token(_unsigned(payload), "expected-nonce")


def test_hmac_with_public_key_is_rejected(client: OidcClient, idp: FakeIdP) -> None:
    payload = jwt.decode(idp.id_token(), options={"verify_signature": False})
    forged = jwt.encode(
        payload,
        "forged-shared-secret-at-least-32-bytes",
        algorithm="HS256",
        headers={"kid": "key-1"},
    )

    with pytest.raises(OidcError, match="id_token_algorithm_rejected"):
        client.validate_id_token(forged, "expected-nonce")


def test_key_rotation_refetches_jwks(client: OidcClient, idp: FakeIdP, clock: Clock) -> None:
    client.validate_id_token(idp.id_token(), "expected-nonce")
    rotated = new_key()
    idp.jwks_keys = [public_jwk(rotated, "key-2")]
    idp.kid = "key-2"
    clock.now += 61

    claims = client.validate_id_token(idp.id_token(key=rotated), "expected-nonce")

    assert claims["sub"] == "user-1"
    assert paths(idp).count("/jwks") == 2


def test_unknown_key_refetch_is_rate_limited(
    client: OidcClient, idp: FakeIdP, clock: Clock
) -> None:
    client.validate_id_token(idp.id_token(), "expected-nonce")

    for _ in range(3):
        with pytest.raises(OidcError, match="id_token_key_unknown"):
            client.validate_id_token(
                idp.id_token(key=new_key(), headers={"kid": "forged"}), "expected-nonce"
            )

    assert paths(idp).count("/jwks") == 1


def test_exchange_uses_basic_auth_and_pkce(client: OidcClient, idp: FakeIdP) -> None:
    pending = new_pending_login()
    idp.authorize(client.authorization_url(pending))

    tokens = client.exchange_code(idp.code, pending.code_verifier)

    request = idp.requests[-1]
    form = parse_qs(request.content.decode())
    assert request.headers["Authorization"].startswith("Basic ")
    assert "client_secret" not in form
    assert form["redirect_uri"] == ["https://baluarte.example.test/api/v1/auth/callback"]
    assert isinstance(tokens["id_token"], str)


def test_exchange_with_client_secret_post(idp: FakeIdP) -> None:
    idp.discovery["token_endpoint_auth_methods_supported"] = ["client_secret_post"]
    client = idp.client(make_settings())
    pending = new_pending_login()
    idp.authorize(client.authorization_url(pending))

    client.exchange_code(idp.code, pending.code_verifier)

    request = idp.requests[-1]
    form = parse_qs(request.content.decode())
    assert "Authorization" not in request.headers
    assert form["client_id"] == [CLIENT_ID]
    assert form["client_secret"] == ["test-client-secret"]


def test_public_client_sends_no_secret(idp: FakeIdP) -> None:
    client = idp.client(make_settings(client_secret=None))
    pending = new_pending_login()
    idp.authorize(client.authorization_url(pending))

    client.exchange_code(idp.code, pending.code_verifier)

    request = idp.requests[-1]
    assert "Authorization" not in request.headers
    assert parse_qs(request.content.decode())["client_id"] == [CLIENT_ID]


def test_wrong_code_verifier_fails(client: OidcClient, idp: FakeIdP) -> None:
    pending = new_pending_login()
    idp.authorize(client.authorization_url(pending))

    with pytest.raises(OidcError, match="token_exchange_failed"):
        client.exchange_code(idp.code, "not-the-verifier")


def test_token_endpoint_error(client: OidcClient, idp: FakeIdP) -> None:
    idp.token_status = 503

    with pytest.raises(OidcError, match="token_exchange_failed"):
        client.exchange_code(idp.code, "verifier")


def test_userinfo_subject_must_match(client: OidcClient, idp: FakeIdP) -> None:
    with pytest.raises(OidcError, match="userinfo_subject_mismatch"):
        client.userinfo("access-1", subject="someone-else")


def test_userinfo_is_skipped_without_endpoint(idp: FakeIdP) -> None:
    del idp.discovery["userinfo_endpoint"]

    assert idp.client(make_settings()).userinfo("access-1", "user-1") == {}


def test_error_messages_contain_no_provider_detail(client: OidcClient, idp: FakeIdP) -> None:
    idp.discovery["issuer"] = ISSUER + "/secret-tenant"

    with pytest.raises(OidcError) as raised:
        client.metadata()
    assert str(raised.value) == "discovery_issuer_mismatch"
