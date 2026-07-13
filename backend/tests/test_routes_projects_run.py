"""Route tests for start, refresh_status, detected_ports, and the project
settings PATCH. DB backed; SSH is a scripted fake."""

from uuid import uuid4

import pytest

from app.deps.services import get_ssh_service
from app.main import app
from tests.conftest import requires_db
from tests.project_mocks import make_conn, result
from tests.run_publish_mocks import (
    CLONE_PATH,
    make_server,
    ps_ndjson,
    publisher,
    seed_project,
    service_entry,
)

pytestmark = requires_db


@pytest.fixture
def runtime_env(mocker):
    """SSH service override whose connection can be re-scripted per test."""
    state = {"conn": make_conn(mocker)}
    ssh = mocker.MagicMock()

    async def get_connection(*args, **kwargs):
        return state["conn"]

    ssh.get_connection = mocker.AsyncMock(side_effect=get_connection)
    app.dependency_overrides[get_ssh_service] = lambda: ssh

    def set_conn(conn):
        state["conn"] = conn

    yield set_conn
    app.dependency_overrides.pop(get_ssh_service, None)


@pytest.fixture
async def project(db_session, test_user):
    server = await make_server(db_session, test_user.id)
    return await seed_project(db_session, test_user.id, server)


def happy_start_conn(mocker):
    return make_conn(
        mocker,
        {
            f"{CLONE_PATH}/compose.yaml": result("yes\n"),
            "up -d --build": result("built"),
            "config --services": result("web\n"),
            "ps -a --format json": result(ps_ndjson([service_entry("web")])),
        },
    )


async def test_no_auth_returns_401(unauthenticated_client):
    ac, _clerk = unauthenticated_client
    assert (await ac.post(f"/api/projects/{uuid4()}/start")).status_code == 401
    assert (await ac.get(f"/api/projects/{uuid4()}/detected_ports")).status_code == 401
    assert (
        await ac.patch(f"/api/projects/{uuid4()}", json={"compose_file_path": None})
    ).status_code == 401


async def test_other_users_project_returns_404(
    client, runtime_env, db_session, other_test_user
):
    foreign_server = await make_server(db_session, other_test_user.id)
    foreign = await seed_project(db_session, other_test_user.id, foreign_server)
    assert (await client.post(f"/api/projects/{foreign.id}/start")).status_code == 404


async def test_start_happy_path(client, runtime_env, db_session, project, mocker):
    runtime_env(happy_start_conn(mocker))
    resp = await client.post(f"/api/projects/{project.id}/start")
    assert resp.status_code == 200
    body = resp.json()
    assert body["runtime_status"] == "running"
    assert body["started_at"] is not None
    assert body["build_output"] == "built"

    await db_session.refresh(project)
    assert project.runtime_status == "running"


async def test_start_failure_returns_502_and_persists_failed(
    client, runtime_env, db_session, project, mocker
):
    runtime_env(
        make_conn(
            mocker,
            {
                f"{CLONE_PATH}/compose.yaml": result("yes\n"),
                "up -d --build": result("", "build exploded", 1),
            },
        )
    )
    resp = await client.post(f"/api/projects/{project.id}/start")
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "captured_output" in detail
    assert "build exploded" in detail["captured_output"]
    # build_output carries the same transcript under the success-path name.
    assert detail["build_output"]
    assert "build exploded" in detail["build_output"]

    await db_session.refresh(project)
    assert project.runtime_status == "failed"
    assert project.started_at is None


async def test_start_without_compose_file_returns_400(
    client, runtime_env, db_session, project, mocker
):
    runtime_env(make_conn(mocker))
    resp = await client.post(f"/api/projects/{project.id}/start")
    assert resp.status_code == 400
    assert "compose" in resp.json()["detail"].lower()

    await db_session.refresh(project)
    assert project.runtime_status == "never_started"


async def test_detected_ports_requires_running(client, runtime_env, project):
    resp = await client.get(f"/api/projects/{project.id}/detected_ports")
    assert resp.status_code == 400
    assert "not running" in resp.json()["detail"]


async def test_detected_ports_happy_path(
    client, runtime_env, db_session, test_user, mocker
):
    server = await make_server(db_session, test_user.id)
    project = await seed_project(
        db_session, test_user.id, server, runtime_status="running"
    )
    services = [
        service_entry("web", publishers=[publisher(8080, 8000)]),
        service_entry("db", publishers=[publisher(5432, 5432)]),
    ]
    runtime_env(
        make_conn(
            mocker,
            {
                f"{CLONE_PATH}/compose.yaml": result("yes\n"),
                "ps -a --format json": result(ps_ndjson(services)),
            },
        )
    )
    resp = await client.get(f"/api/projects/{project.id}/detected_ports")
    assert resp.status_code == 200
    assert resp.json() == [
        {"service": "db", "host_port": 5432, "container_port": 5432, "is_dangerous": True},
        {"service": "web", "host_port": 8080, "container_port": 8000, "is_dangerous": False},
    ]


async def test_refresh_status_updates_project(
    client, runtime_env, db_session, test_user, mocker
):
    server = await make_server(db_session, test_user.id)
    project = await seed_project(
        db_session, test_user.id, server, runtime_status="running"
    )
    services = [service_entry("web", state="exited")]
    runtime_env(
        make_conn(
            mocker,
            {
                f"{CLONE_PATH}/compose.yaml": result("yes\n"),
                "ps -a --format json": result(ps_ndjson(services)),
            },
        )
    )
    resp = await client.post(f"/api/projects/{project.id}/refresh_status")
    assert resp.status_code == 200
    assert resp.json()["runtime_status"] == "failed"


async def test_patch_compose_file_path_set_and_clear(client, db_session, project):
    resp = await client.patch(
        f"/api/projects/{project.id}",
        json={"compose_file_path": "deploy/compose.prod.yml"},
    )
    assert resp.status_code == 200
    assert resp.json()["compose_file_path"] == "deploy/compose.prod.yml"

    resp = await client.patch(
        f"/api/projects/{project.id}", json={"compose_file_path": None}
    )
    assert resp.status_code == 200
    assert resp.json()["compose_file_path"] is None


async def test_patch_compose_file_path_rejects_escaping_path(client, project):
    resp = await client.patch(
        f"/api/projects/{project.id}", json={"compose_file_path": "../outside.yml"}
    )
    assert resp.status_code == 422


# -- POST /pull -----------------------------------------------------------------


async def test_pull_no_auth_returns_401(unauthenticated_client):
    ac, _clerk = unauthenticated_client
    assert (await ac.post(f"/api/projects/{uuid4()}/pull")).status_code == 401


async def test_pull_other_users_project_returns_404(
    client, runtime_env, db_session, other_test_user
):
    foreign_server = await make_server(db_session, other_test_user.id)
    foreign = await seed_project(db_session, other_test_user.id, foreign_server)
    assert (await client.post(f"/api/projects/{foreign.id}/pull")).status_code == 404


async def test_pull_happy_path(client, runtime_env, db_session, project, mocker):
    old_updated_at = project.updated_at
    runtime_env(
        make_conn(
            mocker,
            {"rev-parse --short HEAD": [result("abc1234\n"), result("def5678\n")]},
        )
    )
    resp = await client.post(f"/api/projects/{project.id}/pull")
    assert resp.status_code == 200
    body = resp.json()
    assert body["before_commit"] == "abc1234"
    assert body["after_commit"] == "def5678"
    assert body["already_up_to_date"] is False

    await db_session.refresh(project)
    assert project.updated_at > old_updated_at


async def test_pull_fetch_failure_returns_502(
    client, runtime_env, db_session, project, mocker
):
    old_updated_at = project.updated_at
    runtime_env(
        make_conn(
            mocker,
            {
                "rev-parse --short HEAD": result("abc1234\n"),
                "git fetch": result("", "fatal: repository not found", 1),
            },
        )
    )
    resp = await client.post(f"/api/projects/{project.id}/pull")
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["message"] == "Pulling the latest code failed"
    assert "repository not found" in detail["captured_output"]

    await db_session.refresh(project)
    assert project.updated_at == old_updated_at


async def test_pull_never_cloned_returns_409(client, runtime_env, db_session, project):
    project.cloned_at = None
    await db_session.commit()
    resp = await client.post(f"/api/projects/{project.id}/pull")
    assert resp.status_code == 409
    assert "clone" in resp.json()["detail"].lower()
