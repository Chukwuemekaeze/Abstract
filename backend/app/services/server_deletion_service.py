"""Delete a server: tear down every project on it, remove Abstract from the VPS,
then hard-delete the server row.

This is the server-level counterpart to project_deletion_service. It orchestrates
per-project deletion and then undoes the parts of onboarding + hardening that gate
the user out of their own box, restoring it to the SSH access state they handed it
over in.

Transaction model (same self-managed pattern the deletion services are allowed to
use):
  * Commit A acquires the locks: a single UPDATE sets active_operation='deleting'
    on every idle project of the server (RETURNING their ids). If any project was
    already busy, the transaction is rolled back and no lock is taken, so the
    caller returns 409 without touching the VPS. Otherwise the server's own
    active_operation is set in the same transaction and committed, so concurrent
    mutations see the lock immediately.
  * All external side effects (per-project deletion, VPS commands) then run with no
    DB transaction held open.
  * Commit B either hard-deletes the server row (cascade removes the app_ssh_key
    row) on success, or, on failure, clears active_operation on the server and every
    remaining project so the user can retry.

Strict abort: any step failure aborts the whole deletion. Completed project
deletions are NOT rolled back; they stay deleted. Every shell command is
idempotent, so a retry after a partial failure lands cleanly.
"""

import shlex
from uuid import UUID

import asyncssh
import redis.asyncio as aioredis
from clerk_backend_api import Clerk
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.logging_config import logger
from app.models import Project, Server, User
from app.schemas.servers import ServerDeletionStepResult
from app.services.github_service import GithubService
from app.services.key_provider import KeyProvider
from app.services.project_deletion_service import (
    ProjectDeletionError,
    delete_project,
)
from app.services.sshd_config import SshdDirectiveResult, apply_sshd_directive
from app.services.ssh_service import SSHService

__all__ = [
    "ServerDeletionError",
    "ServerOperationInFlight",
    "delete_server",
]

_TIMEOUT_CHECK = 30

# The order projects are torn down in: running first (most state to unwind), then
# failed, then never_started, created_at ascending within each group.
_RUNTIME_ORDER = {"running": 0, "failed": 1, "never_started": 2}


class ServerOperationInFlight(Exception):
    """A project on this server is already busy, so the lock cannot be acquired.

    Carries the structured 409 detail the route returns verbatim."""

    def __init__(self, project_name: str):
        self.project_name = project_name
        self.detail = {"active_operation": f"in flight on project {project_name}"}
        super().__init__(self.detail["active_operation"])


class ServerDeletionError(Exception):
    """A deletion step failed. The server row is intact and every lock has been
    cleared so the user can retry. Carries the ordered step list plus, when the
    failure was inside the per-project loop, which project failed."""

    def __init__(
        self,
        *,
        failed_step: str,
        steps: list[ServerDeletionStepResult],
        message: str,
        failed_project_id: UUID | None = None,
        failed_project_name: str | None = None,
    ):
        self.failed_step = failed_step
        self.steps = steps
        self.message = message
        self.failed_project_id = failed_project_id
        self.failed_project_name = failed_project_name
        super().__init__(message)


class _StepFailed(Exception):
    """Internal: a single VPS step failed. Carries the step name and user detail."""

    def __init__(self, step: str, detail: str):
        self.step = step
        self.detail = detail
        super().__init__(detail)


def _output(result: asyncssh.SSHCompletedProcess) -> str:
    return f"{result.stdout or ''}{result.stderr or ''}".rstrip()


def _priv(server: Server) -> str:
    """Privilege prefix: empty as root, "sudo " as the non-root sudo user."""
    return "" if server.username == "root" else "sudo "


async def _run_step(
    conn: asyncssh.SSHClientConnection,
    step: str,
    command: str,
    timeout: int = _TIMEOUT_CHECK,
) -> asyncssh.SSHCompletedProcess:
    """Run one idempotent shell command for a step. Any transport error or nonzero
    exit raises _StepFailed so the sequence aborts."""
    try:
        result = await conn.run(command, check=False, timeout=timeout)
    except (TimeoutError, asyncssh.Error, OSError) as exc:
        raise _StepFailed(step, f"{step} did not complete: {exc}") from exc
    if result.exit_status not in (0, None):
        raise _StepFailed(step, _output(result) or f"{step} exited {result.exit_status}")
    return result


def _authorized_key_blob(public_key: str) -> str | None:
    """The base64 middle field of an OpenSSH public key line
    ("ssh-ed25519 <blob> [comment]"). Handles keys with and without a trailing
    comment. None if the line is empty."""
    parts = public_key.split()
    if len(parts) >= 2:
        return parts[1]
    if parts:
        return parts[0]
    return None


def _remove_authorized_key_command(blob: str) -> str:
    """Surgically drop the one authorized_keys line carrying this blob, never
    overwriting the whole file blindly. Runs as the connected user (no sudo) so ~
    resolves to that user's home, which is where the key was installed. `|| true`
    keeps the step green when the blob was the file's only line (grep -vF then
    selects zero lines and exits 1); the tmp file is still the correct, now empty,
    result. No-op when the file is absent or the blob is not present."""
    quoted = shlex.quote(blob)
    return (
        "AUTH=~/.ssh/authorized_keys; "
        'if [ -f "$AUTH" ]; then '
        f'{{ grep -vF {quoted} "$AUTH" || true; }} > "$AUTH.tmp" '
        '&& mv "$AUTH.tmp" "$AUTH" && chmod 600 "$AUTH"; fi'
    )


def _order_projects(projects: list[Project]) -> list[Project]:
    return sorted(
        projects,
        key=lambda p: (_RUNTIME_ORDER.get(p.runtime_status, 99), p.created_at),
    )


async def _acquire_locks(db: AsyncSession, server: Server) -> None:
    """Commit A: lock every project of the server plus the server itself, atomically.

    Raises ServerOperationInFlight (no lock taken, transaction rolled back) if any
    project was already busy."""
    all_projects = (
        await db.execute(
            select(Project.id, Project.name).where(Project.server_id == server.id)
        )
    ).all()
    locked_ids = set(
        (
            await db.execute(
                update(Project)
                .where(
                    Project.server_id == server.id,
                    Project.active_operation.is_(None),
                )
                .values(active_operation="deleting")
                .returning(Project.id)
            )
        )
        .scalars()
        .all()
    )
    busy = [name for pid, name in all_projects if pid not in locked_ids]
    if busy:
        # Discard the partial locks: nothing is committed, so no project is left
        # marked and the server lock is never set.
        await db.rollback()
        raise ServerOperationInFlight(busy[0])

    server.active_operation = "deleting"
    await db.commit()


async def _clear_locks(db: AsyncSession, server_id: UUID) -> None:
    """Failure path: drop the server lock and every remaining project lock so the
    user can retry. The failed project already cleared its own lock inside
    delete_project."""
    await db.rollback()
    await db.execute(
        update(Project)
        .where(
            Project.server_id == server_id,
            Project.active_operation.is_not(None),
        )
        .values(active_operation=None)
    )
    server = await db.get(Server, server_id)
    if server is not None:
        server.active_operation = None
    await db.commit()


async def delete_server(
    *,
    server: Server,
    current_user: User,
    session_id: str,
    db: AsyncSession,
    ssh: SSHService,
    redis: aioredis.Redis,
    key_provider: KeyProvider,
    clerk: Clerk,
    github: GithubService,
) -> list[ServerDeletionStepResult]:
    """Run the ordered teardown. Returns the step list on success; raises
    ServerDeletionError (row intact, locks cleared) on any failure, or
    ServerOperationInFlight (nothing touched) if a project is already busy."""
    server_id = server.id

    # -- Commit A: acquire the server + project locks ------------------------
    await _acquire_locks(db, server)

    # Eager load everything the loop and VPS steps need, after the lock is held so
    # no concurrent request can slip in between. The per-project deletion service
    # re-queries what it needs internally, so only projects and the app key are
    # loaded here; the SSH round trips, not the DB, are the loop's real cost.
    server = await db.scalar(
        select(Server)
        .options(selectinload(Server.app_ssh_key), selectinload(Server.projects))
        .where(Server.id == server_id)
    )
    assert server is not None  # the lock we just committed holds the row in place

    steps: list[ServerDeletionStepResult] = []
    try:
        # -- 1. per-project teardown -----------------------------------------
        for project in _order_projects(list(server.projects)):
            project_id = project.id
            project_name = project.name
            try:
                await delete_project(
                    project=project,
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
            except ProjectDeletionError as exc:
                steps.append(
                    ServerDeletionStepResult(
                        name="delete_project",
                        status="failed",
                        detail=(
                            f"Project deletion failed at step "
                            f"'{exc.failed_step}': {exc.message}"
                        ),
                        project_id=project_id,
                        project_name=project_name,
                    )
                )
                # The failed project cleared its own lock; clear the rest and abort.
                # Completed project deletions above stay deleted.
                await _clear_locks(db, server_id)
                logger.warning(
                    "Server deletion aborted: project {} ({}) failed at step {}",
                    project_name,
                    project_id,
                    exc.failed_step,
                )
                raise ServerDeletionError(
                    failed_step=exc.failed_step,
                    steps=steps,
                    message=(
                        f"Deleting project '{project_name}' failed at step "
                        f"'{exc.failed_step}'."
                    ),
                    failed_project_id=project_id,
                    failed_project_name=project_name,
                ) from exc
            steps.append(
                ServerDeletionStepResult(
                    name="delete_project",
                    status="completed",
                    project_id=project_id,
                    project_name=project_name,
                )
            )

        # -- VPS teardown: one pooled connection for the remaining shell steps.
        try:
            conn = await ssh.get_connection(
                server, current_user.id, session_id, redis, db, key_provider
            )
        except Exception as exc:
            raise _StepFailed(
                "connect_ssh", f"Could not connect to the server: {exc}"
            ) from exc

        # -- 2. restore_ssh_access -------------------------------------------
        # Re-enable password auth and root login so the user can log back in with
        # their provider root password. Reuses the same idempotent sshd_config
        # helper hardening uses to disable them, so the edit, the drop-in override,
        # the reload, and the sshd -T verification all match. sed no-ops when the
        # value is already correct. This runs while the sudo grant is still in
        # place: revoke_sudoers is deliberately last, because removing the
        # passwordless sudoers file would make these sudo commands prompt for a
        # password over a non-interactive session and fail.
        async def _sshd_run(script: str) -> asyncssh.SSHCompletedProcess:
            cmd = f"{_priv(server)}sh -c {shlex.quote(script)}"
            return await _run_step(conn, "restore_ssh_access", cmd)

        for directive, alternatives in (
            ("PasswordAuthentication", "yes|no"),
            ("PermitRootLogin", "yes|no|prohibit-password|without-password"),
        ):
            outcome = await apply_sshd_directive(
                _sshd_run,
                directive=directive,
                value="yes",
                value_alternatives=alternatives,
            )
            if outcome is SshdDirectiveResult.MISMATCH:
                raise _StepFailed(
                    "restore_ssh_access",
                    f"sshd still does not report {directive} yes after the edit.",
                )
        steps.append(
            ServerDeletionStepResult(name="restore_ssh_access", status="completed")
        )

        # -- 3. remove_authorized_key ----------------------------------------
        # Runs as the connected user (no sudo), so it is independent of the sudo
        # grant. After this runs, Abstract can no longer SSH in with this key.
        blob = (
            _authorized_key_blob(server.app_ssh_key.public_key)
            if server.app_ssh_key
            else None
        )
        if blob is None:
            steps.append(
                ServerDeletionStepResult(
                    name="remove_authorized_key",
                    status="skipped",
                    detail="No app SSH key on record for this server.",
                )
            )
        else:
            await _run_step(
                conn,
                "remove_authorized_key",
                _remove_authorized_key_command(blob),
            )
            steps.append(
                ServerDeletionStepResult(
                    name="remove_authorized_key", status="completed"
                )
            )

        # -- 4. revoke_sudoers -----------------------------------------------
        # Must be the LAST sudo-dependent step: removing the passwordless sudoers
        # file makes every later `sudo` prompt for a password, which cannot be
        # answered over a non-interactive SSH session. Hardening writes
        # /etc/sudoers.d/{sudo_user_name} (see hardening_service); we remove that
        # exact file. rm -f is idempotent. Skip cleanly when the server was
        # registered but never fully hardened (no sudo user).
        if server.sudo_user_name:
            await _run_step(
                conn,
                "revoke_sudoers",
                f"{_priv(server)}rm -f "
                f"{shlex.quote(f'/etc/sudoers.d/{server.sudo_user_name}')}",
            )
            steps.append(
                ServerDeletionStepResult(name="revoke_sudoers", status="completed")
            )
        else:
            steps.append(
                ServerDeletionStepResult(
                    name="revoke_sudoers",
                    status="skipped",
                    detail="Server was never fully hardened (no sudo user).",
                )
            )

        # -- 5. evict_ssh_connection -----------------------------------------
        ssh.evict_connection(current_user.id, server_id)
        steps.append(
            ServerDeletionStepResult(name="evict_ssh_connection", status="completed")
        )

        # -- 6. delete_server_record (Commit B, success) ---------------------
        try:
            row = await db.get(Server, server_id)
            if row is not None:
                await db.delete(row)
            await db.commit()
        except Exception as exc:
            await db.rollback()
            raise _StepFailed(
                "delete_server_record",
                f"Could not delete the server record: {exc}",
            ) from exc
        steps.append(
            ServerDeletionStepResult(name="delete_server_record", status="completed")
        )
    except _StepFailed as exc:
        steps.append(
            ServerDeletionStepResult(name=exc.step, status="failed", detail=exc.detail)
        )
        await _clear_locks(db, server_id)
        logger.warning(
            "Server deletion aborted at step {} for server {}: {}",
            exc.step,
            server_id,
            exc.detail,
        )
        raise ServerDeletionError(
            failed_step=exc.step,
            steps=steps,
            message=f"Deletion failed at step '{exc.step}'.",
        ) from exc

    return steps
