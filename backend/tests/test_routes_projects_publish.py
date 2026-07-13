"""Route tests for POST /api/projects/{id}/publish. DB backed; DNS and SSH
are mocked at the service boundary."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.deps.services import get_ssh_service
from app.main import app
from tests.conftest import requires_db
from tests.project_mocks import make_conn, result
from tests.run_publish_mocks import make_server, seed_project

pytestmark = requires_db

DOMAIN = "app.example.com"
SERVER_HOST = "203.0.113.10"
BODY = {"domain": DOMAIN, "internal_port": 8080}


@pytest.fixture
def publish_env(mocker):
    """SSH override with a happy-path conn (re-scriptable) and DNS resolving
    to the seeded server host."""
    state = {"conn": make_conn(mocker, {"https://": result("200")})}
    ssh = mocker.MagicMock()

    async def get_connection(*args, **kwargs):
        return state["conn"]

    ssh.get_connection = mocker.AsyncMock(side_effect=get_connection)
    app.dependency_overrides[get_ssh_service] = lambda: ssh

    mocker.patch(
        "app.services.publish_service.resolve_domain_dns",
        mocker.AsyncMock(return_value=[SERVER_HOST]),
    )

    def set_conn(conn):
        state["conn"] = conn

    yield set_conn
    app.dependency_overrides.pop(get_ssh_service, None)


async def seed_running(db_session, user, **kwargs):
    server = await make_server(db_session, user.id, host=SERVER_HOST, **kwargs)
    project = await seed_project(
        db_session, user.id, server, runtime_status="running"
    )
    return server, project


async def test_no_auth_returns_401(unauthenticated_client):
    ac, _clerk = unauthenticated_client
    resp = await ac.post(f"/api/projects/{uuid4()}/publish", json=BODY)
    assert resp.status_code == 401


async def test_other_users_project_returns_404(
    client, publish_env, db_session, other_test_user
):
    _, foreign = await seed_running(db_session, other_test_user)
    resp = await client.post(f"/api/projects/{foreign.id}/publish", json=BODY)
    assert resp.status_code == 404


async def test_publish_happy_path(client, publish_env, db_session, test_user):
    _, project = await seed_running(db_session, test_user)
    resp = await client.post(f"/api/projects/{project.id}/publish", json=BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["domain"] == DOMAIN
    assert body["internal_port"] == 8080
    assert body["published_at"] is not None

    await db_session.refresh(project)
    assert project.domain == DOMAIN


async def test_publish_not_running_returns_400(client, publish_env, db_session, test_user):
    server = await make_server(db_session, test_user.id, host=SERVER_HOST)
    project = await seed_project(
        db_session, test_user.id, server, runtime_status="never_started"
    )
    resp = await client.post(f"/api/projects/{project.id}/publish", json=BODY)
    assert resp.status_code == 400


async def test_publish_already_published_returns_409(
    client, publish_env, db_session, test_user
):
    server = await make_server(db_session, test_user.id, host=SERVER_HOST)
    project = await seed_project(
        db_session,
        test_user.id,
        server,
        runtime_status="running",
        domain="live.example.com",
        internal_port=3000,
        published_at=datetime.now(timezone.utc),
    )
    resp = await client.post(f"/api/projects/{project.id}/publish", json=BODY)
    assert resp.status_code == 409


async def test_publish_nginx_missing_returns_400(
    client, publish_env, db_session, test_user
):
    _, project = await seed_running(db_session, test_user, nginx_installed=False)
    resp = await client.post(f"/api/projects/{project.id}/publish", json=BODY)
    assert resp.status_code == 400
    assert "nginx" in resp.json()["detail"]


async def test_publish_domain_conflict_returns_409(
    client, publish_env, db_session, test_user
):
    server, project = await seed_running(db_session, test_user)
    await seed_project(
        db_session,
        test_user.id,
        server,
        slug="other",
        repo_id=888,
        runtime_status="running",
        domain=DOMAIN,
        internal_port=3000,
        published_at=datetime.now(timezone.utc),
    )
    resp = await client.post(f"/api/projects/{project.id}/publish", json=BODY)
    assert resp.status_code == 409


async def test_publish_dns_mismatch_returns_400(client, db_session, test_user, mocker):
    ssh = mocker.MagicMock()
    ssh.get_connection = mocker.AsyncMock(return_value=make_conn(mocker))
    app.dependency_overrides[get_ssh_service] = lambda: ssh
    mocker.patch(
        "app.services.publish_service.resolve_domain_dns",
        mocker.AsyncMock(return_value=["198.51.100.7"]),
    )
    try:
        _, project = await seed_running(db_session, test_user)
        resp = await client.post(f"/api/projects/{project.id}/publish", json=BODY)
        assert resp.status_code == 400
        assert "DNS" in resp.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_ssh_service, None)


async def test_publish_certbot_failure_returns_502_with_output(
    client, publish_env, db_session, test_user, mocker
):
    _, project = await seed_running(db_session, test_user)
    publish_env(
        make_conn(
            mocker,
            {
                "certbot --nginx": result("", "challenge failed", 1),
                "https://": result("200"),
            },
        )
    )
    resp = await client.post(f"/api/projects/{project.id}/publish", json=BODY)
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "challenge failed" in detail["captured_output"]

    await db_session.refresh(project)
    assert project.domain is None
    assert project.published_at is None


async def test_publish_nothing_listening_returns_502(
    client, publish_env, db_session, test_user, mocker
):
    _, project = await seed_running(db_session, test_user)
    publish_env(
        make_conn(
            mocker,
            {"https://": result("000"), "http://localhost": result("000")},
        )
    )
    resp = await client.post(f"/api/projects/{project.id}/publish", json=BODY)
    assert resp.status_code == 502
    assert "listening" in resp.json()["detail"]["message"]


@pytest.mark.parametrize(
    "bad_domain",
    [
        "APP.EXAMPLE.COM",
        "203.0.113.10",
        "nodomain",
        "bad..labels.com",
        "-leading.example.com",
        "app.localhost",
        "a" * 64 + ".example.com",
    ],
)
async def test_publish_invalid_domain_returns_422(
    client, publish_env, db_session, test_user, bad_domain
):
    _, project = await seed_running(db_session, test_user)
    resp = await client.post(
        f"/api/projects/{project.id}/publish",
        json={"domain": bad_domain, "internal_port": 8080},
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("bad_port", [0, 65536, -1])
async def test_publish_invalid_port_returns_422(
    client, publish_env, db_session, test_user, bad_port
):
    _, project = await seed_running(db_session, test_user)
    resp = await client.post(
        f"/api/projects/{project.id}/publish",
        json={"domain": DOMAIN, "internal_port": bad_port},
    )
    assert resp.status_code == 422
