"""Project runtime API: start, pull latest, refresh status, detected ports, publish.

Client supplied values carry the _from_client suffix. user_id is never read
from the client. Each handler is a single commit point and the services never
commit; the one deliberate exception is POST /start, which after a rollback
persists runtime_status='failed' in a tiny follow-up commit so the UI shows
the true state of the VPS.

502 error bodies use the {message, captured_output} shape shared with the
hardening and project-creation routes, which the frontend already parses.
"""

from uuid import UUID

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps.auth import get_current_session_id, get_current_user
from app.deps.project_ownership import get_owned_project
from app.deps.services import get_key_provider_dep, get_ssh_service
from app.models import Project, ProjectDeployKey, Server, User
from app.redis_client import get_redis
from app.routes.projects import _project_fields
from app.schemas.env import DetectedPortResponse, PublishRequest, RunResultResponse
from app.schemas.projects import ProjectResponse, PullResultResponse
from app.services import project_service, publish_service, run_service
from app.services.key_provider import KeyProvider
from app.services.publish_service import (
    AlreadyPublished,
    AppNotRunning,
    CertbotFailed,
    DomainAlreadyUsed,
    DomainDoesNotResolve,
    NginxConfigInvalid,
    NginxNotInstalled,
    NothingListening,
    PortAlreadyUsed,
    PublishVerificationFailed,
)
from app.services.project_service import CloneMissing, PullFailed
from app.services.run_service import (
    ComposeConfigInvalid,
    ComposeFileNotFound,
    ComposeUpFailed,
    ContainerNotRunning,
    EnvFileKeyCollision,
)
from app.services.ssh_service import SSHService

router = APIRouter(tags=["project-runtime"])


async def _fingerprint(db: AsyncSession, project_id: UUID) -> str:
    fingerprint = await db.scalar(
        select(ProjectDeployKey.deploy_key_fingerprint).where(
            ProjectDeployKey.project_id == project_id
        )
    )
    return fingerprint or ""


async def _get_server(db: AsyncSession, project: Project) -> Server:
    server = await db.get(Server, project.server_id)
    if server is None:
        raise HTTPException(404, "Server not found")
    return server


@router.post("/api/projects/{project_id}/start", response_model=RunResultResponse)
async def start_project_route(
    project: Project = Depends(get_owned_project),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
) -> RunResultResponse:
    """Write env files to the VPS and `docker compose up -d --build`.

    On compose or container failure the transaction rolls back, then
    runtime_status='failed' is committed separately so the card reflects
    reality, and the 502 body carries the captured output.
    """
    project_id = project.id
    server = await _get_server(db, project)

    try:
        conn = await ssh.get_connection(
            server, current_user.id, session_id, redis, db, key_provider
        )
        result = await run_service.start_project(
            conn=conn, project=project, db=db, key_provider=key_provider
        )
    except (ComposeFileNotFound, EnvFileKeyCollision) as exc:
        await db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except ComposeConfigInvalid as exc:
        await db.rollback()
        failed_project = await db.get(Project, project_id)
        if failed_project is not None:
            failed_project.runtime_status = "failed"
            await db.commit()
        raise HTTPException(
            502,
            detail={
                "message": "Your docker-compose file is invalid or could not be read.",
                "captured_output": exc.captured_output,
                "build_output": exc.captured_output,
            },
        ) from exc
    except (ComposeUpFailed, ContainerNotRunning) as exc:
        await db.rollback()
        failed_project = await db.get(Project, project_id)
        if failed_project is not None:
            failed_project.runtime_status = "failed"
            await db.commit()
        raise HTTPException(
            502,
            detail={
                "message": str(exc),
                # captured_output kept for the existing frontend error parser;
                # build_output is the same value under the success-path name.
                "captured_output": exc.captured_output,
                "build_output": exc.captured_output,
            },
        ) from exc
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    await db.commit()
    return RunResultResponse(
        runtime_status=result.runtime_status,
        started_at=result.started_at,
        captured_output=result.captured_output,
        build_output=result.build_output,
    )


@router.post("/api/projects/{project_id}/pull", response_model=PullResultResponse)
async def pull_latest_route(
    project: Project = Depends(get_owned_project),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
) -> PullResultResponse:
    """Sync the clone with origin's default branch (fetch + hard reset).

    Pulling only changes files on disk; running containers keep the old build
    until the user restarts. A failed pull leaves the repo untouched.
    """
    if project.cloned_at is None:
        raise HTTPException(
            409, "The initial clone never completed; the project cannot be pulled."
        )
    server = await _get_server(db, project)

    try:
        conn = await ssh.get_connection(
            server, current_user.id, session_id, redis, db, key_provider
        )
        result = await project_service.pull_latest(conn=conn, project=project)
    except CloneMissing as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except PullFailed as exc:
        await db.rollback()
        raise HTTPException(
            502,
            detail={
                "message": "Pulling the latest code failed",
                "captured_output": exc.captured_output,
            },
        ) from exc
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    await db.commit()
    await db.refresh(project)
    return PullResultResponse(
        before_commit=result.before_commit,
        after_commit=result.after_commit,
        already_up_to_date=result.already_up_to_date,
        updated_at=project.updated_at,
    )


@router.post(
    "/api/projects/{project_id}/refresh_status", response_model=ProjectResponse
)
async def refresh_status_route(
    project: Project = Depends(get_owned_project),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
) -> ProjectResponse:
    server = await _get_server(db, project)

    try:
        conn = await ssh.get_connection(
            server, current_user.id, session_id, redis, db, key_provider
        )
        await run_service.refresh_status(conn=conn, project=project)
        fingerprint = await _fingerprint(db, project.id)
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    await db.commit()
    await db.refresh(project)
    return ProjectResponse(**_project_fields(project, fingerprint))


@router.get(
    "/api/projects/{project_id}/detected_ports",
    response_model=list[DetectedPortResponse],
)
async def detected_ports_route(
    project: Project = Depends(get_owned_project),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
) -> list[DetectedPortResponse]:
    if project.runtime_status != "running":
        raise HTTPException(
            400, "Project is not running; start it before detecting ports."
        )
    server = await _get_server(db, project)

    try:
        conn = await ssh.get_connection(
            server, current_user.id, session_id, redis, db, key_provider
        )
        ports = await run_service.get_detected_ports(conn=conn, project=project)
    except (ComposeFileNotFound, ContainerNotRunning) as exc:
        raise HTTPException(400, str(exc)) from exc

    return [
        DetectedPortResponse(
            service=port.service,
            host_port=port.host_port,
            container_port=port.container_port,
            is_dangerous=port.is_dangerous,
        )
        for port in ports
    ]


@router.post("/api/projects/{project_id}/publish", response_model=ProjectResponse)
async def publish_project_route(
    body: PublishRequest,
    project: Project = Depends(get_owned_project),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
) -> ProjectResponse:
    """Point a domain at the running app: nginx config + certbot certificate.

    Runs inside a single transaction; the service cleans up its VPS side
    effects best-effort on every failure path before re-raising.
    """
    domain_from_client = body.domain
    internal_port_from_client = body.internal_port

    server = await _get_server(db, project)

    try:
        conn = await ssh.get_connection(
            server, current_user.id, session_id, redis, db, key_provider
        )
        await publish_service.publish_project(
            conn=conn,
            project=project,
            server=server,
            current_user=current_user,
            domain_from_client=domain_from_client,
            internal_port_from_client=internal_port_from_client,
            db=db,
        )
        fingerprint = await _fingerprint(db, project.id)
    except (AppNotRunning, NginxNotInstalled, DomainDoesNotResolve) as exc:
        await db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except (AlreadyPublished, DomainAlreadyUsed, PortAlreadyUsed) as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except NothingListening as exc:
        await db.rollback()
        raise HTTPException(
            502, detail={"message": str(exc), "captured_output": None}
        ) from exc
    except (NginxConfigInvalid, CertbotFailed, PublishVerificationFailed) as exc:
        await db.rollback()
        raise HTTPException(
            502,
            detail={"message": str(exc), "captured_output": exc.captured_output},
        ) from exc
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    try:
        await db.commit()
    except IntegrityError as exc:
        # Race on the partial unique indexes: another publish claimed the
        # domain or port between our read check and this commit.
        await db.rollback()
        raise HTTPException(
            409, "The domain or port was just claimed by another project."
        ) from exc

    await db.refresh(project)
    return ProjectResponse(**_project_fields(project, fingerprint))
