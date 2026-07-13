"""Env files API.

Per-project dotenv files whose values are encrypted at rest and never leave
the server after being saved: every response exposes keys and counts only.
Client supplied values carry the _from_client suffix. Ownership is enforced
by get_owned_project (404, no leak). Each handler is its own single commit
point; the service never commits.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps.project_ownership import get_owned_project
from app.deps.services import get_key_provider_dep
from app.models import Project
from app.schemas.env import (
    CreateEnvFileRequest,
    EnvFileDetailResponse,
    EnvFileListItemResponse,
    UpdateEnvFileRequest,
)
from app.services import env_file_service
from app.services.env_file_service import (
    EnvFileAlreadyExists,
    EnvFileNotFound,
    EnvFilePathInvalid,
)
from app.services.key_provider import KeyProvider

router = APIRouter(
    prefix="/api/projects/{project_id}/env-files", tags=["env-files"]
)


async def _detail_response(
    db: AsyncSession, env_file
) -> EnvFileDetailResponse:
    keys = await env_file_service.get_env_file_keys(db, env_file)
    return EnvFileDetailResponse(
        id=env_file.id,
        path=env_file.path,
        keys=keys,
        updated_at=env_file.updated_at,
    )


@router.get("", response_model=list[EnvFileListItemResponse])
async def list_env_files_route(
    project: Project = Depends(get_owned_project),
    db: AsyncSession = Depends(get_db),
) -> list[EnvFileListItemResponse]:
    rows = await env_file_service.list_env_files(db, project)
    return [
        EnvFileListItemResponse(
            id=env_file.id,
            path=env_file.path,
            variable_count=count,
            updated_at=env_file.updated_at,
        )
        for env_file, count in rows
    ]


@router.get("/{env_file_id}", response_model=EnvFileDetailResponse)
async def get_env_file_route(
    env_file_id: UUID,
    project: Project = Depends(get_owned_project),
    db: AsyncSession = Depends(get_db),
) -> EnvFileDetailResponse:
    try:
        env_file = await env_file_service.get_env_file(db, project, env_file_id)
    except EnvFileNotFound as exc:
        raise HTTPException(404, "Env file not found") from exc
    return await _detail_response(db, env_file)


@router.post("", response_model=EnvFileDetailResponse)
async def create_env_file_route(
    body: CreateEnvFileRequest,
    project: Project = Depends(get_owned_project),
    db: AsyncSession = Depends(get_db),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
) -> EnvFileDetailResponse:
    path_from_client = body.path
    variables_from_client = body.variables

    try:
        env_file = await env_file_service.create_env_file(
            db=db,
            project=project,
            path_from_client=path_from_client,
            variables_from_client=variables_from_client,
            key_provider=key_provider,
        )
        response = await _detail_response(db, env_file)
    except EnvFilePathInvalid as exc:
        await db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except EnvFileAlreadyExists as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        await db.rollback()
        raise

    await db.commit()
    return response


@router.patch("/{env_file_id}", response_model=EnvFileDetailResponse)
async def update_env_file_route(
    env_file_id: UUID,
    body: UpdateEnvFileRequest,
    project: Project = Depends(get_owned_project),
    db: AsyncSession = Depends(get_db),
    key_provider: KeyProvider = Depends(get_key_provider_dep),
) -> EnvFileDetailResponse:
    path_from_client = body.path
    set_variables_from_client = body.set_variables
    remove_keys_from_client = body.remove_keys

    try:
        env_file = await env_file_service.get_env_file(db, project, env_file_id)
        env_file = await env_file_service.update_env_file(
            db=db,
            project=project,
            env_file=env_file,
            path_from_client=path_from_client,
            set_variables_from_client=set_variables_from_client,
            remove_keys_from_client=remove_keys_from_client,
            key_provider=key_provider,
        )
        response = await _detail_response(db, env_file)
    except EnvFileNotFound as exc:
        await db.rollback()
        raise HTTPException(404, "Env file not found") from exc
    except EnvFilePathInvalid as exc:
        await db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except EnvFileAlreadyExists as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        await db.rollback()
        raise

    await db.commit()
    return response


@router.delete("/{env_file_id}", status_code=204)
async def delete_env_file_route(
    env_file_id: UUID,
    project: Project = Depends(get_owned_project),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        env_file = await env_file_service.get_env_file(db, project, env_file_id)
        await env_file_service.delete_env_file(db=db, env_file=env_file)
    except EnvFileNotFound as exc:
        await db.rollback()
        raise HTTPException(404, "Env file not found") from exc
    except Exception:
        await db.rollback()
        raise

    await db.commit()
    return Response(status_code=204)
