"""Request and response models for project run history and rollback.

build_output can be large and may contain secrets a user echoed during a build,
so it is exposed only from the single-run detail endpoint (ProjectRunDetail),
never in list responses (ProjectRunRead).

Rollback reuses the Run flow's RunResultResponse: a rollback returns the same
shape as a start, and on failure the route raises the same 502 body.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ProjectRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    git_commit_sha: str
    git_ref: str | None
    status: str
    started_at: datetime
    finished_at: datetime | None
    created_at: datetime


class ProjectRunDetail(ProjectRunRead):
    build_output: str | None


class RollbackRequest(BaseModel):
    target_run_id: UUID
