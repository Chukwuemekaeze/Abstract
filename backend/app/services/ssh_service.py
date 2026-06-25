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

import logging
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import asyncssh
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AppSshKey, Server
from app.services.key_provider import KeyProvider

logger = logging.getLogger(__name__)


# Connection pool. Maps (user_id, server_id) -> (connection, last_used_at).
_connection_pool: dict[
    tuple[UUID, UUID], tuple[asyncssh.SSHClientConnection, datetime]
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
    conn, last_used_at = entry
    idle_seconds = (_now() - last_used_at).total_seconds()
    if idle_seconds > get_settings().ssh_pool_idle_timeout_seconds:
        conn.close()
        _connection_pool.pop(key, None)


def clear_pool() -> None:
    """Close and drop all pooled connections. Used on shutdown and in tests."""
    for conn, _ in _connection_pool.values():
        conn.close()
    _connection_pool.clear()


async def _run_checked(conn: asyncssh.SSHClientConnection, command: str) -> None:
    result = await conn.run(command, check=False)
    if result.exit_status not in (0, None):
        raise KeyInstallVerificationFailed(
            f"Command failed (exit {result.exit_status}): {command}\n"
            f"{(result.stderr or '').strip()}"
        )


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

    async def install_key(
        self,
        server: Server,
        password_from_client: str,
        app_public_key: str,
        app_private_key: bytes,
        disable_password_auth: bool,
    ) -> None:
        # Re-probe and confirm the host key has not changed since registration.
        current = await self.probe(server.host, server.port, server.username)
        if current.host_key != server.host_key:
            raise HostKeyChangedDuringInstall(
                "Host key changed since this server was registered. Aborting install."
            )

        known_hosts = _known_hosts_for(server)
        quoted_pubkey = shlex.quote(app_public_key.strip())

        # Password authenticated session to install the key. Strict host key check.
        async with asyncssh.connect(
            host=server.host,
            port=server.port,
            username=server.username,
            password=password_from_client,
            client_keys=None,
            known_hosts=known_hosts,
        ) as conn:
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
                # Replace any existing (possibly commented) PasswordAuthentication
                # yes|no line, then ensure the directive is present. The regex only
                # matches yes|no values so directives like PasswordAuthenticationMethods
                # are left untouched. -i.bak keeps a recovery copy at sshd_config.bak.
                sed_cmd = (
                    "sed -ri.bak "
                    "'s/^[#[:space:]]*PasswordAuthentication[[:space:]]+(yes|no)"
                    "[[:space:]]*.*$/PasswordAuthentication no/' /etc/ssh/sshd_config"
                )
                ensure_cmd = (
                    "grep -qE '^PasswordAuthentication no' /etc/ssh/sshd_config || "
                    "echo 'PasswordAuthentication no' >> /etc/ssh/sshd_config"
                )
                await _run_checked(conn, sed_cmd)
                await _run_checked(conn, ensure_cmd)

                # Reload sshd. Try the common service names in order. The final
                # branch exits non zero with a clear marker so _run_checked raises
                # instead of surfacing a misleading "command not found".
                reload_cmd = (
                    "systemctl reload sshd 2>/dev/null || "
                    "systemctl reload ssh 2>/dev/null || "
                    "service ssh reload 2>/dev/null || "
                    "service sshd reload 2>/dev/null || "
                    "(echo 'no_reload_method_succeeded' >&2; exit 1)"
                )
                await _run_checked(conn, reload_cmd)

                # The file edit and reload can both report success while the running
                # daemon still accepts passwords. Read the effective runtime config
                # via sshd -T and fail loudly if it disagrees.
                verify_cmd = (
                    "sshd -T 2>/dev/null | grep -i '^passwordauthentication' || "
                    "echo 'sshd_t_unavailable'"
                )
                result = await conn.run(verify_cmd, check=False)
                output = (result.stdout or "").strip().lower()
                if output == "sshd_t_unavailable":
                    # sshd -T not supported here. The file edit succeeded but we
                    # cannot confirm runtime state, so warn rather than fail.
                    logger.warning(
                        "Could not verify sshd runtime config (sshd -T unavailable). "
                        "File edit succeeded but runtime state unconfirmed."
                    )
                elif "passwordauthentication no" not in output:
                    raise SshHardeningFailed(
                        "Edited sshd_config and reloaded sshd, but sshd is still "
                        "reporting password authentication as enabled. sshd -T "
                        f"output: {result.stdout!r}. Manual intervention required "
                        "on the server."
                    )

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
        redis: aioredis.Redis,
        db: AsyncSession,
        key_provider: KeyProvider,
    ) -> asyncssh.SSHClientConnection:
        pool_key = (user_id, server.id)
        _evict_if_stale(pool_key)
        entry = _connection_pool.get(pool_key)
        if entry is not None:
            conn, _ = entry
            _connection_pool[pool_key] = (conn, _now())
            return conn

        key_bytes = await self._load_private_key(user_id, redis, db, key_provider)
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

        _connection_pool[pool_key] = (conn, _now())
        return conn

    async def run_command(
        self,
        server: Server,
        user_id: UUID,
        command: str,
        redis: aioredis.Redis,
        db: AsyncSession,
        key_provider: KeyProvider,
    ) -> CommandResult:
        conn = await self.get_connection(server, user_id, redis, db, key_provider)
        result = await conn.run(command, check=False)
        return CommandResult(
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            exit_status=result.exit_status if result.exit_status is not None else -1,
        )

    async def _load_private_key(
        self,
        user_id: UUID,
        redis: aioredis.Redis,
        db: AsyncSession,
        key_provider: KeyProvider,
    ) -> bytes:
        # Cache marker is fixed because auth is stubbed. With real auth this would
        # be the session id.
        cache_key = f"ssh_key:{user_id}:dev-session"
        cached = await redis.get(cache_key)
        if cached is not None:
            return cached

        app_key = await db.scalar(
            select(AppSshKey)
            .where(AppSshKey.user_id == user_id)
            .order_by(AppSshKey.created_at.desc())
        )
        if app_key is None:
            raise ProbeError(
                "No app SSH key found for this user. Register a server first."
            )
        private_bytes = await key_provider.decrypt(app_key.encrypted_private_key)
        await redis.set(
            cache_key,
            private_bytes,
            ex=get_settings().ssh_key_cache_ttl_seconds,
        )
        return private_bytes


ssh_service = SSHService()
