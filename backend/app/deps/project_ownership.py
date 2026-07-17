"""Ownership helper.

Resolves a project by id and enforces that it belongs to the current user.
Returns 404 (not 403) on a mismatch so we do not leak which project ids exist.

A project also carries a single active_operation lock (start, rollback,
publish, delete). Mutating endpoints depend on get_idle_project so that while
one operation runs, concurrent ones are rejected with 409 instead of racing on
the same VPS. Read endpoints keep get_owned_project so the frontend can still
render the in-progress state.
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


async def get_idle_project(
    project: Project = Depends(get_owned_project),
) -> Project:
    """Owned project with no operation in flight.

    Rejects with 409 and a structured {"active_operation": ...} detail the
    frontend banner reads, so a second start/rollback/publish cannot race the
    one already running against the VPS.
    """
    if project.active_operation is not None:
        raise HTTPException(
            409,
            detail={"active_operation": project.active_operation},
        )
    return project


# Backwards-compatible alias: existing mutating endpoints imported
# get_editable_project when the only lock was is_deleting. It now guards against
# every active_operation.
get_editable_project = get_idle_project
