"""EnvKeyProvider encrypt/decrypt roundtrip."""

import base64
import secrets

import pytest

from app.services.key_provider import EnvKeyProvider, get_key_provider


def _master_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


async def test_encrypt_decrypt_roundtrip():
    provider = EnvKeyProvider(_master_key())
    plaintext = b"super secret ssh private key bytes"

    ciphertext = await provider.encrypt(plaintext)
    assert ciphertext != plaintext

    recovered = await provider.decrypt(ciphertext)
    assert recovered == plaintext


async def test_key_id_is_env_v1():
    provider = EnvKeyProvider(_master_key())
    assert provider.key_id == "env-v1"


def test_get_key_provider_rejects_unknown():
    class FakeSettings:
        key_provider = "kms"
        app_master_key = _master_key()

    with pytest.raises(NotImplementedError):
        get_key_provider(FakeSettings())
