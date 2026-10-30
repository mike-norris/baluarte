"""Encryption of secrets stored in the database (ADR-0008).

Secrets are sealed with AES-256-GCM under a key read from ``BALUARTE_SECRET_KEY_FILE``: 32 random
bytes, base64-encoded. Each ciphertext records the ID of the key that sealed it, and the secret's
name is bound as associated data, so a ciphertext copied to another name does not decrypt.
During rotation the old key is read from ``BALUARTE_SECRET_KEY_PREVIOUS_FILE`` and only used to
decrypt; ``baluarte-reencrypt-secrets`` then seals every stored secret under the current key.
"""

import base64
import binascii
import hashlib
import os
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from baluarte.settings import ConfigurationError, read_secret

KEY_VARIABLE = "BALUARTE_SECRET_KEY"
PREVIOUS_KEY_VARIABLE = "BALUARTE_SECRET_KEY_PREVIOUS"
KEY_BYTES = 32
NONCE_BYTES = 12


class SecretUnavailable(ConfigurationError):
    """A stored secret cannot be decrypted: no key, a different key, or tampered data."""


def key_id(key: bytes) -> str:
    """A short, non-secret identifier for a key, stored next to each ciphertext."""
    return hashlib.sha256(b"baluarte-secret-key-id:" + key).hexdigest()[:16]


def _associated_data(name: str) -> bytes:
    return f"baluarte-secret:{name}".encode()


@dataclass(frozen=True)
class Sealed:
    key_id: str
    nonce: bytes
    ciphertext: bytes


@dataclass(frozen=True)
class SecretBox:
    key: bytes = field(repr=False)
    previous: tuple[bytes, ...] = field(default=(), repr=False)

    @property
    def key_id(self) -> str:
        return key_id(self.key)

    def seal(self, name: str, plaintext: str) -> Sealed:
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(self.key).encrypt(
            nonce, plaintext.encode("utf-8"), _associated_data(name)
        )
        return Sealed(self.key_id, nonce, ciphertext)

    def open(self, name: str, sealed: Sealed) -> str:
        for candidate in (self.key, *self.previous):
            if key_id(candidate) != sealed.key_id:
                continue
            try:
                plaintext = AESGCM(candidate).decrypt(
                    sealed.nonce, sealed.ciphertext, _associated_data(name)
                )
            except InvalidTag as exc:
                raise SecretUnavailable(f"Stored secret {name} failed its integrity check") from exc
            return plaintext.decode("utf-8")
        raise SecretUnavailable(
            f"Stored secret {name} was encrypted with a key that is not configured "
            f"(key ID {sealed.key_id})"
        )


def _decode(name: str, raw: str) -> bytes:
    try:
        key = base64.b64decode(raw.replace("-", "+").replace("_", "/"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ConfigurationError(f"{name}_FILE must hold a base64-encoded key") from exc
    if len(key) != KEY_BYTES:
        raise ConfigurationError(
            f"{name}_FILE must hold {KEY_BYTES} random bytes, base64-encoded "
            "(for example: openssl rand -base64 32)"
        )
    return key


def load_secret_box() -> SecretBox | None:
    """The configured key, or None when ``BALUARTE_SECRET_KEY_FILE`` is not set."""
    raw = read_secret(KEY_VARIABLE)
    if raw is None:
        return None
    previous_raw = read_secret(PREVIOUS_KEY_VARIABLE)
    previous = (_decode(PREVIOUS_KEY_VARIABLE, previous_raw),) if previous_raw else ()
    return SecretBox(_decode(KEY_VARIABLE, raw), previous)
