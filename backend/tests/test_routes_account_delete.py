"""Route tests for DELETE /api/account.

DB backed (TEST_DATABASE_URL). Account deletion tears down every server through the
real server_deletion_service (SSH is a substring-scripted fake, GitHub and the Clerk
OAuth token fetch are mocked), then deletes the Postgres user row, then deletes the
Clerk user. These tests assert the HTTP contract plus the DB side effects and the
Clerk delete call, including the strict-abort behaviour when a server can't be torn
down and the account is left intact.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.clerk import get_clerk_client
from app.deps.services import get_github_service, get_ssh_service
from app.main import app
from app.models import AppSshKey, Server, User
from tests.conftest import requires_db
from tests.project_mocks import make_conn, make_github, result
from tests.run_publish_mocks import make_server, seed_project

pytestmark = requires_db

APP_PUBLIC_KEY_BLOB = "AAAAC3NzaC1lZDI1NTE5AAAAITESTBLOB"
APP_PUBLIC_KEY = f"ssh-ed25519 {APP_PUBLIC_KEY_BLOB} abstract-web1"


def make_server_conn(mocker, overrides: dict | None = None):
    """A fake connection whose sshd -T verification reports the directives we set,
    so apply_sshd_directive sees OK. Everything else succeeds by default."""
    base = {
        "passwordauthentication": result("passwordauthentication yes\n"),
        "permitrootlogin": result("permitrootlogin yes\n"),
    }
    base.update(overrides or {})
    return make_conn(mocker, base)


@pytest.fixture
def del_acct_env(mocker):
    """Override SSH, GitHub, and Clerk deps; patch the per-project OAuth fetch.

    The Clerk mock's users.delete_async is an AsyncMock so the account-level Clerk
    delete can be asserted. Yields a namespace with set_conn (to inject a teardown
    failure) and clerk (to assert / script the user delete)."""
    state = {"conn": make_server_conn(mocker)}
    ssh = mocker.MagicMock()

    async def get_connection(*args, **kwargs):
        return state["conn"]

    ssh.get_connection = mocker.AsyncMock(side_effect=get_connection)
    ssh.evict_connection = mocker.MagicMock()
    github = make_github(mocker)

    mocker.patch(
        "app.services.project_deletion_service.get_github_oauth_token",
        mocker.AsyncMock(return_value="gho_test_token"),
    )

    clerk = mocker.MagicMock()
    clerk.users.delete_async = mocker.AsyncMock(return_value=None)

    app.dependency_overrides[get_ssh_service] = lambda: ssh
    app.dependency_overrides[get_github_service] = lambda: github
    app.dependency_overrides[get_clerk_client] = lambda: clerk

    def set_conn(conn):
        state["conn"] = conn

    yield SimpleNamespace(
        set_conn=set_conn,
        get_conn=lambda: state["conn"],
        ssh=ssh,
        github=github,
        clerk=clerk,
    )

    app.dependency_overrides.pop(get_ssh_service, None)
    app.dependency_overrides.pop(get_github_service, None)
    app.dependency_overrides.pop(get_clerk_client, None)


async def _seed_app_key(db_session, server, public_key=APP_PUBLIC_KEY):
    key = AppSshKey(
        server_id=server.id,
        public_key=public_key,
        encrypted_private_key=b"ciphertext",
        key_type="ssh-ed25519",
        encryption_key_id="env-v1",
    )
    db_session.add(key)
    await db_session.commit()
    return key


# -- auth --------------------------------------------------------------------


async def test_delete_account_no_auth_returns_401(unauthenticated_client):
    ac, _clerk = unauthenticated_client
    assert (await ac.delete("/api/account")).status_code == 401


# -- happy paths -------------------------------------------------------------


async def test_delete_account_with_no_servers(
    client, del_acct_env, db_session, test_user
):
    uid = test_user.id
    clerk_user_id = test_user.clerk_user_id

    resp = await client.delete("/api/account")

    assert resp.status_code == 200
    assert resp.json() == {"success": True}
    # User row gone, Clerk user deleted exactly once.
    assert await db_session.get(User, uid) is None
    del_acct_env.clerk.users.delete_async.assert_awaited_once_with(
        user_id=clerk_user_id
    )


async def test_delete_account_tears_down_every_server_then_purges(
    client, del_acct_env, db_session, test_user
):
    uid = test_user.id
    clerk_user_id = test_user.clerk_user_id
    server_a = await make_server(db_session, test_user.id, name="web1")
    await _seed_app_key(db_session, server_a)
    server_b = await make_server(
        db_session, test_user.id, host="203.0.113.20", name="web2"
    )
    await _seed_app_key(db_session, server_b)
    a_id, b_id = server_a.id, server_b.id

    resp = await client.delete("/api/account")

    assert resp.status_code == 200
    # Both server rows are gone, the user row is gone, Clerk was called last.
    assert await db_session.get(Server, a_id) is None
    assert await db_session.get(Server, b_id) is None
    assert await db_session.get(User, uid) is None
    del_acct_env.clerk.users.delete_async.assert_awaited_once_with(
        user_id=clerk_user_id
    )


# -- strict abort: a server that can't be torn down blocks everything --------


async def test_server_teardown_failure_aborts_and_keeps_account(
    client, del_acct_env, db_session, test_user, mocker
):
    uid = test_user.id
    server = await make_server(db_session, test_user.id, name="web1")
    await _seed_app_key(db_session, server)
    # A project whose deploy-key teardown fails on the box, which raises
    # ProjectDeletionError -> ServerDeletionError inside delete_server.
    await seed_project(
        db_session, test_user.id, server, slug="boom", repo_id=42
    )
    server_id = server.id

    del_acct_env.set_conn(
        make_server_conn(mocker, {"boom-deploy.pub": result("", "boom on the box", 1)})
    )

    resp = await client.delete("/api/account")

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["blocking_server_id"] == str(server_id)
    assert detail["blocking_server_name"] == "web1"
    assert "web1" in detail["message"]

    # Nothing beyond the failed server is touched: the account and the Clerk user
    # are intact, and the server row survives with its lock cleared for a retry.
    del_acct_env.clerk.users.delete_async.assert_not_awaited()
    assert await db_session.get(User, uid) is not None
    survivor = await db_session.get(Server, server_id)
    assert survivor is not None
    assert survivor.active_operation is None


async def test_busy_server_aborts_with_409(
    client, del_acct_env, db_session, test_user
):
    uid = test_user.id
    server = await make_server(db_session, test_user.id, name="web1")
    server.active_operation = "deleting"
    await db_session.commit()
    server_id = server.id

    resp = await client.delete("/api/account")

    assert resp.status_code == 409
    assert resp.json()["detail"]["blocking_server_name"] == "web1"
    del_acct_env.clerk.users.delete_async.assert_not_awaited()
    assert await db_session.get(User, uid) is not None
    assert await db_session.get(Server, server_id) is not None


# -- Clerk delete failing after the DB is already purged ---------------------


async def test_clerk_delete_failure_returns_502_after_db_purged(
    client, del_acct_env, db_session, test_user
):
    uid = test_user.id
    del_acct_env.clerk.users.delete_async.side_effect = RuntimeError("clerk down")

    resp = await client.delete("/api/account")

    assert resp.status_code == 502
    assert "message" in resp.json()["detail"]
    # The local row is already gone even though Clerk failed.
    assert await db_session.get(User, uid) is None
