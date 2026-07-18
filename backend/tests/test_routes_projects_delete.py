"""Route tests for DELETE /api/projects/{id}.

DB backed (TEST_DATABASE_URL). SSH is a substring-scripted fake, GitHub and the
Clerk OAuth token fetch are mocked at the boundary. The deletion service owns
its own transactions, so these tests assert both the HTTP contract and the DB
side effects (row gone on success, row intact and active_operation cleared on
any failure).
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.clerk import get_clerk_client
from app.deps.services import get_github_service, get_ssh_service
from app.main import app
from app.models import Project
from app.services.github_service import GithubApiError
from app.services.project_deletion_service import _compose_down_command
from tests.conftest import requires_db
from tests.project_mocks import make_conn, make_github, result
from tests.run_publish_mocks import make_server, seed_project

pytestmark = requires_db

STEP_ORDER = [
    "unpublish",
    "remove_docker_artifacts",
    "delete_clone",
    "remove_ssh_config_block",
    "delete_vps_deploy_key_files",
    "revoke_github_deploy_key",
    "delete_db_row",
]


@pytest.fixture
def delete_env(mocker):
    """Override SSH, GitHub, and Clerk deps; patch the OAuth token fetch.

    Yields (set_conn, github) where set_conn swaps in a differently scripted
    fake connection to inject a step failure.
    """
    state = {"conn": make_conn(mocker)}
    ssh = mocker.MagicMock()

    async def get_connection(*args, **kwargs):
        return state["conn"]

    ssh.get_connection = mocker.AsyncMock(side_effect=get_connection)
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

    yield set_conn, github

    app.dependency_overrides.pop(get_ssh_service, None)
    app.dependency_overrides.pop(get_github_service, None)
    app.dependency_overrides.pop(get_clerk_client, None)


async def _published_running_project(db_session, user):
    """A project that has exercised every post-create step: published, running,
    cloned. So a delete runs all seven steps rather than skipping."""
    server = await make_server(db_session, user.id)
    return await seed_project(
        db_session,
        user.id,
        server,
        runtime_status="running",
        domain="app.example.com",
        internal_port=3000,
        published_at=datetime.now(timezone.utc),
    )


def _steps_by_name(body: dict) -> dict[str, dict]:
    return {step["name"]: step for step in body["steps"]}


# -- compose down command shape ---------------------------------------------


def test_compose_down_guards_on_present_compose_file():
    cmd = _compose_down_command("/home/deploy/app", None)
    # -v drops named volumes and --rmi all drops images, so nothing docker
    # related survives the delete.
    assert "down -v --rmi all --remove-orphans" in cmd
    # A missing compose file (e.g. stripped by a prior partial rm) is a no-op,
    # not an error, so retries land cleanly.
    assert "[ -f compose.yaml ]" in cmd
    assert "if [ -d /home/deploy/app ]" in cmd


def test_compose_down_honors_override():
    cmd = _compose_down_command("/home/deploy/app", "docker-compose.prod.yml")
    assert (
        "docker compose -f docker-compose.prod.yml down -v --rmi all --remove-orphans"
        in cmd
    )
    assert "[ -f docker-compose.prod.yml ]" in cmd


# -- auth / ownership --------------------------------------------------------


async def test_no_auth_returns_401(unauthenticated_client):
    ac, _clerk = unauthenticated_client
    assert (await ac.delete(f"/api/projects/{uuid4()}")).status_code == 401


async def test_other_users_project_returns_404(
    client, delete_env, db_session, other_test_user
):
    foreign_server = await make_server(db_session, other_test_user.id)
    foreign = await seed_project(db_session, other_test_user.id, foreign_server)
    assert (await client.delete(f"/api/projects/{foreign.id}")).status_code == 404


# -- happy path --------------------------------------------------------------


async def test_delete_runs_all_steps_and_removes_row(
    client, delete_env, db_session, test_user
):
    _set_conn, github = delete_env
    project = await _published_running_project(db_session, test_user)

    resp = await client.delete(f"/api/projects/{project.id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert [s["name"] for s in body["steps"]] == STEP_ORDER
    assert all(s["status"] == "completed" for s in body["steps"])
    github.delete_deploy_key.assert_awaited_once()

    # Row and its children are gone (cascade).
    assert await db_session.get(Project, project.id) is None
    listing = await client.get("/api/projects")
    assert all(item["id"] != str(project.id) for item in listing.json())


# -- skipped steps -----------------------------------------------------------


async def test_never_published_skips_unpublish(
    client, delete_env, db_session, test_user
):
    server = await make_server(db_session, test_user.id)
    project = await seed_project(
        db_session, test_user.id, server, runtime_status="running"
    )

    body = (await client.delete(f"/api/projects/{project.id}")).json()
    assert _steps_by_name(body)["unpublish"]["status"] == "skipped"
    assert _steps_by_name(body)["remove_docker_artifacts"]["status"] == "completed"


async def test_failed_status_still_removes_docker_artifacts(
    client, delete_env, db_session, test_user
):
    # A failed start leaves containers, volumes, and images behind, so the
    # cleanup must run even though the project is not running.
    server = await make_server(db_session, test_user.id)
    project = await seed_project(
        db_session, test_user.id, server, runtime_status="failed"
    )

    body = (await client.delete(f"/api/projects/{project.id}")).json()
    assert _steps_by_name(body)["remove_docker_artifacts"]["status"] == "completed"


async def test_never_started_with_clone_path_still_removes_docker_artifacts(
    client, delete_env, db_session, test_user
):
    # never_started but cloned: the box may still hold a partially built image
    # or volume, so gate on whether it was cloned, not runtime status.
    server = await make_server(db_session, test_user.id)
    project = await seed_project(db_session, test_user.id, server)  # never_started

    body = (await client.delete(f"/api/projects/{project.id}")).json()
    assert _steps_by_name(body)["remove_docker_artifacts"]["status"] == "completed"


async def test_never_cloned_skips_clone_and_docker_artifacts(
    client, delete_env, db_session, test_user
):
    # clone_path is NOT NULL in the schema (a planned path set at create time),
    # so cloned_at is the real "was it ever cloned" signal.
    server = await make_server(db_session, test_user.id)
    project = await seed_project(db_session, test_user.id, server)
    project.cloned_at = None
    await db_session.commit()

    body = (await client.delete(f"/api/projects/{project.id}")).json()
    steps = _steps_by_name(body)
    assert steps["remove_docker_artifacts"]["status"] == "skipped"
    assert steps["remove_docker_artifacts"]["detail"] == "Project was never cloned."
    assert steps["delete_clone"]["status"] == "skipped"


# -- github 404 treated as success -------------------------------------------


async def test_github_404_is_success(client, delete_env, db_session, test_user):
    _set_conn, github = delete_env
    # delete_deploy_key already maps 404 to success internally, so the mock
    # returning None models "already gone".
    github.delete_deploy_key.return_value = None
    project = await _published_running_project(db_session, test_user)

    body = (await client.delete(f"/api/projects/{project.id}")).json()
    assert _steps_by_name(body)["revoke_github_deploy_key"]["status"] == "completed"
    assert await db_session.get(Project, project.id) is None


# -- each step failing aborts and clears active_operation --------------------


@pytest.mark.parametrize(
    "failed_step,needle",
    [
        ("unpublish", "nginx -t"),
        ("remove_docker_artifacts", "down -v --rmi all --remove-orphans"),
        ("delete_clone", "rm -rf"),
        ("remove_ssh_config_block", "# BEGIN abstract project"),
        ("delete_vps_deploy_key_files", "-deploy.pub"),
    ],
)
async def test_shell_step_failure_aborts(
    client, delete_env, db_session, test_user, mocker, failed_step, needle
):
    set_conn, _github = delete_env
    project = await _published_running_project(db_session, test_user)
    set_conn(make_conn(mocker, {needle: result("", "boom on the box", 1)}))

    resp = await client.delete(f"/api/projects/{project.id}")

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["failed_step"] == failed_step
    assert _steps_by_name(detail)[failed_step]["status"] == "failed"
    assert "boom on the box" in (_steps_by_name(detail)[failed_step]["detail"] or "")

    # Row survives and is retryable.
    survivor = await db_session.get(Project, project.id)
    await db_session.refresh(survivor)
    assert survivor is not None
    assert survivor.active_operation is None


async def test_github_revoke_failure_aborts(
    client, delete_env, db_session, test_user
):
    _set_conn, github = delete_env
    github.delete_deploy_key.side_effect = GithubApiError(500, "github down")
    project = await _published_running_project(db_session, test_user)

    resp = await client.delete(f"/api/projects/{project.id}")

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["failed_step"] == "revoke_github_deploy_key"

    survivor = await db_session.get(Project, project.id)
    await db_session.refresh(survivor)
    assert survivor is not None
    assert survivor.active_operation is None


# -- concurrency guard -------------------------------------------------------


async def test_delete_while_deleting_returns_409(
    client, delete_env, db_session, test_user
):
    project = await _published_running_project(db_session, test_user)
    project.active_operation = "deleting"
    await db_session.commit()

    resp = await client.delete(f"/api/projects/{project.id}")
    assert resp.status_code == 409
    assert await db_session.get(Project, project.id) is not None


async def test_mutations_blocked_while_deleting(
    client, delete_env, db_session, test_user
):
    project = await _published_running_project(db_session, test_user)
    project.active_operation = "deleting"
    await db_session.commit()

    # A representative mutating endpoint is rejected with 409, not 404/500.
    patch_resp = await client.patch(
        f"/api/projects/{project.id}", json={"compose_file_path": None}
    )
    assert patch_resp.status_code == 409

    start_resp = await client.post(f"/api/projects/{project.id}/start")
    assert start_resp.status_code == 409

    # Reads still work so the frontend can render the deleting banner.
    get_resp = await client.get("/api/projects")
    assert get_resp.status_code == 200
