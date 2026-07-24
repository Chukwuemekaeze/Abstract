"""Unified password-based re-registration engine.

When a user rebuilds their VPS the box comes back as bare Ubuntu: a new SSH host key,
no Abstract deploy key, no hardening, and sometimes a provider temporary root password
the OS forces to be changed on first login (PAM expired password). This module drives
recovery using only the user's password. SSH keys stay the internal mechanism but are
never surfaced.

Design notes:
  - Every SSH interaction goes through the module-level ``asyncssh`` so tests can
    substitute a fake connection layer.
  - Nothing here commits. The route owns the transaction and commits at each persisted
    state transition, which is what makes a retried /complete resumable and the
    bootstrap-password write-ahead durable.
  - The engine branches on server behavior, never on a user choice. The user only ever
    supplies a password.
  - Failures map to structured error codes (see ReregistrationError). Credentials and
    raw session transcripts are never logged.
"""

from __future__ import annotations

import asyncio
import secrets
import string
from dataclasses import dataclass
from uuid import UUID

import asyncssh
import redis.asyncio as aioredis
from clerk_backend_api import Clerk
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_config import logger
from app.models import AppSshKey, Project, ProjectDeployKey, Server, User
from app.services.app_key_service import create_key_for_server
from app.services.clerk_oauth import get_github_oauth_token
from app.services.github_service import GithubService
from app.services.key_provider import KeyProvider
from app.services.ssh_service import (
    ProbeError,
    SSHService,
    _append_authorized_key,
    _known_hosts_from,
)

# Per-prompt read timeout while driving an interactive change, separate from the
# overall operation deadline that bounds the whole exchange.
_PROMPT_TIMEOUT = 15.0
_OVERALL_DEADLINE = 120.0
# Total exchange attempts (fresh connection each) before giving up as locked out.
_MAX_EXCHANGE_ATTEMPTS = 3

# The rebuilt box always authenticates as root; a sudo user only exists after Quick
# Harden, which a rebuild wiped.
_ROOT = "root"

# Shell/passwd signatures. Matched case-insensitively against accumulated output.
_EXPIRED_SIGNATURES = (
    "password has expired",
    "password change required",
    "you are required to change your password",
)
_CHANGE_SUCCESS = ("successfully", "password updated", "password changed")
_CHANGE_FAILURE = (
    "authentication token manipulation error",
    "bad password",
    "password unchanged",
    "too short",
    "too simple",
)


class ReregistrationError(Exception):
    """A re-registration failure mapped to a structured, user-facing error code.

    The message never mentions SSH keys, PAM, or keyboard-interactive. ``retryable``
    marks failures the client may retry with backoff (transient reachability), as
    opposed to failures that need the user to act at their provider first.
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(f"{code}: {message}")


def _host_key_changed_again() -> ReregistrationError:
    return ReregistrationError(
        "HOST_KEY_CHANGED_AGAIN",
        "The server's identity changed again during setup. Start over and re-check "
        "the fingerprint.",
    )


def _auth_failed() -> ReregistrationError:
    return ReregistrationError(
        "AUTH_FAILED",
        "That password did not work. Double-check the password from your provider "
        "and try again.",
    )


def _password_auth_unavailable() -> ReregistrationError:
    return ReregistrationError(
        "PASSWORD_AUTH_UNAVAILABLE",
        "This server does not accept password logins. Reset the root password from "
        "your provider's control panel, then try again.",
    )


def _change_incomplete() -> ReregistrationError:
    return ReregistrationError(
        "CHANGE_INCOMPLETE",
        "Your provider required a password reset and it could not be completed "
        "automatically. Reset the password from your provider's control panel and "
        "try again.",
    )


def _locked_out() -> ReregistrationError:
    return ReregistrationError(
        "LOCKED_OUT",
        "Abstract could not complete the login. Reset the root password from your "
        "provider's control panel and try again.",
    )


def _network_unreachable() -> ReregistrationError:
    return ReregistrationError(
        "NETWORK_UNREACHABLE",
        "Could not reach the server. Check that it is powered on and try again.",
        retryable=True,
    )


def classify_prompt(text: str) -> str:
    """Map a password prompt to which credential answers it, case-insensitive and
    never dependent on prompt order.

    Returns "current" (answer with the user-supplied password), "new", or "retype"
    (both answered with the generated password). "retype"/"re-enter"/"again" is checked
    before "new" so "Retype new password:" classifies as a confirmation, and "new"
    before the bare-password fallback so "New password:" is not mistaken for the
    current one.
    """
    lowered = text.lower()
    if (
        "retype" in lowered
        or "re-enter" in lowered
        or "reenter" in lowered
        or "again" in lowered
    ):
        return "retype"
    if "new" in lowered:
        return "new"
    return "current"


def generate_bootstrap_password(length: int = 32) -> str:
    """Cryptographically random password with at least one upper, lower, digit, and
    symbol, so it satisfies typical PAM complexity rules on the first try."""
    upper = string.ascii_uppercase
    lower = string.ascii_lowercase
    digits = string.digits
    symbols = "!@#$%^&*()-_=+[]{}"
    alphabet = upper + lower + digits + symbols
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c in upper for c in pw)
            and any(c in lower for c in pw)
            and any(c in digits for c in pw)
            and any(c in symbols for c in pw)
        ):
            return pw


class _ForcedChangeClient(asyncssh.SSHClient):
    """Answers a forced password change during authentication with no TTY.

    ``password_offered`` records whether the server actually offered a password-based
    method (plain password or keyboard-interactive). If authentication fails and this
    stayed False, the server does not accept password logins at all. ``change_requested``
    records whether a change was driven during auth (surface 1), so a connection that
    drops immediately afterward can be treated as a probable success.
    """

    def __init__(self, user_password: str, generated_password: str) -> None:
        self._user = user_password
        self._generated = generated_password
        self.password_offered = False
        self.change_requested = False

    def password_auth_requested(self) -> str:
        self.password_offered = True
        return self._user

    def password_change_requested(self, prompt: str, lang: str) -> tuple[str, str]:
        self.change_requested = True
        return self._user, self._generated

    def password_changed(self) -> None:
        logger.info("Forced password change completed during authentication.")

    def password_change_failed(self) -> None:
        logger.warning("Server rejected the generated password during authentication.")

    def kbdint_auth_requested(self) -> str:
        self.password_offered = True
        return ""

    def kbdint_challenge_received(
        self,
        name: str,
        instructions: str,
        lang: str,
        prompts: list[tuple[str, bool]],
    ) -> list[str]:
        responses: list[str] = []
        for prompt, _echo in prompts:
            kind = classify_prompt(prompt)
            if kind == "current":
                responses.append(self._user)
            else:
                self.change_requested = True
                responses.append(self._generated)
        return responses


@dataclass
class AccessResult:
    # The password that ended up working: the user's on a clean login, the generated
    # one after a forced change.
    working_password: str
    changed: bool


def _looks_like_shell_prompt(tail: str) -> bool:
    """True when the PTY tail is a ready interactive shell prompt (e.g. root@host:~#)
    rather than a passwd prompt, meaning no change is being forced."""
    stripped = tail.rstrip()
    return stripped.endswith(("#", "$")) and (":~" in stripped or "@" in stripped)


def _classify_pty_tail(tail: str) -> str | None:
    """Classify the tail of accumulated PTY output. Returns "current"/"new"/"retype"
    for an actionable prompt, "shell" for a ready prompt, or None when there is no
    prompt to answer yet (keep reading)."""
    if _looks_like_shell_prompt(tail):
        return "shell"
    if (
        "retype" in tail
        or "re-enter" in tail
        or "reenter" in tail
        or "again" in tail
    ):
        return "retype"
    if "new" in tail and "password" in tail:
        return "new"
    if "password" in tail and "@" not in tail:
        return "current"
    return None


def _is_expired(text: str) -> bool:
    lowered = text.lower()
    return any(sig in lowered for sig in _EXPIRED_SIGNATURES)


async def _drive_pty_change(
    conn: asyncssh.SSHClientConnection,
    user_password: str,
    generated_password: str,
) -> bool:
    """Drive a login-shell forced change over a PTY (surface 2), feeding each prompt
    through the classifier: current -> user password, new/retype -> generated.

    Returns True if the change was driven to completion (including a disconnect right
    after the retype was answered, which many sshd builds do on success). Raises
    CHANGE_INCOMPLETE if the server actively rejects the new password.
    """
    process = await conn.create_process(term_type="xterm")
    accumulated = ""
    answered_retype = False
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(
                    process.stdout.read(1024), timeout=_PROMPT_TIMEOUT
                )
            except asyncio.TimeoutError:
                break
            if chunk == "":
                break  # EOF: shell closed.
            accumulated += chunk
            lowered = accumulated.lower()
            if any(sig in lowered for sig in _CHANGE_FAILURE):
                raise _change_incomplete()
            if any(sig in lowered for sig in _CHANGE_SUCCESS):
                return True
            tail = lowered.rsplit("\n", 1)[-1]
            kind = _classify_pty_tail(tail)
            if kind == "current":
                process.stdin.write(user_password + "\n")
                accumulated = ""
            elif kind == "new":
                process.stdin.write(generated_password + "\n")
                accumulated = ""
            elif kind == "retype":
                process.stdin.write(generated_password + "\n")
                accumulated = ""
                answered_retype = True
            elif kind == "shell":
                break
    except (asyncssh.ConnectionLost, BrokenPipeError, ConnectionResetError):
        # Dropped mid-conversation. If we had already answered the retype, the change
        # very likely landed; let verification be the judge.
        if answered_retype:
            return True
        raise _change_incomplete()
    finally:
        process.close()

    # EOF or timeout after answering the retype is a probable success on many builds.
    return answered_retype


async def _open_access(
    server: Server, user_password: str, generated_password: str
) -> AccessResult:
    """Open a password session, handling a forced change on either surface.

    Branch A (clean login) returns the user password. Branch B (a demanded change,
    whether during keyboard-interactive auth or in the login shell) drives it and
    returns the generated password. Raises the mapped error for an unavailable method,
    a rejected password, or an unreachable host.
    """
    known_hosts = _known_hosts_from(server.host, server.pending_host_key)
    client = _ForcedChangeClient(user_password, generated_password)
    try:
        # No password= kwarg on purpose: when it is set asyncssh authenticates without
        # calling password_auth_requested, which would leave password_offered False even
        # when the server offered the method. Supplying the credential only through the
        # client hooks keeps the offered/denied distinction reliable.
        conn = await asyncssh.connect(
            host=server.host,
            port=server.port,
            username=_ROOT,
            client_factory=lambda: client,
            kbdint_auth=True,
            client_keys=None,
            known_hosts=known_hosts,
        )
    except asyncssh.PermissionDenied as exc:
        # The server offered a password method but rejected the credential, versus never
        # offering one at all (publickey-only). The client flag tells them apart.
        if client.password_offered:
            raise _auth_failed() from exc
        raise _password_auth_unavailable() from exc
    except (asyncssh.ConnectionLost, ConnectionResetError) as exc:
        # A drop right after answering the change is probable success; otherwise the box
        # was not reachable enough to finish.
        if client.change_requested:
            return AccessResult(generated_password, changed=True)
        raise _network_unreachable() from exc
    except (OSError, asyncssh.Error) as exc:
        raise _network_unreachable() from exc

    async with conn:
        # Surface 1: the change already completed during auth.
        if client.change_requested:
            return AccessResult(generated_password, changed=True)
        # Surface 2: auth succeeded on the old password but the login shell forces the
        # change. Detect it by running a command and inspecting the output.
        result = await conn.run("whoami", check=False)
        output = (result.stdout or "") + (result.stderr or "")
        if _is_expired(output) or (
            result.exit_status not in (0, None) and (result.stdout or "").strip() == ""
        ):
            changed = await _drive_pty_change(conn, user_password, generated_password)
            if not changed:
                # The expiry signal did not lead to a real change prompt; a fresh
                # verification decides whether the account is usable.
                return AccessResult(generated_password, changed=True)
            return AccessResult(generated_password, changed=True)
        # Clean login, no change.
        return AccessResult(user_password, changed=False)


async def _verify_password(server: Server, password: str) -> bool:
    """Open a fresh password connection pinned to the pending host key and confirm a
    command runs as root. Verification is the only definition of success."""
    known_hosts = _known_hosts_from(server.host, server.pending_host_key)
    try:
        async with asyncssh.connect(
            host=server.host,
            port=server.port,
            username=_ROOT,
            password=password,
            client_keys=None,
            known_hosts=known_hosts,
        ) as conn:
            result = await conn.run("whoami", check=False)
            return (result.stdout or "").strip() == _ROOT
    except (OSError, asyncssh.Error):
        return False


async def verify_password_for_resume(server: Server, password: str) -> bool:
    """Preflight (b): a bootstrap password persisted by a prior attempt still logs in,
    meaning that attempt's forced change took. True means skip straight to post-access
    using it."""
    return await _verify_password(server, password)


async def recheck_pending_host_key(server: Server, ssh: SSHService) -> None:
    """Re-probe and confirm the host still presents the pending host key captured at
    probe time (a cheap MITM-swap check). Raises HOST_KEY_CHANGED_AGAIN or
    NETWORK_UNREACHABLE."""
    try:
        current = await ssh.probe(server.host, server.port, _ROOT)
    except ProbeError as exc:
        raise _network_unreachable() from exc
    if current.host_key != server.pending_host_key:
        raise _host_key_changed_again()


async def try_resume_with_pending_key(server: Server, app_private_key: bytes) -> bool:
    """Preflight (a): a pending keypair from a prior attempt authenticates cleanly, so
    the earlier attempt got far enough to install it. True means skip to post-access."""
    known_hosts = _known_hosts_from(server.host, server.pending_host_key)
    try:
        async with asyncssh.connect(
            host=server.host,
            port=server.port,
            username=_ROOT,
            client_keys=[asyncssh.import_private_key(app_private_key)],
            known_hosts=known_hosts,
        ) as conn:
            result = await conn.run("whoami", check=False)
            return (result.stdout or "").strip() == _ROOT
    except (OSError, asyncssh.Error):
        return False


async def run_exchange_and_verify(
    server: Server, user_password: str, generated_password: str
) -> str:
    """Access engine steps 1 and 2. Open access, then verify (the only success signal).
    Up to 3 total attempts with a fresh connection each, bounded by the overall
    deadline. Returns the working password. Raises the mapped error on failure."""

    async def _attempt_loop() -> str:
        last_error: ReregistrationError | None = None
        for _attempt in range(_MAX_EXCHANGE_ATTEMPTS):
            result = await _open_access(server, user_password, generated_password)
            if not result.changed:
                # Branch A: the user password must still authenticate a fresh session.
                if await _verify_password(server, user_password):
                    return user_password
                last_error = _locked_out()
                continue
            # Branch B: the generated password is the new credential.
            if await _verify_password(server, generated_password):
                return generated_password
            # It did not take. If the old password still works, retry the exchange;
            # if neither works, the account is locked out.
            if await _verify_password(server, user_password):
                last_error = _change_incomplete()
                continue
            raise _locked_out()
        raise last_error or _locked_out()

    try:
        return await asyncio.wait_for(_attempt_loop(), timeout=_OVERALL_DEADLINE)
    except asyncio.TimeoutError as exc:
        raise _change_incomplete() from exc


async def regenerate_pending_keypair(
    server: Server, db: AsyncSession, key_provider: KeyProvider
) -> AppSshKey:
    """Post-access (1): drop the stale keypair and insert a fresh ed25519 one marked
    pending (is_active False) until the key-based smoke test promotes it. Stages the
    DB writes only; the route owns the commit."""
    old = await db.scalar(select(AppSshKey).where(AppSshKey.server_id == server.id))
    if old is not None:
        await db.delete(old)
        await db.flush()
    app_key = await create_key_for_server(server, db, key_provider)
    app_key.is_active = False
    await db.flush()
    return app_key


async def install_public_key(
    server: Server, working_password: str, app_public_key: str
) -> None:
    """Post-access (2): install the new public key over the password connection,
    idempotently. Retryable on a transient failure since it changes nothing on a
    second run."""
    known_hosts = _known_hosts_from(server.host, server.pending_host_key)
    try:
        async with asyncssh.connect(
            host=server.host,
            port=server.port,
            username=_ROOT,
            password=working_password,
            client_keys=None,
            known_hosts=known_hosts,
        ) as conn:
            await _append_authorized_key(conn, app_public_key)
    except (OSError, asyncssh.Error) as exc:
        raise _network_unreachable() from exc


async def smoke_test_pending_key(server: Server, app_private_key: bytes) -> bool:
    """Post-access (3): open a key-only connection with strict checking against the
    pending host key and confirm whoami. The single gate before promotion."""
    return await try_resume_with_pending_key(server, app_private_key)


async def evict_stale_ssh_state(
    server: Server, user_id: UUID, session_id: str, redis: aioredis.Redis, ssh: SSHService
) -> None:
    """Post-access (4): drop the in-process pooled connection and every Redis-cached
    decrypted key for this server so nothing stale survives the rebuild."""
    ssh.evict_connection(user_id, server.id)
    async for key in redis.scan_iter(match=f"ssh_key:{server.id}:*"):
        await redis.delete(key)


async def purge_server_projects(
    server: Server,
    db: AsyncSession,
    clerk: Clerk,
    github: GithubService,
    current_user: User,
) -> None:
    """Post-access (6): the rebuild destroyed every clone, container, and deploy key on
    the box, so a re-registered server is a blank slate. Delete every project row on it
    (DB ondelete=CASCADE clears runs, env files/vars, and deploy-key rows) so the user
    re-creates projects from scratch, exactly like a newly added server.

    GitHub deploy keys are revoked best-effort: the repos still exist, but a failure
    there (GitHub down, no linked account) must never block recovery, so we only log it.
    A subsequent revocation of an already-gone key is a 404, which delete_deploy_key
    treats as success, keeping this idempotent on a resumed retry. Stages the deletes
    only; the route owns the commit."""
    projects = (
        await db.scalars(select(Project).where(Project.server_id == server.id))
    ).all()
    for project in projects:
        deploy_key_id = await db.scalar(
            select(ProjectDeployKey.github_deploy_key_id).where(
                ProjectDeployKey.project_id == project.id
            )
        )
        if deploy_key_id is not None:
            try:
                token = await get_github_oauth_token(clerk, current_user.clerk_user_id)
                # delete_deploy_key treats 404 (already gone) as success.
                await github.delete_deploy_key(
                    token, project.github_repo_full_name, deploy_key_id
                )
            except Exception as exc:  # best-effort: never block recovery on GitHub
                logger.warning(
                    "Re-registration: could not revoke GitHub deploy key for "
                    "project %s (%s): %s",
                    project.name,
                    project.id,
                    exc,
                )
        await db.delete(project)
