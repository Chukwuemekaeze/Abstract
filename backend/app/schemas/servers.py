"""Request and response models for the servers API.

Response models intentionally exclude sensitive bytea fields (host_key,
encrypted_private_key). Only fingerprint_sha256 is exposed for display.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CreateServerRequest(BaseModel):
    name: str = Field(min_length=1)
    host: str = Field(min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(default="root", min_length=1)


class InstallKeyRequest(BaseModel):
    password: str = Field(min_length=1)
    disable_password_auth: bool = True
    # Supplied only on a retry when the server forced a password change on first login
    # (an expired password). Used once, over keyboard-interactive, to complete the
    # change during authentication; never stored.
    new_password: str | None = Field(default=None, min_length=1)


# A valid lowercase Linux username: starts with a letter or underscore, then
# lowercase letters, digits, underscores, or hyphens.
_LINUX_USERNAME = r"^[a-z_][a-z0-9_-]*$"


class CreateSudoUserRequest(BaseModel):
    sudo_user_name: str = Field(min_length=1, max_length=32, pattern=_LINUX_USERNAME)


class QuickHardenRequest(BaseModel):
    sudo_user_name: str = Field(min_length=1, max_length=32, pattern=_LINUX_USERNAME)


class ProbeResponse(BaseModel):
    server_id: UUID
    fingerprint_sha256: str
    app_public_key: str


class ServerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    host: str
    port: int
    username: str
    status: str
    active_operation: str | None
    fingerprint_sha256: str | None
    host_key_type: str | None
    password_auth_disabled: bool
    verification_source: str
    created_at: datetime
    verified_at: datetime | None

    # Hardening state. All safe to expose (no secrets).
    sudo_user_name: str | None
    root_login_disabled: bool
    firewall_enabled: bool
    docker_installed: bool
    base_packages_installed: bool
    nginx_installed: bool
    swap_configured: bool
    last_system_update_at: datetime | None


class CommandResultResponse(BaseModel):
    stdout: str
    stderr: str
    exit_status: int


class ServerDeletionStepResult(BaseModel):
    """One step of a server deletion. project_id/project_name are populated only
    for steps that ran inside the per-project deletion loop."""

    name: str
    status: Literal["completed", "skipped", "failed"]
    detail: str | None = None
    project_id: UUID | None = None
    project_name: str | None = None


class DeleteServerResponse(BaseModel):
    success: bool
    steps: list[ServerDeletionStepResult]


class ServerDeletionPreviewProject(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str
    domain: str | None
    runtime_status: str


class ServerDeletionPreviewResponse(BaseModel):
    projects: list[ServerDeletionPreviewProject]
