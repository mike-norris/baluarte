"""Built-in TOTP, the second factor when the identity provider does not prove MFA
(docs/SECURITY.md#built-in-totp).

RFC 6238 with the parameters every authenticator app supports (Authy, Google Authenticator,
1Password, Microsoft Authenticator): HMAC-SHA1, 6 digits, 30-second steps. A code is accepted
one step either side of now for clock drift, and never twice: the last accepted step is kept and
only a later one is accepted. Secrets are sealed with the deployment's secret key like other
stored secrets; recovery codes are stored as SHA-256 digests only.
"""

import base64
import hashlib
import hmac
import secrets
import struct
from urllib.parse import quote, urlencode

from reportlab.graphics.barcode import qrencoder

ISSUER = "Baluarte"
DIGITS = 6
PERIOD_SECONDS = 30
# Steps either side of now that are accepted, for clock drift.
WINDOW = 1
SECRET_BYTES = 20
RECOVERY_CODES = 10
# Wrong codes a sign-in may enter before its session is ended.
MAX_FAILURES = 5
# Recovery codes: 10 characters from an alphabet without look-alikes (no 0/o, 1/l/i).
_RECOVERY_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"


def new_secret() -> str:
    """A new shared secret, base32 without padding, as authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded)


def step_at(unix_seconds: float) -> int:
    return int(unix_seconds // PERIOD_SECONDS)


def code_at(secret: str, step: int) -> str:
    """The code for one time step (RFC 4226 dynamic truncation)."""
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def verify(secret: str, code: str, unix_seconds: float, last_step: int | None) -> int | None:
    """The step ``code`` belongs to, if it is valid now and later than ``last_step``."""
    entered = "".join(code.split())
    if len(entered) != DIGITS or not all("0" <= ch <= "9" for ch in entered):
        return None
    now = step_at(unix_seconds)
    for step in range(now - WINDOW, now + WINDOW + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(code_at(secret, step), entered):
            return step
    return None


def otpauth_uri(secret: str, account: str) -> str:
    """The provisioning URI authenticator apps read from the QR code."""
    label = quote(f"{ISSUER}:{account}", safe=":@")
    query = urlencode(
        {
            "secret": secret,
            "issuer": ISSUER,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": PERIOD_SECONDS,
        }
    )
    return f"otpauth://totp/{label}?{query}"


def qr_rows(text: str) -> list[str]:
    """The QR code for ``text`` as rows of "1" (dark) and "0" (light), without the quiet zone.

    The browser draws it as SVG, so no image or inline markup needs to pass the page's CSP.
    """
    code = qrencoder.QRCode(None, qrencoder.QRErrorCorrectLevel.M)
    code.addData(text)
    code.make()
    modules: list[list[bool]] = code.modules
    return ["".join("1" if dark else "0" for dark in row) for row in modules]


def new_recovery_codes() -> list[str]:
    """One-time codes for a lost authenticator, shown once as ``xxxxx-xxxxx``."""
    codes = []
    for _ in range(RECOVERY_CODES):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def normalize_recovery_code(code: str) -> str:
    return "".join(ch for ch in code.lower() if ch in _RECOVERY_ALPHABET or ch in "01ilo")


def recovery_digest(code: str) -> str:
    return hashlib.sha256(normalize_recovery_code(code).encode("ascii", "ignore")).hexdigest()


def looks_like_recovery_code(code: str) -> bool:
    normalized = normalize_recovery_code(code)
    return len(normalized) == 10 and all(ch in _RECOVERY_ALPHABET for ch in normalized)
