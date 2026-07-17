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
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps.auth import get_current_session_id, get_current_user
from app.deps.project_ownership import (
    get_editable_project,
    get_idle_project,
    get_owned_project,
)
from app.deps.services import get_key_provider_dep, get_ssh_service
from app.models import Project, ProjectDeployKey, Server, User
from app.redis_client import get_redis
from app.routes.projects import _project_fields
from app.schemas.env import DetectedPortResponse, PublishRequest, RunResultResponse
from app.schemas.project_runs import (
    ProjectRunDetail,
    ProjectRunRead,
    RollbackRequest,
)
from app.schemas.projects import ProjectResponse, PullResultResponse
from app.services import (
    project_run_service,
    project_service,
    publish_service,
    rollback_service,
    run_service,
)
from app.services.key_provider import KeyProvider
from app.services.operation_lock import acquire_operation, release_operation
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
from app.services.rollback_service import (
    AlreadyAtThisVersion,
    CannotRollbackToFailedRun,
    TargetRunNotFound,
)
from app.services.run_service import (
    ComposeConfigInvalid,
    ComposeFileNotFound,
    ComposeUpFailed,
    ContainerNotRunning,
    EnvFileKeyCollision,
    RollbackCheckoutFailed,
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


async def _record_run_failure(
    db: AsyncSession,
    project_id: UUID,
    commit_sha: str | None,
    git_ref: str | None,
    captured_output: str | None,
) -> None:
    """Failure path shared by start and rollback: after the work rolled back,
    persist runtime_status='failed', insert a failed run row (if the commit was
    resolved), and clear the operation lock, all in one fresh commit. A failed
    attempt never supersedes the previous running row."""
    await db.rollback()
    failed_project = await db.get(Project, project_id)
    if failed_project is not None:
        failed_project.runtime_status = "failed"
        failed_project.active_operation = None
    if commit_sha:
        await project_run_service.record_failed_run(
            db, project_id, commit_sha, git_ref, captured_output
        )
    await db.commit()


@router.post("/api/projects/{project_id}/start", response_model=RunResultResponse)
async def start_project_route(
    project: Project = Depends(get_idle_project),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
) -> RunResultResponse:
    """Write env files to the VPS and `docker compose up -d --build`, recording
    the run in project history.

    On success a 'running' run row is inserted (superseding the previous one)
    against the freshly resolved HEAD. On compose or container failure the work
    rolls back, runtime_status='failed' plus a 'failed' run row are committed
    separately so the card and history reflect reality, and the 502 body carries
    the captured output. The active_operation lock is released on every path.
    """
    project_id = project.id
    server = await _get_server(db, project)
    commit_sha: str | None = None
    git_ref: str | None = None

    await acquire_operation(db, project, "starting")
    try:
        conn = await ssh.get_connection(
            server, current_user.id, session_id, redis, db, key_provider
        )
        commit_sha, git_ref = await project_run_service.resolve_git_state(
            conn, project.clone_path
        )
        result = await run_service.execute_run(
            conn=conn, project=project, db=db, key_provider=key_provider
        )
        await project_run_service.record_running_run(
            db, project_id, commit_sha, git_ref, result.build_output
        )
        project.active_operation = None
    except (ComposeFileNotFound, EnvFileKeyCollision) as exc:
        await release_operation(db, project_id)
        raise HTTPException(400, str(exc)) from exc
    except ComposeConfigInvalid as exc:
        await _record_run_failure(
            db, project_id, commit_sha, git_ref, exc.captured_output
        )
        raise HTTPException(
            502,
            detail={
                "message": "Your docker-compose file is invalid or could not be read.",
                "captured_output": exc.captured_output,
                "build_output": exc.captured_output,
            },
        ) from exc
    except (ComposeUpFailed, ContainerNotRunning) as exc:
        await _record_run_failure(
            db, project_id, commit_sha, git_ref, exc.captured_output
        )
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
        await release_operation(db, project_id)
        raise
    except Exception:
        await release_operation(db, project_id)
        raise

    try:
        await db.commit()
    except IntegrityError as exc:
        # Another run claimed the single running slot between our supersede and
        # insert. The lock makes this practically unreachable, but the partial
        # unique index is the real guarantee.
        await release_operation(db, project_id)
        raise HTTPException(
            409, detail={"active_operation": "starting"}
        ) from exc

    return RunResultResponse(
        runtime_status=result.runtime_status,
        started_at=result.started_at,
        captured_output=result.captured_output,
        build_output=result.build_output,
    )


@router.post("/api/projects/{project_id}/pull", response_model=PullResultResponse)
async def pull_latest_route(
    project: Project = Depends(get_editable_project),
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
    project: Project = Depends(get_editable_project),
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
    project: Project = Depends(get_idle_project),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
) -> ProjectResponse:
    """Point a domain at the running app: nginx config + certbot certificate.

    Holds the 'publishing' operation lock so a start, rollback, or delete cannot
    race it. The service cleans up its VPS side effects best-effort on every
    failure path before re-raising; the lock is released on every path.
    """
    project_id = project.id
    domain_from_client = body.domain
    internal_port_from_client = body.internal_port

    server = await _get_server(db, project)

    await acquire_operation(db, project, "publishing")
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
        project.active_operation = None
    except (AppNotRunning, NginxNotInstalled, DomainDoesNotResolve) as exc:
        await release_operation(db, project_id)
        raise HTTPException(400, str(exc)) from exc
    except (AlreadyPublished, DomainAlreadyUsed, PortAlreadyUsed) as exc:
        await release_operation(db, project_id)
        raise HTTPException(409, str(exc)) from exc
    except NothingListening as exc:
        await release_operation(db, project_id)
        raise HTTPException(
            502, detail={"message": str(exc), "captured_output": None}
        ) from exc
    except (NginxConfigInvalid, CertbotFailed, PublishVerificationFailed) as exc:
        await release_operation(db, project_id)
        raise HTTPException(
            502,
            detail={"message": str(exc), "captured_output": exc.captured_output},
        ) from exc
    except HTTPException:
        await release_operation(db, project_id)
        raise
    except Exception:
        await release_operation(db, project_id)
        raise

    try:
        await db.commit()
    except IntegrityError as exc:
        # Race on the partial unique indexes: another publish claimed the
        # domain or port between our read check and this commit.
        await release_operation(db, project_id)
        raise HTTPException(
            409, "The domain or port was just claimed by another project."
        ) from exc

    await db.refresh(project)
    return ProjectResponse(**_project_fields(project, fingerprint))


@router.get(
    "/api/projects/{project_id}/runs", response_model=list[ProjectRunRead]
)
async def list_project_runs_route(
    project: Project = Depends(get_owned_project),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    before: UUID | None = Query(None),
) -> list[ProjectRunRead]:
    """Run history for a project, newest first. Reads are always allowed, so
    ownership is via get_owned_project. build_output is omitted from the list."""
    runs = await project_run_service.list_runs(
        db, project.id, limit=limit, before=before
    )
    return [ProjectRunRead.model_validate(run) for run in runs]


@router.get(
    "/api/projects/{project_id}/runs/{run_id}", response_model=ProjectRunDetail
)
async def get_project_run_route(
    run_id: UUID,
    project: Project = Depends(get_owned_project),
    db: AsyncSession = Depends(get_db),
) -> ProjectRunDetail:
    """A single run including its build_output. 404 if it belongs to another
    project."""
    run = await project_run_service.get_run(db, project.id, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return ProjectRunDetail.model_validate(run)


@router.post("/api/projects/{project_id}/rollback", response_model=RunResultResponse)
async def rollback_project_route(
    body: RollbackRequest,
    project: Project = Depends(get_idle_project),
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
) -> RunResultResponse:
    """Roll back to a prior run's commit: git checkout the recorded SHA, then
    rebuild via the shared Run flow.

    Env vars come from the project's CURRENT database state, never a snapshot, so
    a rollback never silently reintroduces rotated secrets. Mirrors /start: on
    success a new 'running' run row (carrying the target's SHA and ref) supersedes
    the previous one; on rebuild failure a 'failed' row is recorded, the previous
    running row is left untouched, and a 502 carries the build output. The
    active_operation lock is released on every path.
    """
    project_id = project.id
    server = await _get_server(db, project)

    try:
        target = await rollback_service.resolve_target(
            db, project, body.target_run_id
        )
    except TargetRunNotFound as exc:
        raise HTTPException(404, "Run not found") from exc
    except CannotRollbackToFailedRun as exc:
        raise HTTPException(400, "Cannot roll back to a failed run.") from exc
    except AlreadyAtThisVersion as exc:
        raise HTTPException(400, "Already at this version.") from exc

    # Preserve the target's SHA and ref for display continuity and for recording
    # a failed attempt even if the rebuild raises.
    commit_sha = target.git_commit_sha
    git_ref = target.git_ref

    await acquire_operation(db, project, "rolling_back")
    try:
        conn = await ssh.get_connection(
            server, current_user.id, session_id, redis, db, key_provider
        )
        await run_service.rollback_checkout(conn, project.clone_path, commit_sha)
        result = await run_service.execute_run(
            conn=conn, project=project, db=db, key_provider=key_provider
        )
        await project_run_service.record_running_run(
            db, project_id, commit_sha, git_ref, result.build_output
        )
        project.active_operation = None
    except RollbackCheckoutFailed as exc:
        # The tree could not be moved to the target commit; nothing was rebuilt.
        await release_operation(db, project_id)
        raise HTTPException(
            500,
            detail={
                "message": "git checkout failed during rollback.",
                "captured_output": exc.captured_output,
                "build_output": exc.captured_output,
            },
        ) from exc
    except (ComposeFileNotFound, EnvFileKeyCollision) as exc:
        await release_operation(db, project_id)
        raise HTTPException(400, str(exc)) from exc
    except ComposeConfigInvalid as exc:
        await _record_run_failure(
            db, project_id, commit_sha, git_ref, exc.captured_output
        )
        raise HTTPException(
            502,
            detail={
                "message": "Your docker-compose file is invalid or could not be read.",
                "captured_output": exc.captured_output,
                "build_output": exc.captured_output,
            },
        ) from exc
    except (ComposeUpFailed, ContainerNotRunning) as exc:
        await _record_run_failure(
            db, project_id, commit_sha, git_ref, exc.captured_output
        )
        raise HTTPException(
            502,
            detail={
                "message": str(exc),
                "captured_output": exc.captured_output,
                "build_output": exc.captured_output,
            },
        ) from exc
    except HTTPException:
        await release_operation(db, project_id)
        raise
    except Exception:
        await release_operation(db, project_id)
        raise

    try:
        await db.commit()
    except IntegrityError as exc:
        await release_operation(db, project_id)
        raise HTTPException(
            409, detail={"active_operation": "rolling_back"}
        ) from exc

    return RunResultResponse(
        runtime_status=result.runtime_status,
        started_at=result.started_at,
        captured_output=result.captured_output,
        build_output=result.build_output,
    )
