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
from clerk_backend_api import Clerk
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.clerk import get_clerk_client
from app.db import get_db
from app.deps.auth import get_current_session_id, get_current_user
from app.deps.server_ownership import get_owned_server
from app.deps.services import (
    get_github_service,
    get_key_provider_dep,
    get_ssh_service,
)
from app.logging_config import logger
from app.models import AppSshKey, Project, Server, User
from app.redis_client import get_redis
from app.schemas.servers import (
    CommandResultResponse,
    CreateServerRequest,
    DeleteServerResponse,
    InstallKeyRequest,
    ProbeResponse,
    ReregisterCompleteRequest,
    ReregisterProbeResponse,
    ServerDeletionPreviewProject,
    ServerDeletionPreviewResponse,
    ServerResponse,
)
from app.services.app_key_service import (
    AppKeyMissing,
    create_key_for_server,
    get_key_for_server,
)
from app.services.github_service import GithubService
from app.services.key_provider import KeyProvider
from app.services.server_deletion_service import (
    ServerDeletionError,
    ServerOperationInFlight,
    cancel_registration,
    delete_server,
)
from app.services.server_reregistration_service import (
    ReregistrationError,
    evict_stale_ssh_state,
    generate_bootstrap_password,
    install_public_key,
    purge_server_projects,
    recheck_pending_host_key,
    regenerate_pending_keypair,
    run_exchange_and_verify,
    smoke_test_pending_key,
    try_resume_with_pending_key,
    verify_password_for_resume,
)
from app.services.ssh_service import (
    HostKeyChangedDuringInstall,
    HostKeyMismatch,
    KeyInstallVerificationFailed,
    PasswordChangeRequired,
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
    new_password_from_client = body.new_password

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
            new_password=new_password_from_client,
        )
    except PasswordChangeRequired as exc:
        # The server forces a password change on first login (expired password) and no
        # new password was supplied. The key never landed, so key_installed stays False;
        # the frontend reads this code, collects a new password, and retries.
        raise HTTPException(
            409,
            detail={"code": "password_change_required", "message": str(exc)},
        ) from exc
    except HostKeyChangedDuringInstall as exc:
        # Aborted before the key was appended; nothing to clean up on cancel.
        raise HTTPException(409, str(exc)) from exc
    except (KeyInstallVerificationFailed, SshHardeningFailed) as exc:
        # These only fire after the key was appended to authorized_keys (disable
        # password auth / key-login verification). Record that the key landed so a
        # later cancel strips it off the VPS, then surface the failure.
        server.key_installed = True
        await db.commit()
        raise HTTPException(502, str(exc)) from exc
    except ProbeError as exc:
        raise HTTPException(502, str(exc)) from exc

    server.status = "verified"
    server.verified_at = datetime.now(timezone.utc)
    server.key_installed = True
    server.password_auth_disabled = disable_password_auth_from_client
    await db.commit()
    await db.refresh(server)

    return ServerResponse.model_validate(server)


# Re-registration (host key changed / VPS rebuilt): a single password-only recovery.
# Both endpoints are allowed while the server is key_mismatch or a re-registration is
# already in flight (so a retried request resumes). HTTP status per error code, never a
# generic 500.
_REREGISTRATION_ERROR_STATUS = {
    "HOST_KEY_CHANGED_AGAIN": 409,
    "AUTH_FAILED": 400,
    "PASSWORD_AUTH_UNAVAILABLE": 400,
    "CHANGE_INCOMPLETE": 400,
    "LOCKED_OUT": 400,
    "NETWORK_UNREACHABLE": 503,
}


def _require_reregisterable(server: Server) -> None:
    """A re-registration entry point is valid only for a server whose host key changed
    or one already mid re-registration (to resume). Everything else is rejected so a
    trusted server is never quietly re-keyed."""
    in_progress = server.reregistration_state not in ("none", "done")
    if server.status != "key_mismatch" and not in_progress:
        raise HTTPException(
            400,
            "Re-registration is only available for a server whose host key has "
            f"changed (status: {server.status}).",
        )


def _reregistration_http_error(exc: ReregistrationError) -> HTTPException:
    return HTTPException(
        _REREGISTRATION_ERROR_STATUS.get(exc.code, 400),
        detail={
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
        },
    )


@router.post(
    "/{server_id}/reregister/probe", response_model=ReregisterProbeResponse
)
async def reregister_probe(
    server: Server = Depends(get_owned_server),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ssh: SSHService = Depends(get_ssh_service),
) -> ReregisterProbeResponse:
    """Capture the rebuilt server's new host key (re-registration step one).

    Opens an unauthenticated probe just long enough to record the presented host key as
    a pending key, held apart from the still-trusted one until the flow finishes.
    Returns the new fingerprint for the user to compare against their provider console.
    Moves the row to awaiting_confirmation.
    """
    _require_reregisterable(server)
    try:
        probe_result = await ssh.probe(server.host, server.port, "root")
    except ProbeError as exc:
        raise _reregistration_http_error(
            ReregistrationError(
                "NETWORK_UNREACHABLE",
                "Could not reach the server. Check that it is powered on and try "
                "again.",
                retryable=True,
            )
        ) from exc

    server.pending_host_key = probe_result.host_key
    server.pending_host_key_type = probe_result.host_key_type
    server.pending_fingerprint_sha256 = probe_result.fingerprint_sha256
    server.reregistration_state = "awaiting_confirmation"
    await db.commit()
    await db.refresh(server)

    return ReregisterProbeResponse(
        server_id=server.id,
        fingerprint_sha256=probe_result.fingerprint_sha256,
    )


@router.post(
    "/{server_id}/reregister/complete", response_model=ServerResponse
)
async def reregister_complete(
    body: ReregisterCompleteRequest,
    server: Server = Depends(get_owned_server),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
    clerk: Clerk = Depends(get_clerk_client),
    github: GithubService = Depends(get_github_service),
) -> ServerResponse:
    """Finish re-registration with the user's root password (step two).

    Re-verifies the host key still matches the pending one, then runs the access engine:
    a clean login, or an automatic provider-forced password change, transparently. On
    success installs a fresh deploy key, promotes the new host key, and returns the
    server verified-but-unhardened so the user re-runs Quick Harden. Every failure is a
    structured error code; the row is left resumable. This route owns all commits, at
    each persisted state transition.
    """
    _require_reregisterable(server)
    if server.pending_host_key is None:
        raise HTTPException(
            400, "Confirm the new fingerprint before completing re-registration."
        )

    password_from_client = body.password

    try:
        # Cheap MITM-swap check: the identity must not have changed since probe.
        await recheck_pending_host_key(server, ssh)

        # Preflight, in order. Both resume paths make a retry idempotent.
        working_password: str | None = None
        resume_installed_key = False

        app_key = await db.scalar(
            select(AppSshKey).where(AppSshKey.server_id == server.id)
        )
        # (a) A pending keypair from a prior attempt already authenticates: the key is
        # installed, so skip straight to the smoke test and promotion.
        if app_key is not None and not app_key.is_active:
            pending_private = await key_provider.decrypt(
                app_key.encrypted_private_key
            )
            if await try_resume_with_pending_key(server, pending_private):
                resume_installed_key = True

        # (b) A bootstrap password from a prior attempt still works: that forced change
        # took, so reuse it as the working password.
        if not resume_installed_key and server.bootstrap_password is not None:
            previous = (
                await key_provider.decrypt(server.bootstrap_password)
            ).decode("utf-8")
            if await verify_password_for_resume(server, previous):
                working_password = previous

        if not resume_installed_key and working_password is None:
            # Write-ahead: generate and persist the replacement password BEFORE the
            # exchange, so a crash mid-change never loses the only working credential.
            generated = generate_bootstrap_password()
            server.reregistration_state = "exchanging"
            server.bootstrap_password = await key_provider.encrypt(
                generated.encode("utf-8")
            )
            await db.commit()

            working_password = await run_exchange_and_verify(
                server, password_from_client, generated
            )
            server.reregistration_state = "verifying"
            await db.commit()

        # Post-access. Fresh keypair unless we resumed onto an already-installed one.
        server.reregistration_state = "installing_key"
        await db.commit()

        if resume_installed_key:
            app_key = await get_key_for_server(server, db)
        else:
            app_key = await regenerate_pending_keypair(server, db, key_provider)
            await install_public_key(
                server, working_password, app_key.public_key
            )

        app_private = await key_provider.decrypt(app_key.encrypted_private_key)
        if not await smoke_test_pending_key(server, app_private):
            raise ReregistrationError(
                "LOCKED_OUT",
                "Abstract could not complete the login. Reset the root password from "
                "your provider's control panel and try again.",
            )

        # Promote the pending host key and key now that key-based login is proven.
        server.host_key = server.pending_host_key
        server.host_key_type = server.pending_host_key_type
        server.fingerprint_sha256 = server.pending_fingerprint_sha256
        server.pending_host_key = None
        server.pending_host_key_type = None
        server.pending_fingerprint_sha256 = None
        app_key.is_active = True
        server.bootstrap_password = None

        # Drop stale pooled connections and cached key material for this server.
        await evict_stale_ssh_state(
            server, current_user.id, session_id, redis, ssh
        )

        # Reset the facts a rebuild invalidated: verified but unhardened, root again.
        server.status = "verified"
        server.verified_at = datetime.now(timezone.utc)
        server.username = "root"
        server.sudo_user_name = None
        server.key_installed = True
        server.password_auth_disabled = False
        server.root_login_disabled = False
        server.firewall_enabled = False
        server.docker_installed = False
        server.base_packages_installed = False
        server.nginx_installed = False
        server.swap_configured = False
        server.last_system_update_at = None
        server.reregistration_state = "done"

        # A rebuilt box is a blank slate: purge every project on it (rows + cascaded
        # runs/env/deploy-key state) and revoke the now-orphaned GitHub deploy keys
        # best-effort. The user re-creates projects from scratch, like a new server.
        await purge_server_projects(server, db, clerk, github, current_user)

        await db.commit()
        await db.refresh(server)
    except ReregistrationError as exc:
        # Discard the staged (uncommitted) post-access work; any write-ahead already
        # committed stays so the next attempt resumes.
        await db.rollback()
        logger.info(
            "Re-registration failed for server %s: %s", server.id, exc.code
        )
        raise _reregistration_http_error(exc) from exc

    return ServerResponse.model_validate(server)


@router.post("/{server_id}/cancel", response_model=DeleteServerResponse)
async def cancel_server(
    server: Server = Depends(get_owned_server),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
) -> DeleteServerResponse:
    """Cancel a server that is still pending verification.

    The explicit "cancel registration" action. If Abstract's key was already
    installed on the VPS (a partial install that then failed), first strip the key
    off the box (re-enabling password login when install had disabled it) before
    deleting the row. Nothing is deleted while the key might still be on the server.

    400 if the server has already been verified (verified servers are deleted through
    DELETE /{server_id}). 502 with the ordered step list if remote cleanup cannot be
    completed; the row is kept intact so the user can retry once the box is reachable.
    """
    if server.status != "pending_verification":
        raise HTTPException(
            400, "Only pending verification servers can be cancelled."
        )
    try:
        steps = await cancel_registration(
            server=server,
            current_user=current_user,
            session_id=session_id,
            db=db,
            ssh=ssh,
            redis=redis,
            key_provider=key_provider,
        )
    except ServerDeletionError as exc:
        raise HTTPException(
            502,
            detail={
                "message": exc.message,
                "failed_step": exc.failed_step,
                "failed_project_id": (
                    str(exc.failed_project_id) if exc.failed_project_id else None
                ),
                "failed_project_name": exc.failed_project_name,
                "steps": [step.model_dump(mode="json") for step in exc.steps],
            },
        ) from exc

    return DeleteServerResponse(success=True, steps=steps)


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


@router.get(
    "/{server_id}/deletion_preview", response_model=ServerDeletionPreviewResponse
)
async def deletion_preview(
    server: Server = Depends(get_owned_server),
    db: AsyncSession = Depends(get_db),
) -> ServerDeletionPreviewResponse:
    """List the projects that a server deletion would destroy. Read-only; used by
    the confirm dialog so the user reviews the blast radius before deleting."""
    rows = await db.scalars(
        select(Project)
        .where(Project.server_id == server.id)
        .order_by(Project.created_at.asc())
    )
    return ServerDeletionPreviewResponse(
        projects=[
            ServerDeletionPreviewProject.model_validate(row) for row in rows
        ]
    )


@router.delete("/{server_id}", response_model=DeleteServerResponse)
async def delete_server_route(
    server: Server = Depends(get_owned_server),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
    clerk: Clerk = Depends(get_clerk_client),
    github: GithubService = Depends(get_github_service),
) -> DeleteServerResponse:
    """Delete a server: tear down every project on it, remove Abstract from the VPS
    (revoke sudoers, restore password + root SSH login, strip the app key from
    authorized_keys), then hard-delete the row.

    409 if the server or any of its projects is already busy. 502 with the ordered
    step list if any step fails; nothing is left half-deleted, so retrying picks up
    cleanly once the underlying problem (for example VPS reachability) is fixed.
    """
    if server.active_operation is not None:
        raise HTTPException(
            409, {"active_operation": server.active_operation}
        )
    try:
        steps = await delete_server(
            server=server,
            current_user=current_user,
            session_id=session_id,
            db=db,
            ssh=ssh,
            redis=redis,
            key_provider=key_provider,
            clerk=clerk,
            github=github,
        )
    except ServerOperationInFlight as exc:
        raise HTTPException(409, exc.detail) from exc
    except ServerDeletionError as exc:
        raise HTTPException(
            502,
            detail={
                "message": exc.message,
                "failed_step": exc.failed_step,
                "failed_project_id": (
                    str(exc.failed_project_id) if exc.failed_project_id else None
                ),
                "failed_project_name": exc.failed_project_name,
                "steps": [step.model_dump(mode="json") for step in exc.steps],
            },
        ) from exc

    return DeleteServerResponse(success=True, steps=steps)
