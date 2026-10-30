"""Sealing secrets stored in the database (ADR-0008). No database needed."""

import base64
import os
from dataclasses import replace
from pathlib import Path

import pytest

from baluarte.configuration.crypto import SecretBox, SecretUnavailable, key_id, load_secret_box
from baluarte.logs import REDACTED, redact
from baluarte.settings import ConfigurationError


def box() -> SecretBox:
    return SecretBox(os.urandom(32))


def test_round_trip() -> None:
    sealer = box()

    sealed = sealer.seal("gitlab_token", "glpat-roundtrip")

    assert sealed.key_id == key_id(sealer.key)
    assert len(sealed.nonce) == 12
    assert b"glpat-roundtrip" not in sealed.ciphertext
    assert sealer.open("gitlab_token", sealed) == "glpat-roundtrip"


def test_every_seal_uses_a_new_nonce() -> None:
    sealer = box()

    assert sealer.seal("gitlab_token", "same").nonce != sealer.seal("gitlab_token", "same").nonce


def test_a_ciphertext_does_not_open_under_another_name() -> None:
    sealer = box()
    sealed = sealer.seal("gitlab_token", "value")

    with pytest.raises(SecretUnavailable, match="integrity"):
        sealer.open("oidc_client_secret", sealed)


def test_tampering_is_detected() -> None:
    sealer = box()
    sealed = sealer.seal("gitlab_token", "value")
    flipped = bytes([sealed.ciphertext[0] ^ 1]) + sealed.ciphertext[1:]

    with pytest.raises(SecretUnavailable, match="integrity"):
        sealer.open("gitlab_token", replace(sealed, ciphertext=flipped))


def test_another_key_cannot_open_it() -> None:
    sealed = box().seal("gitlab_token", "value")

    with pytest.raises(SecretUnavailable, match="not configured"):
        box().open("gitlab_token", sealed)


def test_the_previous_key_opens_old_secrets() -> None:
    old = box()
    sealed = old.seal("gitlab_token", "value")

    assert SecretBox(os.urandom(32), previous=(old.key,)).open("gitlab_token", sealed) == "value"


def test_the_key_is_not_in_repr() -> None:
    sealer = box()

    assert repr(sealer.key) not in repr(sealer)


def test_no_key_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BALUARTE_SECRET_KEY_FILE", raising=False)

    assert load_secret_box() is None


def test_key_from_file_is_redacted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    raw = base64.b64encode(os.urandom(32)).decode()
    path = tmp_path / "key"
    path.write_text(raw + "\n")
    monkeypatch.setenv("BALUARTE_SECRET_KEY_FILE", str(path))
    monkeypatch.delenv("BALUARTE_SECRET_KEY_PREVIOUS_FILE", raising=False)

    loaded = load_secret_box()

    assert loaded is not None
    assert loaded.key == base64.b64decode(raw)
    assert redact(f"key {raw}") == f"key {REDACTED}"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not base64!", "base64-encoded"),
        (base64.b64encode(b"short").decode(), "32 random bytes"),
    ],
)
def test_invalid_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str, message: str
) -> None:
    path = tmp_path / "key"
    path.write_text(content)
    monkeypatch.setenv("BALUARTE_SECRET_KEY_FILE", str(path))

    with pytest.raises(ConfigurationError, match=message):
        load_secret_box()


def test_the_key_cannot_be_set_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BALUARTE_SECRET_KEY", "x" * 44)

    with pytest.raises(ConfigurationError, match="must not be set directly"):
        load_secret_box()
