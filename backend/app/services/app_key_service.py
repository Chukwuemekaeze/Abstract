"""App SSH key bootstrapping.

Generates and stores the app managed keypair used to authenticate to a server after
the public key has been installed. One keypair per server, generated fresh when the
server is registered, so a compromised key never exposes the user's other servers.
"""

import asyncssh
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSshKey, Server
from app.services.key_provider import KeyProvider


class AppKeyMissing(Exception):
    """No app SSH key exists for a server. Never expected for a verified server."""


async def create_key_for_server(
    server: Server,
    db: AsyncSession,
    key_provider: KeyProvider,
) -> AppSshKey:
    """Generate and persist a fresh ed25519 keypair scoped to this server.

    Does NOT commit: the caller's route handler owns the transaction. Flushes so the
    generated id and server defaults are populated on the returned row.
    """
    private_key = asyncssh.generate_private_key("ssh-ed25519")
    # The comment lands in the server's authorized_keys, letting the user trace an
    # installed key back to a specific Abstract server.
    private_key.set_comment(f"abstract-server-{server.id.hex[:8]}")

    # OpenSSH format public key with the recognizable comment, decoded to str.
    public_openssh = private_key.export_public_key().decode("utf-8").strip()
    # OpenSSH format private key bytes, encrypted at rest.
    private_openssh = private_key.export_private_key()
    encrypted_private = await key_provider.encrypt(private_openssh)

    app_key = AppSshKey(
        server_id=server.id,
        public_key=public_openssh,
        encrypted_private_key=encrypted_private,
        key_type="ssh-ed25519",
        encryption_key_id=key_provider.key_id,
    )
    db.add(app_key)
    await db.flush()
    return app_key


async def get_key_for_server(server: Server, db: AsyncSession) -> AppSshKey:
    """Fetch the app SSH key for a server. Raises AppKeyMissing if none exists."""
    app_key = await db.scalar(
        select(AppSshKey).where(AppSshKey.server_id == server.id)
    )
    if app_key is None:
        raise AppKeyMissing(f"No app SSH key found for server {server.id}.")
    return app_key
