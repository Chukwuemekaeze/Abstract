"""Ownership helper.

Resolves a server by id and enforces that it belongs to the current user. Returns
404 (not 403) on a mismatch so we do not leak which server ids exist.
"""

from uuid import UUID

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps.auth import get_current_user
from app.models import Server, User


async def get_owned_server(
    server_id: UUID,  # from path, but UUIDs are not security sensitive in themselves
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Server:
    server = await db.get(Server, server_id)
    if server is None or server.user_id != current_user.id:
        raise HTTPException(404, "Server not found")
    return server
