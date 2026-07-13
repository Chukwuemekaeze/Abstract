"""Run service tests: compose file detection, env file writes, compose up,
container verification, and port detection. SSH is faked; the start_project
tests are DB backed."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.config import get_settings
from app.services.env_file_service import create_env_file
from app.services.key_provider import get_key_provider
from app.services import run_service
from app.services.run_service import (
    BUILD_OUTPUT_MAX_BYTES,
    ComposeConfigInvalid,
    ComposeFileNotFound,
    ComposeUpFailed,
    ContainerNotRunning,
    EnvFileKeyCollision,
    detect_compose_file,
    get_detected_ports,
    refresh_status,
    run_compose_up,
    start_project,
    truncate_build_output,
    verify_containers_running,
    write_env_files_to_vps,
)
from tests.conftest import requires_db
from tests.project_mocks import make_conn, ran_commands, result
from tests.run_publish_mocks import (
    CLONE_PATH,
    make_server,
    ps_array,
    ps_ndjson,
    publisher,
    seed_project,
    service_entry,
)


def fake_project(**overrides):
    defaults = dict(
        id=uuid4(),
        clone_path=CLONE_PATH,
        compose_file_path=None,
        runtime_status="never_started",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# -- detect_compose_file ------------------------------------------------------


async def test_detect_returns_first_candidate(mocker):
    conn = make_conn(mocker, {f"{CLONE_PATH}/compose.yaml": result("yes\n")})
    assert await detect_compose_file(conn, CLONE_PATH, None) == "compose.yaml"


async def test_detect_iterates_candidates_in_order(mocker):
    conn = make_conn(mocker, {f"{CLONE_PATH}/docker-compose.yml": result("yes\n")})
    assert await detect_compose_file(conn, CLONE_PATH, None) == "docker-compose.yml"


async def test_detect_honors_override(mocker):
    conn = make_conn(
        mocker, {f"{CLONE_PATH}/deploy/compose.prod.yml": result("yes\n")}
    )
    found = await detect_compose_file(conn, CLONE_PATH, "deploy/compose.prod.yml")
    assert found == "deploy/compose.prod.yml"
    # The override short-circuits: the default candidates are never probed.
    assert all("compose.yaml" not in c for c in ran_commands(conn))


async def test_detect_missing_override_raises_and_names_it(mocker):
    conn = make_conn(mocker)
    with pytest.raises(ComposeFileNotFound) as exc_info:
        await detect_compose_file(conn, CLONE_PATH, "deploy/compose.prod.yml")
    assert "deploy/compose.prod.yml" in str(exc_info.value)


async def test_detect_none_found_raises(mocker):
    conn = make_conn(mocker)
    with pytest.raises(ComposeFileNotFound):
        await detect_compose_file(conn, CLONE_PATH, None)


# -- write_env_files_to_vps ---------------------------------------------------


async def test_key_collision_across_files_raises_before_any_write(mocker):
    conn = make_conn(mocker)
    with pytest.raises(EnvFileKeyCollision) as exc_info:
        await write_env_files_to_vps(
            conn,
            fake_project(),
            {"backend/.env": {"PORT": "8000"}, "frontend/.env": {"PORT": "3000"}},
        )
    assert exc_info.value.key == "PORT"
    assert sorted(exc_info.value.files) == ["backend/.env", "frontend/.env"]
    assert conn.sftp.opened_paths == []


async def test_same_key_same_value_is_not_a_collision(mocker):
    conn = make_conn(mocker)
    await write_env_files_to_vps(
        conn,
        fake_project(),
        {"backend/.env": {"PORT": "8000"}, "frontend/.env": {"PORT": "8000"}},
    )
    assert f"{CLONE_PATH}/.env" in conn.sftp.opened_paths


async def test_merged_root_env_written_when_user_has_none(mocker):
    conn = make_conn(mocker)
    await write_env_files_to_vps(
        conn,
        fake_project(),
        {"backend/.env": {"A": "1"}, "frontend/.env": {"B": "2"}},
    )
    assert set(conn.sftp.opened_paths) == {
        f"{CLONE_PATH}/backend/.env",
        f"{CLONE_PATH}/frontend/.env",
        f"{CLONE_PATH}/.env",
    }
    # The merged file carries the union.
    assert b"A=1\n" in conn.sftp.file.written
    assert b"B=2\n" in conn.sftp.file.written


async def test_merged_root_env_not_written_when_user_has_one(mocker):
    conn = make_conn(mocker)
    await write_env_files_to_vps(
        conn, fake_project(), {".env": {"A": "1"}, "backend/.env": {"B": "2"}}
    )
    assert conn.sftp.opened_paths.count(f"{CLONE_PATH}/.env") == 1


async def test_env_files_chmodded_600(mocker):
    conn = make_conn(mocker)
    await write_env_files_to_vps(conn, fake_project(), {".env": {"A": "1"}})
    assert any(
        f"chmod 600 {CLONE_PATH}/.env" in c for c in ran_commands(conn)
    )


# -- run_compose_up -----------------------------------------------------------


async def test_compose_up_without_f_flag_for_default_names(mocker):
    conn = make_conn(mocker, {"up -d --build": result("built ok")})
    exit_code, output = await run_compose_up(conn, CLONE_PATH, "compose.yaml")
    assert (exit_code, output) == (0, "built ok")
    up_command = next(c for c in ran_commands(conn) if "up -d --build" in c)
    assert " -f " not in up_command
    # Orphan containers from removed services are cleaned up on every build.
    assert "--remove-orphans" in up_command


async def test_compose_up_with_f_flag_for_custom_file(mocker):
    conn = make_conn(mocker, {"up -d --build": result("built ok")})
    await run_compose_up(conn, CLONE_PATH, "deploy/compose.prod.yml")
    up_command = next(c for c in ran_commands(conn) if "up -d --build" in c)
    assert "-f deploy/compose.prod.yml" in up_command


async def test_compose_up_nonzero_exit_returns_code_and_output(mocker):
    # The raise moved to start_project; run_compose_up reports the exit code
    # and the transcript so the caller decides.
    conn = make_conn(
        mocker, {"up -d --build": result("", "build exploded", 1)}
    )
    exit_code, output = await run_compose_up(conn, CLONE_PATH, "compose.yaml")
    assert exit_code == 1
    assert "build exploded" in output


async def test_compose_up_folds_stderr_into_stdout_channel(mocker):
    # asyncssh folds stderr into stdout in prod; assert we ask it to.
    conn = make_conn(mocker, {"up -d --build": result("built ok")})
    await run_compose_up(conn, CLONE_PATH, "compose.yaml")
    up_call = next(
        call for call in conn.run.await_args_list if "up -d --build" in call.args[0]
    )
    assert up_call.kwargs.get("stderr") is not None


async def test_compose_up_precheck_failure_mentions_docker_group(mocker):
    conn = make_conn(
        mocker, {"docker compose version": result("", "command not found", 127)}
    )
    with pytest.raises(ComposeUpFailed) as exc_info:
        await run_compose_up(conn, CLONE_PATH, "compose.yaml")
    assert "docker group" in exc_info.value.captured_output


# -- verify_containers_running ------------------------------------------------


async def test_verify_all_running_ndjson(mocker):
    services = [service_entry("web"), service_entry("worker")]
    conn = make_conn(
        mocker,
        {
            "config --services": result("web\nworker\n"),
            "ps -a --format json": result(ps_ndjson(services)),
        },
    )
    ok, output = await verify_containers_running(conn, CLONE_PATH, "compose.yaml")
    assert ok is True
    assert output is None


async def test_verify_all_running_array_format(mocker):
    services = [service_entry("web")]
    conn = make_conn(
        mocker,
        {
            "config --services": result("web\n"),
            "ps -a --format json": result(ps_array(services)),
        },
    )
    ok, _ = await verify_containers_running(conn, CLONE_PATH, "compose.yaml")
    assert ok is True


async def test_verify_collects_logs_for_stopped_service(mocker):
    services = [service_entry("web"), service_entry("worker", state="exited")]
    conn = make_conn(
        mocker,
        {
            "config --services": result("web\nworker\n"),
            "ps -a --format json": result(ps_ndjson(services)),
            "logs --tail 50": result("worker crash log"),
        },
    )
    ok, output = await verify_containers_running(conn, CLONE_PATH, "compose.yaml")
    assert ok is False
    assert "worker" in output
    assert "worker crash log" in output
    logs_command = next(c for c in ran_commands(conn) if "logs --tail 50" in c)
    assert "worker" in logs_command


async def test_verify_ignores_orphan_not_in_compose_file(mocker):
    # nginx was removed from the compose file but its container lingers as an
    # orphan in state "created"; it must not fail verification.
    services = [
        service_entry("backend"),
        service_entry("frontend"),
        service_entry("nginx", state="created"),
    ]
    conn = make_conn(
        mocker,
        {
            "config --services": result("backend\nfrontend\n"),
            "ps -a --format json": result(ps_ndjson(services)),
        },
    )
    ok, output = await verify_containers_running(conn, CLONE_PATH, "compose.yaml")
    assert ok is True
    assert output is None
    # And logs are never fetched for the orphan.
    assert not any("logs --tail 50" in c for c in ran_commands(conn))


async def test_verify_missing_defined_service_is_failure(mocker):
    services = [service_entry("backend")]
    conn = make_conn(
        mocker,
        {
            "config --services": result("backend\nfrontend\n"),
            "ps -a --format json": result(ps_ndjson(services)),
        },
    )
    ok, output = await verify_containers_running(conn, CLONE_PATH, "compose.yaml")
    assert ok is False
    assert "frontend" in output
    assert "defined in compose but was not created" in output


async def test_verify_log_fetch_is_scoped_to_defined_services(mocker):
    # frontend really failed; nginx is a stale orphan also in ps output. Logs
    # must be fetched for frontend only, never the orphan.
    services = [
        service_entry("backend"),
        service_entry("nginx", state="created"),
        service_entry("frontend", state="exited"),
    ]
    conn = make_conn(
        mocker,
        {
            "config --services": result("backend\nfrontend\n"),
            "ps -a --format json": result(ps_ndjson(services)),
            "logs --tail 50": result("frontend crashed"),
        },
    )
    ok, output = await verify_containers_running(conn, CLONE_PATH, "compose.yaml")
    assert ok is False
    assert "frontend crashed" in output
    log_commands = [c for c in ran_commands(conn) if "logs --tail 50" in c]
    assert log_commands and all("nginx" not in c for c in log_commands)


async def test_verify_config_failure_raises_compose_config_invalid(mocker):
    conn = make_conn(
        mocker,
        {"config --services": result("", "yaml: line 3: mapping error", 1)},
    )
    with pytest.raises(ComposeConfigInvalid) as exc_info:
        await verify_containers_running(conn, CLONE_PATH, "compose.yaml")
    assert "mapping error" in exc_info.value.captured_output


# -- get_detected_ports -------------------------------------------------------


async def test_detected_ports_parses_and_flags(mocker):
    services = [
        service_entry(
            "web",
            publishers=[
                publisher(8080, 8000),
                publisher(9999, 9999, url="127.0.0.1"),  # not host-published
            ],
        ),
        service_entry("db", publishers=[publisher(5432, 5432)]),
    ]
    conn = make_conn(
        mocker,
        {
            f"{CLONE_PATH}/compose.yaml": result("yes\n"),
            "ps -a --format json": result(ps_ndjson(services)),
        },
    )
    ports = await get_detected_ports(conn=conn, project=fake_project())
    assert [(p.service, p.host_port, p.container_port, p.is_dangerous) for p in ports] == [
        ("db", 5432, 5432, True),
        ("web", 8080, 8000, False),
    ]


# -- refresh_status -----------------------------------------------------------


async def test_refresh_status_running(mocker):
    conn = make_conn(
        mocker,
        {
            f"{CLONE_PATH}/compose.yaml": result("yes\n"),
            "config --services": result("web\n"),
            "ps -a --format json": result(ps_ndjson([service_entry("web")])),
        },
    )
    project = fake_project(runtime_status="failed", updated_at=None)
    await refresh_status(conn=conn, project=project)
    assert project.runtime_status == "running"


async def test_refresh_status_partial_failure(mocker):
    services = [service_entry("web"), service_entry("worker", state="exited")]
    conn = make_conn(
        mocker,
        {
            f"{CLONE_PATH}/compose.yaml": result("yes\n"),
            "config --services": result("web\nworker\n"),
            "ps -a --format json": result(ps_ndjson(services)),
        },
    )
    project = fake_project(runtime_status="running", updated_at=None)
    await refresh_status(conn=conn, project=project)
    assert project.runtime_status == "failed"


async def test_refresh_status_never_started_stays_when_nothing_running(mocker):
    conn = make_conn(
        mocker,
        {
            f"{CLONE_PATH}/compose.yaml": result("yes\n"),
            "config --services": result("web\n"),
            "ps -a --format json": result(""),
        },
    )
    project = fake_project(runtime_status="never_started", updated_at=None)
    await refresh_status(conn=conn, project=project)
    assert project.runtime_status == "never_started"


async def test_refresh_status_ignores_orphan(mocker):
    # A leftover orphan (state created) from a removed service must not flip a
    # running app to failed.
    services = [service_entry("backend"), service_entry("nginx", state="created")]
    conn = make_conn(
        mocker,
        {
            f"{CLONE_PATH}/compose.yaml": result("yes\n"),
            "config --services": result("backend\n"),
            "ps -a --format json": result(ps_ndjson(services)),
        },
    )
    project = fake_project(runtime_status="running", updated_at=None)
    await refresh_status(conn=conn, project=project)
    assert project.runtime_status == "running"


async def test_refresh_status_compose_file_missing_is_failed(mocker):
    conn = make_conn(mocker)
    project = fake_project(runtime_status="running", updated_at=None)
    await refresh_status(conn=conn, project=project)
    assert project.runtime_status == "failed"


# -- start_project (DB backed) -------------------------------------------------


@pytest.fixture
def key_provider():
    return get_key_provider(get_settings())


@requires_db
async def test_start_project_happy_path_marks_running(
    mocker, db_session, test_user, key_provider
):
    server = await make_server(db_session, test_user.id)
    project = await seed_project(db_session, test_user.id, server)
    await create_env_file(
        db=db_session,
        project=project,
        path_from_client=".env",
        variables_from_client={"PORT": "8000"},
        key_provider=key_provider,
    )
    await db_session.commit()

    conn = make_conn(
        mocker,
        {
            f"{CLONE_PATH}/compose.yaml": result("yes\n"),
            "up -d --build": result("built"),
            "config --services": result("web\n"),
            "ps -a --format json": result(ps_ndjson([service_entry("web")])),
        },
    )
    run_result = await start_project(
        conn=conn, project=project, db=db_session, key_provider=key_provider
    )
    await db_session.commit()
    await db_session.refresh(project)

    assert run_result.runtime_status == "running"
    assert run_result.build_output == "built"
    assert project.runtime_status == "running"
    assert project.started_at is not None
    # Env file landed on the VPS before compose up.
    assert f"{CLONE_PATH}/.env" in conn.sftp.opened_paths
    assert b"PORT=8000\n" in conn.sftp.file.written


@requires_db
async def test_start_project_compose_failure_leaves_db_unchanged(
    mocker, db_session, test_user, key_provider
):
    server = await make_server(db_session, test_user.id)
    project = await seed_project(db_session, test_user.id, server)

    conn = make_conn(
        mocker,
        {
            f"{CLONE_PATH}/compose.yaml": result("yes\n"),
            "up -d --build": result("", "boom", 1),
        },
    )
    with pytest.raises(ComposeUpFailed):
        await start_project(
            conn=conn, project=project, db=db_session, key_provider=key_provider
        )
    await db_session.rollback()
    await db_session.refresh(project)
    assert project.runtime_status == "never_started"
    assert project.started_at is None


@requires_db
async def test_start_project_container_not_running_raises_with_logs(
    mocker, db_session, test_user, key_provider
):
    server = await make_server(db_session, test_user.id)
    project = await seed_project(db_session, test_user.id, server)

    services = [service_entry("web", state="restarting")]
    conn = make_conn(
        mocker,
        {
            f"{CLONE_PATH}/compose.yaml": result("yes\n"),
            "up -d --build": result("build transcript here"),
            "config --services": result("web\n"),
            "ps -a --format json": result(ps_ndjson(services)),
            "logs --tail 50": result("web keeps dying"),
        },
    )
    with pytest.raises(ContainerNotRunning) as exc_info:
        await start_project(
            conn=conn, project=project, db=db_session, key_provider=key_provider
        )
    # Both the build transcript and the container logs are surfaced.
    output = exc_info.value.captured_output
    assert "build transcript here" in output
    assert "--- container logs ---" in output
    assert "web keeps dying" in output
    # No docker compose down: pre-existing containers must not be torn down.
    assert not any("compose down" in c for c in ran_commands(conn))


@requires_db
async def test_start_project_compose_failure_attaches_build_output(
    mocker, db_session, test_user, key_provider
):
    server = await make_server(db_session, test_user.id)
    project = await seed_project(db_session, test_user.id, server)

    conn = make_conn(
        mocker,
        {
            f"{CLONE_PATH}/compose.yaml": result("yes\n"),
            "up -d --build": result("", "ERROR: build step failed", 1),
        },
    )
    with pytest.raises(ComposeUpFailed) as exc_info:
        await start_project(
            conn=conn, project=project, db=db_session, key_provider=key_provider
        )
    assert "ERROR: build step failed" in exc_info.value.captured_output
    # A build failure does not additionally fetch container logs.
    assert not any("logs --tail" in c for c in ran_commands(conn))


# -- truncate_build_output ----------------------------------------------------


def test_truncate_under_cap_returns_input_unchanged():
    small = "line one\nline two\n"
    assert truncate_build_output(small) == small


def test_truncate_over_cap_returns_marker_and_tail():
    big = "x" * (BUILD_OUTPUT_MAX_BYTES + 5000)
    truncated = truncate_build_output(big)
    assert truncated != big
    assert truncated.startswith("[Output truncated.")
    assert truncated.rstrip().endswith("x")
    # Bounded: at most the cap plus the marker.
    marker = (
        f"[Output truncated. Showing last {BUILD_OUTPUT_MAX_BYTES // 1024}KB "
        f"of {(len(big.encode()) ) // 1024}KB total.]\n\n"
    )
    assert len(truncated.encode("utf-8")) <= BUILD_OUTPUT_MAX_BYTES + len(
        marker.encode("utf-8")
    )


def test_truncate_handles_multibyte_split_at_boundary():
    # Many 4-byte characters guarantee the cut lands mid-character.
    big = "🎉" * (BUILD_OUTPUT_MAX_BYTES // 2)
    truncated = truncate_build_output(big)  # must not raise
    # Result is valid utf-8 (round-trips) and is truncated.
    assert truncated.encode("utf-8").decode("utf-8") == truncated
    assert truncated.startswith("[Output truncated.")


# -- build output is never logged ---------------------------------------------


@requires_db
async def test_build_output_is_never_logged(
    mocker, db_session, test_user, key_provider
):
    secret_transcript = "SUPER-SECRET-BUILD-TRANSCRIPT-9f3a"
    server = await make_server(db_session, test_user.id)
    project = await seed_project(db_session, test_user.id, server)

    log_spy = mocker.spy(run_service.logger, "info")

    conn = make_conn(
        mocker,
        {
            f"{CLONE_PATH}/compose.yaml": result("yes\n"),
            "up -d --build": result(secret_transcript),
            "config --services": result("web\n"),
            "ps -a --format json": result(ps_ndjson([service_entry("web")])),
        },
    )
    await start_project(
        conn=conn, project=project, db=db_session, key_provider=key_provider
    )

    # A build was logged (metadata), but the transcript never appears in any
    # logger call.
    assert log_spy.call_count >= 1
    for call in log_spy.call_args_list:
        rendered = " ".join(str(a) for a in (call.args + tuple(call.kwargs.values())))
        assert secret_transcript not in rendered
