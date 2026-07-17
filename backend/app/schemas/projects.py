"""Request and response models for the projects API.

Response models intentionally exclude everything key-related except the SHA256
fingerprint: never the encrypted private key, never the public key, never
GitHub's deploy key ID.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.env import validate_env_file_path

# owner/repo where both parts are GitHub-legal characters. This value is later
# interpolated into shell commands and API paths, so the tight charset is a
# security boundary, not just validation.
_REPO_FULL_NAME = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    server_id: UUID
    github_repo_id: int = Field(gt=0)
    github_repo_full_name: str = Field(
        min_length=3, max_length=256, pattern=_REPO_FULL_NAME
    )

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str
    server_id: UUID
    github_repo_full_name: str
    github_repo_id: int
    clone_path: str
    cloned_at: datetime | None
    created_at: datetime
    updated_at: datetime
    deploy_key_fingerprint: str
    runtime_status: str
    started_at: datetime | None
    compose_file_path: str | None
    domain: str | None
    internal_port: int | None
    published_at: datetime | None
    active_operation: str | None


class ProjectListItemResponse(ProjectResponse):
    server_name: str
    server_host: str


class UpdateProjectRequest(BaseModel):
    """Advanced settings. compose_file_path=None clears the override so
    detection falls back to the standard compose file names."""

    compose_file_path: str | None = None

    @field_validator("compose_file_path")
    @classmethod
    def _compose_path_valid(cls, v: str | None) -> str | None:
        return None if v is None else validate_env_file_path(v)


class PullResultResponse(BaseModel):
    before_commit: str
    after_commit: str
    already_up_to_date: bool
    updated_at: datetime


class GithubRepoResponse(BaseModel):
    id: int
    full_name: str
    name: str
    pushed_at: datetime | None
    private: bool


class DeletionStepResult(BaseModel):
    """One step of a project deletion: what it was and how it finished."""

    name: str
    status: Literal["completed", "skipped", "failed"]
    detail: str | None = None


class DeleteProjectResponse(BaseModel):
    success: bool
    steps: list[DeletionStepResult]
