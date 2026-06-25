"""Single module-level async Redis client and its FastAPI dependency."""

import redis.asyncio as aioredis

from app.config import get_settings

_settings = get_settings()

# One client for the whole process. redis.asyncio manages its own connection pool.
redis_client: aioredis.Redis = aioredis.from_url(
    _settings.redis_url,
    encoding="utf-8",
    decode_responses=False,
)


async def get_redis() -> aioredis.Redis:
    return redis_client
