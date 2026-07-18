"""End to end route tests using httpx ASGITransport, a test DB, and a mocked SSH service."""

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import Server
from tests.conftest import requires_db

pytestmark = requires_db


async def test_probe_install_smoke_flow(client, mock_ssh, db_session, test_user):
    # Probe: registers the server and returns the fingerprint plus app public key.
    probe_resp = await client.post(
        "/api/servers/probe",
        json={"name": "web1", "host": "203.0.113.10", "port": 22, "username": "root"},
    )
    assert probe_resp.status_code == 200, probe_resp.text
    body = probe_resp.json()
    server_id = body["server_id"]
    assert body["fingerprint_sha256"] == "SHA256:testfingerprintvalue"
    assert body["app_public_key"].startswith("ssh-ed25519 ")
    mock_ssh.probe.assert_awaited_once()

    # The created row is pending_verification and owned by the dev user.
    row = await db_session.scalar(select(Server).where(Server.id == server_id))
    assert row is not None
    assert row.status == "pending_verification"
    assert row.user_id == test_user.id

    # Install key: verifies and flips status to verified.
    install_resp = await client.post(
        f"/api/servers/{server_id}/install_key",
        json={"password": "hunter2", "disable_password_auth": True},
    )
    assert install_resp.status_code == 200, install_resp.text
    installed = install_resp.json()
    assert installed["status"] == "verified"
    assert installed["password_auth_disabled"] is True
    assert "host_key" not in installed  # sensitive field never serialized
    mock_ssh.install_key.assert_awaited_once()

    # Smoke test: runs the hello world command over the pooled connection.
    smoke_resp = await client.post(f"/api/servers/{server_id}/smoke_test")
    assert smoke_resp.status_code == 200, smoke_resp.text
    smoke = smoke_resp.json()
    assert smoke["exit_status"] == 0
    assert "hello from Abstract" in smoke["stdout"]
    mock_ssh.run_command.assert_awaited_once()


async def test_install_key_uses_the_servers_own_key(
    client, mock_ssh, db_session, test_user
):
    # Register two servers. Each probe generates a distinct per-server keypair.
    probe_a = (
        await client.post(
            "/api/servers/probe",
            json={"name": "a", "host": "203.0.113.10"},
        )
    ).json()
    probe_b = (
        await client.post(
            "/api/servers/probe",
            json={"name": "b", "host": "203.0.113.11"},
        )
    ).json()
    assert probe_a["app_public_key"] != probe_b["app_public_key"]

    # Installing server B must use server B's key, not server A's.
    resp = await client.post(
        f"/api/servers/{probe_b['server_id']}/install_key",
        json={"password": "hunter2", "disable_password_auth": False},
    )
    assert resp.status_code == 200, resp.text
    _args, kwargs = mock_ssh.install_key.call_args
    assert kwargs["app_public_key"] == probe_b["app_public_key"]
    assert kwargs["app_public_key"] != probe_a["app_public_key"]


async def test_list_returns_only_current_user_servers(client, mock_ssh, db_session, test_user):
    await client.post(
        "/api/servers/probe",
        json={"name": "mine", "host": "203.0.113.11"},
    )
    list_resp = await client.get("/api/servers")
    assert list_resp.status_code == 200
    servers = list_resp.json()
    assert len(servers) == 1
    assert servers[0]["name"] == "mine"


async def test_other_users_server_returns_404(client, db_session, test_user, other_test_user):
    # A server owned by someone else must not be reachable by the dev user.
    foreign = Server(
        user_id=other_test_user.id,
        name="foreign",
        host="203.0.113.99",
        port=22,
        username="root",
        status="verified",
        verification_source="tofu",
    )
    db_session.add(foreign)
    await db_session.commit()
    await db_session.refresh(foreign)

    resp = await client.get(f"/api/servers/{foreign.id}")
    assert resp.status_code == 404


async def test_smoke_test_on_unknown_id_returns_404(client):
    resp = await client.post(f"/api/servers/{uuid4()}/smoke_test")
    assert resp.status_code == 404


async def test_probe_missing_fields_returns_422(client):
    resp = await client.post("/api/servers/probe", json={"host": "203.0.113.10"})
    assert resp.status_code == 422


async def test_install_key_rejected_when_not_pending(client, mock_ssh, db_session, test_user):
    server = Server(
        user_id=test_user.id,
        name="already",
        host="203.0.113.12",
        port=22,
        username="root",
        status="verified",
        verification_source="tofu",
    )
    db_session.add(server)
    await db_session.commit()
    await db_session.refresh(server)

    resp = await client.post(
        f"/api/servers/{server.id}/install_key",
        json={"password": "hunter2"},
    )
    assert resp.status_code == 400


async def test_no_auth_header_returns_401(unauthenticated_client):
    # No token: Clerk reports signed out, so the auth dependency rejects with 401.
    ac, _clerk = unauthenticated_client
    resp = await ac.get("/api/servers")
    assert resp.status_code == 401


async def test_invalid_token_returns_401(unauthenticated_client, mocker):
    # A token that fails verification: Clerk returns is_signed_in False.
    ac, clerk = unauthenticated_client
    clerk.authenticate_request.return_value = mocker.MagicMock(
        is_signed_in=False, payload=None
    )
    resp = await ac.get(
        "/api/servers", headers={"Authorization": "Bearer not-a-real-jwt"}
    )
    assert resp.status_code == 401


async def test_malformed_token_returns_401(unauthenticated_client, mocker):
    # Verified but missing the session id claim: rejected as malformed.
    ac, clerk = unauthenticated_client
    clerk.authenticate_request.return_value = mocker.MagicMock(
        is_signed_in=True, payload={"sub": "user_x"}
    )
    resp = await ac.get(
        "/api/servers", headers={"Authorization": "Bearer partial-token"}
    )
    assert resp.status_code == 401
