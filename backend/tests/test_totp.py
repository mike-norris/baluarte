"""RFC 6238 arithmetic, provisioning URIs, QR codes and recovery codes for built-in TOTP."""

import base64
from urllib.parse import parse_qs, urlsplit

import pytest

from baluarte.auth import totp

# RFC 6238 appendix B: the SHA-1 seed, and the 8-digit codes truncated to the 6 apps show.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")


@pytest.mark.parametrize(
    ("unix", "code"),
    [
        (59, "287082"),
        (1111111109, "081804"),
        (1111111111, "050471"),
        (1234567890, "005924"),
        (2000000000, "279037"),
        (20000000000, "353130"),
    ],
)
def test_codes_match_rfc_6238(unix: int, code: str) -> None:
    assert totp.code_at(RFC_SECRET, totp.step_at(unix)) == code


def test_a_code_is_accepted_one_step_either_side_and_never_again() -> None:
    now = 1_700_000_000.0
    step = totp.step_at(now)

    for offset in (-1, 0, 1):
        code = totp.code_at(RFC_SECRET, step + offset)
        assert totp.verify(RFC_SECRET, code, now, None) == step + offset
        assert totp.verify(RFC_SECRET, code, now, step + offset) is None
    assert totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, step + 2), now, None) is None
    assert totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, step - 2), now, None) is None
    # An earlier code is refused once a later one was used.
    assert totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, step - 1), now, step) is None


@pytest.mark.parametrize(
    "code", ["", "12345", "1234567", "12a456", "\uff11\uff12\uff13\uff14\uff15\uff16"]
)
def test_malformed_codes_are_refused(code: str) -> None:
    assert totp.verify(RFC_SECRET, code, 59, None) is None


def test_spaces_in_a_code_are_ignored() -> None:
    assert totp.verify(RFC_SECRET, "287 082", 59, None) == 1


def test_new_secrets_are_160_bit_base32() -> None:
    secret = totp.new_secret()

    assert len(secret) == 32
    assert len(base64.b32decode(secret)) == 20
    assert secret != totp.new_secret()


def test_provisioning_uri_names_the_issuer_and_parameters() -> None:
    uri = totp.otpauth_uri("JBSWY3DPEHPK3PXP", "ada lovelace@example.test")
    parts = urlsplit(uri)

    assert parts.scheme == "otpauth"
    assert parts.netloc == "totp"
    assert parts.path == "/Baluarte:ada%20lovelace@example.test"
    assert parse_qs(parts.query) == {
        "secret": ["JBSWY3DPEHPK3PXP"],
        "issuer": ["Baluarte"],
        "algorithm": ["SHA1"],
        "digits": ["6"],
        "period": ["30"],
    }


def test_qr_code_is_a_square_grid_with_finder_patterns() -> None:
    rows = totp.qr_rows(totp.otpauth_uri(totp.new_secret(), "ada@example.test"))

    size = len(rows)
    assert size >= 21 and (size - 17) % 4 == 0
    assert all(len(row) == size and set(row) <= {"0", "1"} for row in rows)
    for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
        assert rows[top][left : left + 7] == "1111111"
        assert rows[top + 1][left : left + 7] == "1000001"
        assert rows[top + 3][left : left + 7] == "1011101"


def test_recovery_codes_are_one_of_a_kind_and_normalised() -> None:
    codes = totp.new_recovery_codes()

    assert len(codes) == totp.RECOVERY_CODES == len(set(codes))
    for code in codes:
        assert len(code) == 11 and code[5] == "-"
        assert totp.looks_like_recovery_code(code)
        assert totp.recovery_digest(code) == totp.recovery_digest(f" {code.upper()} ")
        assert totp.recovery_digest(code) == totp.recovery_digest(code.replace("-", ""))
    assert not totp.looks_like_recovery_code("123456")
    assert not totp.looks_like_recovery_code("abcde-fghi0")
