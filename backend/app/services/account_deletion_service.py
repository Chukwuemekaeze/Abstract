"""Delete a user account: tear down every server the proper way, then purge the
Postgres user row, then delete the Clerk user.

This is the account-level counterpart to server_deletion_service. It exists so a
user deleting their account goes through *our* backend first, rather than deleting
their Clerk identity directly and orphaning the Postgres row (whose UNIQUE email
then collides on re-signup).

Ordering and safety:
  * Servers are torn down one at a time via server_deletion_service.delete_server,
    which strips Abstract's key off each VPS, restores password/root SSH login,
    tears down every project, and hard-deletes the server row. servers.user_id is
    ON DELETE CASCADE, so deleting the user row directly would drop the servers in
    the DB *without* this remote teardown, orphaning Abstract's key on live boxes.
    That is why we never rely on the cascade for servers.
  * Strict abort: if any server cannot be cleanly torn down (for example an
    unreachable VPS), the whole account deletion stops and raises
    AccountDeletionError naming the blocking server. Nothing else is touched: the
    user row and the Clerk user are left intact so the user can resolve the server
    and retry. Servers torn down before the failure stay deleted (their teardown is
    idempotent and already committed).
  * The Clerk user is deleted last. If it fails, the local DB is already purged, so
    the original email-collision bug is resolved; the lingering Clerk user can be
    cleaned up manually rather than leaving the user stuck.
"""

from uuid import UUID

import redis.asyncio as aioredis
from clerk_backend_api import Clerk
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_config import logger
from app.models import Server, User
from app.schemas.servers import ServerDeletionStepResult
from app.services.github_service import GithubService
from app.services.key_provider import KeyProvider
from app.services.server_deletion_service import (
    ServerDeletionError,
    ServerOperationInFlight,
    delete_server,
)
from app.services.ssh_service import SSHService

__all__ = [
    "AccountDeletionError",
    "ClerkAccountDeletionError",
    "delete_account",
]


class AccountDeletionError(Exception):
    """A server could not be torn down, so account deletion aborted. The user row
    and the Clerk user are intact. Carries the blocking server so the route can tell
    the user which one to resolve first, plus the underlying teardown step list."""

    def __init__(
        self,
        *,
        message: str,
        blocking_server_id: UUID,
        blocking_server_name: str,
        steps: list[ServerDeletionStepResult] | None = None,
    ):
        self.message = message
        self.blocking_server_id = blocking_server_id
        self.blocking_server_name = blocking_server_name
        self.steps = steps or []
        super().__init__(message)


class ClerkAccountDeletionError(Exception):
    """The Clerk user delete failed *after* the local DB was already purged. The
    email-collision bug is resolved (our row is gone); the lingering Clerk user may
    need manual cleanup. Distinct from AccountDeletionError because the outcome is
    different: nothing to retry against our DB, and the account is effectively gone
    from the app's point of view."""


async def delete_account(
    *,
    current_user: User,
    session_id: str,
    db: AsyncSession,
    ssh: SSHService,
    redis: aioredis.Redis,
    key_provider: KeyProvider,
    clerk: Clerk,
    github: GithubService,
) -> None:
    """Tear down every server, then delete the user row, then delete the Clerk user.

    Raises AccountDeletionError (nothing beyond already-completed server teardowns
    touched) if a server cannot be cleanly deleted."""
    result = await db.execute(
        select(Server)
        .where(Server.user_id == current_user.id)
        .order_by(Server.created_at.asc())
    )
    servers = list(result.scalars().all())

    for server in servers:
        server_id = server.id
        server_name = server.name
        # The server's own active_operation ("deleting") guard lives in the server
        # route, not in delete_server; enforce it here too so account deletion never
        # bulldozes a server that is already mid-operation.
        if server.active_operation is not None:
            raise AccountDeletionError(
                message=(
                    f"Server '{server_name}' is busy ({server.active_operation}), so "
                    f"your account can't be deleted yet. Wait for that to finish and "
                    f"try again."
                ),
                blocking_server_id=server_id,
                blocking_server_name=server_name,
            )
        try:
            await delete_server(
                server=server,
                current_user=current_user,
                session_id=session_id,
                db=db,
                ssh=ssh,
                redis=redis,
                key_provider=key_provider,
                clerk=clerk,
                github=github,
            )
        except ServerOperationInFlight as exc:
            raise AccountDeletionError(
                message=(
                    f"Server '{server_name}' is busy, so your account can't be "
                    f"deleted yet. Wait for that operation to finish and try again."
                ),
                blocking_server_id=server_id,
                blocking_server_name=server_name,
            ) from exc
        except ServerDeletionError as exc:
            raise AccountDeletionError(
                message=(
                    f"Server '{server_name}' could not be torn down "
                    f"({exc.message}). Resolve or delete that server, then delete "
                    f"your account again."
                ),
                blocking_server_id=server_id,
                blocking_server_name=server_name,
                steps=exc.steps,
            ) from exc

    # Every server is gone (and with it every project, app key, and cached SSH
    # state). Purge the user row. There are no remaining server-scoped rows to
    # cascade at this point.
    clerk_user_id = current_user.clerk_user_id
    await db.delete(current_user)
    await db.commit()

    # Clerk last: the local DB is already clean, so a failure here doesn't strand
    # the user in the email-collision state that motivated this whole flow.
    try:
        await clerk.users.delete_async(user_id=clerk_user_id)
    except Exception as exc:
        logger.exception(
            "Deleted local account for clerk_user_id={} but the Clerk user delete "
            "failed; it may need manual cleanup.",
            clerk_user_id,
        )
        raise ClerkAccountDeletionError(
            "Your data was deleted, but removing your login failed. Please try "
            "signing out; contact support if you can still sign in."
        ) from exc
