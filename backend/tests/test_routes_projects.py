"""Route level tests for the projects API.

DB backed (TEST_DATABASE_URL). SSH, GitHub, and the Clerk token fetch are
mocked at the boundary; no network, no real VPS, no real GitHub account.
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.clerk import get_clerk_client
from app.deps.services import get_github_service, get_ssh_service
from app.main import app
from app.models import Project, ProjectDeployKey, Server
from app.schemas.projects import GithubRepoResponse
from app.services.github_service import GithubRateLimited
from tests.conftest import requires_db
from tests.project_mocks import make_conn, make_github, result

pytestmark = requires_db

REPO_ID = 777001
REPO_FULL_NAME = "Chukwuemekaeze/anibantsdotNG"

CREATE_BODY = {
    "name": "Anibants",
    "github_repo_id": REPO_ID,
    "github_repo_full_name": REPO_FULL_NAME,
}


@pytest.fixture
def project_env(mocker):
    """Override SSH, GitHub, and Clerk deps; patch the OAuth token fetch.

    Yields (conn, github, set_conn) where set_conn swaps in a differently
    scripted fake connection for a specific test.
    """
    state = {"conn": make_conn(mocker)}
    ssh = mocker.MagicMock()

    async def get_connection(*args, **kwargs):
        return state["conn"]

    ssh.get_connection = mocker.AsyncMock(side_effect=get_connection)
    github = make_github(mocker)

    mocker.patch(
        "app.services.project_service.get_github_oauth_token",
        mocker.AsyncMock(return_value="gho_test_token"),
    )
    mocker.patch(
        "app.routes.projects.get_github_oauth_token",
        mocker.AsyncMock(return_value="gho_test_token"),
    )

    app.dependency_overrides[get_ssh_service] = lambda: ssh
    app.dependency_overrides[get_github_service] = lambda: github
    app.dependency_overrides[get_clerk_client] = lambda: mocker.MagicMock()

    def set_conn(conn):
        state["conn"] = conn

    yield state["conn"], github, set_conn

    app.dependency_overrides.pop(get_ssh_service, None)
    app.dependency_overrides.pop(get_github_service, None)
    app.dependency_overrides.pop(get_clerk_client, None)


async def _make_server(
    db_session,
    user_id,
    *,
    status="verified",
    sudo_user_name="deploy",
    base_packages_installed=True,
    name="web1",
    host="203.0.113.10",
):
    server = Server(
        user_id=user_id,
        name=name,
        host=host,
        port=22,
        username=sudo_user_name or "root",
        status=status,
        verification_source="tofu",
        sudo_user_name=sudo_user_name,
        base_packages_installed=base_packages_installed,
    )
    db_session.add(server)
    await db_session.commit()
    await db_session.refresh(server)
    return server


async def _seed_project(db_session, user_id, server, *, slug="seeded", repo_id=REPO_ID):
    project = Project(
        user_id=user_id,
        server_id=server.id,
        name=slug,
        slug=slug,
        github_repo_full_name=REPO_FULL_NAME,
        github_repo_id=repo_id,
        clone_path="/home/deploy/anibantsdotNG",
        cloned_at=datetime.now(timezone.utc),
    )
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        ProjectDeployKey(
            project_id=project.id,
            github_deploy_key_id=111,
            deploy_key_public_key="ssh-ed25519 AAAA seeded",
            encrypted_deploy_key_private_key=b"ciphertext",
            deploy_key_fingerprint="SHA256:seededfingerprint",
        )
    )
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def test_no_auth_returns_401(unauthenticated_client):
    ac, _clerk = unauthenticated_client
    assert (await ac.get("/api/projects")).status_code == 401
    assert (await ac.get("/api/github/repos")).status_code == 401
    resp = await ac.post(
        "/api/projects", json={**CREATE_BODY, "server_id": str(uuid4())}
    )
    assert resp.status_code == 401


async def test_foreign_server_projects_list_returns_404(
    client, project_env, db_session, other_test_user
):
    foreign = await _make_server(db_session, other_test_user.id)
    resp = await client.get(f"/api/servers/{foreign.id}/projects")
    assert resp.status_code == 404


async def test_create_on_foreign_server_rejected(
    client, project_env, db_session, other_test_user
):
    foreign = await _make_server(db_session, other_test_user.id)
    resp = await client.post(
        "/api/projects", json={**CREATE_BODY, "server_id": str(foreign.id)}
    )
    assert resp.status_code == 400
    assert "not found" in resp.json()["detail"].lower()


@pytest.mark.parametrize(
    "server_kwargs",
    [
        {"status": "pending_verification"},
        {"sudo_user_name": None},
        {"base_packages_installed": False},
    ],
)
async def test_create_precondition_failures_return_400(
    client, project_env, db_session, test_user, server_kwargs
):
    server = await _make_server(db_session, test_user.id, **server_kwargs)
    resp = await client.post(
        "/api/projects", json={**CREATE_BODY, "server_id": str(server.id)}
    )
    assert resp.status_code == 400


async def test_create_400_when_git_missing(
    client, project_env, db_session, test_user, mocker
):
    _conn, _github, set_conn = project_env
    set_conn(make_conn(mocker, {"command -v git": result("no\n")}))
    server = await _make_server(db_session, test_user.id)
    resp = await client.post(
        "/api/projects", json={**CREATE_BODY, "server_id": str(server.id)}
    )
    assert resp.status_code == 400
    assert "git" in resp.json()["detail"]


async def test_create_duplicate_returns_409(
    client, project_env, db_session, test_user
):
    server = await _make_server(db_session, test_user.id)
    await _seed_project(db_session, test_user.id, server)
    resp = await client.post(
        "/api/projects", json={**CREATE_BODY, "server_id": str(server.id)}
    )
    assert resp.status_code == 409


async def test_create_clone_path_occupied_returns_409(
    client, project_env, db_session, test_user, mocker
):
    _conn, _github, set_conn = project_env
    set_conn(make_conn(mocker, {"test -d": result("exists\n")}))
    server = await _make_server(db_session, test_user.id)
    resp = await client.post(
        "/api/projects", json={**CREATE_BODY, "server_id": str(server.id)}
    )
    assert resp.status_code == 409


async def test_create_rate_limited_returns_429(
    client, project_env, db_session, test_user, mocker
):
    _conn, github, _set_conn = project_env
    github.add_deploy_key = mocker.AsyncMock(
        side_effect=GithubRateLimited(datetime.now(timezone.utc))
    )
    server = await _make_server(db_session, test_user.id)
    resp = await client.post(
        "/api/projects", json={**CREATE_BODY, "server_id": str(server.id)}
    )
    assert resp.status_code == 429


async def test_create_happy_path_shape_and_no_secrets(
    client, project_env, db_session, test_user
):
    server = await _make_server(db_session, test_user.id)
    resp = await client.post(
        "/api/projects", json={**CREATE_BODY, "server_id": str(server.id)}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["name"] == "Anibants"
    assert body["slug"] == "anibants"
    assert body["server_id"] == str(server.id)
    assert body["github_repo_full_name"] == REPO_FULL_NAME
    assert body["github_repo_id"] == REPO_ID
    assert body["clone_path"] == "/home/deploy/anibantsdotNG"
    assert body["cloned_at"] is not None
    assert body["deploy_key_fingerprint"].startswith("SHA256:")

    # Never serialized: key material and GitHub's key id.
    for forbidden in (
        "encrypted_deploy_key_private_key",
        "deploy_key_public_key",
        "github_deploy_key_id",
        "user_id",
    ):
        assert forbidden not in body

    # And the row was actually committed.
    row = await db_session.scalar(
        select(Project).where(Project.id == body["id"])
    )
    assert row is not None


async def test_github_repos_proxies_through(client, project_env):
    _conn, github, _set_conn = project_env
    github.list_admin_repos.return_value = [
        GithubRepoResponse(
            id=1,
            full_name="me/repo",
            name="repo",
            pushed_at=datetime.now(timezone.utc),
            private=True,
        )
    ]
    resp = await client.get("/api/github/repos")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["full_name"] == "me/repo"
    github.list_admin_repos.assert_awaited_once_with("gho_test_token")


async def test_list_projects_includes_server_info(
    client, project_env, db_session, test_user
):
    server = await _make_server(
        db_session, test_user.id, name="prod-box", host="203.0.113.99"
    )
    await _seed_project(db_session, test_user.id, server)
    resp = await client.get("/api/projects")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["server_name"] == "prod-box"
    assert body[0]["server_host"] == "203.0.113.99"
    assert body[0]["deploy_key_fingerprint"] == "SHA256:seededfingerprint"


async def test_list_projects_by_server(client, project_env, db_session, test_user):
    server = await _make_server(db_session, test_user.id)
    other_server = await _make_server(
        db_session, test_user.id, name="web2", host="203.0.113.11"
    )
    await _seed_project(db_session, test_user.id, server)
    await _seed_project(
        db_session, test_user.id, other_server, slug="elsewhere", repo_id=REPO_ID + 1
    )
    resp = await client.get(f"/api/servers/{server.id}/projects")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["slug"] == "seeded"
