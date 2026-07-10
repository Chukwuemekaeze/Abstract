"""Projects API.

A project is a GitHub repo cloned onto a verified, hardened server with a
per-project deploy key. Client supplied values carry the _from_client suffix.
user_id is never read from the client: it always comes from current_user.id.

Database atomicity: POST /api/projects commits exactly once on success and
rolls back on any failure; create_project never commits. The GitHub key and
VPS files are cleaned up best-effort by the service on failure, so a retry
starts from a clean slate either way.
"""

from uuid import UUID  # noqa: F401  (server_id path param type is resolved by FastAPI)

import redis.asyncio as aioredis
from clerk_backend_api import Clerk
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clerk import get_clerk_client
from app.db import get_db
from app.deps.auth import get_current_session_id, get_current_user
from app.deps.server_ownership import get_owned_server
from app.deps.services import (
    get_github_service,
    get_key_provider_dep,
    get_ssh_service,
)
from app.models import Project, ProjectDeployKey, Server, User
from app.redis_client import get_redis
from app.schemas.projects import (
    CreateProjectRequest,
    GithubRepoResponse,
    ProjectListItemResponse,
    ProjectResponse,
)
from app.services.clerk_oauth import GithubTokenUnavailable, get_github_oauth_token
from app.services.github_service import (
    GithubApiError,
    GithubRateLimited,
    GithubRepoNotFound,
    GithubService,
)
from app.services.key_provider import KeyProvider
from app.services.project_service import (
    ClonePathOccupied,
    CloneVerificationFailed,
    DuplicateProject,
    ServerNotEligible,
    create_project,
)
from app.services.ssh_service import SSHService

router = APIRouter(tags=["projects"])

_TOKEN_UNAVAILABLE = (
    "GitHub account not linked; sign out and sign back in with GitHub."
)


def _project_fields(project: Project, fingerprint: str) -> dict:
    return {
        "id": project.id,
        "name": project.name,
        "slug": project.slug,
        "server_id": project.server_id,
        "github_repo_full_name": project.github_repo_full_name,
        "github_repo_id": project.github_repo_id,
        "clone_path": project.clone_path,
        "cloned_at": project.cloned_at,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "deploy_key_fingerprint": fingerprint,
    }


@router.get("/api/projects", response_model=list[ProjectListItemResponse])
async def list_projects(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectListItemResponse]:
    """All of the user's projects across all servers, newest first, with the
    owning server's name and host so the list page needs no second round trip."""
    rows = await db.execute(
        select(
            Project,
            ProjectDeployKey.deploy_key_fingerprint,
            Server.name,
            Server.host,
        )
        .join(ProjectDeployKey, ProjectDeployKey.project_id == Project.id)
        .join(Server, Server.id == Project.server_id)
        .where(Project.user_id == current_user.id)
        .order_by(Project.created_at.desc())
    )
    return [
        ProjectListItemResponse(
            **_project_fields(project, fingerprint),
            server_name=server_name,
            server_host=server_host,
        )
        for project, fingerprint, server_name, server_host in rows.all()
    ]


@router.get(
    "/api/servers/{server_id}/projects", response_model=list[ProjectResponse]
)
async def list_projects_by_server(
    server: Server = Depends(get_owned_server),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectResponse]:
    """Projects on one server, newest first. Ownership enforced by get_owned_server."""
    rows = await db.execute(
        select(Project, ProjectDeployKey.deploy_key_fingerprint)
        .join(ProjectDeployKey, ProjectDeployKey.project_id == Project.id)
        .where(Project.server_id == server.id)
        .order_by(Project.created_at.desc())
    )
    return [
        ProjectResponse(**_project_fields(project, fingerprint))
        for project, fingerprint in rows.all()
    ]


@router.get("/api/github/repos", response_model=list[GithubRepoResponse])
async def list_github_repos(
    current_user: User = Depends(get_current_user),
    clerk: Clerk = Depends(get_clerk_client),
    github: GithubService = Depends(get_github_service),
) -> list[GithubRepoResponse]:
    """Repos the user can add deploy keys to (admin permission), newest push first.

    Hits GitHub on every call; no caching in v1. The authenticated rate limit of
    5000/hour makes that fine for a dropdown.
    """
    try:
        token = await get_github_oauth_token(clerk, current_user.clerk_user_id)
        return await github.list_admin_repos(token)
    except GithubTokenUnavailable as exc:
        raise HTTPException(400, _TOKEN_UNAVAILABLE) from exc
    except GithubRateLimited as exc:
        raise HTTPException(
            429,
            f"GitHub rate limit exceeded; resets at {exc.reset_at.isoformat()}.",
        ) from exc
    except GithubApiError as exc:
        raise HTTPException(
            502, f"GitHub API request failed with status {exc.status_code}."
        ) from exc


@router.post("/api/projects", response_model=ProjectResponse)
async def create_project_route(
    body: CreateProjectRequest,
    current_user: User = Depends(get_current_user),
    session_id: str = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    ssh: SSHService = Depends(get_ssh_service),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
    clerk: Clerk = Depends(get_clerk_client),
    github: GithubService = Depends(get_github_service),
) -> ProjectResponse:
    """Provision a project: deploy key on GitHub, key and config on the VPS, clone.

    Runs inside a single transaction; this handler is the only commit point.
    On failure the transaction rolls back and the service has already attempted
    best-effort cleanup of GitHub and VPS side effects.
    """
    name_from_client = body.name
    server_id_from_client = body.server_id
    github_repo_id_from_client = body.github_repo_id
    github_repo_full_name_from_client = body.github_repo_full_name

    try:
        project, fingerprint = await create_project(
            name_from_client=name_from_client,
            server_id_from_client=server_id_from_client,
            github_repo_id_from_client=github_repo_id_from_client,
            github_repo_full_name_from_client=github_repo_full_name_from_client,
            current_user=current_user,
            session_id=session_id,
            db=db,
            ssh=ssh,
            redis=redis,
            key_provider=key_provider,
            clerk=clerk,
            github=github,
        )
    except ServerNotEligible as exc:
        await db.rollback()
        raise HTTPException(400, exc.reason) from exc
    except DuplicateProject as exc:
        await db.rollback()
        raise HTTPException(
            409, "This repo is already a project on this server."
        ) from exc
    except ClonePathOccupied as exc:
        await db.rollback()
        raise HTTPException(
            409, "The clone path already exists on the server."
        ) from exc
    except GithubTokenUnavailable as exc:
        await db.rollback()
        raise HTTPException(400, _TOKEN_UNAVAILABLE) from exc
    except GithubRepoNotFound as exc:
        await db.rollback()
        raise HTTPException(
            404, "Repo not found on GitHub or you do not have admin access to it."
        ) from exc
    except GithubRateLimited as exc:
        await db.rollback()
        raise HTTPException(
            429,
            f"GitHub rate limit exceeded; resets at {exc.reset_at.isoformat()}.",
        ) from exc
    except GithubApiError as exc:
        await db.rollback()
        raise HTTPException(
            502, f"GitHub API request failed with status {exc.status_code}."
        ) from exc
    except CloneVerificationFailed as exc:
        await db.rollback()
        raise HTTPException(
            502,
            detail={
                "message": "Cloning the repository failed",
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
    return ProjectResponse(**_project_fields(project, fingerprint))
