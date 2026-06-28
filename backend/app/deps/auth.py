"""Clerk based authentication.

Identity enters the system in exactly one place: the Clerk JWT in the Authorization
header, verified by the Clerk SDK. The values pulled from a verified token (sub, sid)
are server trustable credentials, so they do not carry the _from_client suffix.

get_clerk_auth_state verifies the token and returns the verified claims.
get_current_user maps those claims to a Postgres user row, lazily syncing from Clerk
on the first request from a new user. get_current_session_id exposes the Clerk
session id for downstream code that scopes per login (the SSH key cache).
"""

from dataclasses import dataclass

from clerk_backend_api import AuthenticateRequestOptions, Clerk
from clerk_backend_api.models.user import User as ClerkUser
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.clerk import get_clerk_client
from app.config import Settings, get_settings
from app.db import get_db
from app.logging_config import logger
from app.models import User


@dataclass
class ClerkAuthState:
    clerk_user_id: str
    session_id: str


async def get_clerk_auth_state(
    request: Request,
    clerk: Clerk = Depends(get_clerk_client),
    settings: Settings = Depends(get_settings),
) -> ClerkAuthState:
    """Verify the Clerk JWT from the Authorization header.

    Returns the verified claims (clerk_user_id, session_id). Raises 401 when no
    token is present, the signature is invalid, the token is expired, or the
    authorized party does not match. authenticate_request performs local JWT
    verification (no per request network call once the JWKS is warm).
    """
    request_state = clerk.authenticate_request(
        request,
        AuthenticateRequestOptions(
            authorized_parties=settings.clerk_authorized_parties,
        ),
    )
    if not request_state.is_signed_in:
        # Surface Clerk's own reason so auth failures are diagnosable.
        logger.warning(
            "Clerk auth rejected request: reason={} message={}",
            getattr(request_state, "reason", None),
            getattr(request_state, "message", None),
        )
        raise HTTPException(401, "Not authenticated")

    payload = request_state.payload or {}
    clerk_user_id = payload.get("sub")
    session_id = payload.get("sid")
    if not clerk_user_id or not session_id:
        raise HTTPException(401, "Malformed token")

    return ClerkAuthState(clerk_user_id=clerk_user_id, session_id=session_id)


def _extract_primary_email(clerk_user: ClerkUser) -> str:
    """Pull the verified primary email from a Clerk user object.

    Prefers the address matching primary_email_address_id, falling back to the
    first available address. Raises 400 when Clerk has no email for the user.
    """
    addresses = clerk_user.email_addresses or []
    primary_id = clerk_user.primary_email_address_id
    if primary_id:
        for address in addresses:
            if address.id == primary_id and address.email_address:
                return address.email_address
    for address in addresses:
        if address.email_address:
            return address.email_address
    raise HTTPException(
        400,
        "Your account has no email address. An email is required to use this app.",
    )


async def get_current_user(
    auth_state: ClerkAuthState = Depends(get_clerk_auth_state),
    db: AsyncSession = Depends(get_db),
    clerk: Clerk = Depends(get_clerk_client),
) -> User:
    """Resolve the Postgres user for the verified Clerk identity.

    Looks up by clerk_user_id. On the first request from a new Clerk user, fetches
    the user's email from Clerk and inserts a row. Two concurrent first requests can
    both try to insert; the loser catches IntegrityError, rolls back, and re-reads.
    """
    result = await db.execute(
        select(User).where(User.clerk_user_id == auth_state.clerk_user_id)
    )
    user = result.scalar_one_or_none()
    if user is not None:
        return user

    # Lazy sync: first request from this Clerk user.
    clerk_user = await clerk.users.get_async(user_id=auth_state.clerk_user_id)
    primary_email = _extract_primary_email(clerk_user)

    user = User(
        clerk_user_id=auth_state.clerk_user_id,
        email=primary_email,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent request created the row first. Recover by re-querying.
        await db.rollback()
        result = await db.execute(
            select(User).where(User.clerk_user_id == auth_state.clerk_user_id)
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            raise
        return existing

    await db.refresh(user)
    return user


async def get_current_session_id(
    auth_state: ClerkAuthState = Depends(get_clerk_auth_state),
) -> str:
    """Expose the Clerk session id for per login scoping (SSH key cache)."""
    return auth_state.session_id
