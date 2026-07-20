"""Recover a server whose SSH host identity changed (status key_mismatch).

Rebuilding a VPS gives it a new host key and wipes Abstract's installed key, even
when the IP is reused. TOFU treats that as an untrusted identity change and flips the
server to key_mismatch (see ssh_service.get_connection), blocking every operation.

This module implements the "re-register this server" recovery path: a fresh probe of
the current host that captures the new host key for manual fingerprint confirmation and
resets the stale state a rebuild leaves behind — the old app SSH key, the hardening
flags, and the project/deployment records that no longer exist on the box. It leaves the
row in status pending_verification with the new host key and a brand-new app keypair, so
the existing install_key step (password + new key install) completes the recovery exactly
like a first registration.

We never auto-trust the new fingerprint: the caller must still confirm it and supply the
current VPS password through install_key.
"""

import redis.asyncio as aioredis
from clerk_backend_api import Clerk
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.logging_config import logger
from app.models import AppSshKey, Project, ProjectDeployKey, Server, User
from app.services.app_key_service import create_key_for_server
from app.services.clerk_oauth import get_github_oauth_token
from app.services.github_service import GithubService
from app.services.key_provider import KeyProvider
from app.services.ssh_service import SSHService

__all__ = ["reprobe_for_reregistration"]


async def _purge_stale_projects(
    *,
    server: Server,
    current_user: User,
    db: AsyncSession,
    clerk: Clerk,
    github: GithubService,
) -> None:
    """Delete every project row on the server. The rebuild already destroyed the VPS
    clones and containers, so there is nothing to tear down on the box; this only
    clears Abstract's now-stale records (cascades runs, env files/vars, deploy-key
    rows). GitHub deploy keys are revoked best-effort — the repos still exist, but a
    failure there must not block recovery, so we only log it."""
    for project in list(server.projects):
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
                    "project {} ({}): {}",
                    project.name,
                    project.id,
                    exc,
                )
        await db.delete(project)


async def reprobe_for_reregistration(
    *,
    server: Server,
    username: str,
    current_user: User,
    session_id: str,
    db: AsyncSession,
    ssh: SSHService,
    redis: aioredis.Redis,
    key_provider: KeyProvider,
    clerk: Clerk,
    github: GithubService,
) -> tuple[str, str]:
    """Probe the current host, reset stale state, and return (new fingerprint, new app
    public key). Leaves the row in pending_verification for install_key to finish.

    Raises ProbeError (from ssh.probe) if the host is unreachable; the caller maps it
    to a 502 and the row stays key_mismatch, so the user can retry.
    """
    server_id = server.id

    # Capture the new host key first: if the box is unreachable we abort before
    # touching any state, leaving the row key_mismatch to retry.
    probe_result = await ssh.probe(server.host, server.port, username)

    # Reload with the relationships the reset needs.
    server = await db.scalar(
        select(Server)
        .options(selectinload(Server.app_ssh_key), selectinload(Server.projects))
        .where(Server.id == server_id)
    )
    assert server is not None

    # Wipe the stale projects/deployments the rebuild destroyed.
    await _purge_stale_projects(
        server=server,
        current_user=current_user,
        db=db,
        clerk=clerk,
        github=github,
    )

    # Replace the app keypair: the old one was wiped from the rebuilt box and its
    # private key is now useless. Unique(server_id) means the old row must go first.
    old_key = await db.scalar(
        select(AppSshKey).where(AppSshKey.server_id == server_id)
    )
    if old_key is not None:
        await db.delete(old_key)
        await db.flush()
    app_key = await create_key_for_server(server, db, key_provider)

    # Adopt the new host identity and reset everything a rebuild invalidates. Back to
    # a fresh pending_verification row: install_key re-probes (now matching this key),
    # installs the new app key over the password session, and re-verifies.
    server.host_key = probe_result.host_key
    server.host_key_type = probe_result.host_key_type
    server.fingerprint_sha256 = probe_result.fingerprint_sha256
    server.status = "pending_verification"
    server.username = username
    server.sudo_user_name = None
    server.key_installed = False
    server.password_auth_disabled = False
    server.verified_at = None
    server.root_login_disabled = False
    server.firewall_enabled = False
    server.docker_installed = False
    server.base_packages_installed = False
    server.nginx_installed = False
    server.swap_configured = False
    server.last_system_update_at = None
    # verification_source stays "tofu": this is still a trust-on-first-use decision,
    # just re-taken against the new fingerprint.

    # Drop any pooled connection and the cached (now-wrong) private key so the next
    # connection is opened fresh with the new key against the new host.
    ssh.evict_connection(current_user.id, server_id)
    await redis.delete(f"ssh_key:{server_id}:{session_id}")

    await db.commit()
    await db.refresh(app_key)

    return probe_result.fingerprint_sha256, app_key.public_key
