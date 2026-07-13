"""Ownership helper.

Resolves a project by id and enforces that it belongs to the current user.
Returns 404 (not 403) on a mismatch so we do not leak which project ids exist.
"""

from uuid import UUID

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps.auth import get_current_user
from app.models import Project, User


async def get_owned_project(
    project_id: UUID,  # from path, but UUIDs are not security sensitive in themselves
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Project:
    project = await db.get(Project, project_id)
    if project is None or project.user_id != current_user.id:
        raise HTTPException(404, "Project not found")
    return project
