"""Hardening API.

Operations that take a verified server and make it deployment-ready. Every endpoint
resolves the server through get_owned_server (404 on mismatch) and requires the
server to be verified. Client supplied values carry the _from_client suffix. user_id
is never read from the client: it comes from current_user.id.

Database atomicity: each handler runs its operation and then commits exactly once on
success, or rolls back on any failure (see _atomic). The HardeningService methods
never commit, so this is the single commit point. The VPS itself may be partially
changed after a failure, which is why every operation is idempotent and safe to
retry.
"""

from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps.auth import get_current_session_id, get_current_user
from app.deps.server_ownership import get_owned_server
from app.deps.services import (
    get_hardening_service,
    get_key_provider_dep,
    get_ssh_service,
)
from app.models import Server, User
from app.redis_client import get_redis
from app.schemas.servers import (
    CreateSudoUserRequest,
    QuickHardenRequest,
    ServerResponse,
)
from app.services.app_key_service import AppKeyMissing, get_key_for_server
from app.services.hardening_service import (
    HardeningContext,
    HardeningError,
    HardeningService,
    RootLoginPrecheckFailed,
)
from app.services.key_provider import KeyProvider
from app.services.ssh_service import HostKeyMismatch, SSHService

router = APIRouter(prefix="/api/servers/{server_id}/harden", tags=["hardening"])

_NOT_VERIFIED = "Server must be verified before it can be hardened."


def _require_verified(server: Server) -> None:
    if server.status != "verified":
        raise HTTPException(400, _NOT_VERIFIED)


async def _build_context(
    server: Server,
    current_user: User,
    session_id: str,
    redis: aioredis.Redis,
    key_provider: KeyProvider,
    db: AsyncSession,
) -> HardeningContext:
    """Load and decrypt this server's app keypair into a HardeningContext.

    The keypair is needed both to authenticate to the server and to open the
    verification sub-connections in create_sudo_user / disable_root_login.
    """
    try:
        app_key = await get_key_for_server(server, db)
    except AppKeyMissing as exc:
        raise HTTPException(
            500, "App SSH key missing. Register a server first."
        ) from exc
    app_private_key = await key_provider.decrypt(app_key.encrypted_private_key)
    return HardeningContext(
        user_id=current_user.id,
        session_id=session_id,
        redis=redis,
        key_provider=key_provider,
        app_public_key=app_key.public_key,
        app_private_key=app_private_key,
    )


@asynccontextmanager
async def _atomic(db: AsyncSession, server: Server):
    """Wrap an operation: commit once on success, roll back and map errors otherwise.

    RootLoginPrecheckFailed is a guard (400). HardeningError surfaces a generic
    message plus the captured shell output for the UI (502). HostKeyMismatch is 409.
    Anything else rolls back and propagates as a 500.
    """
    try:
        yield
    except RootLoginPrecheckFailed as exc:
        await db.rollback()
        raise HTTPException(400, exc.captured_output) from exc
    except HostKeyMismatch as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except HardeningError as exc:
        await db.rollback()
        raise HTTPException(
            502,
            detail={
                "message": "Operation failed",
                "captured_output": exc.captured_output,
            },
        ) from exc
    except Exception:
        await db.rollback()
        raise
    else:
        await db.commit()
        await db.refresh(server)


@router.post("/update_system", response_model=ServerResponse)
async def update_system(
    server: Server = Depends(get_owned_server),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
    hardening: HardeningService = Depends(get_hardening_service),
) -> ServerResponse:
    _require_verified(server)
    async with _atomic(db, server):
        conn = await ssh.get_connection(
            server, current_user.id, session_id, redis, db, key_provider
        )
        await hardening.update_system(conn, server, db)
    return ServerResponse.model_validate(server)


@router.post("/install_base_packages", response_model=ServerResponse)
async def install_base_packages(
    server: Server = Depends(get_owned_server),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
    hardening: HardeningService = Depends(get_hardening_service),
) -> ServerResponse:
    _require_verified(server)
    async with _atomic(db, server):
        conn = await ssh.get_connection(
            server, current_user.id, session_id, redis, db, key_provider
        )
        await hardening.install_base_packages(conn, server, db)
    return ServerResponse.model_validate(server)


@router.post("/install_docker", response_model=ServerResponse)
async def install_docker(
    server: Server = Depends(get_owned_server),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
    hardening: HardeningService = Depends(get_hardening_service),
) -> ServerResponse:
    _require_verified(server)
    async with _atomic(db, server):
        conn = await ssh.get_connection(
            server, current_user.id, session_id, redis, db, key_provider
        )
        await hardening.install_docker(conn, server, db)
    return ServerResponse.model_validate(server)


@router.post("/install_nginx", response_model=ServerResponse)
async def install_nginx(
    server: Server = Depends(get_owned_server),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
    hardening: HardeningService = Depends(get_hardening_service),
) -> ServerResponse:
    _require_verified(server)
    async with _atomic(db, server):
        conn = await ssh.get_connection(
            server, current_user.id, session_id, redis, db, key_provider
        )
        await hardening.install_nginx(conn, server, db)
    return ServerResponse.model_validate(server)


@router.post("/create_sudo_user", response_model=ServerResponse)
async def create_sudo_user(
    body: CreateSudoUserRequest,
    server: Server = Depends(get_owned_server),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
    hardening: HardeningService = Depends(get_hardening_service),
) -> ServerResponse:
    _require_verified(server)
    sudo_user_name_from_client = body.sudo_user_name
    ctx = await _build_context(
        server, current_user, session_id, redis, key_provider, db
    )
    async with _atomic(db, server):
        conn = await ssh.get_connection(
            server, current_user.id, session_id, redis, db, key_provider
        )
        await hardening.create_sudo_user(
            conn, server, db, ctx, sudo_user_name_from_client
        )
    return ServerResponse.model_validate(server)


@router.post("/disable_root_login", response_model=ServerResponse)
async def disable_root_login(
    server: Server = Depends(get_owned_server),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
    hardening: HardeningService = Depends(get_hardening_service),
) -> ServerResponse:
    _require_verified(server)
    # Guard before any SSH: cannot disable root without a confirmed alternative login.
    if server.sudo_user_name is None:
        raise HTTPException(
            400, "Create a sudo user before disabling root login."
        )
    ctx = await _build_context(
        server, current_user, session_id, redis, key_provider, db
    )
    async with _atomic(db, server):
        conn = await ssh.get_connection(
            server, current_user.id, session_id, redis, db, key_provider
        )
        await hardening.disable_root_login(conn, server, db, ctx)
    return ServerResponse.model_validate(server)


@router.post("/disable_password_auth", response_model=ServerResponse)
async def disable_password_auth(
    server: Server = Depends(get_owned_server),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
    hardening: HardeningService = Depends(get_hardening_service),
) -> ServerResponse:
    _require_verified(server)
    async with _atomic(db, server):
        conn = await ssh.get_connection(
            server, current_user.id, session_id, redis, db, key_provider
        )
        await hardening.disable_password_auth(conn, server, db)
    return ServerResponse.model_validate(server)


@router.post("/configure_firewall", response_model=ServerResponse)
async def configure_firewall(
    server: Server = Depends(get_owned_server),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
    hardening: HardeningService = Depends(get_hardening_service),
) -> ServerResponse:
    _require_verified(server)
    async with _atomic(db, server):
        conn = await ssh.get_connection(
            server, current_user.id, session_id, redis, db, key_provider
        )
        await hardening.configure_firewall(conn, server, db)
    return ServerResponse.model_validate(server)


@router.post("/create_swap", response_model=ServerResponse)
async def create_swap(
    server: Server = Depends(get_owned_server),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
    hardening: HardeningService = Depends(get_hardening_service),
) -> ServerResponse:
    _require_verified(server)
    async with _atomic(db, server):
        conn = await ssh.get_connection(
            server, current_user.id, session_id, redis, db, key_provider
        )
        await hardening.create_swap(conn, server, db)
    return ServerResponse.model_validate(server)


@router.post("/reboot", response_model=ServerResponse)
async def reboot(
    server: Server = Depends(get_owned_server),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
    hardening: HardeningService = Depends(get_hardening_service),
) -> ServerResponse:
    _require_verified(server)
    ctx = await _build_context(
        server, current_user, session_id, redis, key_provider, db
    )
    async with _atomic(db, server):
        conn = await ssh.get_connection(
            server, current_user.id, session_id, redis, db, key_provider
        )
        await hardening.reboot(conn, server, db, ctx)
    return ServerResponse.model_validate(server)


@router.post("/quick_harden", response_model=ServerResponse)
async def quick_harden(
    body: QuickHardenRequest,
    server: Server = Depends(get_owned_server),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
    hardening: HardeningService = Depends(get_hardening_service),
) -> ServerResponse:
    _require_verified(server)
    sudo_user_name_from_client = body.sudo_user_name
    ctx = await _build_context(
        server, current_user, session_id, redis, key_provider, db
    )
    async with _atomic(db, server):
        await hardening.quick_harden(server, db, ctx, sudo_user_name_from_client)
    return ServerResponse.model_validate(server)
