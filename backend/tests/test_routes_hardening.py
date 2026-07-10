"""Route level tests for the hardening API using a mocked HardeningService.

The SSH service and HardeningService are overridden so no network or real SSH runs.
DB backed (uses TEST_DATABASE_URL) so the atomicity / rollback behavior is exercised
against a real Postgres transaction.
"""

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import get_settings
from app.deps.services import get_hardening_service, get_ssh_service
from app.main import app
from app.models import AppSshKey, Server
from app.services.hardening_service import HardeningError, NginxInstallFailed
from app.services.key_provider import get_key_provider
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def harden_env(mocker):
    """Override get_ssh_service and get_hardening_service with mocks."""
    ssh = mocker.MagicMock()
    ssh.get_connection = mocker.AsyncMock(return_value=mocker.MagicMock())

    hardening = mocker.MagicMock()
    for name in (
        "update_system",
        "install_base_packages",
        "install_docker",
        "install_nginx",
        "create_sudo_user",
        "disable_root_login",
        "configure_firewall",
        "create_swap",
        "reboot",
        "quick_harden",
    ):
        setattr(hardening, name, mocker.AsyncMock())

    app.dependency_overrides[get_ssh_service] = lambda: ssh
    app.dependency_overrides[get_hardening_service] = lambda: hardening
    yield ssh, hardening
    app.dependency_overrides.pop(get_ssh_service, None)
    app.dependency_overrides.pop(get_hardening_service, None)


@pytest_asyncio.fixture
async def app_key(db_session, test_user):
    """Seed the user's app keypair so context-building endpoints can decrypt it."""
    provider = get_key_provider(get_settings())
    encrypted = await provider.encrypt(b"PRIVATEKEYBYTES")
    row = AppSshKey(
        user_id=test_user.id,
        public_key="ssh-ed25519 AAAAAPPKEY app-deploy-x",
        encrypted_private_key=encrypted,
        key_type="ssh-ed25519",
        encryption_key_id=provider.key_id,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


async def _make_server(db_session, user_id, *, status="verified", sudo_user_name=None):
    server = Server(
        user_id=user_id,
        name="web1",
        host="203.0.113.10",
        port=22,
        username="root",
        status=status,
        verification_source="tofu",
        sudo_user_name=sudo_user_name,
    )
    db_session.add(server)
    await db_session.commit()
    await db_session.refresh(server)
    return server


async def test_no_auth_returns_401(unauthenticated_client):
    ac, _clerk = unauthenticated_client
    resp = await ac.post(f"/api/servers/{uuid4()}/harden/update_system")
    assert resp.status_code == 401


async def test_other_users_server_returns_404(
    client, harden_env, db_session, test_user, other_test_user
):
    foreign = await _make_server(db_session, other_test_user.id)
    resp = await client.post(f"/api/servers/{foreign.id}/harden/update_system")
    assert resp.status_code == 404


async def test_rejected_when_not_verified(client, harden_env, db_session, test_user):
    pending = await _make_server(db_session, test_user.id, status="pending_verification")
    resp = await client.post(f"/api/servers/{pending.id}/harden/update_system")
    assert resp.status_code == 400


async def test_disable_root_login_requires_sudo_user(
    client, harden_env, db_session, test_user
):
    server = await _make_server(db_session, test_user.id, sudo_user_name=None)
    resp = await client.post(f"/api/servers/{server.id}/harden/disable_root_login")
    assert resp.status_code == 400


async def test_update_system_happy(client, harden_env, db_session, test_user):
    _ssh, hardening = harden_env
    server = await _make_server(db_session, test_user.id)
    resp = await client.post(f"/api/servers/{server.id}/harden/update_system")
    assert resp.status_code == 200, resp.text
    hardening.update_system.assert_awaited_once()


async def test_install_nginx_no_auth_returns_401(unauthenticated_client):
    ac, _clerk = unauthenticated_client
    resp = await ac.post(f"/api/servers/{uuid4()}/harden/install_nginx")
    assert resp.status_code == 401


async def test_install_nginx_other_users_server_returns_404(
    client, harden_env, db_session, test_user, other_test_user
):
    foreign = await _make_server(db_session, other_test_user.id)
    resp = await client.post(f"/api/servers/{foreign.id}/harden/install_nginx")
    assert resp.status_code == 404


async def test_install_nginx_rejected_when_not_verified(
    client, harden_env, db_session, test_user
):
    pending = await _make_server(db_session, test_user.id, status="pending_verification")
    resp = await client.post(f"/api/servers/{pending.id}/harden/install_nginx")
    assert resp.status_code == 400


async def test_install_nginx_happy(client, harden_env, db_session, test_user):
    _ssh, hardening = harden_env
    server = await _make_server(db_session, test_user.id)

    async def fake_install_nginx(conn, srv, db):
        srv.nginx_installed = True

    hardening.install_nginx.side_effect = fake_install_nginx

    resp = await client.post(f"/api/servers/{server.id}/harden/install_nginx")
    assert resp.status_code == 200, resp.text
    assert resp.json()["nginx_installed"] is True
    hardening.install_nginx.assert_awaited_once()


async def test_install_nginx_failure_returns_output_and_rolls_back(
    client, harden_env, db_session, test_user
):
    _ssh, hardening = harden_env
    server = await _make_server(db_session, test_user.id)
    server_id = server.id

    async def failing_install_nginx(conn, srv, db):
        srv.nginx_installed = True
        raise NginxInstallFailed("$ systemctl is-active nginx\ninactive")

    hardening.install_nginx.side_effect = failing_install_nginx

    resp = await client.post(f"/api/servers/{server_id}/harden/install_nginx")
    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert detail["message"] == "Operation failed"
    assert "inactive" in detail["captured_output"]

    # Transaction rolled back: the flag never persisted.
    row = await db_session.scalar(select(Server).where(Server.id == server_id))
    assert row.nginx_installed is False


async def test_quick_harden_happy_updates_all_fields(
    client, harden_env, db_session, test_user, app_key
):
    _ssh, hardening = harden_env
    server = await _make_server(db_session, test_user.id)

    async def fake_quick_harden(srv, db, ctx, name):
        # Simulate the orchestrator mutating the row (the route commits it).
        srv.docker_installed = True
        srv.nginx_installed = True
        srv.sudo_user_name = name
        srv.username = name
        srv.firewall_enabled = True
        srv.swap_configured = True
        srv.root_login_disabled = True

    hardening.quick_harden.side_effect = fake_quick_harden

    resp = await client.post(
        f"/api/servers/{server.id}/harden/quick_harden",
        json={"sudo_user_name": "deploy"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["docker_installed"] is True
    assert body["nginx_installed"] is True
    assert body["sudo_user_name"] == "deploy"
    assert body["firewall_enabled"] is True
    assert body["swap_configured"] is True
    assert body["root_login_disabled"] is True


async def test_quick_harden_failure_rolls_back_all_state(
    client, harden_env, db_session, test_user, app_key
):
    _ssh, hardening = harden_env
    server = await _make_server(db_session, test_user.id)
    server_id = server.id

    async def failing_quick_harden(srv, db, ctx, name):
        # Mutate a field, then fail partway through.
        srv.docker_installed = True
        srv.sudo_user_name = name
        raise HardeningError("apt-get upgrade failed\nE: dpkg interrupted")

    hardening.quick_harden.side_effect = failing_quick_harden

    resp = await client.post(
        f"/api/servers/{server_id}/harden/quick_harden",
        json={"sudo_user_name": "deploy"},
    )
    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert detail["message"] == "Operation failed"
    assert "dpkg interrupted" in detail["captured_output"]

    # Transaction rolled back: nothing persisted for any field.
    row = await db_session.scalar(select(Server).where(Server.id == server_id))
    assert row.docker_installed is False
    assert row.sudo_user_name is None
    assert row.username == "root"
