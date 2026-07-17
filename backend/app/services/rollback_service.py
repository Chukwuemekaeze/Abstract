"""Rollback target validation.

Rollback rebuilds a project from an older recorded run's commit. The rebuild
mechanics (git checkout then the shared Run flow) live in run_service; this
module only decides whether a given target run is a legal rollback destination.

Rollback deliberately uses the project's CURRENT env vars from the database, not
a snapshot from when the target run first executed. Snapshotting would let a
rotated secret silently come back to life, so it is a security choice, not an
oversight.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, ProjectRun
from app.services import project_run_service


class RollbackTargetError(Exception):
    """Base for rollback target validation failures."""


class TargetRunNotFound(RollbackTargetError):
    """The target run id does not belong to this project."""


class CannotRollbackToFailedRun(RollbackTargetError):
    """The target run never came up, so there is nothing good to roll back to."""


class AlreadyAtThisVersion(RollbackTargetError):
    """The target run is the row currently running; a rollback would be a no-op."""


async def resolve_target(
    db: AsyncSession, project: Project, target_run_id: UUID
) -> ProjectRun:
    """Return the run to roll back to, or raise a RollbackTargetError.

    A legal target is a 'superseded' run belonging to this project. 'failed' and
    the current 'running' row are rejected.
    """
    target = await project_run_service.get_run(db, project.id, target_run_id)
    if target is None:
        raise TargetRunNotFound()
    if target.status == "failed":
        raise CannotRollbackToFailedRun()
    if target.status == "running":
        raise AlreadyAtThisVersion()
    return target
