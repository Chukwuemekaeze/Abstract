"""Route tests for the env files API.

DB backed. The core invariant tested here: env var VALUES never appear in any
API response once saved."""

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import ProjectEnvVar
from tests.conftest import requires_db
from tests.run_publish_mocks import make_server, seed_project

pytestmark = requires_db

SECRET_VALUE = "super-secret-value-123"


@pytest.fixture
async def project(db_session, test_user):
    server = await make_server(db_session, test_user.id)
    return await seed_project(db_session, test_user.id, server)


def env_files_url(project_id) -> str:
    return f"/api/projects/{project_id}/env-files"


async def test_no_auth_returns_401(unauthenticated_client):
    ac, _clerk = unauthenticated_client
    assert (await ac.get(env_files_url(uuid4()))).status_code == 401
    resp = await ac.post(
        env_files_url(uuid4()), json={"path": ".env", "variables": {}}
    )
    assert resp.status_code == 401


async def test_other_users_project_returns_404(client, db_session, other_test_user):
    foreign_server = await make_server(db_session, other_test_user.id)
    foreign_project = await seed_project(
        db_session, other_test_user.id, foreign_server
    )
    resp = await client.get(env_files_url(foreign_project.id))
    assert resp.status_code == 404


async def test_create_list_detail_flow(client, project):
    resp = await client.post(
        env_files_url(project.id),
        json={"path": "backend/.env", "variables": {"SECRET_KEY": SECRET_VALUE}},
    )
    assert resp.status_code == 200
    created = resp.json()
    assert created["path"] == "backend/.env"
    assert created["keys"] == ["SECRET_KEY"]

    resp = await client.get(env_files_url(project.id))
    assert resp.status_code == 200
    listed = resp.json()
    assert len(listed) == 1
    assert listed[0]["path"] == "backend/.env"
    assert listed[0]["variable_count"] == 1

    resp = await client.get(f"{env_files_url(project.id)}/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["keys"] == ["SECRET_KEY"]


async def test_values_never_appear_in_any_response(client, db_session, project):
    create_resp = await client.post(
        env_files_url(project.id),
        json={"path": ".env", "variables": {"SECRET_KEY": SECRET_VALUE}},
    )
    env_file_id = create_resp.json()["id"]

    list_resp = await client.get(env_files_url(project.id))
    detail_resp = await client.get(f"{env_files_url(project.id)}/{env_file_id}")
    patch_resp = await client.patch(
        f"{env_files_url(project.id)}/{env_file_id}",
        json={"set_variables": {"ANOTHER": SECRET_VALUE}},
    )

    for resp in (create_resp, list_resp, detail_resp, patch_resp):
        assert SECRET_VALUE not in resp.text

    # And the value is not sitting in the DB as plaintext either.
    stored = (await db_session.scalars(select(ProjectEnvVar))).all()
    for var in stored:
        assert SECRET_VALUE.encode() not in var.encrypted_value


async def test_patch_upserts_and_removes(client, project):
    create_resp = await client.post(
        env_files_url(project.id),
        json={"path": ".env", "variables": {"KEEP": "a", "DROP": "b"}},
    )
    env_file_id = create_resp.json()["id"]

    resp = await client.patch(
        f"{env_files_url(project.id)}/{env_file_id}",
        json={"set_variables": {"ADDED": "c"}, "remove_keys": ["DROP"]},
    )
    assert resp.status_code == 200
    assert resp.json()["keys"] == ["ADDED", "KEEP"]


async def test_patch_rename_to_taken_path_returns_409(client, project):
    await client.post(
        env_files_url(project.id), json={"path": ".env", "variables": {}}
    )
    second = await client.post(
        env_files_url(project.id), json={"path": "backend/.env", "variables": {}}
    )
    resp = await client.patch(
        f"{env_files_url(project.id)}/{second.json()['id']}",
        json={"path": ".env"},
    )
    assert resp.status_code == 409


async def test_duplicate_path_returns_409(client, project):
    body = {"path": ".env", "variables": {}}
    assert (await client.post(env_files_url(project.id), json=body)).status_code == 200
    assert (await client.post(env_files_url(project.id), json=body)).status_code == 409


async def test_delete_returns_204_and_removes(client, project):
    created = await client.post(
        env_files_url(project.id), json={"path": ".env", "variables": {}}
    )
    env_file_id = created.json()["id"]
    resp = await client.delete(f"{env_files_url(project.id)}/{env_file_id}")
    assert resp.status_code == 204
    assert (await client.get(env_files_url(project.id))).json() == []


async def test_missing_env_file_returns_404(client, project):
    resp = await client.get(f"{env_files_url(project.id)}/{uuid4()}")
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"path": "/etc/absolute", "variables": {}},
        {"path": "../escape", "variables": {}},
        {"path": ".env", "variables": {"BAD=KEY": "v"}},
        {"path": ".env", "variables": {"KEY": "multi\nline"}},
        {"path": ".env", "variables": {"": "v"}},
    ],
)
async def test_invalid_bodies_return_422(client, project, body):
    resp = await client.post(env_files_url(project.id), json=body)
    assert resp.status_code == 422
