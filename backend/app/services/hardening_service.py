"""HardeningService: idempotent VPS hardening operations.

Each method runs one hardening operation over a pooled SSH connection and updates
the relevant fields on the server row using the caller's db session. Methods never
commit. The route handler owns the transaction boundary (a single terminal commit on
success, rollback on any failure), which gives database atomicity: either all field
writes for an operation land together or none do. The VPS itself may be partially
changed after a failure, so every shell command is written to be idempotent and safe
to retry.

All SSH is async via asyncssh, reusing the connection pool in ssh_service.

Privileged commands are sudo-prefixed based on the current connection identity. While
we are still root (server.username == "root") the prefix is empty. After
create_sudo_user switches server.username to the non-root sudo user, the prefix
becomes "sudo " so privileged commands keep working. Commands run as
`{priv}sh -c '<script>'` so environment assignments and redirects to root-owned files
behave correctly under sudo.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import asyncssh
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_config import logger
from app.models import Server
from app.services.key_provider import KeyProvider
from app.services.ssh_service import SSHService, _known_hosts_for, ssh_service
from app.services.sshd_config import (
    SshdDirectiveResult,
    apply_sshd_directive,
)

# Command timeouts in seconds. apt and the Docker installer are slow, the rest are
# quick. These are fixed, not user configurable.
TIMEOUT_LONG = 600  # update_system, install_docker
TIMEOUT_MED = 300  # install_base_packages
TIMEOUT_SHORT = 120  # everything else

# Swap is sized at 25% of the box's RAM, with a floor so tiny instances still get a
# usable swap file.
SWAP_FLOOR_MB = 512


class HardeningError(Exception):
    """Base class for hardening failures. Carries captured shell output for the UI."""

    def __init__(self, captured_output: str) -> None:
        super().__init__(captured_output)
        self.captured_output = captured_output


class SystemUpdateFailed(HardeningError):
    pass


class DockerInstallFailed(HardeningError):
    pass


class SudoUserVerificationFailed(HardeningError):
    pass


class RootLoginDisableFailed(HardeningError):
    pass


class PasswordAuthDisableFailed(HardeningError):
    pass


class RootLoginPrecheckFailed(HardeningError):
    """Cannot safely disable root login: no sudo user, or it failed re-verification."""


class FirewallConfigFailed(HardeningError):
    pass


class SwapConfigFailed(HardeningError):
    pass


@dataclass
class HardeningContext:
    """Everything an operation needs beyond the connection and the server row.

    user_id and session_id come from the verified Clerk token, never the client.
    app_public_key / app_private_key are the user's app managed deploy keypair,
    loaded and decrypted by the route before the operation runs.
    """

    user_id: UUID
    session_id: str
    redis: aioredis.Redis
    key_provider: KeyProvider
    app_public_key: str
    app_private_key: bytes


def _now() -> datetime:
    return datetime.now(timezone.utc)


class HardeningService:
    def __init__(self, ssh: SSHService) -> None:
        self._ssh = ssh

    # -- internal helpers ---------------------------------------------------

    @staticmethod
    def _priv(server: Server) -> str:
        """Privilege prefix: empty as root, "sudo " as the non-root sudo user."""
        return "" if server.username == "root" else "sudo "

    async def _run(
        self,
        conn: asyncssh.SSHClientConnection,
        server: Server,
        script: str,
        *,
        timeout: int,
        error_cls: type[HardeningError],
        captured: list[str],
    ) -> asyncssh.SSHCompletedProcess:
        """Run one shell script over the connection, raising error_cls on failure.

        The script is executed as `{priv}sh -c '<script>'`. Stdout and stderr are
        accumulated into `captured` so the route can surface them to the UI on
        failure. A nonzero exit, a timeout, or a transport error all raise error_cls.
        """
        cmd = f"{self._priv(server)}sh -c {shlex.quote(script)}"
        try:
            result = await conn.run(cmd, check=False, timeout=timeout)
        except (TimeoutError, OSError, asyncssh.Error) as exc:
            captured.append(f"$ {cmd}\n{exc}")
            raise error_cls("\n".join(captured)) from exc

        body = f"{result.stdout or ''}{result.stderr or ''}".rstrip()
        captured.append(f"$ {cmd}\n{body}".rstrip())
        if result.exit_status not in (0, None):
            raise error_cls("\n".join(captured))
        return result

    async def _verify_user_access(
        self, server: Server, username: str, app_private_key: bytes
    ) -> bool:
        """Open a fresh connection as `username` and confirm shell + passwordless sudo.

        Returns True only if whoami matches and `sudo -n true` exits zero. Used before
        we switch to the sudo user and before we disable root login, so we never lose
        root access without a confirmed alternative.
        """
        try:
            known_hosts = _known_hosts_for(server)
            async with asyncssh.connect(
                host=server.host,
                port=server.port,
                username=username,
                client_keys=[asyncssh.import_private_key(app_private_key)],
                known_hosts=known_hosts,
            ) as conn:
                whoami = await conn.run("whoami", check=False)
                if (whoami.stdout or "").strip() != username:
                    return False
                sudo_check = await conn.run("sudo -n true", check=False)
                return sudo_check.exit_status in (0, None)
        except (OSError, asyncssh.Error):
            return False

    async def _set_sshd_directive(
        self,
        conn: asyncssh.SSHClientConnection,
        server: Server,
        *,
        directive: str,
        value: str,
        value_alternatives: str,
        error_cls: type[HardeningError],
        captured: list[str],
    ) -> None:
        """Set an sshd_config directive idempotently and confirm it took effect.

        Delegates the command text and runtime verification to the shared
        sshd_config helper, supplying a runner that wraps each command with the sudo
        prefix and accumulates output into `captured` for error reporting. If
        `sshd -T` is unavailable the file edit is trusted with a warning rather than
        failing. Shared by disable_root_login and disable_password_auth.
        """

        async def run(script: str):
            return await self._run(
                conn,
                server,
                script,
                timeout=TIMEOUT_SHORT,
                error_cls=error_cls,
                captured=captured,
            )

        result = await apply_sshd_directive(
            run,
            directive=directive,
            value=value,
            value_alternatives=value_alternatives,
        )
        if result is SshdDirectiveResult.UNAVAILABLE:
            logger.warning(
                f"Could not verify sshd runtime config for {directive} "
                "(sshd -T unavailable). File edit succeeded but runtime state "
                "unconfirmed."
            )
        elif result is SshdDirectiveResult.MISMATCH:
            raise error_cls("\n".join(captured))

    # -- operations ---------------------------------------------------------

    async def update_system(
        self, conn: asyncssh.SSHClientConnection, server: Server, db: AsyncSession
    ) -> None:
        captured: list[str] = []
        # DEBIAN_FRONTEND=noninteractive prevents apt from blocking on prompts.
        await self._run(
            conn,
            server,
            "DEBIAN_FRONTEND=noninteractive apt-get update -y",
            timeout=TIMEOUT_LONG,
            error_cls=SystemUpdateFailed,
            captured=captured,
        )
        await self._run(
            conn,
            server,
            "DEBIAN_FRONTEND=noninteractive apt-get upgrade -y",
            timeout=TIMEOUT_LONG,
            error_cls=SystemUpdateFailed,
            captured=captured,
        )
        server.last_system_update_at = _now()

    async def install_base_packages(
        self, conn: asyncssh.SSHClientConnection, server: Server, db: AsyncSession
    ) -> None:
        captured: list[str] = []
        # apt-get install is a no-op for already installed packages, so idempotent.
        await self._run(
            conn,
            server,
            "DEBIAN_FRONTEND=noninteractive apt-get install -y "
            "git certbot ufw curl ca-certificates",
            timeout=TIMEOUT_MED,
            error_cls=SystemUpdateFailed,
            captured=captured,
        )
        server.base_packages_installed = True

    async def install_docker(
        self, conn: asyncssh.SSHClientConnection, server: Server, db: AsyncSession
    ) -> None:
        captured: list[str] = []
        # The official installer detects an existing Docker and skips, so idempotent.
        await self._run(
            conn,
            server,
            "curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && "
            "sh /tmp/get-docker.sh && rm /tmp/get-docker.sh",
            timeout=TIMEOUT_LONG,
            error_cls=DockerInstallFailed,
            captured=captured,
        )
        await self._run(
            conn,
            server,
            "docker --version",
            timeout=TIMEOUT_SHORT,
            error_cls=DockerInstallFailed,
            captured=captured,
        )
        server.docker_installed = True

    async def create_sudo_user(
        self,
        conn: asyncssh.SSHClientConnection,
        server: Server,
        db: AsyncSession,
        ctx: HardeningContext,
        sudo_user_name_from_client: str,
    ) -> None:
        name = sudo_user_name_from_client
        captured: list[str] = []

        # 1. Skip user creation if the account already exists (idempotent). The
        # script always exits 0 and reports existence via stdout, so a missing user
        # is not treated as a command failure.
        check = await self._run(
            conn,
            server,
            f"id -u {name} >/dev/null 2>&1 && echo exists || echo absent",
            timeout=TIMEOUT_SHORT,
            error_cls=SudoUserVerificationFailed,
            captured=captured,
        )
        user_already_exists = (check.stdout or "").strip() == "exists"

        if not user_already_exists:
            await self._run(
                conn,
                server,
                f"adduser --disabled-password --gecos '' {name}",
                timeout=TIMEOUT_SHORT,
                error_cls=SudoUserVerificationFailed,
                captured=captured,
            )
            await self._run(
                conn,
                server,
                f"usermod -aG sudo {name}",
                timeout=TIMEOUT_SHORT,
                error_cls=SudoUserVerificationFailed,
                captured=captured,
            )

        # 4. Add to docker group only if Docker is installed. Re-running this method
        # after a later Docker install retries the group add (the steps below are
        # idempotent and adduser is skipped via the id -u check).
        if server.docker_installed:
            await self._run(
                conn,
                server,
                f"usermod -aG docker {name}",
                timeout=TIMEOUT_SHORT,
                error_cls=SudoUserVerificationFailed,
                captured=captured,
            )

        # 5. Passwordless sudo for this user (v1 simplification for automation).
        sudoers = f"/etc/sudoers.d/{name}"
        await self._run(
            conn,
            server,
            f"echo '{name} ALL=(ALL) NOPASSWD:ALL' > {sudoers} && chmod 0440 {sudoers}",
            timeout=TIMEOUT_SHORT,
            error_cls=SudoUserVerificationFailed,
            captured=captured,
        )

        # 6 + 7. Authorized key for the new user. install -d creates the dir owned by
        # the user with mode 700; the key append is idempotent via grep -qF.
        ssh_dir = f"/home/{name}/.ssh"
        akf = f"{ssh_dir}/authorized_keys"
        pub = shlex.quote(ctx.app_public_key.strip())
        await self._run(
            conn,
            server,
            f"install -d -m 700 -o {name} -g {name} {ssh_dir} && "
            f"touch {akf} && "
            f"(grep -qF {pub} {akf} || echo {pub} >> {akf}) && "
            f"chmod 600 {akf} && chown {name}:{name} {akf}",
            timeout=TIMEOUT_SHORT,
            error_cls=SudoUserVerificationFailed,
            captured=captured,
        )

        # 8. Confirm the new user works (shell + passwordless sudo) over a fresh
        # connection BEFORE switching identity, so a failure here rolls back without
        # ever losing confirmed access.
        if not await self._verify_user_access(server, name, ctx.app_private_key):
            captured.append(
                f"Verification as '{name}' failed: could not confirm shell access "
                "and passwordless sudo over a fresh connection."
            )
            raise SudoUserVerificationFailed("\n".join(captured))

        # 9. Switch identity. Subsequent connections authenticate as this user.
        server.sudo_user_name = name
        server.username = name

        # 10. Drop the pooled root connection so the next op reconnects as the user.
        self._ssh.evict_connection(ctx.user_id, server.id)

    async def disable_root_login(
        self,
        conn: asyncssh.SSHClientConnection,
        server: Server,
        db: AsyncSession,
        ctx: HardeningContext,
    ) -> None:
        # Guard: refuse without a confirmed alternative login.
        if server.sudo_user_name is None:
            raise RootLoginPrecheckFailed(
                "Cannot disable root login before a sudo user has been created."
            )

        # Re-verify the sudo user before touching sshd_config.
        if not await self._verify_user_access(
            server, server.sudo_user_name, ctx.app_private_key
        ):
            raise RootLoginPrecheckFailed(
                f"Sudo user '{server.sudo_user_name}' failed re-verification. "
                "Not editing sshd_config."
            )

        captured: list[str] = []
        await self._set_sshd_directive(
            conn,
            server,
            directive="PermitRootLogin",
            value="no",
            value_alternatives="yes|no|prohibit-password|forced-commands-only",
            error_cls=RootLoginDisableFailed,
            captured=captured,
        )
        server.root_login_disabled = True

    async def disable_password_auth(
        self, conn: asyncssh.SSHClientConnection, server: Server, db: AsyncSession
    ) -> None:
        """Disable SSH password authentication daemon-wide.

        No lockout risk: the server is already verified, meaning app key based login
        works, so turning password auth off only removes a path we do not use. This
        closes the gap where a user who skipped disabling password auth at install
        time could otherwise harden the box (sudo user, root login disabled) while
        password auth stayed globally enabled.
        """
        captured: list[str] = []
        await self._set_sshd_directive(
            conn,
            server,
            directive="PasswordAuthentication",
            value="no",
            value_alternatives="yes|no",
            error_cls=PasswordAuthDisableFailed,
            captured=captured,
        )
        server.password_auth_disabled = True

    async def configure_firewall(
        self, conn: asyncssh.SSHClientConnection, server: Server, db: AsyncSession
    ) -> None:
        captured: list[str] = []
        # Allow SSH first so enabling the firewall cannot lock us out. OpenSSH is the
        # canonical UFW profile (equivalent to 22/tcp).
        for rule in ("ufw allow OpenSSH", "ufw allow 80/tcp", "ufw allow 443/tcp"):
            await self._run(
                conn,
                server,
                rule,
                timeout=TIMEOUT_SHORT,
                error_cls=FirewallConfigFailed,
                captured=captured,
            )
        # --force skips the interactive confirmation; the rules above keep SSH open.
        await self._run(
            conn,
            server,
            "ufw --force enable",
            timeout=TIMEOUT_SHORT,
            error_cls=FirewallConfigFailed,
            captured=captured,
        )
        await self._run(
            conn,
            server,
            "ufw status | grep -q 'Status: active'",
            timeout=TIMEOUT_SHORT,
            error_cls=FirewallConfigFailed,
            captured=captured,
        )
        server.firewall_enabled = True

    async def create_swap(
        self, conn: asyncssh.SSHClientConnection, server: Server, db: AsyncSession
    ) -> None:
        captured: list[str] = []
        # One script: if /swapfile exists and is already in fstab, do nothing.
        # Otherwise size it at 25% of RAM (floored), create, enable, and persist it.
        # fallocate is unreliable for swap on some filesystems, so fall back to dd.
        script = (
            "set -e\n"
            "if test -f /swapfile && grep -q '/swapfile' /etc/fstab; then\n"
            "  echo swap_already_configured\n"
            "  exit 0\n"
            "fi\n"
            "MEM_KB=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)\n"
            "SWAP_MB=$((MEM_KB / 4 / 1024))\n"
            f"if [ \"$SWAP_MB\" -lt {SWAP_FLOOR_MB} ]; then SWAP_MB={SWAP_FLOOR_MB}; fi\n"
            "if ! test -f /swapfile; then\n"
            "  fallocate -l ${SWAP_MB}M /swapfile || "
            "dd if=/dev/zero of=/swapfile bs=1M count=${SWAP_MB}\n"
            "fi\n"
            "chmod 600 /swapfile\n"
            "mkswap /swapfile\n"
            "swapon /swapfile 2>/dev/null || true\n"
            "grep -qF '/swapfile' /etc/fstab || "
            "echo '/swapfile none swap sw 0 0' >> /etc/fstab\n"
        )
        await self._run(
            conn,
            server,
            script,
            timeout=TIMEOUT_SHORT,
            error_cls=SwapConfigFailed,
            captured=captured,
        )
        await self._run(
            conn,
            server,
            "swapon --show | grep -q /swapfile",
            timeout=TIMEOUT_SHORT,
            error_cls=SwapConfigFailed,
            captured=captured,
        )
        server.swap_configured = True

    async def reboot(
        self,
        conn: asyncssh.SSHClientConnection,
        server: Server,
        db: AsyncSession,
        ctx: HardeningContext,
    ) -> None:
        captured: list[str] = []
        # Detach the reboot so our command returns before the box goes down. sleep 1
        # gives the SSH session time to close cleanly.
        await self._run(
            conn,
            server,
            "nohup sh -c 'sleep 1 && reboot' >/dev/null 2>&1 &",
            timeout=TIMEOUT_SHORT,
            error_cls=HardeningError,
            captured=captured,
        )
        # The pooled connection is about to die with the reboot. Drop it so the next
        # call (ping) opens a fresh one.
        self._ssh.evict_connection(ctx.user_id, server.id)

    async def quick_harden(
        self,
        server: Server,
        db: AsyncSession,
        ctx: HardeningContext,
        sudo_user_name_from_client: str,
    ) -> None:
        """Run the standard sequence in order over a single (caller's) transaction.

        Reboot is intentionally excluded: it closes the connection mid-orchestration.
        After create_sudo_user switches server.username, the pool is evicted and we
        re-get a connection, which authenticates as the new sudo user because
        get_connection reads server.username from the in-memory object (the DB commit
        only happens once at the end, in the route).
        """
        conn = await self._ssh.get_connection(
            server, ctx.user_id, ctx.session_id, ctx.redis, db, ctx.key_provider
        )
        await self.update_system(conn, server, db)
        await self.install_base_packages(conn, server, db)
        await self.install_docker(conn, server, db)
        await self.create_sudo_user(conn, server, db, ctx, sudo_user_name_from_client)

        # create_sudo_user evicted the root connection. Reconnect as the sudo user.
        conn = await self._ssh.get_connection(
            server, ctx.user_id, ctx.session_id, ctx.redis, db, ctx.key_provider
        )
        await self.configure_firewall(conn, server, db)
        await self.create_swap(conn, server, db)
        await self.disable_password_auth(conn, server, db)
        await self.disable_root_login(conn, server, db, ctx)


hardening_service = HardeningService(ssh_service)
