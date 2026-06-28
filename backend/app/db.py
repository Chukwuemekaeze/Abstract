"""Async SQLAlchemy engine, session factory, and FastAPI dependency."""
import ssl
from collections.abc import AsyncGenerator

ssl_ctx = ssl.create_default_context()

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()

engine = create_async_engine(
    _settings.database_url, 
    future=True,
    pool_pre_ping=True,
    connect_args={"ssl": ssl_ctx}
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
