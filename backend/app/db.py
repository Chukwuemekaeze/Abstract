"""Async SQLAlchemy engine, session factory, and FastAPI dependency."""

import ssl
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

# TLS context for the asyncpg driver.
ssl_ctx = ssl.create_default_context()


class Base(DeclarativeBase):
    pass


_settings = get_settings()

# Connect to Neon's DIRECT endpoint (the host WITHOUT "-pooler"), not its PgBouncer
# style pooler, and keep a real connection pool so requests reuse a warm connection
# instead of paying a full connect (several network round trips: TCP + TLS + auth)
# on every request.
#
# The pooler is why we previously ran NullPool (commit a0ca236): it drops connections
# server side, and reusing one surfaced as a TLS "bad record mac" error that asyncpg
# does not report as a disconnect, so pool_pre_ping could not recover. The direct
# endpoint is a plain Postgres session (no PgBouncer), so a dropped connection is a
# real disconnect asyncpg reports -- pool_pre_ping can recover, and prepared statement
# caching works again (no statement_cache_size=0 needed). pool_recycle retires
# connections before Neon's ~300s idle/autosuspend window so we never reuse a stale one.
engine = create_async_engine(
    _settings.database_url,
    future=True,
    pool_size=5,
    max_overflow=5,
    pool_recycle=240,
    pool_pre_ping=True,
    connect_args={"ssl": ssl_ctx},
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
