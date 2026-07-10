"""Project provisioning: deploy key, VPS files, git clone, all or nothing.

Database atomicity is real: the caller owns the transaction and this module
never commits, so any failure rolls every row back. External state atomicity
is best-effort only: if a step fails after the GitHub deploy key was
registered or VPS files were written, we attempt to undo those side effects,
log any cleanup failure, and never let cleanup mask the original error. The
DB rollback is the guarantee that protects the user's view of the world; the
cleanup just leaves the VPS and GitHub tidy for a retry.

Shell rules: every command is idempotent so retries are safe, every
interpolated value is shlex-quoted, and nothing runs under sudo because all
project files belong to the sudo user's own home.
"""

import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import asyncssh
import redis.asyncio as aioredis
from clerk_backend_api import Clerk
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_config import logger
from app.models import Project, ProjectDeployKey, Server, User
from app.services.clerk_oauth import GithubTokenUnavailable, get_github_oauth_token
from app.services.github_service import GithubService
from app.services.key_provider import KeyProvider
from app.services.project_key_service import generate_deploy_keypair
from app.services.ssh_service import SSHService
from app.utils.slug import slugify

__all__ = [
    "ProjectServiceError",
    "ServerNotEligible",
    "DuplicateProject",
    "ClonePathOccupied",
    "CloneVerificationFailed",
    "GithubTokenUnavailable",
    "create_project",
]

_TIMEOUT_CHECK = 30
_TIMEOUT_CLONE = 300


class ProjectServiceError(Exception):
    pass


class ServerNotEligible(ProjectServiceError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class DuplicateProject(ProjectServiceError):
    pass


class ClonePathOccupied(ProjectServiceError):
    pass


class CloneVerificationFailed(ProjectServiceError):
    def __init__(self, captured_output: str):
        self.captured_output = captured_output
        super().__init__(captured_output)


@dataclass
class _ExternalState:
    """What has been done outside Postgres, so cleanup knows what to undo."""

    github_key_id: int | None = None
    key_file_written: bool = False
    config_block_written: bool = False
    clone_started: bool = False


async def _run(
    conn: asyncssh.SSHClientConnection, command: str, timeout: int = _TIMEOUT_CHECK
) -> asyncssh.SSHCompletedProcess:
    """Run a command as the login (sudo) user. Never sudo: project files live in
    the user's own home."""
    return await conn.run(command, check=False, timeout=timeout)


def _ssh_config_block(slug: str) -> str:
    return (
        f"\nHost github-{slug}\n"
        f"  HostName github.com\n"
        f"  User git\n"
        f"  IdentityFile ~/.ssh/{slug}-deploy\n"
        f"  IdentitiesOnly yes\n"
    )


async def _unique_slug(db: AsyncSession, user_id: UUID, name: str) -> str:
    """Slugify and append -2, -3, ... until free for this user.

    Runs inside the caller's transaction, so the UNIQUE(user_id, slug)
    constraint still backstops any concurrent create that races us.
    """
    base = slugify(name)
    slug = base
    suffix = 2
    while await db.scalar(
        select(Project.id).where(Project.user_id == user_id, Project.slug == slug)
    ):
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


async def create_project(
    *,
    name_from_client: str,
    server_id_from_client: UUID,
    github_repo_id_from_client: int,
    github_repo_full_name_from_client: str,
    current_user: User,
    session_id: str,
    db: AsyncSession,
    ssh: SSHService,
    redis: aioredis.Redis,
    key_provider: KeyProvider,
    clerk: Clerk,
    github: GithubService,
) -> tuple[Project, str]:
    """Provision a project end to end. Returns (project, key fingerprint).

    Never commits; the route handler is the single commit point. On failure the
    exception propagates (triggering the caller's rollback) after best-effort
    external cleanup.
    """
    # -- 1. Preconditions, all before any state is created -------------------
    server = await db.get(Server, server_id_from_client)
    if server is None or server.user_id != current_user.id:
        raise ServerNotEligible("Server not found.")
    if server.status != "verified":
        raise ServerNotEligible("Server must be verified before adding projects.")
    if server.sudo_user_name is None:
        raise ServerNotEligible("Server must have a sudo user; run hardening first.")
    if not server.base_packages_installed:
        raise ServerNotEligible(
            "Base packages are not installed; run hardening first."
        )

    existing = await db.scalar(
        select(Project.id).where(
            Project.server_id == server.id,
            Project.github_repo_id == github_repo_id_from_client,
        )
    )
    if existing is not None:
        raise DuplicateProject()

    slug = await _unique_slug(db, current_user.id, name_from_client)
    repo_name = github_repo_full_name_from_client.split("/", 1)[1]
    clone_path = f"/home/{server.sudo_user_name}/{repo_name}"
    quoted_clone_path = shlex.quote(clone_path)

    conn = await ssh.get_connection(
        server, current_user.id, session_id, redis, db, key_provider
    )

    # The base_packages_installed flag says git was installed once; verify the
    # runtime truth since the box could have changed since hardening.
    result = await _run(conn, "command -v git >/dev/null && echo yes || echo no")
    if (result.stdout or "").strip() != "yes":
        raise ServerNotEligible("git is not installed; run hardening first.")

    result = await _run(
        conn, f"test -d {quoted_clone_path} && echo exists || echo missing"
    )
    if (result.stdout or "").strip() == "exists":
        raise ClonePathOccupied()

    # -- 2. GitHub OAuth token (fetched fresh, never cached) -----------------
    token = await get_github_oauth_token(clerk, current_user.clerk_user_id)

    # -- 3. Deploy keypair ----------------------------------------------------
    private_key_bytes, public_key_text, fingerprint = generate_deploy_keypair()
    encrypted_private_key = await key_provider.encrypt(private_key_bytes)

    # -- 4. Project row (committed only if everything below succeeds) --------
    project = Project(
        user_id=current_user.id,
        server_id=server.id,
        name=name_from_client,
        slug=slug,
        github_repo_full_name=github_repo_full_name_from_client,
        github_repo_id=github_repo_id_from_client,
        clone_path=clone_path,
    )
    db.add(project)
    await db.flush()

    state = _ExternalState()
    try:
        # -- 5. Register the deploy key with GitHub --------------------------
        state.github_key_id = await github.add_deploy_key(
            token,
            github_repo_full_name_from_client,
            f"Abstract: {name_from_client}",
            public_key_text,
            read_only=True,
        )

        # -- 6. Deploy key row ------------------------------------------------
        db.add(
            ProjectDeployKey(
                project_id=project.id,
                github_deploy_key_id=state.github_key_id,
                deploy_key_public_key=public_key_text,
                encrypted_deploy_key_private_key=encrypted_private_key,
                deploy_key_fingerprint=fingerprint,
                key_type="ssh-ed25519",
                encryption_key_id=key_provider.key_id,
            )
        )
        await db.flush()

        # -- 7. Private key onto the VPS (SFTP, never echo/heredoc: key bytes
        # must not pass through shell quoting) --------------------------------
        await _run(conn, "mkdir -p ~/.ssh && chmod 700 ~/.ssh")
        key_file = f".ssh/{slug}-deploy"
        sftp = await conn.start_sftp_client()
        try:
            async with sftp.open(key_file, "wb") as f:
                await f.write(private_key_bytes)
        finally:
            sftp.exit()
        state.key_file_written = True
        await _run(conn, f"chmod 600 ~/{key_file}")

        # -- 8. Per-project Host alias in ~/.ssh/config ----------------------
        block = _ssh_config_block(slug)
        await _run(
            conn,
            "touch ~/.ssh/config && "
            f"grep -qF {shlex.quote(f'Host github-{slug}')} ~/.ssh/config || "
            f"printf '%s' {shlex.quote(block)} >> ~/.ssh/config",
        )
        state.config_block_written = True
        await _run(conn, "chmod 600 ~/.ssh/config")

        # -- 9. Clone ----------------------------------------------------------
        state.clone_started = True
        clone_url = f"git@github-{slug}:{github_repo_full_name_from_client}.git"
        result = await _run(
            conn,
            f"git clone {shlex.quote(clone_url)} {quoted_clone_path}",
            timeout=_TIMEOUT_CLONE,
        )
        clone_output = f"{result.stdout or ''}{result.stderr or ''}".rstrip()
        if result.exit_status not in (0, None):
            raise CloneVerificationFailed(clone_output)

        # -- 10. Verify --------------------------------------------------------
        result = await _run(
            conn, f"test -d {quoted_clone_path}/.git && echo yes || echo no"
        )
        if (result.stdout or "").strip() != "yes":
            raise CloneVerificationFailed(clone_output)

        # -- 11. Mark cloned ---------------------------------------------------
        now = datetime.now(timezone.utc)
        project.cloned_at = now
        project.updated_at = now
    except Exception:
        await _cleanup_external_state(
            conn,
            github,
            token,
            github_repo_full_name_from_client,
            slug,
            quoted_clone_path,
            state,
        )
        raise

    return project, fingerprint


async def _cleanup_external_state(
    conn: asyncssh.SSHClientConnection,
    github: GithubService,
    token: str,
    repo_full_name: str,
    slug: str,
    quoted_clone_path: str,
    state: _ExternalState,
) -> None:
    """Best-effort undo of external side effects after a failed provision.

    Each step is individually wrapped: a cleanup failure is logged and
    swallowed, never raised, so the original provisioning error always
    propagates. The DB rollback (owned by the caller) is the real guarantee;
    this only tidies GitHub and the VPS so a retry starts clean.
    """
    if state.github_key_id is not None:
        try:
            await github.delete_deploy_key(token, repo_full_name, state.github_key_id)
        except Exception as exc:
            logger.warning(
                "Cleanup failed: could not delete GitHub deploy key {} on {}: {}",
                state.github_key_id,
                repo_full_name,
                exc,
            )

    if state.key_file_written:
        try:
            await _run(conn, f"rm -f ~/.ssh/{slug}-deploy")
        except Exception as exc:
            logger.warning(
                "Cleanup failed: could not remove deploy key file for {}: {}",
                slug,
                exc,
            )

    if state.config_block_written:
        try:
            # Drop the block from "Host github-{slug}" up to (not including) the
            # next Host line. Idempotent: no-op when the block is absent.
            await _run(
                conn,
                "test -f ~/.ssh/config && "
                f"awk -v host='Host github-{slug}' "
                "'$0 == host {skip=1; next} skip && /^Host / {skip=0} !skip' "
                "~/.ssh/config > ~/.ssh/config.tmp && "
                "mv ~/.ssh/config.tmp ~/.ssh/config && chmod 600 ~/.ssh/config",
            )
        except Exception as exc:
            logger.warning(
                "Cleanup failed: could not remove ssh config block for {}: {}",
                slug,
                exc,
            )

    if state.clone_started:
        try:
            await _run(conn, f"rm -rf {quoted_clone_path}", timeout=_TIMEOUT_CLONE)
        except Exception as exc:
            logger.warning(
                "Cleanup failed: could not remove clone directory {}: {}",
                quoted_clone_path,
                exc,
            )
