"""Publish service tests: DNS pre-check, preconditions, nginx/certbot flow,
verification branches, and best-effort cleanup. DNS and SSH are mocked; the
tests are DB backed because the domain/port conflict checks query projects."""

import pytest

from app.services.publish_service import (
    AlreadyPublished,
    AppNotRunning,
    CertbotFailed,
    DomainAlreadyUsed,
    DomainDoesNotResolve,
    NginxConfigInvalid,
    NginxNotInstalled,
    NothingListening,
    PortAlreadyUsed,
    PublishVerificationFailed,
    _PublishState,
    build_nginx_config,
    cleanup_publish,
    publish_project,
)
from datetime import datetime, timezone

from tests.conftest import requires_db
from tests.project_mocks import make_conn, ran_commands, result
from tests.run_publish_mocks import make_server, seed_project

pytestmark = requires_db

DOMAIN = "app.example.com"
SERVER_HOST = "203.0.113.10"
PORT = 8080


@pytest.fixture
def dns_ok(mocker):
    return mocker.patch(
        "app.services.publish_service.resolve_domain_dns",
        mocker.AsyncMock(return_value=[SERVER_HOST]),
    )


def happy_conn(mocker, extra: dict | None = None):
    overrides = {"https://": result("200")}
    overrides.update(extra or {})
    return make_conn(mocker, overrides)


async def run_publish(conn, db_session, project, server, user, domain=DOMAIN, port=PORT):
    return await publish_project(
        conn=conn,
        project=project,
        server=server,
        current_user=user,
        domain_from_client=domain,
        internal_port_from_client=port,
        db=db_session,
    )


async def seed(db_session, user, **project_kwargs):
    server = await make_server(db_session, user.id, host=SERVER_HOST)
    project = await seed_project(
        db_session,
        user.id,
        server,
        runtime_status=project_kwargs.pop("runtime_status", "running"),
        **project_kwargs,
    )
    return server, project


# -- config rendering ---------------------------------------------------------


def test_nginx_config_contains_domain_port_and_websocket_headers():
    config = build_nginx_config(DOMAIN, PORT)
    assert f"server_name {DOMAIN};" in config
    assert f"proxy_pass http://127.0.0.1:{PORT};" in config
    assert "proxy_set_header Upgrade $http_upgrade;" in config
    assert 'proxy_set_header Connection "upgrade";' in config


# -- preconditions ------------------------------------------------------------


async def test_not_running_raises(mocker, db_session, test_user, dns_ok):
    server, project = await seed(db_session, test_user, runtime_status="never_started")
    with pytest.raises(AppNotRunning):
        await run_publish(happy_conn(mocker), db_session, project, server, test_user)


async def test_already_published_raises(mocker, db_session, test_user, dns_ok):
    server, project = await seed(
        db_session,
        test_user,
        domain="live.example.com",
        internal_port=3000,
        published_at=datetime.now(timezone.utc),
    )
    with pytest.raises(AlreadyPublished):
        await run_publish(happy_conn(mocker), db_session, project, server, test_user)


async def test_nginx_not_installed_raises(mocker, db_session, test_user, dns_ok):
    server = await make_server(
        db_session, test_user.id, host=SERVER_HOST, nginx_installed=False
    )
    project = await seed_project(
        db_session, test_user.id, server, runtime_status="running"
    )
    with pytest.raises(NginxNotInstalled):
        await run_publish(happy_conn(mocker), db_session, project, server, test_user)


async def test_domain_conflict_on_same_server_raises(
    mocker, db_session, test_user, dns_ok
):
    server, project = await seed(db_session, test_user)
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
    with pytest.raises(DomainAlreadyUsed):
        await run_publish(happy_conn(mocker), db_session, project, server, test_user)


async def test_port_conflict_on_same_server_raises(
    mocker, db_session, test_user, dns_ok
):
    server, project = await seed(db_session, test_user)
    await seed_project(
        db_session,
        test_user.id,
        server,
        slug="other",
        repo_id=888,
        runtime_status="running",
        domain="other.example.com",
        internal_port=PORT,
        published_at=datetime.now(timezone.utc),
    )
    with pytest.raises(PortAlreadyUsed):
        await run_publish(happy_conn(mocker), db_session, project, server, test_user)


# -- DNS pre-check ------------------------------------------------------------


async def test_dns_mismatch_raises_with_resolved_ips(mocker, db_session, test_user):
    mocker.patch(
        "app.services.publish_service.resolve_domain_dns",
        mocker.AsyncMock(return_value=["198.51.100.7"]),
    )
    server, project = await seed(db_session, test_user)
    conn = happy_conn(mocker)
    with pytest.raises(DomainDoesNotResolve) as exc_info:
        await run_publish(conn, db_session, project, server, test_user)
    assert exc_info.value.resolved == ["198.51.100.7"]
    # Nothing touched the VPS.
    assert ran_commands(conn) == []


async def test_dns_no_answer_raises(mocker, db_session, test_user):
    mocker.patch(
        "app.services.publish_service.resolve_domain_dns",
        mocker.AsyncMock(return_value=[]),
    )
    server, project = await seed(db_session, test_user)
    with pytest.raises(DomainDoesNotResolve):
        await run_publish(happy_conn(mocker), db_session, project, server, test_user)


# -- failure paths with cleanup -----------------------------------------------


def assert_cleanup_ran(conn, slug="seeded", expect_cert_delete=False):
    commands = ran_commands(conn)
    assert any(f"rm -f /etc/nginx/sites-enabled/{slug}.conf" in c for c in commands)
    assert any(f"rm -f /etc/nginx/sites-available/{slug}.conf" in c for c in commands)
    assert any("systemctl reload nginx" in c for c in commands)
    has_cert_delete = any("certbot delete" in c for c in commands)
    assert has_cert_delete == expect_cert_delete


async def test_nginx_config_invalid_cleans_up(mocker, db_session, test_user, dns_ok):
    server, project = await seed(db_session, test_user)
    conn = happy_conn(mocker, {"nginx -t": result("", "config broken", 1)})
    with pytest.raises(NginxConfigInvalid) as exc_info:
        await run_publish(conn, db_session, project, server, test_user)
    assert "config broken" in exc_info.value.captured_output
    assert_cleanup_ran(conn, expect_cert_delete=False)


async def test_certbot_failure_cleans_up_including_cert(
    mocker, db_session, test_user, dns_ok
):
    server, project = await seed(db_session, test_user)
    conn = happy_conn(mocker, {"certbot --nginx": result("", "challenge failed", 1)})
    with pytest.raises(CertbotFailed) as exc_info:
        await run_publish(conn, db_session, project, server, test_user)
    assert "challenge failed" in exc_info.value.captured_output
    assert_cleanup_ran(conn, expect_cert_delete=True)


async def test_verification_failure_with_localhost_up(
    mocker, db_session, test_user, dns_ok
):
    server, project = await seed(db_session, test_user)
    conn = make_conn(
        mocker,
        {"https://": result("000"), "http://localhost": result("200")},
    )
    with pytest.raises(PublishVerificationFailed):
        await run_publish(conn, db_session, project, server, test_user)
    assert_cleanup_ran(conn, expect_cert_delete=True)


async def test_verification_failure_with_localhost_down_raises_nothing_listening(
    mocker, db_session, test_user, dns_ok
):
    server, project = await seed(db_session, test_user)
    conn = make_conn(
        mocker,
        {"https://": result("000"), "http://localhost": result("000")},
    )
    with pytest.raises(NothingListening) as exc_info:
        await run_publish(conn, db_session, project, server, test_user)
    assert exc_info.value.port == PORT
    assert_cleanup_ran(conn, expect_cert_delete=True)


# -- happy path ----------------------------------------------------------------


async def test_publish_happy_path_updates_project(
    mocker, db_session, test_user, dns_ok
):
    server, project = await seed(db_session, test_user)
    conn = happy_conn(mocker)
    await run_publish(conn, db_session, project, server, test_user)
    await db_session.commit()
    await db_session.refresh(project)

    assert project.domain == DOMAIN
    assert project.internal_port == PORT
    assert project.published_at is not None

    commands = ran_commands(conn)
    assert any("sudo tee /etc/nginx/sites-available/seeded.conf" in c for c in commands)
    assert any("ln -sfn" in c for c in commands)
    assert any("nginx -t" in c for c in commands)
    assert any(f"certbot --nginx -d {DOMAIN}" in c for c in commands)
    assert any(test_user.email in c for c in commands if "certbot" in c)
    # No cleanup on success.
    assert not any("rm -f /etc/nginx" in c for c in commands)


# -- cleanup helper ------------------------------------------------------------


async def test_cleanup_never_raises_even_when_every_step_fails(mocker, db_session, test_user):
    server = await make_server(db_session, test_user.id, host=SERVER_HOST)
    conn = make_conn(mocker, {"": RuntimeError("ssh transport dead")})
    state = _PublishState(config_written=True, symlinked=True, cert_requested=True)
    # Must not raise.
    await cleanup_publish(conn, server, "seeded", DOMAIN, state)
