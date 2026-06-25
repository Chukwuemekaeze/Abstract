"""Shared test fixtures.

Environment is configured before any app module is imported, because app.config
reads required settings at import time and app.db builds an engine from them.

DB backed tests use TEST_DATABASE_URL. When it is not set those tests are skipped
so the mocked SSH and key provider tests can still run anywhere.
"""

import base64
import os
import secrets
from uuid import UUID

# Configure environment before importing app modules.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://placeholder:placeholder@localhost/placeholder"
)
os.environ.setdefault(
    "APP_MASTER_KEY",
    base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
)
os.environ.setdefault("KEY_PROVIDER", "env")

import asyncio  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.deps.auth import get_current_user  # noqa: E402
from app.deps.services import get_ssh_service  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402
from app.redis_client import get_redis  # noqa: E402

DEV_USER_ID = UUID(get_settings().dev_user_id)
OTHER_USER_ID = UUID("00000000-0000-0000-0000-0000000000ff")

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL not set; skipping DB backed tests.",
)


async def _ensure_test_database() -> None:
    """Create the test database if it does not already exist.

    Connects to the server's default 'postgres' maintenance database and issues
    CREATE DATABASE so the suite is self contained and does not rely on any
    external init script. Safe to call repeatedly.
    """
    import asyncpg

    url = make_url(TEST_DATABASE_URL)
    target_db = url.database
    conn = await asyncpg.connect(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        database="postgres",
    )
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", target_db
        )
        if not exists:
            # Identifier cannot be parameterized; target_db comes from our own env.
            await conn.execute(f'CREATE DATABASE "{target_db}"')
    finally:
        await conn.close()


if TEST_DATABASE_URL:
    # Provision the test database once at collection time.
    asyncio.run(_ensure_test_database())


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    """Fresh schema per test on TEST_DATABASE_URL, dropped afterwards."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set.")

    engine = create_async_engine(TEST_DATABASE_URL, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        await session.close()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest_asyncio.fixture
async def dev_user(db_session: AsyncSession) -> User:
    user = User(id=DEV_USER_ID, email="dev@localhost")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def other_user(db_session: AsyncSession) -> User:
    user = User(id=OTHER_USER_ID, email="other@localhost")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


class FakeRedis:
    """Minimal async Redis stand in for tests. Stores bytes in memory."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    async def get(self, key: str):
        return self._store.get(key)

    async def set(self, key: str, value: bytes, ex: int | None = None):
        self._store[key] = value


@pytest_asyncio.fixture
async def client(db_session: AsyncSession, dev_user: User):
    """httpx client wired to the app with DB, auth, redis, and SSH overridden.

    get_ssh_service is left to be overridden per test via app.dependency_overrides
    using the mock_ssh fixture.
    """

    async def override_get_db():
        # Yield the shared test session and do not close it; the fixture owns it.
        yield db_session

    async def override_get_current_user():
        return dev_user

    async def override_get_redis():
        return FakeRedis()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    app.dependency_overrides[get_redis] = override_get_redis

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture
def mock_ssh(mocker):
    """An AsyncMock SSHService installed as the get_ssh_service override."""
    from app.services.ssh_service import CommandResult, ProbeResult

    service = mocker.MagicMock()
    service.probe = mocker.AsyncMock(
        return_value=ProbeResult(
            host_key=b"ssh-ed25519 AAAATESTKEY",
            host_key_type="ssh-ed25519",
            fingerprint_sha256="SHA256:testfingerprintvalue",
        )
    )
    service.install_key = mocker.AsyncMock(return_value=None)
    service.run_command = mocker.AsyncMock(
        return_value=CommandResult(
            stdout="hello from deployment pipeline\nLinux test\n",
            stderr="",
            exit_status=0,
        )
    )

    app.dependency_overrides[get_ssh_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_ssh_service, None)
