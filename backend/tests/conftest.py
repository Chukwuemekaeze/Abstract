"""Shared test fixtures.

Environment is configured before any app module is imported, because app.config
reads required settings at import time and app.db builds an engine from them.

DB backed tests use TEST_DATABASE_URL. When it is not set those tests are skipped
so the mocked SSH and key provider tests can still run anywhere.
"""

import base64
import os
import secrets

# Configure environment before importing app modules.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://placeholder:placeholder@localhost/placeholder"
)
os.environ.setdefault(
    "APP_MASTER_KEY",
    base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
)
os.environ.setdefault("KEY_PROVIDER", "env")
# Clerk settings are required at import time. Tests never hit Clerk; the auth
# dependency is mocked, so these are placeholders.
os.environ.setdefault("CLERK_SECRET_KEY", "sk_test_placeholder")
os.environ.setdefault("CLERK_PUBLISHABLE_KEY", "pk_test_placeholder")
os.environ.setdefault("CLERK_JWT_ISSUER", "https://placeholder.clerk.accounts.dev")
os.environ.setdefault("CLERK_AUTHORIZED_PARTIES", "http://localhost:5173")

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

from app.clerk import get_clerk_client  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.deps.auth import (  # noqa: E402
    ClerkAuthState,
    get_clerk_auth_state,
    get_current_session_id,
    get_current_user,
)
from app.deps.services import get_ssh_service  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402
from app.redis_client import get_redis  # noqa: E402

# Deterministic Clerk identity used by the mocked auth fixtures.
TEST_CLERK_USER_ID = "user_test_123"
TEST_SESSION_ID = "sess_test_123"
OTHER_CLERK_USER_ID = "user_other_456"

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
async def test_user(db_session: AsyncSession) -> User:
    user = User(clerk_user_id=TEST_CLERK_USER_ID, email="test@localhost")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def other_test_user(db_session: AsyncSession) -> User:
    user = User(clerk_user_id=OTHER_CLERK_USER_ID, email="other@localhost")
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
async def client(db_session: AsyncSession, test_user: User):
    """httpx client wired to the app with DB, auth, redis, and SSH overridden.

    Auth is short circuited: get_current_user returns the seeded test_user and
    get_current_session_id returns a deterministic session id, so route tests run
    without hitting Clerk. get_ssh_service is left to be overridden per test via
    app.dependency_overrides using the mock_ssh fixture.
    """

    async def override_get_db():
        # Yield the shared test session and do not close it; the fixture owns it.
        yield db_session

    async def override_get_current_user():
        return test_user

    async def override_get_current_session_id():
        return TEST_SESSION_ID

    async def override_get_redis():
        return FakeRedis()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    app.dependency_overrides[get_current_session_id] = override_get_current_session_id
    app.dependency_overrides[get_redis] = override_get_redis

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture
def mock_clerk_auth(mocker):
    """Override get_clerk_auth_state to return a deterministic verified identity.

    Lets the real get_current_user run (against the test DB) without calling Clerk.
    """

    async def override_get_clerk_auth_state() -> ClerkAuthState:
        return ClerkAuthState(
            clerk_user_id=TEST_CLERK_USER_ID, session_id=TEST_SESSION_ID
        )

    app.dependency_overrides[get_clerk_auth_state] = override_get_clerk_auth_state
    yield
    app.dependency_overrides.pop(get_clerk_auth_state, None)


@pytest_asyncio.fixture
async def unauthenticated_client(db_session: AsyncSession, mocker):
    """Client with DB and redis overridden but the real auth dependency in place.

    The Clerk client is replaced by a mock so tests can drive token verification
    outcomes (signed out, raising) without network access. Yields (client, clerk_mock).
    """

    async def override_get_db():
        yield db_session

    async def override_get_redis():
        return FakeRedis()

    clerk_mock = mocker.MagicMock()
    # Default: no valid session.
    clerk_mock.authenticate_request.return_value = mocker.MagicMock(
        is_signed_in=False, payload=None
    )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_redis] = override_get_redis
    app.dependency_overrides[get_clerk_client] = lambda: clerk_mock

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, clerk_mock

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
            stdout="hello from Abstract\nLinux test\n",
            stderr="",
            exit_status=0,
        )
    )

    app.dependency_overrides[get_ssh_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_ssh_service, None)
