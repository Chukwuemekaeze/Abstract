"""Delete a project: the exact reverse of create_project plus teardown of all
state accumulated after creation (published nginx config, running containers,
Let's Encrypt cert).

Transaction model (this is the one service allowed to manage its own
transactions):
  * Commit A sets active_operation='deleting' so concurrent mutations see it
    immediately.
  * All external side effects (SSH, GitHub) then run with NO DB transaction
    held open.
  * Commit B either hard-deletes the row (cascade removes env files, env vars,
    the deploy key row, and run history) on success, or clears active_operation
    on failure so the user can retry.

Any step failure aborts the whole deletion, leaves the row intact, and raises
ProjectDeletionError carrying the ordered step list so the route can return a
structured 502. Every shell command is idempotent, so a retry after a partial
failure lands cleanly.
"""

import shlex
from uuid import UUID

import asyncssh
import redis.asyncio as aioredis
from clerk_backend_api import Clerk
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_config import logger
from app.models import Project, ProjectDeployKey, Server, User
from app.schemas.projects import DeletionStepResult
from app.services.clerk_oauth import get_github_oauth_token
from app.services.github_service import GithubService
from app.services.key_provider import KeyProvider
from app.services.project_service import ssh_config_removal_command
from app.services.run_service import COMPOSE_FILE_CANDIDATES
from app.services.ssh_service import SSHService

__all__ = ["ProjectDeletionError", "delete_project"]

_TIMEOUT_CHECK = 30
_TIMEOUT_CERTBOT = 180
_TIMEOUT_COMPOSE_DOWN = 300
_TIMEOUT_CLONE = 300


class ProjectDeletionError(Exception):
    """A deletion step failed; the row is intact and active_operation is cleared."""

    def __init__(
        self, *, failed_step: str, steps: list[DeletionStepResult], message: str
    ):
        self.failed_step = failed_step
        self.steps = steps
        self.message = message
        super().__init__(message)


class _StepFailed(Exception):
    """Internal: a single step failed. Carries the step name and user detail."""

    def __init__(self, step: str, detail: str):
        self.step = step
        self.detail = detail
        super().__init__(detail)


def _output(result: asyncssh.SSHCompletedProcess) -> str:
    return f"{result.stdout or ''}{result.stderr or ''}".rstrip()


async def _run_step(
    conn: asyncssh.SSHClientConnection,
    step: str,
    command: str,
    timeout: int = _TIMEOUT_CHECK,
) -> None:
    """Run one idempotent shell command for a step. Any transport error or
    nonzero exit raises _StepFailed so the sequence aborts."""
    try:
        result = await conn.run(command, check=False, timeout=timeout)
    except (TimeoutError, asyncssh.Error, OSError) as exc:
        raise _StepFailed(step, f"{step} did not complete: {exc}") from exc
    if result.exit_status not in (0, None):
        raise _StepFailed(step, _output(result) or f"{step} exited {result.exit_status}")


def _priv(server: Server) -> str:
    """Privilege prefix: empty as root, "sudo " as the non-root sudo user."""
    return "" if server.username == "root" else "sudo "


def _unpublish_command(server: Server, slug: str, domain: str) -> str:
    """Remove the enabled symlink and available config, delete the cert (guarded
    by test -d so a retry after a successful delete is a no-op), then validate
    and reload nginx. All idempotent."""
    priv = _priv(server)
    symlink = shlex.quote(f"/etc/nginx/sites-enabled/{slug}.conf")
    config = shlex.quote(f"/etc/nginx/sites-available/{slug}.conf")
    live_dir = shlex.quote(f"/etc/letsencrypt/live/{domain}")
    quoted_domain = shlex.quote(domain)
    return (
        f"{priv}rm -f {symlink} && "
        f"{priv}rm -f {config} && "
        f"if [ -d {live_dir} ]; then "
        f"{priv}certbot delete --cert-name {quoted_domain} --non-interactive; fi && "
        f"{priv}nginx -t && "
        f"{priv}systemctl reload nginx"
    )


def _compose_down_command(clone_path: str, compose_file_path: str | None) -> str:
    """`docker compose down --remove-orphans` in the clone dir, honoring a
    compose file override. Two guards keep retries idempotent: the clone dir
    must exist, and a compose file must still be present. The second guard
    matters after a partially failed delete: a prior `rm -rf` can strip the
    (deploy-owned) compose file while leaving root-owned build artifacts, and
    `docker compose down` with no file errors out ("no configuration file
    provided") even though the containers are already gone. No file means
    nothing to bring down, so the step is a clean no-op."""
    quoted_clone = shlex.quote(clone_path)
    if compose_file_path and compose_file_path not in COMPOSE_FILE_CANDIDATES:
        prefix = f"docker compose -f {shlex.quote(compose_file_path)}"
        file_test = f"[ -f {shlex.quote(compose_file_path)} ]"
    else:
        prefix = "docker compose"
        file_test = " || ".join(
            f"[ -f {shlex.quote(name)} ]" for name in COMPOSE_FILE_CANDIDATES
        )
    return (
        f"if [ -d {quoted_clone} ]; then cd {quoted_clone} && "
        f"if {file_test}; then {prefix} down --remove-orphans; fi; fi"
    )


async def _clear_active_operation(db: AsyncSession, project_id: UUID) -> None:
    """Commit B (failure path): drop the lock so the user can retry."""
    await db.rollback()
    project = await db.get(Project, project_id)
    if project is not None:
        project.active_operation = None
        await db.commit()


async def delete_project(
    *,
    project: Project,
    server: Server,
    current_user: User,
    session_id: str,
    db: AsyncSession,
    ssh: SSHService,
    redis: aioredis.Redis,
    key_provider: KeyProvider,
    clerk: Clerk,
    github: GithubService,
) -> list[DeletionStepResult]:
    """Run the ordered teardown. Returns the step list on success; raises
    ProjectDeletionError (row intact, active_operation cleared) on any failure."""
    # Snapshot everything the side-effect steps need before Commit A expires the
    # instance, so no lazy load happens while there is no transaction open.
    project_id = project.id
    slug = project.slug
    clone_path = project.clone_path
    runtime_status = project.runtime_status
    published = project.published_at is not None
    cloned = project.cloned_at is not None
    domain = project.domain
    compose_file_path = project.compose_file_path
    repo_full_name = project.github_repo_full_name
    deploy_key_id = await db.scalar(
        select(ProjectDeployKey.github_deploy_key_id).where(
            ProjectDeployKey.project_id == project_id
        )
    )

    # -- Commit A: the lock is visible to concurrent requests immediately -----
    project.active_operation = "deleting"
    await db.commit()

    steps: list[DeletionStepResult] = []
    try:
        try:
            conn = await ssh.get_connection(
                server, current_user.id, session_id, redis, db, key_provider
            )
        except Exception as exc:
            raise _StepFailed(
                "connect_ssh", f"Could not connect to the server: {exc}"
            ) from exc

        # -- 1. unpublish -----------------------------------------------------
        if not published:
            steps.append(
                DeletionStepResult(
                    name="unpublish",
                    status="skipped",
                    detail="Project was never published.",
                )
            )
        else:
            await _run_step(
                conn,
                "unpublish",
                _unpublish_command(server, slug, domain or ""),
                timeout=_TIMEOUT_CERTBOT,
            )
            steps.append(DeletionStepResult(name="unpublish", status="completed"))

        # -- 2. stop_containers ----------------------------------------------
        if runtime_status != "running":
            steps.append(
                DeletionStepResult(
                    name="stop_containers",
                    status="skipped",
                    detail="Project is not running.",
                )
            )
        else:
            await _run_step(
                conn,
                "stop_containers",
                _compose_down_command(clone_path, compose_file_path),
                timeout=_TIMEOUT_COMPOSE_DOWN,
            )
            steps.append(
                DeletionStepResult(name="stop_containers", status="completed")
            )

        # -- 3. delete_clone --------------------------------------------------
        if not cloned:
            steps.append(
                DeletionStepResult(
                    name="delete_clone",
                    status="skipped",
                    detail="Project was never cloned.",
                )
            )
        else:
            # sudo, unlike the clone at create time: a container run as root can
            # leave root-owned build artifacts (e.g. a bind-mounted dist/) inside
            # the clone dir, which the sudo user cannot rm on its own.
            await _run_step(
                conn,
                "delete_clone",
                f"{_priv(server)}rm -rf {shlex.quote(clone_path)}",
                timeout=_TIMEOUT_CLONE,
            )
            steps.append(DeletionStepResult(name="delete_clone", status="completed"))

        # -- 4. remove_ssh_config_block --------------------------------------
        await _run_step(
            conn, "remove_ssh_config_block", ssh_config_removal_command(slug)
        )
        steps.append(
            DeletionStepResult(name="remove_ssh_config_block", status="completed")
        )

        # -- 5. delete_vps_deploy_key_files ----------------------------------
        await _run_step(
            conn,
            "delete_vps_deploy_key_files",
            f"rm -f ~/.ssh/{slug}-deploy ~/.ssh/{slug}-deploy.pub",
        )
        steps.append(
            DeletionStepResult(name="delete_vps_deploy_key_files", status="completed")
        )

        # -- 6. revoke_github_deploy_key -------------------------------------
        if deploy_key_id is None:
            steps.append(
                DeletionStepResult(
                    name="revoke_github_deploy_key",
                    status="skipped",
                    detail="No deploy key on record.",
                )
            )
        else:
            try:
                token = await get_github_oauth_token(
                    clerk, current_user.clerk_user_id
                )
                # delete_deploy_key treats 404 (already gone) as success.
                await github.delete_deploy_key(token, repo_full_name, deploy_key_id)
            except Exception as exc:
                raise _StepFailed(
                    "revoke_github_deploy_key",
                    f"Could not revoke the GitHub deploy key: {exc}",
                ) from exc
            steps.append(
                DeletionStepResult(
                    name="revoke_github_deploy_key", status="completed"
                )
            )

        # -- 7. delete_db_row (Commit B, success) ----------------------------
        try:
            row = await db.get(Project, project_id)
            if row is not None:
                await db.delete(row)
            await db.commit()
        except Exception as exc:
            await db.rollback()
            raise _StepFailed(
                "delete_db_row", f"Could not delete the project record: {exc}"
            ) from exc
        steps.append(DeletionStepResult(name="delete_db_row", status="completed"))
    except _StepFailed as exc:
        steps.append(
            DeletionStepResult(name=exc.step, status="failed", detail=exc.detail)
        )
        await _clear_active_operation(db, project_id)
        logger.warning(
            "Project deletion aborted at step {} for project {}: {}",
            exc.step,
            project_id,
            exc.detail,
        )
        raise ProjectDeletionError(
            failed_step=exc.step,
            steps=steps,
            message=f"Deletion failed at step '{exc.step}'.",
        ) from exc

    return steps
