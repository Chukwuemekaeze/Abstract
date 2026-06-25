"""App SSH key bootstrapping.

Generates and stores the app managed keypair used to authenticate to the user's
servers after the public key has been installed. v1 keeps one key per user and
creates it lazily the first time the user registers a server.
"""

from uuid import UUID

import asyncssh
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSshKey
from app.services.key_provider import KeyProvider


async def ensure_app_key_for_user(
    user_id: UUID,
    db: AsyncSession,
    key_provider: KeyProvider,
) -> AppSshKey:
    existing = await db.scalar(
        select(AppSshKey)
        .where(AppSshKey.user_id == user_id)
        .order_by(AppSshKey.created_at.desc())
    )
    if existing is not None:
        return existing

    private_key = asyncssh.generate_private_key("ssh-ed25519")
    user_id_short = str(user_id).split("-")[0]
    private_key.set_comment(f"app-deploy-{user_id_short}")

    # OpenSSH format public key with a recognizable comment, decoded to str.
    public_openssh = private_key.export_public_key().decode("utf-8").strip()
    # OpenSSH format private key bytes, encrypted at rest.
    private_openssh = private_key.export_private_key()
    encrypted_private = await key_provider.encrypt(private_openssh)

    app_key = AppSshKey(
        user_id=user_id,
        public_key=public_openssh,
        encrypted_private_key=encrypted_private,
        key_type="ssh-ed25519",
        encryption_key_id=key_provider.key_id,
    )
    db.add(app_key)
    await db.commit()
    await db.refresh(app_key)
    return app_key
