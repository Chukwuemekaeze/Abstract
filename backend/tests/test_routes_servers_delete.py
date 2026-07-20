"""Route tests for DELETE /api/servers/{id} and GET .../deletion_preview.

DB backed (TEST_DATABASE_URL). SSH is a substring-scripted fake, GitHub and the
Clerk OAuth token fetch (used transitively by each project deletion) are mocked at
the boundary. The deletion service owns its own transactions, so these tests
assert both the HTTP contract and the DB side effects: server row gone on success,
row intact with every lock cleared on any failure, and the exact teardown order.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.clerk import get_clerk_client
from app.deps.services import get_github_service, get_ssh_service
from app.main import app
from app.models import AppSshKey, Project, Server
from app.services.server_deletion_service import (
    _authorized_key_blob,
    _order_projects,
    _remove_authorized_key_command,
)
from tests.conftest import requires_db
from tests.project_mocks import make_conn, make_github, ran_commands, result
from tests.run_publish_mocks import make_server, seed_project

pytestmark = requires_db

APP_PUBLIC_KEY_BLOB = "AAAAC3NzaC1lZDI1NTE5AAAAITESTBLOB"
APP_PUBLIC_KEY = f"ssh-ed25519 {APP_PUBLIC_KEY_BLOB} abstract-web1"


def make_server_conn(mocker, overrides: dict | None = None):
    """A fake connection whose sshd -T verification reports the directives we set,
    so apply_sshd_directive sees OK. Everything else succeeds by default. Extra
    per-test overrides (for example a failing project command) merge on top and win.
    """
    base = {
        # The verify scripts lowercase the directive; the edit/reload scripts use
        # the capitalized form, so these lowercase needles match only the verify.
        "passwordauthentication": result("passwordauthentication yes\n"),
        "permitrootlogin": result("permitrootlogin yes\n"),
    }
    base.update(overrides or {})
    return make_conn(mocker, base)


@pytest.fixture
def del_srv_env(mocker):
    """Override SSH, GitHub, and Clerk deps; patch the per-project OAuth fetch.

    Yields a namespace with set_conn (swap in a differently scripted connection to
    inject a failure), ssh (to assert evict_connection), and github.
    """
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

    app.dependency_overrides[get_ssh_service] = lambda: ssh
    app.dependency_overrides[get_github_service] = lambda: github
    app.dependency_overrides[get_clerk_client] = lambda: mocker.MagicMock()

    def set_conn(conn):
        state["conn"] = conn

    yield SimpleNamespace(
        set_conn=set_conn,
        get_conn=lambda: state["conn"],
        ssh=ssh,
        github=github,
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


async def _mixed_projects(db_session, user, server):
    """Three projects, one per runtime status, seeded in an order that does NOT
    match the teardown order, so the sort is actually exercised. Returns them keyed
    by status."""
    never = await seed_project(
        db_session, user.id, server, slug="never", repo_id=1, runtime_status="never_started"
    )
    running = await seed_project(
        db_session, user.id, server, slug="running", repo_id=2, runtime_status="running"
    )
    failed = await seed_project(
        db_session, user.id, server, slug="failed", repo_id=3, runtime_status="failed"
    )
    return {"never": never, "running": running, "failed": failed}


def _project_step_order(body: dict) -> list[str]:
    return [
        step["project_name"]
        for step in body["steps"]
        if step["name"] == "delete_project"
    ]


def _steps_by_name(body: dict) -> dict[str, dict]:
    return {step["name"]: step for step in body["steps"]}


# -- pure helpers ------------------------------------------------------------


def test_blob_extraction_with_and_without_comment():
    assert _authorized_key_blob("ssh-ed25519 AAAAB comment") == "AAAAB"
    assert _authorized_key_blob("ssh-ed25519 AAAAB") == "AAAAB"


def test_remove_authorized_key_command_is_guarded():
    cmd = _remove_authorized_key_command("AAAAB")
    # Never overwrites blindly: guarded on the file existing, and `|| true` keeps
    # the step green when the blob was the file's only line (grep -vF exits 1).
    assert '[ -f "$AUTH" ]' in cmd
    assert "|| true" in cmd
    assert "grep -vF AAAAB" in cmd


def test_order_projects_running_then_failed_then_never_started():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def p(status, minutes):
        return SimpleNamespace(
            runtime_status=status, created_at=base + timedelta(minutes=minutes)
        )

    # created_at is scrambled relative to the group order to prove the group key
    # dominates and created_at only breaks ties within a group.
    never = p("never_started", 0)
    failed = p("failed", 1)
    running_late = p("running", 2)
    running_early = p("running", 3)
    running_early.created_at = base - timedelta(minutes=5)

    ordered = _order_projects([never, failed, running_late, running_early])
    assert ordered == [running_early, running_late, failed, never]


# -- auth / ownership --------------------------------------------------------


async def test_delete_no_auth_returns_401(unauthenticated_client):
    ac, _clerk = unauthenticated_client
    assert (await ac.delete(f"/api/servers/{uuid4()}")).status_code == 401


async def test_delete_other_users_server_returns_404(
    client, del_srv_env, db_session, other_test_user
):
    foreign = await make_server(db_session, other_test_user.id)
    assert (await client.delete(f"/api/servers/{foreign.id}")).status_code == 404


async def test_preview_other_users_server_returns_404(
    client, db_session, other_test_user
):
    foreign = await make_server(db_session, other_test_user.id)
    resp = await client.get(f"/api/servers/{foreign.id}/deletion_preview")
    assert resp.status_code == 404


# -- deletion_preview --------------------------------------------------------


async def test_deletion_preview_lists_projects(
    client, db_session, test_user
):
    server = await make_server(db_session, test_user.id)
    await _mixed_projects(db_session, test_user, server)

    resp = await client.get(f"/api/servers/{server.id}/deletion_preview")
    assert resp.status_code == 200
    names = {p["name"] for p in resp.json()["projects"]}
    assert names == {"never", "running", "failed"}


# -- happy path --------------------------------------------------------------


async def test_delete_runs_all_projects_in_order_and_removes_row(
    client, del_srv_env, db_session, test_user
):
    server = await make_server(db_session, test_user.id)
    await _seed_app_key(db_session, server)
    projects = await _mixed_projects(db_session, test_user, server)

    resp = await client.delete(f"/api/servers/{server.id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True

    # Teardown order: running, then failed, then never_started.
    assert _project_step_order(body) == ["running", "failed", "never"]

    steps = _steps_by_name(body)
    assert steps["revoke_sudoers"]["status"] == "completed"
    assert steps["restore_ssh_access"]["status"] == "completed"
    assert steps["remove_authorized_key"]["status"] == "completed"
    assert steps["evict_ssh_connection"]["status"] == "completed"
    assert steps["delete_server_record"]["status"] == "completed"

    # revoke_sudoers MUST come after the sudo-dependent steps: removing the
    # passwordless sudoers file makes any later sudo command prompt for a password
    # and fail over the non-interactive session.
    order = [s["name"] for s in body["steps"]]
    assert order.index("restore_ssh_access") < order.index("revoke_sudoers")

    # The server row and every project are gone.
    assert await db_session.get(Server, server.id) is None
    for project in projects.values():
        assert await db_session.get(Project, project.id) is None

    # VPS steps actually ran on the box.
    commands = ran_commands(del_srv_env.get_conn())
    assert any("/etc/sudoers.d/deploy" in c for c in commands)
    assert any(APP_PUBLIC_KEY_BLOB in c for c in commands)


async def test_cascade_removes_app_ssh_key(
    client, del_srv_env, db_session, test_user
):
    server = await make_server(db_session, test_user.id)
    await _seed_app_key(db_session, server)

    resp = await client.delete(f"/api/servers/{server.id}")
    assert resp.status_code == 200

    key = await db_session.scalar(
        select(AppSshKey).where(AppSshKey.server_id == server.id)
    )
    assert key is None


async def test_ssh_pool_evicted_once(client, del_srv_env, db_session, test_user):
    server = await make_server(db_session, test_user.id)
    await _seed_app_key(db_session, server)

    await client.delete(f"/api/servers/{server.id}")

    del_srv_env.ssh.evict_connection.assert_called_once_with(test_user.id, server.id)


# -- skipped / edge steps ----------------------------------------------------


async def test_sudoers_skipped_when_never_hardened(
    client, del_srv_env, db_session, test_user
):
    # No sudo user means the server was registered but never fully hardened.
    server = await make_server(db_session, test_user.id, sudo_user_name=None)
    await _seed_app_key(db_session, server)

    body = (await client.delete(f"/api/servers/{server.id}")).json()
    steps = _steps_by_name(body)
    assert steps["revoke_sudoers"]["status"] == "skipped"
    assert steps["restore_ssh_access"]["status"] == "completed"
    assert steps["remove_authorized_key"]["status"] == "completed"
    assert await db_session.get(Server, server.id) is None


async def test_no_projects_still_runs_vps_steps(
    client, del_srv_env, db_session, test_user
):
    server = await make_server(db_session, test_user.id)
    await _seed_app_key(db_session, server)

    body = (await client.delete(f"/api/servers/{server.id}")).json()
    assert _project_step_order(body) == []
    steps = _steps_by_name(body)
    assert steps["restore_ssh_access"]["status"] == "completed"
    assert steps["delete_server_record"]["status"] == "completed"
    assert await db_session.get(Server, server.id) is None


async def test_authorized_keys_step_is_idempotent_guarded(
    client, del_srv_env, db_session, test_user, mocker
):
    # A missing authorized_keys file is a shell no-op (the command is guarded on
    # [ -f "$AUTH" ]); the mock returns success and the step completes.
    server = await make_server(db_session, test_user.id)
    await _seed_app_key(db_session, server)

    body = (await client.delete(f"/api/servers/{server.id}")).json()
    step = _steps_by_name(body)["remove_authorized_key"]
    assert step["status"] == "completed"


async def test_no_app_key_skips_authorized_key_removal(
    client, del_srv_env, db_session, test_user
):
    # An edge case: a server row with no app key (never got past probe). The key
    # removal is skipped cleanly rather than crashing on a null key.
    server = await make_server(db_session, test_user.id)

    body = (await client.delete(f"/api/servers/{server.id}")).json()
    assert _steps_by_name(body)["remove_authorized_key"]["status"] == "skipped"
    assert await db_session.get(Server, server.id) is None


# -- a project failing aborts the whole deletion -----------------------------


async def test_project_failure_aborts_and_clears_locks(
    client, del_srv_env, db_session, test_user, mocker
):
    server = await make_server(db_session, test_user.id)
    await _seed_app_key(db_session, server)
    projects = await _mixed_projects(db_session, test_user, server)
    # Snapshot ids up front: the service rolls back on the shared test session,
    # which expires these ORM instances, so reading their attributes afterwards
    # would trigger sync IO. The ids are stable identifiers to re-fetch by.
    server_id = server.id
    pid = {k: p.id for k, p in projects.items()}

    # Fail the second project in teardown order (the 'failed'-status one) at its
    # deploy-key-files step, scoped to that project's slug so the others still pass.
    del_srv_env.set_conn(
        make_server_conn(
            mocker, {"failed-deploy.pub": result("", "boom on the box", 1)}
        )
    )

    resp = await client.delete(f"/api/servers/{server_id}")

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["failed_step"] == "delete_vps_deploy_key_files"
    assert detail["failed_project_name"] == "failed"
    assert detail["failed_project_id"] == str(pid["failed"])

    # Project 1 (running) stays deleted; the server and projects 2 and 3 survive.
    assert await db_session.get(Project, pid["running"]) is None

    survivor_failed = await db_session.get(Project, pid["failed"])
    survivor_never = await db_session.get(Project, pid["never"])
    server_row = await db_session.get(Server, server_id)
    for row in (survivor_failed, survivor_never, server_row):
        assert row is not None

    # Every remaining lock is cleared so the user can retry.
    assert survivor_failed.active_operation is None
    assert survivor_never.active_operation is None
    assert server_row.active_operation is None


# -- concurrency guards ------------------------------------------------------


async def test_delete_while_server_operation_in_flight_returns_409(
    client, del_srv_env, db_session, test_user
):
    server = await make_server(db_session, test_user.id)
    server.active_operation = "deleting"
    await db_session.commit()

    resp = await client.delete(f"/api/servers/{server.id}")
    assert resp.status_code == 409
    assert await db_session.get(Server, server.id) is not None
    # The VPS was never touched.
    del_srv_env.ssh.get_connection.assert_not_awaited()


async def test_delete_with_busy_project_returns_409_and_takes_no_locks(
    client, del_srv_env, db_session, test_user
):
    server = await make_server(db_session, test_user.id)
    await _seed_app_key(db_session, server)
    projects = await _mixed_projects(db_session, test_user, server)

    # One project is already busy with another operation.
    projects["running"].active_operation = "publishing"
    await db_session.commit()
    # Snapshot ids: the rollback inside lock acquisition expires the ORM instances.
    server_id = server.id
    pid = {k: p.id for k, p in projects.items()}

    resp = await client.delete(f"/api/servers/{server_id}")
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "active_operation": "in flight on project running"
    }

    # No VPS work happened and no other lock was acquired.
    del_srv_env.ssh.get_connection.assert_not_awaited()
    server_row = await db_session.get(Server, server_id)
    assert server_row.active_operation is None
    for key in ("failed", "never"):
        row = await db_session.get(Project, pid[key])
        assert row.active_operation is None
    # The originally busy project is untouched.
    busy_row = await db_session.get(Project, pid["running"])
    assert busy_row.active_operation == "publishing"
