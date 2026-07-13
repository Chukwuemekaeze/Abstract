"""Request and response models for env files, run, and publish.

Env var VALUES are secrets: they enter through the create/update requests,
get encrypted immediately, and never appear in any response model. Responses
expose keys and counts only.
"""

import ipaddress
import posixpath
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Path and value validation is a security boundary: paths are interpolated
# into shell commands and resolved on the VPS; values are written verbatim
# into dotenv files that docker compose parses line by line.

MAX_ENV_PATH_LENGTH = 200
MAX_ENV_KEY_LENGTH = 200

# TLDs that indicate a non-public name certbot could never validate.
_PRIVATE_TLDS = frozenset(
    {"localhost", "local", "internal", "lan", "home", "test", "invalid", "example"}
)
_DOMAIN_LABEL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


def validate_env_file_path(path: str) -> str:
    """Reject anything that could escape the clone directory.

    The service layer re-checks containment against the actual clone_path;
    this layer enforces everything knowable without it.
    """
    if not path or not path.strip():
        raise ValueError("path must not be empty")
    path = path.strip()
    if len(path) > MAX_ENV_PATH_LENGTH:
        raise ValueError(f"path must be at most {MAX_ENV_PATH_LENGTH} characters")
    if "\x00" in path:
        raise ValueError("path must not contain null bytes")
    if path.startswith("/"):
        raise ValueError("path must be relative, not absolute")
    if ".." in path.split("/"):
        raise ValueError("path must not contain '..' segments")
    normalized = posixpath.normpath(path)
    if normalized.startswith("..") or normalized.startswith("/"):
        raise ValueError("path must resolve inside the project directory")
    return path


def validate_env_key(key: str) -> str:
    if not key or not key.strip():
        raise ValueError("variable keys must not be empty")
    key = key.strip()
    if len(key) > MAX_ENV_KEY_LENGTH:
        raise ValueError(f"variable keys must be at most {MAX_ENV_KEY_LENGTH} characters")
    if "=" in key:
        raise ValueError(f"variable key {key!r} must not contain '='")
    if key.startswith("#"):
        raise ValueError(f"variable key {key!r} must not start with '#'")
    if any(c.isspace() for c in key) or "\x00" in key:
        raise ValueError(f"variable key {key!r} must not contain whitespace or null bytes")
    return key


def validate_env_value(key: str, value: str) -> str:
    # Dotenv files are line-oriented; docker compose cannot represent
    # multi-line values, so reject them up front with a clear error.
    if "\n" in value or "\r" in value:
        raise ValueError(f"value for {key!r} must not contain newlines")
    if "\x00" in value:
        raise ValueError(f"value for {key!r} must not contain null bytes")
    return value


def _validate_variables(variables: dict[str, str]) -> dict[str, str]:
    validated: dict[str, str] = {}
    for key, value in variables.items():
        clean_key = validate_env_key(key)
        validated[clean_key] = validate_env_value(clean_key, value)
    return validated


class CreateEnvFileRequest(BaseModel):
    path: str
    variables: dict[str, str] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def _path_valid(cls, v: str) -> str:
        return validate_env_file_path(v)

    @field_validator("variables")
    @classmethod
    def _variables_valid(cls, v: dict[str, str]) -> dict[str, str]:
        return _validate_variables(v)


class UpdateEnvFileRequest(BaseModel):
    """Partial update. set_variables upserts, remove_keys deletes; keys the
    client does not mention keep their stored values."""

    path: str | None = None
    set_variables: dict[str, str] | None = None
    remove_keys: list[str] | None = None

    @field_validator("path")
    @classmethod
    def _path_valid(cls, v: str | None) -> str | None:
        return None if v is None else validate_env_file_path(v)

    @field_validator("set_variables")
    @classmethod
    def _variables_valid(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        return None if v is None else _validate_variables(v)

    @field_validator("remove_keys")
    @classmethod
    def _keys_valid(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else [validate_env_key(k) for k in v]


class EnvFileListItemResponse(BaseModel):
    id: UUID
    path: str
    variable_count: int
    updated_at: datetime


class EnvFileDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    path: str
    keys: list[str]
    updated_at: datetime


class RunResultResponse(BaseModel):
    runtime_status: str
    started_at: datetime | None
    captured_output: str | None
    build_output: str | None = None


class DetectedPortResponse(BaseModel):
    service: str
    host_port: int
    container_port: int
    is_dangerous: bool


def validate_domain(domain: str) -> str:
    domain = domain.strip().rstrip(".")
    if not domain:
        raise ValueError("domain must not be empty")
    if len(domain) > 253:
        raise ValueError("domain must be at most 253 characters")
    if domain != domain.lower():
        raise ValueError("domain must be lowercase")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise ValueError("domain must be a hostname, not an IP address")
    labels = domain.split(".")
    if len(labels) < 2:
        raise ValueError("domain must contain at least one dot")
    for label in labels:
        if not label:
            raise ValueError("domain must not contain empty labels")
        if len(label) > 63:
            raise ValueError("each domain label must be at most 63 characters")
        if label.startswith("-") or label.endswith("-"):
            raise ValueError("domain labels must not start or end with a hyphen")
        if not set(label) <= _DOMAIN_LABEL_CHARS:
            raise ValueError(
                "domain may only contain lowercase letters, digits, hyphens, and dots"
            )
    if "localhost" in labels:
        raise ValueError("domain must not reference localhost")
    if labels[-1] in _PRIVATE_TLDS:
        raise ValueError(f"domain ends in a non-public TLD '.{labels[-1]}'")
    return domain


class PublishRequest(BaseModel):
    domain: str
    internal_port: int = Field(ge=1, le=65535)

    @field_validator("domain")
    @classmethod
    def _domain_valid(cls, v: str) -> str:
        return validate_domain(v)
