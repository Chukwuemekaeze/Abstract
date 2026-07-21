"""SSHService: the core of this milestone.

Responsibilities:
  - probe a host to capture its SSH host key (TOFU).
  - install the app public key over a password authenticated session, then verify
    that key based login works.
  - maintain a small connection pool keyed by (user_id, server_id) for downstream
    features, loading and caching the decrypted app private key via Redis.

All methods are async. No paramiko, no thread pools.
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import asyncssh
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.logging_config import logger
from app.models import AppSshKey, Server
from app.services.key_provider import KeyProvider
from app.services.sshd_config import (
    SshdDirectiveResult,
    apply_sshd_directive,
)


# Connection pool. Maps (user_id, server_id) -> (connection, last_used_at, username).
# The username is recorded so a pooled connection whose login identity no longer
# matches the server (for example after a hardening transaction that switched to a
# sudo user was rolled back) is discarded rather than reused with the wrong identity.
_connection_pool: dict[
    tuple[UUID, UUID], tuple[asyncssh.SSHClientConnection, datetime, str]
] = {}


@dataclass
class ProbeResult:
    host_key: bytes  # OpenSSH single line public key, e.g. b"ssh-ed25519 AAAA..."
    host_key_type: str  # e.g. "ssh-ed25519"
    fingerprint_sha256: str  # OpenSSH format, e.g. "SHA256:base64nopadding"


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_status: int


class ProbeError(Exception):
    """Host unreachable, timed out, or did not present a host key."""


class HostKeyChangedDuringInstall(Exception):
    """The re-probed host key differs from the one captured at registration time."""


class KeyInstallVerificationFailed(Exception):
    """Key based login did not work after installing the app public key."""


class SshHardeningFailed(Exception):
    """sshd hardening (disabling password auth) could not be confirmed."""


class HostKeyMismatch(Exception):
    """The host presented a key that does not match the verified host key on record."""


class PasswordChangeRequired(Exception):
    """The server forces a password change on login (an expired password, as freshly
    rebuilt DigitalOcean droplets ship with) and no new password was supplied. The
    caller collects a new password from the user and retries install_key with it."""


# The PAM/sshd signature for an account whose password must be changed before any
# command will run. Rebuilt DO droplets arrive with the root password pre-expired
# ("chage -d 0"), so the first command over a plain password session fails with this.
# Kept specific: a bare "new password" also appears in unrelated shell output and would
# false-positive us into the interactive-change path on a perfectly healthy box.
_PASSWORD_EXPIRED_SIGNATURES = (
    "password has expired",
    "password change required",
    "you are required to change your password",
)


def _is_password_expired(text: str) -> bool:
    lowered = text.lower()
    return any(sig in lowered for sig in _PASSWORD_EXPIRED_SIGNATURES)


class _PasswordChangeClient(asyncssh.SSHClient):
    """Answers the forced password change OpenSSH runs when an account's password is
    expired, so the change completes during authentication with no TTY. OpenSSH exposes
    this in two ways depending on the server's PAM/auth config, and we answer both:

      - the plain *password* method: the server sends PASSWD_CHANGEREQ and asyncssh calls
        password_change_requested(), to which we return (old, new).
      - keyboard-interactive (PAM): the chauthtok arrives as challenge prompts; the login
        / "(current) UNIX password:" prompt is answered with the old password, the
        "New password:" / "Retype new password:" prompts with the new one.

    A non-expired account just sees a single "Password:" challenge answered with the old
    password (no change). If neither auth-time mechanism fires, the server defers the
    change to a login shell; SSHService drives that over a PTY separately."""

    def __init__(self, old_password: str, new_password: str):
        self._old = old_password
        self._new = new_password

    def password_change_requested(
        self, prompt: str, lang: str
    ) -> tuple[str, str]:
        # Plain password method: the account's password is expired. Return the old and
        # new passwords so asyncssh performs the change during authentication.
        return self._old, self._new

    def password_changed(self) -> None:
        logger.info("Expired VPS password changed during authentication.")

    def password_change_failed(self) -> None:
        logger.warning("Server rejected the new password during authentication.")

    def kbdint_auth_requested(self) -> str:
        # Empty string: let the server pick the PAM submethods.
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
            p = prompt.strip().lower()
            if "new" in p or "retype" in p or "again" in p:
                responses.append(self._new)
            else:
                responses.append(self._old)
        return responses


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _known_hosts_for(server: Server) -> asyncssh.SSHKnownHosts:
    """Build a strict known_hosts object from the server's stored host key."""
    if not server.host_key:
        raise HostKeyMismatch("No verified host key on record for this server.")
    host_key_line = f"{server.host} {server.host_key.decode('utf-8')}"
    return asyncssh.import_known_hosts(host_key_line)


def _evict_if_stale(key: tuple[UUID, UUID]) -> None:
    entry = _connection_pool.get(key)
    if entry is None:
        return
    conn, last_used_at, _username = entry
    idle_seconds = (_now() - last_used_at).total_seconds()
    if idle_seconds > get_settings().ssh_pool_idle_timeout_seconds:
        conn.close()
        _connection_pool.pop(key, None)


def clear_pool() -> None:
    """Close and drop all pooled connections. Used on shutdown and in tests."""
    for conn, _last_used, _username in _connection_pool.values():
        conn.close()
    _connection_pool.clear()


async def _run_checked(conn: asyncssh.SSHClientConnection, command: str):
    result = await conn.run(command, check=False)
    if result.exit_status not in (0, None):
        raise KeyInstallVerificationFailed(
            f"Command failed (exit {result.exit_status}): {command}\n"
            f"{(result.stderr or '').strip()}"
        )
    return result


# Success / failure markers a login-shell chauthtok prints once the passwd conversation
# finishes. Matched case-insensitively against the accumulated PTY output.
_PASSWORD_CHANGE_SUCCESS = ("successfully", "password updated", "password changed")
_PASSWORD_CHANGE_FAILURE = (
    "authentication token manipulation error",
    "bad password",
    "password unchanged",
    "too short",
    "too simple",
)
# Seconds to wait for each prompt/result while driving the interactive change.
_PTY_PROMPT_TIMEOUT = 20.0


def _looks_like_shell_prompt(tail: str) -> bool:
    """True when the PTY tail is a ready interactive shell prompt rather than a passwd
    prompt: e.g. "root@ubuntu-...:~#" or "user@host:~$". If we see this the account is
    NOT being forced to change its password, so there is nothing to drive."""
    stripped = tail.rstrip()
    return stripped.endswith(("#", "$")) and (":~" in stripped or "@" in stripped)


async def _change_expired_password_interactively(
    conn: asyncssh.SSHClientConnection, old_password: str, new_password: str
) -> bool:
    """Drive a login-shell `passwd`-style forced change over a PTY.

    Some servers (freshly rebuilt DigitalOcean droplets among them) do not perform the
    expired-password chauthtok during authentication; they authenticate the old password
    and then force the change in the login shell, re-prompting for the *current* password
    before asking for the new one twice. asyncssh's auth-time hooks never see this, so we
    open a real PTY and answer the prompts ourselves: current -> old, new/retype -> new.

    Returns True if it drove the change to completion, or False if the box dropped us
    straight at a ready shell prompt (no change was actually required — the earlier
    expiry signal was a false positive). Raises KeyInstallVerificationFailed only when
    the server actively rejects the new password (too weak, wrong current password).
    """
    process = await conn.create_process(term_type="xterm")
    accumulated = ""
    changed = False
    # A single prompt may need more than one write (current, new, retype), so keep
    # reading until we see a success/failure marker, a shell prompt, or run out of time.
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(
                    process.stdout.read(1024), timeout=_PTY_PROMPT_TIMEOUT
                )
            except asyncio.TimeoutError:
                # Nothing more is coming. If we never saw a passwd prompt, treat it as
                # "no change required" rather than hanging; the caller re-raises the real
                # underlying error. If we did drive a change, the success check below
                # decides the outcome.
                break
            if chunk == "":
                break  # EOF: shell closed. Fall through to the outcome check below.
            accumulated += chunk
            lowered = accumulated.lower()

            if any(sig in lowered for sig in _PASSWORD_CHANGE_FAILURE):
                raise KeyInstallVerificationFailed(
                    "The server rejected the new password (it may be too weak, or the "
                    "current password is wrong). Choose a different password and retry.\n"
                    f"{accumulated.strip()[-500:]}"
                )
            if any(sig in lowered for sig in _PASSWORD_CHANGE_SUCCESS):
                changed = True
                break

            # Answer whichever prompt the tail of the output is showing. Reset the buffer
            # after each write so the same prompt is not answered twice.
            tail = lowered.rsplit("\n", 1)[-1]
            if "new password" in tail:
                process.stdin.write(new_password + "\n")
                accumulated = ""
            elif "retype" in tail or "again" in tail or "re-enter" in tail:
                process.stdin.write(new_password + "\n")
                accumulated = ""
            elif "unix password" in tail or (
                "password" in tail and "@" not in tail
            ):  # "(current) UNIX password:" or bare "Password:"
                process.stdin.write(old_password + "\n")
                accumulated = ""
            elif _looks_like_shell_prompt(tail):
                # Ready shell, no change being forced. Nothing to do here.
                break
    finally:
        process.stdin.write_eof()
        process.close()

    return changed


class SSHService:
    async def probe(self, host: str, port: int, username: str) -> ProbeResult:
        """Capture the host key during the key exchange, without authenticating.

        asyncssh.get_server_host_key performs the version and key exchange and
        returns the server host key without attempting authentication, which is
        exactly the TOFU probe we want. TimeoutError is a subclass of OSError so
        the two except clauses below cover unreachable and timeout cases.
        """
        try:
            key = await asyncssh.get_server_host_key(host, port=port)
        except (OSError, asyncssh.Error) as exc:
            raise ProbeError(f"Could not reach {host}:{port}: {exc}") from exc

        if key is None:
            raise ProbeError(f"{host}:{port} did not present a host key.")

        return ProbeResult(
            host_key=key.export_public_key(),
            host_key_type=key.get_algorithm(),
            fingerprint_sha256=key.get_fingerprint(),
        )

    async def _write_key_and_harden(
        self,
        conn: asyncssh.SSHClientConnection,
        server: Server,
        app_public_key: str,
        disable_password_auth: bool,
    ) -> None:
        """Append the app public key idempotently and, if requested, disable password
        auth. Shared by both password-session paths (normal and forced-change)."""
        quoted_pubkey = shlex.quote(app_public_key.strip())

        await _run_checked(conn, "mkdir -p ~/.ssh && chmod 700 ~/.ssh")
        # Idempotent append: only add the key if it is not already present.
        append_cmd = (
            "touch ~/.ssh/authorized_keys && "
            f"grep -qF {quoted_pubkey} ~/.ssh/authorized_keys || "
            f"echo {quoted_pubkey} >> ~/.ssh/authorized_keys"
        )
        await _run_checked(conn, append_cmd)
        await _run_checked(conn, "chmod 600 ~/.ssh/authorized_keys")

        if disable_password_auth:
            # Disable password auth idempotently and confirm the running daemon
            # reports it, via the shared sshd_config helper. _run_checked raises
            # KeyInstallVerificationFailed if any edit or reload command fails; a
            # runtime mismatch surfaces as SshHardeningFailed below.
            async def run(script: str):
                return await _run_checked(conn, script)

            result = await apply_sshd_directive(
                run,
                directive="PasswordAuthentication",
                value="no",
                value_alternatives="yes|no",
            )
            if result is SshdDirectiveResult.UNAVAILABLE:
                # sshd -T not supported here. The file edit succeeded but we
                # cannot confirm runtime state, so warn rather than fail.
                logger.warning(
                    "Could not verify sshd runtime config (sshd -T unavailable). "
                    "File edit succeeded but runtime state unconfirmed."
                )
            elif result is SshdDirectiveResult.MISMATCH:
                raise SshHardeningFailed(
                    "Edited sshd_config and reloaded sshd, but sshd is still "
                    "reporting password authentication as enabled. Manual "
                    "intervention required on the server."
                )

    async def install_key(
        self,
        server: Server,
        password_from_client: str,
        app_public_key: str,
        app_private_key: bytes,
        disable_password_auth: bool,
        new_password: str | None = None,
    ) -> None:
        # Re-probe and confirm the host key has not changed since registration.
        current = await self.probe(server.host, server.port, server.username)
        if current.host_key != server.host_key:
            raise HostKeyChangedDuringInstall(
                "Host key changed since this server was registered. Aborting install."
            )

        known_hosts = _known_hosts_for(server)

        if new_password is None:
            # Normal path: plain password auth. If the account's password is expired
            # (a freshly rebuilt DO droplet), the server refuses every command until a
            # change happens, so the first command fails with the expiry signature.
            # Surface that as PasswordChangeRequired so the caller can collect a new
            # password and retry through the forced-change path below.
            async with asyncssh.connect(
                host=server.host,
                port=server.port,
                username=server.username,
                password=password_from_client,
                client_keys=None,
                known_hosts=known_hosts,
            ) as conn:
                try:
                    await self._write_key_and_harden(
                        conn, server, app_public_key, disable_password_auth
                    )
                except KeyInstallVerificationFailed as exc:
                    if _is_password_expired(str(exc)):
                        raise PasswordChangeRequired(
                            "This server requires a password change on first login "
                            "(the password has expired). Provide a new password to "
                            "continue."
                        ) from exc
                    raise
        else:
            # Forced-change path. A server can force the expired-password change in one of
            # three ways, so we cover all of them:
            #   1. plain password method (PASSWD_CHANGEREQ) -> answered by
            #      _PasswordChangeClient.password_change_requested during auth.
            #   2. keyboard-interactive (PAM) chauthtok -> answered by
            #      kbdint_challenge_received during auth.
            #   3. login-shell chauthtok -> auth succeeds with the old password, then the
            #      shell forces the change; we drive it over a PTY, then reconnect with the
            #      new password to install the key on a clean session.
            # Both auth methods are left enabled so asyncssh answers whichever the server
            # negotiates; the client_factory supplies the old/new passwords to both hooks.
            try:
                async with asyncssh.connect(
                    host=server.host,
                    port=server.port,
                    username=server.username,
                    client_factory=lambda: _PasswordChangeClient(
                        password_from_client, new_password
                    ),
                    password=password_from_client,
                    kbdint_auth=True,
                    client_keys=None,
                    known_hosts=known_hosts,
                ) as conn:
                    try:
                        # Cases 1 & 2: the change already completed during auth, so this
                        # session is authenticated as the new password. Install directly.
                        await self._write_key_and_harden(
                            conn, server, app_public_key, disable_password_auth
                        )
                    except KeyInstallVerificationFailed as exc:
                        if not _is_password_expired(str(exc)):
                            raise
                        # Case 3: the change may not have been done at auth. Drive the
                        # login-shell chauthtok over a PTY. If it reports no change was
                        # actually being forced (a ready shell), the expiry signal was a
                        # false positive, so re-raise the real install failure instead of
                        # silently "succeeding". Otherwise reconnect as the now-current
                        # password and run the install once on a clean session.
                        changed = await _change_expired_password_interactively(
                            conn, password_from_client, new_password
                        )
                        if not changed:
                            raise KeyInstallVerificationFailed(
                                "The server reported an expired password but then "
                                "presented a normal shell instead of a change prompt, so "
                                "the key install could not complete. The password may "
                                "already have been changed; retry with the current "
                                "password and leave the new-password field blank."
                            ) from exc
                        async with asyncssh.connect(
                            host=server.host,
                            port=server.port,
                            username=server.username,
                            password=new_password,
                            client_keys=None,
                            known_hosts=known_hosts,
                        ) as changed_conn:
                            await self._write_key_and_harden(
                                changed_conn,
                                server,
                                app_public_key,
                                disable_password_auth,
                            )
            except asyncssh.PermissionDenied as exc:
                raise KeyInstallVerificationFailed(
                    "The server rejected the new password (it may be too weak, or the "
                    "current password is wrong). Choose a different password and retry."
                ) from exc

        # Fresh key authenticated connection to prove the install worked.
        try:
            async with asyncssh.connect(
                host=server.host,
                port=server.port,
                username=server.username,
                client_keys=[asyncssh.import_private_key(app_private_key)],
                known_hosts=known_hosts,
            ) as verify_conn:
                result = await verify_conn.run("whoami", check=False)
                whoami_from_server = (result.stdout or "").strip()
                if whoami_from_server != server.username:
                    raise KeyInstallVerificationFailed(
                        f"Expected whoami to return '{server.username}', "
                        f"got '{whoami_from_server}'."
                    )
        except KeyInstallVerificationFailed:
            raise
        except (OSError, asyncssh.Error) as exc:
            raise KeyInstallVerificationFailed(
                f"Key based login failed after install: {exc}"
            ) from exc

    async def get_connection(
        self,
        server: Server,
        user_id: UUID,
        session_id: str,
        redis: aioredis.Redis,
        db: AsyncSession,
        key_provider: KeyProvider,
    ) -> asyncssh.SSHClientConnection:
        pool_key = (user_id, server.id)
        _evict_if_stale(pool_key)
        entry = _connection_pool.get(pool_key)
        if entry is not None:
            conn, _last_used, pooled_username = entry
            if pooled_username == server.username:
                _connection_pool[pool_key] = (conn, _now(), pooled_username)
                return conn
            # Identity changed since this connection was pooled. Drop it and open a
            # fresh one as the current user.
            conn.close()
            _connection_pool.pop(pool_key, None)

        key_bytes = await self._load_private_key(
            server, session_id, redis, db, key_provider
        )
        known_hosts = _known_hosts_for(server)

        try:
            conn = await asyncssh.connect(
                host=server.host,
                port=server.port,
                username=server.username,
                client_keys=[asyncssh.import_private_key(key_bytes)],
                known_hosts=known_hosts,
            )
        except asyncssh.HostKeyNotVerifiable as exc:
            server.status = "key_mismatch"
            await db.commit()
            raise HostKeyMismatch(
                f"{server.host} presented a host key that does not match the "
                "verified key on record."
            ) from exc

        _connection_pool[pool_key] = (conn, _now(), server.username)
        return conn

    def evict_connection(self, user_id: UUID, server_id: UUID) -> None:
        """Close and drop a pooled connection. No-op if none is present.

        Used after operations that change how we authenticate to a server (for
        example switching from root to a sudo user) so the next get_connection opens
        a fresh session reflecting the new identity.
        """
        entry = _connection_pool.pop((user_id, server_id), None)
        if entry is not None:
            conn, _last_used, _username = entry
            conn.close()

    async def ping(
        self,
        server: Server,
        user_id: UUID,
        session_id: str,
        redis: aioredis.Redis,
        db: AsyncSession,
        key_provider: KeyProvider,
    ) -> bool:
        """Open a fresh (never pooled) connection and run a trivial command.

        Used to poll a server after a reboot. Deliberately bypasses the pool because
        a pooled entry can be stale (pointing at a connection the box dropped while
        rebooting). Returns True only if a brand new connection succeeds.
        """
        try:
            key_bytes = await self._load_private_key(
                server, session_id, redis, db, key_provider
            )
            known_hosts = _known_hosts_for(server)
            async with asyncssh.connect(
                host=server.host,
                port=server.port,
                username=server.username,
                client_keys=[asyncssh.import_private_key(key_bytes)],
                known_hosts=known_hosts,
            ) as conn:
                result = await conn.run("echo ok", check=False)
                return (result.stdout or "").strip() == "ok"
        except (OSError, asyncssh.Error):
            return False

    async def run_command(
        self,
        server: Server,
        user_id: UUID,
        session_id: str,
        command: str,
        redis: aioredis.Redis,
        db: AsyncSession,
        key_provider: KeyProvider,
    ) -> CommandResult:
        conn = await self.get_connection(
            server, user_id, session_id, redis, db, key_provider
        )
        result = await conn.run(command, check=False)
        return CommandResult(
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            exit_status=result.exit_status if result.exit_status is not None else -1,
        )

    async def _load_private_key(
        self,
        server: Server,
        session_id: str,
        redis: aioredis.Redis,
        db: AsyncSession,
        key_provider: KeyProvider,
    ) -> bytes:
        # Keyed per server (each server has its own keypair) and per Clerk login.
        # Signing out and back in yields a new session id and a fresh cache entry.
        cache_key = f"ssh_key:{server.id}:{session_id}"
        cached = await redis.get(cache_key)
        if cached is not None:
            return cached

        app_key = await db.scalar(
            select(AppSshKey).where(AppSshKey.server_id == server.id)
        )
        if app_key is None:
            raise ProbeError(
                "No app SSH key found for this server. Register it first."
            )
        private_bytes = await key_provider.decrypt(app_key.encrypted_private_key)
        await redis.set(
            cache_key,
            private_bytes,
            ex=get_settings().ssh_key_cache_ttl_seconds,
        )
        return private_bytes


ssh_service = SSHService()
