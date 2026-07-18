"""Servers API.

Every endpoint depends on get_current_user. Every endpoint that takes a server_id
resolves it through get_owned_server, which enforces ownership and returns 404 on a
mismatch. Client supplied values carry the _from_client suffix at the point they
enter Python. user_id is never read from the client: it always comes from
current_user.id (server side session).
"""

from datetime import datetime, timezone
from uuid import UUID  # noqa: F401  (server_id path param type is resolved by FastAPI)

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps.auth import get_current_session_id, get_current_user
from app.deps.server_ownership import get_owned_server
from app.deps.services import get_key_provider_dep, get_ssh_service
from app.models import Server, User
from app.redis_client import get_redis
from app.schemas.servers import (
    CommandResultResponse,
    CreateServerRequest,
    InstallKeyRequest,
    ProbeResponse,
    ServerResponse,
)
from app.services.app_key_service import (
    AppKeyMissing,
    create_key_for_server,
    get_key_for_server,
)
from app.services.key_provider import KeyProvider
from app.services.ssh_service import (
    HostKeyChangedDuringInstall,
    HostKeyMismatch,
    KeyInstallVerificationFailed,
    ProbeError,
    SSHService,
    SshHardeningFailed,
)

router = APIRouter(prefix="/api/servers", tags=["servers"])


@router.post("/probe", response_model=ProbeResponse)
async def probe_server(
    body: CreateServerRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
) -> ProbeResponse:
    """Register a server and capture its SSH host key (TOFU step one).

    Opens an unauthenticated probe to the host, records the presented host key and
    its SHA256 fingerprint, and creates a server row in status pending_verification
    owned by the current user. Generates a fresh app managed keypair scoped to this
    server. Returns the new server id, the fingerprint for the user to compare
    against their VPS console, and the app public key to be installed next.
    """
    name_from_client = body.name
    host_from_client = body.host
    port_from_client = body.port
    username_from_client = body.username

    try:
        probe_result = await ssh.probe(
            host_from_client, port_from_client, username_from_client
        )
    except ProbeError as exc:
        raise HTTPException(502, str(exc)) from exc

    server = Server(
        user_id=current_user.id,
        name=name_from_client,
        host=host_from_client,
        port=port_from_client,
        username=username_from_client,
        host_key=probe_result.host_key,
        host_key_type=probe_result.host_key_type,
        fingerprint_sha256=probe_result.fingerprint_sha256,
        status="pending_verification",
        verification_source="tofu",
    )
    db.add(server)
    await db.commit()
    await db.refresh(server)

    # Fresh keypair scoped to this server. The service does not commit, so the route
    # owns the transaction for the key row.
    app_key = await create_key_for_server(server, db, key_provider)
    await db.commit()
    await db.refresh(app_key)

    return ProbeResponse(
        server_id=server.id,
        fingerprint_sha256=probe_result.fingerprint_sha256,
        app_public_key=app_key.public_key,
    )


@router.post("/{server_id}/install_key", response_model=ServerResponse)
async def install_key(
    body: InstallKeyRequest,
    server: Server = Depends(get_owned_server),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
) -> ServerResponse:
    """Install the app public key on the server and verify it (TOFU step two).

    Only valid while the server is pending_verification. Re-probes to confirm the
    host key has not changed, then uses the supplied password over a strict host key
    checked session to install the app public key (idempotently) and optionally
    disable password authentication. Finally proves key based login works with a
    whoami smoke test. On success the server moves to status verified. The password
    is used transiently and never stored.
    """
    password_from_client = body.password
    disable_password_auth_from_client = body.disable_password_auth

    if server.status != "pending_verification":
        raise HTTPException(
            400,
            f"Server is not pending verification (status: {server.status}).",
        )

    try:
        app_key = await get_key_for_server(server, db)
    except AppKeyMissing as exc:
        raise HTTPException(500, "App SSH key missing. Re-run the probe step.") from exc

    app_private_key = await key_provider.decrypt(app_key.encrypted_private_key)

    try:
        await ssh.install_key(
            server=server,
            password_from_client=password_from_client,
            app_public_key=app_key.public_key,
            app_private_key=app_private_key,
            disable_password_auth=disable_password_auth_from_client,
        )
    except HostKeyChangedDuringInstall as exc:
        raise HTTPException(409, str(exc)) from exc
    except (KeyInstallVerificationFailed, SshHardeningFailed) as exc:
        raise HTTPException(502, str(exc)) from exc
    except ProbeError as exc:
        raise HTTPException(502, str(exc)) from exc

    server.status = "verified"
    server.verified_at = datetime.now(timezone.utc)
    server.password_auth_disabled = disable_password_auth_from_client
    await db.commit()
    await db.refresh(server)

    return ServerResponse.model_validate(server)


@router.post("/{server_id}/cancel", status_code=204)
async def cancel_server(
    server: Server = Depends(get_owned_server),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Cancel and delete a server that is still pending verification.

    Used to abort the add server flow before the key is installed. Returns 400 if the
    server has already been verified, since verified servers are deleted through a
    different (future) path.
    """
    if server.status != "pending_verification":
        raise HTTPException(
            400, "Only pending verification servers can be cancelled."
        )
    await db.delete(server)
    await db.commit()


@router.get("", response_model=list[ServerResponse])
async def list_servers(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ServerResponse]:
    """List all servers owned by the current user, newest first."""
    rows = await db.scalars(
        select(Server)
        .where(Server.user_id == current_user.id)
        .order_by(Server.created_at.desc())
    )
    return [ServerResponse.model_validate(row) for row in rows]


@router.get("/{server_id}", response_model=ServerResponse)
async def get_server(
    server: Server = Depends(get_owned_server),
) -> ServerResponse:
    """Return a single server owned by the current user, or 404 if not found."""
    return ServerResponse.model_validate(server)


@router.post("/{server_id}/smoke_test", response_model=CommandResultResponse)
async def smoke_test(
    server: Server = Depends(get_owned_server),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
) -> CommandResultResponse:
    """Run the hello world smoke test over the pooled key based SSH connection.

    Proves the end to end connection path works: load and decrypt the app private
    key (via the Redis cache), open or reuse a strict host key checked connection,
    and run a harmless command. Only valid once the server is verified. Returns the
    command stdout, stderr, and exit status.
    """
    if server.status != "verified":
        raise HTTPException(400, "Server must be verified before running a smoke test.")
    try:
        result = await ssh.run_command(
            server=server,
            user_id=current_user.id,
            session_id=session_id,
            command="echo 'hello from Abstract' && uname -a && date",
            redis=redis,
            db=db,
            key_provider=key_provider,
        )
    except HostKeyMismatch as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ProbeError, OSError) as exc:
        raise HTTPException(502, str(exc)) from exc

    return CommandResultResponse(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_status=result.exit_status,
    )


@router.post("/{server_id}/ping")
async def ping_server(
    server: Server = Depends(get_owned_server),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
) -> dict[str, str]:
    """Check whether the server is reachable over SSH right now.

    Forces a fresh connection (never the pool) so a stale pooled entry cannot mask a
    box that is down, for example while it reboots. Returns 200 when reachable, 503
    otherwise. The frontend polls this after a reboot.
    """
    reachable = await ssh.ping(
        server=server,
        user_id=current_user.id,
        session_id=session_id,
        redis=redis,
        db=db,
        key_provider=key_provider,
    )
    if not reachable:
        raise HTTPException(503, "Server is not reachable.")
    return {"status": "ok"}
