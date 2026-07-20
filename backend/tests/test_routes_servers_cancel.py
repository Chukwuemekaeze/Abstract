"""Route tests for POST /api/servers/{id}/cancel (cancel a pending registration).

DB backed (TEST_DATABASE_URL). SSH is a substring-scripted fake. These assert both
the HTTP contract and the DB side effects: the row is gone once cancellation
completes, and — crucially — the row is left intact when remote key cleanup cannot
complete, so cancel never silently claims a cleanup that did not happen.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.deps.services import get_ssh_service
from app.main import app
from app.models import AppSshKey, Server
from tests.conftest import requires_db
from tests.project_mocks import make_conn, ran_commands, result

pytestmark = requires_db

APP_PUBLIC_KEY_BLOB = "AAAAC3NzaC1lZDI1NTE5AAAAITESTBLOB"
APP_PUBLIC_KEY = f"ssh-ed25519 {APP_PUBLIC_KEY_BLOB} abstract-pending"


def make_server_conn(mocker, overrides: dict | None = None):
    """A fake connection whose sshd -T verification reports PasswordAuthentication
    yes, so apply_sshd_directive (used by restore_ssh_access) sees OK. Everything
    else succeeds by default; per-test overrides merge on top and win."""
    base = {"passwordauthentication": result("passwordauthentication yes\n")}
    base.update(overrides or {})
    return make_conn(mocker, base)


@pytest.fixture
def cancel_env(mocker):
    """Override the SSH service with a scripted fake connection. Yields helpers to
    swap the connection (inject a failure) and to inspect evict_connection."""
    state = {"conn": make_server_conn(mocker)}
    ssh = mocker.MagicMock()

    async def get_connection(*args, **kwargs):
        return state["conn"]

    ssh.get_connection = mocker.AsyncMock(side_effect=get_connection)
    ssh.evict_connection = mocker.MagicMock()

    app.dependency_overrides[get_ssh_service] = lambda: ssh

    def set_conn(conn):
        state["conn"] = conn

    yield SimpleNamespace(
        ssh=ssh, get_conn=lambda: state["conn"], set_conn=set_conn
    )

    app.dependency_overrides.pop(get_ssh_service, None)


async def _pending_server(
    db_session,
    user_id,
    *,
    key_installed: bool,
    password_auth_disabled: bool = False,
    with_key: bool = True,
    name: str = "pending1",
):
    server = Server(
        user_id=user_id,
        name=name,
        host="203.0.113.50",
        port=22,
        username="root",
        host_key=b"hostkeybytes",
        host_key_type="ssh-ed25519",
        fingerprint_sha256="SHA256:pendingfp",
        status="pending_verification",
        verification_source="tofu",
        key_installed=key_installed,
        password_auth_disabled=password_auth_disabled,
    )
    db_session.add(server)
    await db_session.commit()
    await db_session.refresh(server)
    if with_key:
        db_session.add(
            AppSshKey(
                server_id=server.id,
                public_key=APP_PUBLIC_KEY,
                encrypted_private_key=b"ciphertext",
                key_type="ssh-ed25519",
                encryption_key_id="env-v1",
            )
        )
        await db_session.commit()
    return server


def _steps_by_name(body: dict) -> dict[str, dict]:
    return {step["name"]: step for step in body["steps"]}


# -- fast path: the key never landed, so no VPS contact ----------------------


async def test_cancel_never_installed_deletes_without_ssh(
    client, cancel_env, db_session, test_user
):
    server = await _pending_server(
        db_session, test_user.id, key_installed=False, with_key=False
    )
    server_id = server.id

    resp = await client.post(f"/api/servers/{server_id}/cancel")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert _steps_by_name(body)["delete_server_record"]["status"] == "completed"
    # The box was never touched: nothing to clean up.
    cancel_env.ssh.get_connection.assert_not_awaited()
    assert await db_session.get(Server, server_id) is None


# -- partial install: strip the key off the box, then delete -----------------


async def test_cancel_partial_install_cleans_and_deletes(
    client, cancel_env, db_session, test_user
):
    server = await _pending_server(
        db_session, test_user.id, key_installed=True, password_auth_disabled=True
    )
    server_id = server.id

    resp = await client.post(f"/api/servers/{server_id}/cancel")

    assert resp.status_code == 200, resp.text
    steps = _steps_by_name(resp.json())
    assert steps["restore_ssh_access"]["status"] == "completed"
    assert steps["remove_authorized_key"]["status"] == "completed"
    assert steps["evict_ssh_connection"]["status"] == "completed"
    assert steps["delete_server_record"]["status"] == "completed"

    # The key was actually stripped off the box (its blob appears in a run command).
    commands = ran_commands(cancel_env.get_conn())
    assert any(APP_PUBLIC_KEY_BLOB in c for c in commands)

    cancel_env.ssh.evict_connection.assert_called_once_with(test_user.id, server_id)
    assert await db_session.get(Server, server_id) is None


async def test_cancel_skips_restore_when_password_auth_not_disabled(
    client, cancel_env, db_session, test_user
):
    # Install appended the key but never got as far as disabling password auth, so
    # there is nothing to restore.
    server = await _pending_server(
        db_session, test_user.id, key_installed=True, password_auth_disabled=False
    )
    server_id = server.id

    resp = await client.post(f"/api/servers/{server_id}/cancel")

    assert resp.status_code == 200, resp.text
    steps = _steps_by_name(resp.json())
    assert steps["restore_ssh_access"]["status"] == "skipped"
    assert steps["remove_authorized_key"]["status"] == "completed"
    assert await db_session.get(Server, server_id) is None


async def test_cancel_skips_key_removal_when_no_app_key(
    client, cancel_env, db_session, test_user
):
    server = await _pending_server(
        db_session, test_user.id, key_installed=True, with_key=False
    )
    server_id = server.id

    resp = await client.post(f"/api/servers/{server_id}/cancel")

    assert resp.status_code == 200, resp.text
    assert _steps_by_name(resp.json())["remove_authorized_key"]["status"] == "skipped"
    assert await db_session.get(Server, server_id) is None


# -- cleanup cannot complete: keep the row, report the failure ---------------


async def test_cancel_keeps_row_when_key_removal_fails(
    client, cancel_env, db_session, test_user, mocker
):
    server = await _pending_server(
        db_session, test_user.id, key_installed=True, password_auth_disabled=True
    )
    server_id = server.id
    # Fail the key-removal command (it carries the key blob).
    cancel_env.set_conn(
        make_server_conn(mocker, {APP_PUBLIC_KEY_BLOB: result("", "boom on box", 1)})
    )

    resp = await client.post(f"/api/servers/{server_id}/cancel")

    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert detail["failed_step"] == "remove_authorized_key"
    steps = {s["name"]: s for s in detail["steps"]}
    assert steps["remove_authorized_key"]["status"] == "failed"
    # The row survives so the user can retry once the box is reachable.
    assert await db_session.get(Server, server_id) is not None


async def test_cancel_keeps_row_when_connect_fails(
    client, cancel_env, db_session, test_user, mocker
):
    server = await _pending_server(
        db_session, test_user.id, key_installed=True
    )
    server_id = server.id
    cancel_env.ssh.get_connection = mocker.AsyncMock(
        side_effect=OSError("host unreachable")
    )

    resp = await client.post(f"/api/servers/{server_id}/cancel")

    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"]["failed_step"] == "connect_ssh"
    assert await db_session.get(Server, server_id) is not None


# -- guards ------------------------------------------------------------------


async def test_cancel_verified_server_returns_400(
    client, cancel_env, db_session, test_user
):
    server = Server(
        user_id=test_user.id,
        name="done",
        host="203.0.113.60",
        port=22,
        username="root",
        status="verified",
        verification_source="tofu",
    )
    db_session.add(server)
    await db_session.commit()
    await db_session.refresh(server)

    resp = await client.post(f"/api/servers/{server.id}/cancel")
    assert resp.status_code == 400
    assert await db_session.get(Server, server.id) is not None


async def test_cancel_other_users_pending_returns_404(
    client, cancel_env, db_session, other_test_user
):
    foreign = await _pending_server(
        db_session, other_test_user.id, key_installed=False, with_key=False
    )
    resp = await client.post(f"/api/servers/{foreign.id}/cancel")
    assert resp.status_code == 404
    assert await db_session.get(Server, foreign.id) is not None


async def test_cancel_unknown_id_returns_404(client, cancel_env):
    resp = await client.post(f"/api/servers/{uuid4()}/cancel")
    assert resp.status_code == 404


async def test_cancel_no_auth_returns_401(unauthenticated_client):
    ac, _clerk = unauthenticated_client
    resp = await ac.post(f"/api/servers/{uuid4()}/cancel")
    assert resp.status_code == 401
