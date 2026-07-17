"""Route and service tests for run history and rollback. DB backed; SSH is a
substring-scripted fake.

Covers: recording a running run on start (and superseding the prior one),
recording a failed run without superseding, the partial unique index invariant,
the list/detail endpoints, the rollback flow and its rejections, and the
per-project operation lock (409 on concurrent operations)."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.deps.services import get_ssh_service
from app.main import app
from app.models import ProjectRun
from tests.conftest import requires_db
from tests.project_mocks import make_conn, result
from tests.run_publish_mocks import (
    CLONE_PATH,
    make_server,
    ps_ndjson,
    seed_project,
    service_entry,
)

pytestmark = requires_db

OLD_SHA = "a" * 40
NEW_SHA = "b" * 40


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


def happy_conn(mocker, *, sha=NEW_SHA, ref="main", build="built", checkout_ok=True):
    """A fake connection that resolves git state, then runs a clean compose up.

    checkout_ok=False makes the rollback checkout command fail so the rebuild is
    never reached.
    """
    overrides = {
        "rev-parse --abbrev-ref HEAD": result(f"{ref}\n"),
        "rev-parse HEAD": result(f"{sha}\n"),
        f"{CLONE_PATH}/compose.yaml": result("yes\n"),
        "up -d --build": result(build),
        "config --services": result("web\n"),
        "ps -a --format json": result(ps_ndjson([service_entry("web")])),
    }
    if not checkout_ok:
        overrides["git checkout"] = result("", "error: pathspec did not match", 1)
    return make_conn(mocker, overrides)


async def seed_run(
    db_session,
    project_id,
    *,
    status="superseded",
    sha=OLD_SHA,
    ref="main",
    build_output="old build",
    created_offset=0,
):
    """Insert a project_runs row with a deterministic created_at so ordering is
    stable within a test."""
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)
    ts = base + timedelta(minutes=created_offset)
    run = ProjectRun(
        project_id=project_id,
        git_commit_sha=sha,
        git_ref=ref,
        status=status,
        started_at=ts,
        finished_at=ts if status != "running" else None,
        build_output=build_output,
        created_at=ts,
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    return run


async def _runs(db_session, project_id):
    rows = await db_session.execute(
        select(ProjectRun)
        .where(ProjectRun.project_id == project_id)
        .order_by(ProjectRun.created_at)
    )
    return list(rows.scalars().all())


# -- start records history ---------------------------------------------------


async def test_start_inserts_running_run_and_supersedes(
    client, runtime_env, db_session, project, mocker
):
    prior = await seed_run(
        db_session, project.id, status="running", sha=OLD_SHA, created_offset=0
    )
    runtime_env(happy_conn(mocker, sha=NEW_SHA))

    resp = await client.post(f"/api/projects/{project.id}/start")
    assert resp.status_code == 200

    runs = await _runs(db_session, project.id)
    await db_session.refresh(prior)
    assert prior.status == "superseded"
    assert prior.finished_at is not None
    running = [r for r in runs if r.status == "running"]
    assert len(running) == 1
    assert running[0].git_commit_sha == NEW_SHA
    assert running[0].git_ref == "main"
    assert running[0].build_output == "built"


async def test_start_failure_inserts_failed_run_without_supersede(
    client, runtime_env, db_session, project, mocker
):
    prior = await seed_run(
        db_session, project.id, status="running", sha=OLD_SHA, created_offset=0
    )
    conn = make_conn(
        mocker,
        {
            "rev-parse --abbrev-ref HEAD": result("main\n"),
            "rev-parse HEAD": result(f"{NEW_SHA}\n"),
            f"{CLONE_PATH}/compose.yaml": result("yes\n"),
            "up -d --build": result("", "build exploded", 1),
        },
    )
    runtime_env(conn)

    resp = await client.post(f"/api/projects/{project.id}/start")
    assert resp.status_code == 502

    await db_session.refresh(prior)
    # A failed start leaves the previous version running.
    assert prior.status == "running"
    runs = await _runs(db_session, project.id)
    failed = [r for r in runs if r.status == "failed"]
    assert len(failed) == 1
    assert failed[0].git_commit_sha == NEW_SHA
    assert "build exploded" in (failed[0].build_output or "")

    await db_session.refresh(project)
    assert project.runtime_status == "failed"
    assert project.active_operation is None


async def test_partial_unique_index_blocks_two_running(db_session, project):
    db_session.add(
        ProjectRun(
            project_id=project.id, git_commit_sha=OLD_SHA, status="running"
        )
    )
    db_session.add(
        ProjectRun(
            project_id=project.id, git_commit_sha=NEW_SHA, status="running"
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


# -- list / detail -----------------------------------------------------------


async def test_list_runs_newest_first_and_respects_limit(
    client, db_session, project
):
    await seed_run(db_session, project.id, status="superseded", sha="1" * 40, created_offset=0)
    await seed_run(db_session, project.id, status="superseded", sha="2" * 40, created_offset=1)
    await seed_run(db_session, project.id, status="running", sha="3" * 40, created_offset=2)

    resp = await client.get(f"/api/projects/{project.id}/runs?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["git_commit_sha"] == "3" * 40
    assert body[1]["git_commit_sha"] == "2" * 40
    # build_output is never in list responses.
    assert "build_output" not in body[0]


async def test_run_detail_returns_build_output(client, db_session, project):
    run = await seed_run(
        db_session, project.id, status="failed", build_output="the transcript"
    )
    resp = await client.get(f"/api/projects/{project.id}/runs/{run.id}")
    assert resp.status_code == 200
    assert resp.json()["build_output"] == "the transcript"


async def test_run_detail_wrong_project_returns_404(
    client, db_session, test_user, project
):
    other_server = await make_server(db_session, test_user.id, name="web2")
    other_project = await seed_project(
        db_session, test_user.id, other_server, slug="other", repo_id=888
    )
    run = await seed_run(db_session, other_project.id, status="failed")
    resp = await client.get(f"/api/projects/{project.id}/runs/{run.id}")
    assert resp.status_code == 404


# -- rollback ----------------------------------------------------------------


async def test_rollback_happy_path(client, runtime_env, db_session, project, mocker):
    target = await seed_run(
        db_session, project.id, status="superseded", sha=OLD_SHA,
        ref="main", created_offset=0,
    )
    current = await seed_run(
        db_session, project.id, status="running", sha=NEW_SHA, created_offset=1
    )
    runtime_env(happy_conn(mocker, sha=NEW_SHA))

    resp = await client.post(
        f"/api/projects/{project.id}/rollback",
        json={"target_run_id": str(target.id)},
    )
    assert resp.status_code == 200
    assert resp.json()["runtime_status"] == "running"

    await db_session.refresh(current)
    assert current.status == "superseded"
    runs = await _runs(db_session, project.id)
    running = [r for r in runs if r.status == "running"]
    assert len(running) == 1
    # The new running row carries the target's SHA and ref for display continuity.
    assert running[0].git_commit_sha == OLD_SHA
    assert running[0].git_ref == "main"
    assert running[0].id not in (target.id, current.id)

    await db_session.refresh(project)
    assert project.active_operation is None


async def test_rollback_target_from_other_project_returns_404(
    client, runtime_env, db_session, test_user, project
):
    other_server = await make_server(db_session, test_user.id, name="web2")
    other_project = await seed_project(
        db_session, test_user.id, other_server, slug="other", repo_id=888
    )
    foreign_run = await seed_run(db_session, other_project.id, status="superseded")
    resp = await client.post(
        f"/api/projects/{project.id}/rollback",
        json={"target_run_id": str(foreign_run.id)},
    )
    assert resp.status_code == 404


async def test_rollback_failed_target_returns_400(
    client, runtime_env, db_session, project
):
    failed = await seed_run(db_session, project.id, status="failed")
    resp = await client.post(
        f"/api/projects/{project.id}/rollback",
        json={"target_run_id": str(failed.id)},
    )
    assert resp.status_code == 400
    assert "failed run" in resp.json()["detail"].lower()


async def test_rollback_current_running_target_returns_400(
    client, runtime_env, db_session, project
):
    running = await seed_run(db_session, project.id, status="running")
    resp = await client.post(
        f"/api/projects/{project.id}/rollback",
        json={"target_run_id": str(running.id)},
    )
    assert resp.status_code == 400
    assert "already at this version" in resp.json()["detail"].lower()


async def test_rollback_rebuild_failure_records_failed_and_keeps_running(
    client, runtime_env, db_session, project, mocker
):
    target = await seed_run(
        db_session, project.id, status="superseded", sha=OLD_SHA, created_offset=0
    )
    current = await seed_run(
        db_session, project.id, status="running", sha=NEW_SHA, created_offset=1
    )
    conn = make_conn(
        mocker,
        {
            f"{CLONE_PATH}/compose.yaml": result("yes\n"),
            "up -d --build": result("", "rebuild exploded", 1),
        },
    )
    runtime_env(conn)

    resp = await client.post(
        f"/api/projects/{project.id}/rollback",
        json={"target_run_id": str(target.id)},
    )
    assert resp.status_code == 502
    assert "rebuild exploded" in resp.json()["detail"]["build_output"]

    # The previous running row is untouched: a failed rollback does not supersede.
    await db_session.refresh(current)
    assert current.status == "running"
    runs = await _runs(db_session, project.id)
    failed = [r for r in runs if r.status == "failed"]
    assert len(failed) == 1
    assert failed[0].git_commit_sha == OLD_SHA

    await db_session.refresh(project)
    assert project.active_operation is None


async def test_rollback_checkout_failure_returns_500_and_clears_lock(
    client, runtime_env, db_session, project, mocker
):
    target = await seed_run(db_session, project.id, status="superseded", sha=OLD_SHA)
    runtime_env(happy_conn(mocker, checkout_ok=False))

    resp = await client.post(
        f"/api/projects/{project.id}/rollback",
        json={"target_run_id": str(target.id)},
    )
    assert resp.status_code == 500
    assert "pathspec" in resp.json()["detail"]["build_output"]

    # Nothing was rebuilt, so no failed run row is recorded.
    runs = await _runs(db_session, project.id)
    assert not [r for r in runs if r.status == "failed"]

    await db_session.refresh(project)
    assert project.active_operation is None


# -- operation lock ----------------------------------------------------------


async def test_operations_return_409_while_one_is_active(
    client, runtime_env, db_session, project
):
    target = await seed_run(db_session, project.id, status="superseded")
    project.active_operation = "starting"
    await db_session.commit()

    start = await client.post(f"/api/projects/{project.id}/start")
    assert start.status_code == 409
    assert start.json()["detail"]["active_operation"] == "starting"

    rollback = await client.post(
        f"/api/projects/{project.id}/rollback",
        json={"target_run_id": str(target.id)},
    )
    assert rollback.status_code == 409

    publish = await client.post(
        f"/api/projects/{project.id}/publish",
        json={"domain": "app.example.com", "internal_port": 3000},
    )
    assert publish.status_code == 409

    delete = await client.delete(f"/api/projects/{project.id}")
    assert delete.status_code == 409


# -- ownership ---------------------------------------------------------------


async def test_other_users_project_returns_404_on_run_endpoints(
    client, runtime_env, db_session, other_test_user
):
    server = await make_server(db_session, other_test_user.id)
    foreign = await seed_project(db_session, other_test_user.id, server)
    foreign_run = await seed_run(db_session, foreign.id, status="superseded")

    assert (await client.get(f"/api/projects/{foreign.id}/runs")).status_code == 404
    assert (
        await client.get(f"/api/projects/{foreign.id}/runs/{foreign_run.id}")
    ).status_code == 404
    assert (
        await client.post(
            f"/api/projects/{foreign.id}/rollback",
            json={"target_run_id": str(foreign_run.id)},
        )
    ).status_code == 404
