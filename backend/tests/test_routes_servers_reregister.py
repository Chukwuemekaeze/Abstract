"""Route tests for POST /api/servers/{id}/reprobe (host key change recovery).

DB backed (TEST_DATABASE_URL). SSH is the shared mock_ssh fixture (probe returns a new
fingerprint, install_key/run_command succeed), GitHub and the Clerk OAuth token fetch
used by the stale-project purge are mocked at the boundary. These assert the HTTP
contract plus the DB side effects: the row moves back to pending_verification with a new
host key and a fresh app key, and the stale hardening/project state is wiped — while a
server that is not key_mismatch (and an unreachable host) is rejected without any change.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.clerk import get_clerk_client
from app.deps.services import get_github_service
from app.main import app
from app.models import AppSshKey, Project, Server
from app.services.ssh_service import ProbeError
from tests.conftest import requires_db
from tests.project_mocks import make_github
from tests.run_publish_mocks import seed_project

pytestmark = requires_db

OLD_HOST_KEY = b"ssh-ed25519 AAAAOLDHOSTKEY"
OLD_FINGERPRINT = "SHA256:oldfingerprintvalue"
OLD_APP_PUBLIC_KEY = "ssh-ed25519 AAAAOLDAPPKEY abstract-old"
NEW_FINGERPRINT = "SHA256:testfingerprintvalue"  # matches the mock_ssh probe result


@pytest.fixture
def github_clerk_env(mocker):
    """Override GitHub + Clerk deps and patch the recovery service's OAuth token fetch,
    so the best-effort deploy-key revocation runs without touching the network."""
    github = make_github(mocker)
    app.dependency_overrides[get_github_service] = lambda: github
    app.dependency_overrides[get_clerk_client] = lambda: mocker.MagicMock()
    mocker.patch(
        "app.services.server_recovery_service.get_github_oauth_token",
        mocker.AsyncMock(return_value="gho_test_token"),
    )
    yield github
    app.dependency_overrides.pop(get_github_service, None)
    app.dependency_overrides.pop(get_clerk_client, None)


async def _seed_key_mismatch_server(db_session, user, *, with_projects=0):
    """A fully hardened, verified server that has since flipped to key_mismatch, plus its
    app key and optionally some projects. Represents the state a rebuild leaves behind."""
    server = Server(
        user_id=user.id,
        name="web1",
        host="203.0.113.10",
        port=22,
        username="deploy",  # hardening had switched off root
        host_key=OLD_HOST_KEY,
        host_key_type="ssh-ed25519",
        fingerprint_sha256=OLD_FINGERPRINT,
        status="key_mismatch",
        verification_source="tofu",
        key_installed=True,
        password_auth_disabled=True,
        verified_at=datetime.now(timezone.utc),
        sudo_user_name="deploy",
        root_login_disabled=True,
        firewall_enabled=True,
        docker_installed=True,
        base_packages_installed=True,
        nginx_installed=True,
        swap_configured=True,
        last_system_update_at=datetime.now(timezone.utc),
    )
    db_session.add(server)
    await db_session.commit()
    await db_session.refresh(server)

    db_session.add(
        AppSshKey(
            server_id=server.id,
            public_key=OLD_APP_PUBLIC_KEY,
            encrypted_private_key=b"ciphertext",
            key_type="ssh-ed25519",
            encryption_key_id="env-v1",
        )
    )
    await db_session.commit()

    for i in range(with_projects):
        await seed_project(
            db_session, user.id, server, slug=f"proj{i}", repo_id=900 + i
        )

    return server


async def test_reprobe_rejected_when_not_key_mismatch(
    client, mock_ssh, github_clerk_env, db_session, test_user
):
    # A verified server is not eligible: re-registration is only for key_mismatch.
    server = Server(
        user_id=test_user.id,
        name="ok",
        host="203.0.113.20",
        port=22,
        username="root",
        status="verified",
        verification_source="tofu",
    )
    db_session.add(server)
    await db_session.commit()
    await db_session.refresh(server)

    resp = await client.post(f"/api/servers/{server.id}/reprobe", json={})
    assert resp.status_code == 400, resp.text
    mock_ssh.probe.assert_not_awaited()


async def test_reprobe_resets_stale_state_and_replaces_key(
    client, mock_ssh, github_clerk_env, db_session, test_user
):
    server = await _seed_key_mismatch_server(db_session, test_user, with_projects=2)

    resp = await client.post(
        f"/api/servers/{server.id}/reprobe", json={"username": "root"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # New fingerprint is surfaced for confirmation and differs from the stale one.
    assert body["fingerprint_sha256"] == NEW_FINGERPRINT
    assert body["fingerprint_sha256"] != OLD_FINGERPRINT
    assert body["app_public_key"].startswith("ssh-ed25519 ")
    assert body["app_public_key"] != OLD_APP_PUBLIC_KEY
    mock_ssh.probe.assert_awaited_once()
    mock_ssh.evict_connection.assert_called_once()

    # The row is back to a fresh pending_verification with the new host identity.
    row = await db_session.scalar(select(Server).where(Server.id == server.id))
    assert row.status == "pending_verification"
    assert row.host_key == b"ssh-ed25519 AAAATESTKEY"  # mock_ssh probe host_key
    assert row.fingerprint_sha256 == NEW_FINGERPRINT
    assert row.username == "root"
    # Stale registration + hardening state is cleared.
    assert row.key_installed is False
    assert row.password_auth_disabled is False
    assert row.verified_at is None
    assert row.sudo_user_name is None
    assert row.root_login_disabled is False
    assert row.firewall_enabled is False
    assert row.docker_installed is False
    assert row.base_packages_installed is False
    assert row.nginx_installed is False
    assert row.swap_configured is False
    assert row.last_system_update_at is None

    # Stale projects are gone; each one's GitHub deploy key was best-effort revoked.
    remaining = (
        await db_session.scalars(
            select(Project).where(Project.server_id == server.id)
        )
    ).all()
    assert remaining == []
    assert github_clerk_env.delete_deploy_key.await_count == 2

    # The app key row was replaced (new public key persisted, matching the response).
    app_key = await db_session.scalar(
        select(AppSshKey).where(AppSshKey.server_id == server.id)
    )
    assert app_key is not None
    assert app_key.public_key == body["app_public_key"]
    assert app_key.public_key != OLD_APP_PUBLIC_KEY


async def test_reprobe_defaults_username_to_root(
    client, mock_ssh, github_clerk_env, db_session, test_user
):
    server = await _seed_key_mismatch_server(db_session, test_user)
    resp = await client.post(f"/api/servers/{server.id}/reprobe", json={})
    assert resp.status_code == 200, resp.text

    row = await db_session.scalar(select(Server).where(Server.id == server.id))
    assert row.username == "root"


async def test_full_reregistration_flow_returns_to_verified(
    client, mock_ssh, github_clerk_env, db_session, test_user
):
    server = await _seed_key_mismatch_server(db_session, test_user, with_projects=1)

    reprobe = await client.post(f"/api/servers/{server.id}/reprobe", json={})
    assert reprobe.status_code == 200, reprobe.text

    # Step two reuses the existing install_key path unchanged.
    install = await client.post(
        f"/api/servers/{server.id}/install_key",
        json={"password": "hunter2", "disable_password_auth": True},
    )
    assert install.status_code == 200, install.text
    assert install.json()["status"] == "verified"
    mock_ssh.install_key.assert_awaited_once()

    row = await db_session.scalar(select(Server).where(Server.id == server.id))
    assert row.status == "verified"
    assert row.key_installed is True
    projects = (
        await db_session.scalars(
            select(Project).where(Project.server_id == server.id)
        )
    ).all()
    assert projects == []  # the rebuild's stale projects stayed wiped


async def test_reprobe_unreachable_keeps_row_key_mismatch(
    client, mock_ssh, github_clerk_env, db_session, test_user
):
    server = await _seed_key_mismatch_server(db_session, test_user, with_projects=1)
    mock_ssh.probe.side_effect = ProbeError("could not reach host")

    resp = await client.post(f"/api/servers/{server.id}/reprobe", json={})
    assert resp.status_code == 502, resp.text

    # Nothing was touched: the row stays key_mismatch and the projects are intact so
    # the user can retry once the box is reachable.
    row = await db_session.scalar(select(Server).where(Server.id == server.id))
    assert row.status == "key_mismatch"
    assert row.fingerprint_sha256 == OLD_FINGERPRINT
    projects = (
        await db_session.scalars(
            select(Project).where(Project.server_id == server.id)
        )
    ).all()
    assert len(projects) == 1


async def test_key_mismatch_server_blocks_operations(
    client, mock_ssh, db_session, test_user
):
    """A key_mismatch server rejects operations up front (before any SSH), so the state
    is a hard block, not just a failed connection."""
    server = await _seed_key_mismatch_server(db_session, test_user)

    smoke = await client.post(f"/api/servers/{server.id}/smoke_test")
    assert smoke.status_code == 400, smoke.text

    harden = await client.post(f"/api/servers/{server.id}/harden/update_system")
    assert harden.status_code == 400, harden.text
