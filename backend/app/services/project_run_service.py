"""Run history: resolve the deployed git commit and record run outcomes.

The caller owns the transaction; this module flushes but never commits. The
supersede + insert of a new running row happen in the caller's single
transaction so the partial unique index (one 'running' row per project) is never
transiently violated.

build_output is stored verbatim (already truncated to 200KB by the Run flow) and
is never logged here.
"""

import shlex
from datetime import datetime, timezone
from uuid import UUID

import asyncssh
from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProjectRun
from app.services.run_service import _run

DEFAULT_RUN_LIMIT = 50
MAX_RUN_LIMIT = 200


def _q(path: str) -> str:
    return shlex.quote(path)


async def resolve_git_state(
    conn: asyncssh.SSHClientConnection, clone_path: str
) -> tuple[str, str | None]:
    """Return (commit_sha, git_ref) for the clone's current HEAD.

    git_ref is the branch/tag name, or None when HEAD is detached (rev-parse
    prints the literal 'HEAD'). A failure to read either is surfaced to the
    caller as an SSH error rather than swallowed: we do not want to record a run
    against an unknown commit.
    """
    sha_result = await _run(conn, f"git -C {_q(clone_path)} rev-parse HEAD")
    commit_sha = (sha_result.stdout or "").strip()

    ref_result = await _run(
        conn, f"git -C {_q(clone_path)} rev-parse --abbrev-ref HEAD"
    )
    ref = (ref_result.stdout or "").strip()
    git_ref = None if ref in ("", "HEAD") else ref
    return commit_sha, git_ref


async def record_running_run(
    db: AsyncSession,
    project_id: UUID,
    commit_sha: str,
    git_ref: str | None,
    build_output: str | None,
) -> ProjectRun:
    """Supersede the current running row and insert a new running row.

    Both happen in the caller's transaction so the one-running-row invariant
    holds atomically.
    """
    now = datetime.now(timezone.utc)
    await db.execute(
        update(ProjectRun)
        .where(ProjectRun.project_id == project_id, ProjectRun.status == "running")
        .values(status="superseded", finished_at=now)
    )
    run = ProjectRun(
        project_id=project_id,
        git_commit_sha=commit_sha,
        git_ref=git_ref,
        status="running",
        started_at=now,
        finished_at=now,
        build_output=build_output,
    )
    db.add(run)
    await db.flush()
    return run


async def record_failed_run(
    db: AsyncSession,
    project_id: UUID,
    commit_sha: str,
    git_ref: str | None,
    build_output: str | None,
) -> ProjectRun:
    """Insert a failed row. The previous running row is left untouched: a failed
    attempt does not change what is actually deployed."""
    now = datetime.now(timezone.utc)
    run = ProjectRun(
        project_id=project_id,
        git_commit_sha=commit_sha,
        git_ref=git_ref,
        status="failed",
        started_at=now,
        finished_at=now,
        build_output=build_output,
    )
    db.add(run)
    await db.flush()
    return run


async def list_runs(
    db: AsyncSession,
    project_id: UUID,
    limit: int = DEFAULT_RUN_LIMIT,
    before: UUID | None = None,
) -> list[ProjectRun]:
    """Runs for a project, newest first. `before` is a run id for keyset
    pagination: only rows older than it are returned."""
    limit = max(1, min(limit, MAX_RUN_LIMIT))
    stmt = (
        select(ProjectRun)
        .where(ProjectRun.project_id == project_id)
        .order_by(ProjectRun.created_at.desc(), ProjectRun.id.desc())
        .limit(limit)
    )
    if before is not None:
        cursor = await get_run(db, project_id, before)
        if cursor is not None:
            stmt = stmt.where(
                tuple_(ProjectRun.created_at, ProjectRun.id)
                < (cursor.created_at, cursor.id)
            )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_run(
    db: AsyncSession, project_id: UUID, run_id: UUID
) -> ProjectRun | None:
    """A single run scoped to the project; None if it belongs to another project."""
    run = await db.get(ProjectRun, run_id)
    if run is None or run.project_id != project_id:
        return None
    return run


async def current_running_run(
    db: AsyncSession, project_id: UUID
) -> ProjectRun | None:
    return await db.scalar(
        select(ProjectRun).where(
            ProjectRun.project_id == project_id, ProjectRun.status == "running"
        )
    )
