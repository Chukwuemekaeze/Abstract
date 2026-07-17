"""Per-project operation lock shared by start, rollback, and publish.

Same short-transaction pattern the deletion flow uses: acquire commits the lock
on its own so concurrent requests see it immediately (and are rejected 409 by
get_idle_project), then the external SSH work runs, then the lock is cleared,
either in the endpoint's final success commit (set active_operation=None inline)
or via release_operation on any failure path.

release_operation opens its own transaction so it is safe to call after the
endpoint has rolled back its work.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project


async def acquire_operation(db: AsyncSession, project: Project, name: str) -> None:
    """Commit A: publish the lock so concurrent mutations see it at once."""
    project.active_operation = name
    await db.commit()


async def release_operation(db: AsyncSession, project_id: UUID) -> None:
    """Failure path: drop the lock in a fresh transaction so the user can retry."""
    await db.rollback()
    project = await db.get(Project, project_id)
    if project is not None and project.active_operation is not None:
        project.active_operation = None
        await db.commit()
