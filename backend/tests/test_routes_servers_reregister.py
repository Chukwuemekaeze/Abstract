"""Route tests for the password-based re-registration endpoints.

The access engine's SSH work is stubbed at the route boundary so these focus on
ownership, state gating, the persisted state machine, the bootstrap-password
write-ahead, resumable idempotency, and error mapping. DB backed: skipped when
TEST_DATABASE_URL is not set.
"""

import os

import pytest
from sqlalchemy import select

from app.clerk import get_clerk_client
from app.deps.services import get_github_service
from app.main import app
from app.models import AppSshKey, Project, ProjectDeployKey, Server
from app.services.key_provider import EnvKeyProvider
from app.services.server_reregistration_service import ReregistrationError

from tests.conftest import requires_db

pytestmark = requires_db

# The key mock_ssh.probe returns; the rebuilt host presents this at re-registration.
PROBE_KEY = b"ssh-ed25519 AAAATESTKEY"
TRUSTED_OLD_KEY = b"ssh-ed25519 TRUSTEDOLD"


def _provider() -> EnvKeyProvider:
    return EnvKeyProvider(os.environ["APP_MASTER_KEY"])


async def _seed_server(
    db_session,
    user,
    *,
    status="key_mismatch",
    reregistration_state="none",
    pending_host_key=None,
    bootstrap_password=None,
    key_is_active=True,
    with_projects=0,
    with_deploy_keys=False,
    owner=None,
) -> Server:
    server = Server(
        user_id=(owner or user).id,
        name="web1",
        host="203.0.113.10",
        port=22,
        username="deploy",  # a prior hardening switched off root; a rebuild wiped it
        host_key=TRUSTED_OLD_KEY,
        host_key_type="ssh-ed25519",
        fingerprint_sha256="SHA256:oldtrusted",
        status=status,
        reregistration_state=reregistration_state,
        pending_host_key=pending_host_key,
        bootstrap_password=bootstrap_password,
        verification_source="tofu",
        sudo_user_name="deploy",
        firewall_enabled=True,
        docker_installed=True,
    )
    db_session.add(server)
    await db_session.commit()
    await db_session.refresh(server)

    provider = _provider()
    encrypted = await provider.encrypt(b"OLDPRIVATEKEY")
    app_key = AppSshKey(
        server_id=server.id,
        public_key="ssh-ed25519 AAAAOLDPUB abstract-server-old",
        encrypted_private_key=encrypted,
        key_type="ssh-ed25519",
        encryption_key_id=provider.key_id,
        is_active=key_is_active,
    )
    db_session.add(app_key)

    for i in range(with_projects):
        project = Project(
            user_id=(owner or user).id,
            server_id=server.id,
            name=f"proj{i}",
            slug=f"proj{i}",
            github_repo_full_name=f"acme/proj{i}",
            github_repo_id=1000 + i,
            clone_path=f"/home/deploy/proj{i}",
            runtime_status="running",
        )
        db_session.add(project)
        if with_deploy_keys:
            await db_session.flush()  # populate project.id for the FK below
            db_session.add(
                ProjectDeployKey(
                    project_id=project.id,
                    github_deploy_key_id=5000 + i,
                    deploy_key_public_key=f"ssh-ed25519 AAAADEPLOY{i}",
                    encrypted_deploy_key_private_key=await provider.encrypt(b"DK"),
                    deploy_key_fingerprint=f"SHA256:dk{i}",
                )
            )
    await db_session.commit()
    await db_session.refresh(server)
    return server


def _stub_engine(mocker, *, resume_pending=False, resume_bootstrap=False):
    """Stub the SSH-touching engine calls the route makes, leaving the DB-staging ones
    (regenerate_pending_keypair, purge_server_projects) real."""
    mocker.patch(
        "app.routes.servers.try_resume_with_pending_key",
        mocker.AsyncMock(return_value=resume_pending),
    )
    mocker.patch(
        "app.routes.servers.verify_password_for_resume",
        mocker.AsyncMock(return_value=resume_bootstrap),
    )
    run = mocker.patch(
        "app.routes.servers.run_exchange_and_verify",
        mocker.AsyncMock(return_value="genpass"),
    )
    mocker.patch("app.routes.servers.install_public_key", mocker.AsyncMock())
    mocker.patch(
        "app.routes.servers.smoke_test_pending_key", mocker.AsyncMock(return_value=True)
    )
    mocker.patch("app.routes.servers.evict_stale_ssh_state", mocker.AsyncMock())
    return run


# --- ownership and state gating --------------------------------------------


async def test_probe_rejects_non_owner(client, db_session, test_user, other_test_user):
    server = await _seed_server(db_session, other_test_user, owner=other_test_user)
    resp = await client.post(f"/api/servers/{server.id}/reregister/probe")
    assert resp.status_code == 404


async def test_endpoints_reject_when_not_mismatch_or_in_progress(
    client, db_session, test_user, mock_ssh
):
    server = await _seed_server(
        db_session, test_user, status="verified", reregistration_state="none"
    )
    probe = await client.post(f"/api/servers/{server.id}/reregister/probe")
    assert probe.status_code == 400
    complete = await client.post(
        f"/api/servers/{server.id}/reregister/complete", json={"password": "p"}
    )
    assert complete.status_code == 400


# --- probe -----------------------------------------------------------------


async def test_probe_captures_pending_key_without_touching_trusted(
    client, db_session, test_user, mock_ssh
):
    server = await _seed_server(db_session, test_user)
    resp = await client.post(f"/api/servers/{server.id}/reregister/probe")
    assert resp.status_code == 200, resp.text
    assert resp.json()["fingerprint_sha256"] == "SHA256:testfingerprintvalue"

    await db_session.refresh(server)
    assert server.pending_host_key == PROBE_KEY
    assert server.host_key == TRUSTED_OLD_KEY  # trusted key untouched until the end
    assert server.reregistration_state == "awaiting_confirmation"
    assert server.status == "key_mismatch"


# --- complete happy path (branch B, generated password) --------------------


async def test_complete_promotes_and_resets_to_verified_unhardened(
    client, db_session, test_user, mock_ssh, mocker
):
    server = await _seed_server(
        db_session, test_user, pending_host_key=PROBE_KEY, with_projects=2
    )
    _stub_engine(mocker)

    resp = await client.post(
        f"/api/servers/{server.id}/reregister/complete", json={"password": "provider"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "verified"
    assert body["reregistration_state"] == "done"

    await db_session.refresh(server)
    # Pending host key promoted to trusted, pending cleared.
    assert server.host_key == PROBE_KEY
    assert server.pending_host_key is None
    assert server.bootstrap_password is None
    # Verified but unhardened, back to root.
    assert server.username == "root"
    assert server.sudo_user_name is None
    assert server.firewall_enabled is False
    assert server.docker_installed is False

    # A fresh, active keypair replaced the old one.
    app_key = await db_session.scalar(
        select(AppSshKey).where(AppSshKey.server_id == server.id)
    )
    assert app_key.is_active is True
    assert app_key.public_key != "ssh-ed25519 AAAAOLDPUB abstract-server-old"

    # A rebuilt box is a blank slate: the projects are purged, not preserved.
    projects = (
        await db_session.scalars(
            select(Project).where(Project.server_id == server.id)
        )
    ).all()
    assert len(projects) == 0


# --- write-ahead -----------------------------------------------------------


async def test_bootstrap_password_written_before_exchange(
    client, db_session, test_user, mock_ssh, mocker
):
    server = await _seed_server(db_session, test_user, pending_host_key=PROBE_KEY)

    captured = {}

    async def fake_exchange(srv_arg, user_password, generated):
        # By the time the exchange runs the write-ahead must be committed: the row
        # already carries an encrypted bootstrap password and the exchanging state.
        captured["state"] = srv_arg.reregistration_state
        captured["bootstrap_set"] = srv_arg.bootstrap_password is not None
        return generated

    mocker.patch("app.routes.servers.try_resume_with_pending_key", mocker.AsyncMock(return_value=False))
    mocker.patch("app.routes.servers.run_exchange_and_verify", side_effect=fake_exchange)
    mocker.patch("app.routes.servers.install_public_key", mocker.AsyncMock())
    mocker.patch("app.routes.servers.smoke_test_pending_key", mocker.AsyncMock(return_value=True))
    mocker.patch("app.routes.servers.evict_stale_ssh_state", mocker.AsyncMock())

    resp = await client.post(
        f"/api/servers/{server.id}/reregister/complete", json={"password": "provider"}
    )
    assert resp.status_code == 200, resp.text
    assert captured["state"] == "exchanging"
    assert captured["bootstrap_set"] is True
    # Cleared once the new key is verified.
    await db_session.refresh(server)
    assert server.bootstrap_password is None


# --- resume / idempotency --------------------------------------------------


async def test_complete_resumes_via_pending_key_without_exchange(
    client, db_session, test_user, mock_ssh, mocker
):
    # A prior attempt got as far as installing a pending keypair. The retry authenticates
    # with it and skips straight to promotion; no exchange happens.
    provider = _provider()
    server = await _seed_server(
        db_session,
        test_user,
        reregistration_state="installing_key",
        pending_host_key=PROBE_KEY,
        bootstrap_password=await provider.encrypt(b"genpass"),
        key_is_active=False,
    )
    run = _stub_engine(mocker, resume_pending=True)

    resp = await client.post(
        f"/api/servers/{server.id}/reregister/complete", json={"password": "provider"}
    )
    assert resp.status_code == 200, resp.text
    run.assert_not_awaited()

    await db_session.refresh(server)
    assert server.reregistration_state == "done"
    assert server.host_key == PROBE_KEY
    app_key = await db_session.scalar(
        select(AppSshKey).where(AppSshKey.server_id == server.id)
    )
    assert app_key.is_active is True


async def test_complete_resumes_via_bootstrap_password_without_exchange(
    client, db_session, test_user, mock_ssh, mocker
):
    # The forced change completed in a prior attempt but the key was never installed.
    provider = _provider()
    server = await _seed_server(
        db_session,
        test_user,
        reregistration_state="exchanging",
        pending_host_key=PROBE_KEY,
        bootstrap_password=await provider.encrypt(b"genpass"),
    )
    run = _stub_engine(mocker, resume_pending=False, resume_bootstrap=True)

    resp = await client.post(
        f"/api/servers/{server.id}/reregister/complete", json={"password": "provider"}
    )
    assert resp.status_code == 200, resp.text
    run.assert_not_awaited()  # bootstrap already works; no new exchange
    await db_session.refresh(server)
    assert server.reregistration_state == "done"


# --- error mapping ---------------------------------------------------------


async def test_complete_maps_auth_failed_and_stays_resumable(
    client, db_session, test_user, mock_ssh, mocker
):
    server = await _seed_server(db_session, test_user, pending_host_key=PROBE_KEY)
    mocker.patch("app.routes.servers.try_resume_with_pending_key", mocker.AsyncMock(return_value=False))
    mocker.patch(
        "app.routes.servers.run_exchange_and_verify",
        mocker.AsyncMock(
            side_effect=ReregistrationError(
                "AUTH_FAILED", "That password did not work.", retryable=False
            )
        ),
    )

    resp = await client.post(
        f"/api/servers/{server.id}/reregister/complete", json={"password": "wrong"}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "AUTH_FAILED"

    # The write-ahead committed before the exchange survives for a resumed retry.
    await db_session.refresh(server)
    assert server.reregistration_state == "exchanging"
    assert server.status == "key_mismatch"


async def test_complete_requires_probe_first(
    client, db_session, test_user, mock_ssh, mocker
):
    # In-progress but no pending host key captured yet.
    server = await _seed_server(
        db_session, test_user, reregistration_state="awaiting_confirmation"
    )
    server.pending_host_key = None
    await db_session.commit()
    resp = await client.post(
        f"/api/servers/{server.id}/reregister/complete", json={"password": "p"}
    )
    assert resp.status_code == 400


# --- project purge / deploy-key revocation ---------------------------------


async def test_complete_revokes_github_deploy_keys_and_purges(
    client, db_session, test_user, mock_ssh, mocker
):
    server = await _seed_server(
        db_session,
        test_user,
        pending_host_key=PROBE_KEY,
        with_projects=1,
        with_deploy_keys=True,
    )
    _stub_engine(mocker)

    github_mock = mocker.MagicMock()
    github_mock.delete_deploy_key = mocker.AsyncMock()
    app.dependency_overrides[get_github_service] = lambda: github_mock
    app.dependency_overrides[get_clerk_client] = lambda: object()
    mocker.patch(
        "app.services.server_reregistration_service.get_github_oauth_token",
        mocker.AsyncMock(return_value="tok"),
    )

    resp = await client.post(
        f"/api/servers/{server.id}/reregister/complete", json={"password": "provider"}
    )
    assert resp.status_code == 200, resp.text

    # The orphaned GitHub deploy key was revoked, and all project state is gone.
    github_mock.delete_deploy_key.assert_awaited_once_with("tok", "acme/proj0", 5000)
    projects = (
        await db_session.scalars(select(Project).where(Project.server_id == server.id))
    ).all()
    assert projects == []
    keys = (await db_session.scalars(select(ProjectDeployKey))).all()
    assert keys == []


async def test_purge_is_best_effort_on_github_failure(
    client, db_session, test_user, mock_ssh, mocker
):
    server = await _seed_server(
        db_session,
        test_user,
        pending_host_key=PROBE_KEY,
        with_projects=1,
        with_deploy_keys=True,
    )
    _stub_engine(mocker)

    app.dependency_overrides[get_github_service] = lambda: mocker.MagicMock()
    app.dependency_overrides[get_clerk_client] = lambda: object()
    # GitHub is unreachable: token fetch blows up. Recovery must still complete.
    mocker.patch(
        "app.services.server_reregistration_service.get_github_oauth_token",
        mocker.AsyncMock(side_effect=Exception("clerk down")),
    )

    resp = await client.post(
        f"/api/servers/{server.id}/reregister/complete", json={"password": "provider"}
    )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(server)
    assert server.reregistration_state == "done"
    projects = (
        await db_session.scalars(select(Project).where(Project.server_id == server.id))
    ).all()
    assert projects == []
