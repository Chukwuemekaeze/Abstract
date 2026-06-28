"""Application settings loaded from the environment.

TTL values are stored in the environment in minutes for human readability and
exposed here both as raw minutes and as computed seconds for use in code.
"""

from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str
    test_database_url: str | None = None
    redis_url: str = "redis://localhost:6379/0"

    app_master_key: str
    key_provider: str = "env"

    # Clerk authentication. All values come from the Clerk dashboard.
    clerk_secret_key: str
    # Kept for reference and any future issuer/CORS derivation.
    clerk_publishable_key: str
    clerk_jwt_issuer: str
    # Frontend origins permitted to present Clerk tokens. Set in the environment as
    # a comma separated list (e.g. "http://localhost:5173,https://yourapp.com").
    # NoDecode disables pydantic-settings' default JSON parsing for this list field
    # so the validator below receives the raw string.
    clerk_authorized_parties: Annotated[list[str], NoDecode]

    @field_validator("clerk_authorized_parties", mode="before")
    @classmethod
    def _split_authorized_parties(cls, value: object) -> object:
        # Accept a plain comma separated string and turn it into trimmed origins.
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    # Stored in minutes in the environment.
    session_ttl_minutes: int = 10080
    ssh_key_cache_ttl_minutes: int = 30
    ssh_pool_idle_timeout_minutes: int = 5

    @property
    def session_ttl_seconds(self) -> int:
        return self.session_ttl_minutes * 60

    @property
    def ssh_key_cache_ttl_seconds(self) -> int:
        return self.ssh_key_cache_ttl_minutes * 60

    @property
    def ssh_pool_idle_timeout_seconds(self) -> int:
        return self.ssh_pool_idle_timeout_minutes * 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
