"""Request and response models for the projects API.

Response models intentionally exclude everything key-related except the SHA256
fingerprint: never the encrypted private key, never the public key, never
GitHub's deploy key ID.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class ProjectListItemResponse(ProjectResponse):
    server_name: str
    server_host: str


class GithubRepoResponse(BaseModel):
    id: int
    full_name: str
    name: str
    pushed_at: datetime | None
    private: bool
