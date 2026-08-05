"""Account API.

Backend-driven account deletion. The user deletes their account against us, not
against Clerk directly: we tear down every server the proper way (removing
Abstract's key from each VPS), purge the Postgres user row, then delete the Clerk
user. This keeps our DB and Clerk in sync and avoids the orphaned-row / email
collision that a direct Clerk deletion causes.
"""

import redis.asyncio as aioredis
from clerk_backend_api import Clerk
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.clerk import get_clerk_client
from app.db import get_db
from app.deps.auth import get_current_session_id, get_current_user
from app.deps.services import (
    get_github_service,
    get_key_provider_dep,
    get_ssh_service,
)
from app.logging_config import logger
from app.models import User
from app.redis_client import get_redis
from app.schemas.account import CurrentUserResponse, DeleteAccountResponse
from app.services.account_deletion_service import (
    AccountDeletionError,
    ClerkAccountDeletionError,
    delete_account,
)
from app.services.github_service import GithubService
from app.services.key_provider import KeyProvider
from app.services.ssh_service import SSHService

router = APIRouter(prefix="/api/account", tags=["account"])


@router.get("/me", response_model=CurrentUserResponse)
async def get_me_route(
    current_user: User = Depends(get_current_user),
) -> CurrentUserResponse:
    """Return the signed-in user's own identity.

    The frontend fetches this so it can tag its monitoring events with the same
    Postgres user id the backend uses, correlating client and server events.
    """
    return CurrentUserResponse(id=current_user.id, email=current_user.email)


@router.delete("", response_model=DeleteAccountResponse)
async def delete_account_route(
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
    clerk: Clerk = Depends(get_clerk_client),
    github: GithubService = Depends(get_github_service),
) -> DeleteAccountResponse:
    """Delete the signed-in user's account: tear down every server, delete the
    Postgres user row, then delete the Clerk user.

    409 with the blocking server named if a server can't be cleanly torn down
    (for example an unreachable VPS); nothing is deleted beyond servers that were
    already torn down before the failure, and the account itself is left intact so
    the user can resolve the server and retry.
    """
    logger.info("Delete account start: user={}", current_user.id)
    try:
        await delete_account(
            current_user=current_user,
            session_id=session_id,
            db=db,
            redis=redis,
            ssh=ssh,
            key_provider=key_provider,
            clerk=clerk,
            github=github,
        )
    except AccountDeletionError as exc:
        logger.warning(
            "Delete account blocked: user={} by server={}",
            current_user.id,
            exc.blocking_server_id,
        )
        raise HTTPException(
            409,
            detail={
                "message": exc.message,
                "blocking_server_id": str(exc.blocking_server_id),
                "blocking_server_name": exc.blocking_server_name,
                "steps": [step.model_dump(mode="json") for step in exc.steps],
            },
        ) from exc
    except ClerkAccountDeletionError as exc:
        logger.error("Delete account: local purge done but Clerk delete failed")
        raise HTTPException(502, detail={"message": str(exc)}) from exc

    logger.info("Delete account ok: user={} account fully deleted", current_user.id)
    return DeleteAccountResponse(success=True)
