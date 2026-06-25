"""Application settings loaded from the environment.

TTL values are stored in the environment in minutes for human readability and
exposed here both as raw minutes and as computed seconds for use in code.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    dev_user_id: str = "00000000-0000-0000-0000-000000000001"
    dev_user_email: str = "dev@localhost"

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
