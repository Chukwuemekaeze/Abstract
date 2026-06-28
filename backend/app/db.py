"""Async SQLAlchemy engine, session factory, and FastAPI dependency."""

import ssl
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.config import get_settings

# TLS context for the asyncpg driver.
ssl_ctx = ssl.create_default_context()


class Base(DeclarativeBase):
    pass


_settings = get_settings()

# We connect through Neon's connection pooler, which already pools server side and
# closes idle connections. Holding our own long lived pool on top of that leads to
# reuse of connections Neon has dropped, surfacing as TLS "bad record mac" errors
# that asyncpg does not report as disconnects (so pool_pre_ping cannot recover).
# NullPool opens a fresh connection per checkout and disposes it on return, which
# avoids stale reuse. statement_cache_size=0 disables asyncpg prepared statement
# caching, required when sitting behind a PgBouncer style transaction pooler.
engine = create_async_engine(
    _settings.database_url,
    future=True,
    poolclass=NullPool,
    connect_args={"ssl": ssl_ctx, "statement_cache_size": 0},
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an AsyncSession and ensure it is closed after the request."""
    async with async_session_factory() as session:
        yield session
