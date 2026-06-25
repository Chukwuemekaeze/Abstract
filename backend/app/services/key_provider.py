"""Key provider abstraction for encrypting SSH private keys at rest.

v1 ships EnvKeyProvider, which uses Fernet keyed from APP_MASTER_KEY. The
APP_MASTER_KEY value is a urlsafe base64 encoded 32 byte key, which is exactly the
key format Fernet expects. A KMS backed provider will be added later behind the same
interface.
"""

from typing import Protocol, runtime_checkable

from cryptography.fernet import Fernet

from app.config import Settings


@runtime_checkable
class KeyProvider(Protocol):
    async def encrypt(self, plaintext: bytes) -> bytes: ...

    async def decrypt(self, ciphertext: bytes) -> bytes: ...

    @property
    def key_id(self) -> str: ...


class EnvKeyProvider:
    """Fernet based provider. Key bytes come from APP_MASTER_KEY in the environment."""

    def __init__(self, master_key: str) -> None:
        # Fernet validates the key format and raises if it is not 32 url-safe
        # base64 encoded bytes.
        self._fernet = Fernet(master_key.encode("utf-8"))

    async def encrypt(self, plaintext: bytes) -> bytes:
        return self._fernet.encrypt(plaintext)

    async def decrypt(self, ciphertext: bytes) -> bytes:
        return self._fernet.decrypt(ciphertext)

    @property
    def key_id(self) -> str:
        return "env-v1"


def get_key_provider(settings: Settings) -> KeyProvider:
    if settings.key_provider == "env":
        return EnvKeyProvider(settings.app_master_key)
    # "kms" will be added later behind this same interface.
    raise NotImplementedError(
        f"Key provider '{settings.key_provider}' is not implemented. Only 'env' exists in v1."
    )
