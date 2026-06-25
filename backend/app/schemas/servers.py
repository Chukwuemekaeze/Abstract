"""Request and response models for the servers API.

Response models intentionally exclude sensitive bytea fields (host_key,
encrypted_private_key). Only fingerprint_sha256 is exposed for display.
"""

from datetime import datetime
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
    fingerprint_sha256: str | None
    host_key_type: str | None
    password_auth_disabled: bool
    verification_source: str
    created_at: datetime
    verified_at: datetime | None


class CommandResultResponse(BaseModel):
    stdout: str
    stderr: str
    exit_status: int
