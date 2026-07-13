"""Publish a running project to a domain: nginx reverse proxy + Let's Encrypt.

The caller owns the transaction; this module never commits. External state
(nginx config, symlink, certificate) is tracked in _PublishState so that any
failure after a side effect triggers best-effort cleanup: log and swallow
cleanup errors, never mask the original error, leave nginx reloaded in its
prior shape. The DB rollback keeps domain/internal_port/published_at unset.

The DNS pre-check happens before any VPS mutation so the common failure mode
(user has not pointed the A record yet) costs nothing to retry.
"""

import asyncio
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone

import asyncssh
import dns.exception
import dns.resolver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_config import logger
from app.models import Project, Server, User

__all__ = [
    "PublishServiceError",
    "AppNotRunning",
    "AlreadyPublished",
    "NginxNotInstalled",
    "DomainAlreadyUsed",
    "PortAlreadyUsed",
    "DomainDoesNotResolve",
    "NginxConfigInvalid",
    "CertbotFailed",
    "NothingListening",
    "PublishVerificationFailed",
    "resolve_domain_dns",
    "build_nginx_config",
    "publish_project",
    "cleanup_publish",
]

_TIMEOUT_CHECK = 30
_TIMEOUT_CERTBOT = 180
_TIMEOUT_CURL = 20
_DNS_LIFETIME = 10.0


class PublishServiceError(Exception):
    pass


class AppNotRunning(PublishServiceError):
    pass


class AlreadyPublished(PublishServiceError):
    pass


class NginxNotInstalled(PublishServiceError):
    pass


class DomainAlreadyUsed(PublishServiceError):
    pass


class PortAlreadyUsed(PublishServiceError):
    pass


class DomainDoesNotResolve(PublishServiceError):
    def __init__(self, server_host: str, resolved: list[str]):
        self.server_host = server_host
        self.resolved = resolved
        if resolved:
            detail = f"it resolves to {', '.join(resolved)}"
        else:
            detail = "it does not resolve to any address yet"
        super().__init__(
            f"The domain must point at your server ({server_host}) but {detail}. "
            "Update your DNS A record and try again; DNS changes can take a few "
            "minutes to propagate."
        )


class NginxConfigInvalid(PublishServiceError):
    def __init__(self, captured_output: str):
        self.captured_output = captured_output
        super().__init__("nginx rejected the generated config")


class CertbotFailed(PublishServiceError):
    def __init__(self, captured_output: str):
        self.captured_output = captured_output
        super().__init__("certbot failed to obtain a certificate")


class NothingListening(PublishServiceError):
    def __init__(self, port: int):
        self.port = port
        super().__init__(
            f"Nothing is listening on port {port} on the server. Check that your "
            "app publishes this port and is still running."
        )


class PublishVerificationFailed(PublishServiceError):
    def __init__(self, status_code: str, captured_output: str):
        self.status_code = status_code
        self.captured_output = captured_output
        super().__init__(
            f"The app is listening locally but https://... returned {status_code}"
        )


@dataclass
class _PublishState:
    """What has been done on the VPS, so cleanup knows what to undo."""

    config_written: bool = False
    symlinked: bool = False
    cert_requested: bool = False


def _priv(server: Server) -> str:
    """Privilege prefix: empty as root, "sudo " as the non-root sudo user."""
    return "" if server.username == "root" else "sudo "


def _resolve_sync(domain: str) -> list[str]:
    resolver = dns.resolver.Resolver(configure=True)
    try:
        answer = resolver.resolve(domain, "A", lifetime=_DNS_LIFETIME)
    except (
        dns.resolver.NXDOMAIN,
        dns.resolver.NoAnswer,
        dns.resolver.NoNameservers,
        dns.exception.Timeout,
    ):
        return []
    return [record.address for record in answer]


async def resolve_domain_dns(domain: str) -> list[str]:
    """IPv4 addresses the domain's A record resolves to; [] when it does not
    resolve. dnspython is synchronous, so it runs in a worker thread."""
    try:
        return await asyncio.to_thread(_resolve_sync, domain)
    except dns.exception.DNSException:
        return []


def build_nginx_config(domain: str, internal_port: int) -> str:
    """HTTP-only server block; certbot --redirect rewrites it for TLS. The
    websocket upgrade headers are always present so websocket apps work
    without extra configuration."""
    return f"""server {{
    listen 80;
    server_name {domain};
    location / {{
        proxy_pass http://127.0.0.1:{internal_port};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }}
}}
"""


async def _run(
    conn: asyncssh.SSHClientConnection, command: str, timeout: int = _TIMEOUT_CHECK
) -> asyncssh.SSHCompletedProcess:
    return await conn.run(command, check=False, timeout=timeout)


def _output(result: asyncssh.SSHCompletedProcess) -> str:
    return f"{result.stdout or ''}{result.stderr or ''}".rstrip()


async def cleanup_publish(
    conn: asyncssh.SSHClientConnection,
    server: Server,
    project_slug: str,
    domain: str,
    state: _PublishState,
) -> None:
    """Best-effort undo of VPS publish state. Logs and swallows every failure;
    must never raise, so it cannot mask the error that triggered it."""
    priv = _priv(server)
    quoted_slug = shlex.quote(f"/etc/nginx/sites-enabled/{project_slug}.conf")
    quoted_avail = shlex.quote(f"/etc/nginx/sites-available/{project_slug}.conf")
    steps: list[tuple[str, str, int]] = []
    if state.symlinked:
        steps.append(("remove nginx symlink", f"{priv}rm -f {quoted_slug}", _TIMEOUT_CHECK))
    if state.config_written:
        steps.append(("remove nginx config", f"{priv}rm -f {quoted_avail}", _TIMEOUT_CHECK))
    if state.cert_requested:
        steps.append(
            (
                "delete certbot certificate",
                f"{priv}certbot delete --cert-name {shlex.quote(domain)} --non-interactive",
                _TIMEOUT_CERTBOT,
            )
        )
    if state.symlinked or state.config_written or state.cert_requested:
        steps.append(("reload nginx", f"{priv}systemctl reload nginx", _TIMEOUT_CHECK))

    for label, command, timeout in steps:
        try:
            result = await _run(conn, command, timeout=timeout)
            if result.exit_status not in (0, None):
                logger.warning(
                    "Publish cleanup step '{}' exited {} for project {}",
                    label,
                    result.exit_status,
                    project_slug,
                )
        except Exception as exc:
            logger.warning(
                "Publish cleanup step '{}' failed for project {}: {}",
                label,
                project_slug,
                exc,
            )


async def publish_project(
    *,
    conn: asyncssh.SSHClientConnection,
    project: Project,
    server: Server,
    current_user: User,
    domain_from_client: str,
    internal_port_from_client: int,
    db: AsyncSession,
) -> Project:
    # -- Preconditions (no side effects yet) --------------------------------
    if project.runtime_status != "running":
        raise AppNotRunning("Start your app before publishing.")
    if project.published_at is not None:
        raise AlreadyPublished(
            f"This project is already published at {project.domain}."
        )
    if not server.nginx_installed:
        raise NginxNotInstalled(
            "nginx is not installed on this server; run the nginx hardening step first."
        )
    domain_taken = await db.scalar(
        select(Project.id).where(
            Project.server_id == server.id,
            Project.domain == domain_from_client,
            Project.id != project.id,
        )
    )
    if domain_taken:
        raise DomainAlreadyUsed(
            f"{domain_from_client} is already used by another project on this server."
        )
    port_taken = await db.scalar(
        select(Project.id).where(
            Project.server_id == server.id,
            Project.internal_port == internal_port_from_client,
            Project.id != project.id,
        )
    )
    if port_taken:
        raise PortAlreadyUsed(
            f"Port {internal_port_from_client} is already published by another "
            "project on this server."
        )

    # -- 1. DNS pre-check ----------------------------------------------------
    resolved = await resolve_domain_dns(domain_from_client)
    if server.host not in resolved:
        raise DomainDoesNotResolve(server_host=server.host, resolved=resolved)

    priv = _priv(server)
    slug = project.slug
    config_path = f"/etc/nginx/sites-available/{slug}.conf"
    symlink_path = f"/etc/nginx/sites-enabled/{slug}.conf"
    quoted_config = shlex.quote(config_path)
    quoted_symlink = shlex.quote(symlink_path)
    quoted_domain = shlex.quote(domain_from_client)
    state = _PublishState()

    try:
        # -- 3. Write nginx config (idempotent overwrite) --------------------
        config = build_nginx_config(domain_from_client, internal_port_from_client)
        result = await _run(
            conn,
            f"printf '%s' {shlex.quote(config)} | {priv}tee {quoted_config} > /dev/null "
            f"&& {priv}chmod 644 {quoted_config}",
        )
        if result.exit_status not in (0, None):
            raise NginxConfigInvalid(_output(result))
        state.config_written = True

        # -- 4. Symlink into sites-enabled ------------------------------------
        result = await _run(conn, f"{priv}ln -sfn {quoted_config} {quoted_symlink}")
        if result.exit_status not in (0, None):
            raise NginxConfigInvalid(_output(result))
        state.symlinked = True

        # -- 5. Validate, 6. reload -------------------------------------------
        result = await _run(conn, f"{priv}nginx -t")
        if result.exit_status not in (0, None):
            raise NginxConfigInvalid(_output(result))
        result = await _run(conn, f"{priv}systemctl reload nginx")
        if result.exit_status not in (0, None):
            raise NginxConfigInvalid(_output(result))

        # -- 7. Certbot ---------------------------------------------------------
        state.cert_requested = True
        try:
            result = await _run(
                conn,
                f"{priv}certbot --nginx -d {quoted_domain} --non-interactive "
                f"--agree-tos --email {shlex.quote(current_user.email)} --redirect",
                timeout=_TIMEOUT_CERTBOT,
            )
        except (TimeoutError, asyncssh.Error, OSError) as exc:
            raise CertbotFailed(f"certbot did not complete: {exc}") from exc
        if result.exit_status not in (0, None):
            raise CertbotFailed(_output(result))

        # -- 8. Verify over HTTPS ------------------------------------------------
        result = await _run(
            conn,
            f'curl -sI -o /dev/null -w "%{{http_code}}" --max-time 10 '
            f"https://{quoted_domain}",
            timeout=_TIMEOUT_CURL,
        )
        status = (result.stdout or "").strip()
        if not status.startswith(("2", "3")) or len(status) != 3:
            local = await _run(
                conn,
                f'curl -sI -o /dev/null -w "%{{http_code}}" --max-time 5 '
                f"http://localhost:{internal_port_from_client}",
                timeout=_TIMEOUT_CURL,
            )
            local_status = (local.stdout or "").strip()
            if not local_status.startswith(("2", "3", "4")) or len(local_status) != 3:
                raise NothingListening(port=internal_port_from_client)
            raise PublishVerificationFailed(
                status_code=status or "no response",
                captured_output=_output(result),
            )
    except Exception:
        await cleanup_publish(conn, server, slug, domain_from_client, state)
        raise

    # -- 9. Mark published ------------------------------------------------------
    now = datetime.now(timezone.utc)
    project.domain = domain_from_client
    project.internal_port = internal_port_from_client
    project.published_at = now
    project.updated_at = now
    return project
