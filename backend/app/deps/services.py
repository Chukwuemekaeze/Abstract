"""Dependencies that provide service singletons."""

from fastapi import Depends

from app.config import Settings, get_settings
from app.services.hardening_service import HardeningService, hardening_service
from app.services.key_provider import KeyProvider, get_key_provider
from app.services.ssh_service import SSHService, ssh_service


def get_ssh_service() -> SSHService:
    return ssh_service


def get_hardening_service() -> HardeningService:
    return hardening_service


def get_key_provider_dep(
    settings: Settings = Depends(get_settings),
) -> KeyProvider:
    return get_key_provider(settings)
