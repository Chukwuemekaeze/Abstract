"""Service level tests for project provisioning.

DB backed (TEST_DATABASE_URL) so flush/rollback semantics run against real
Postgres. GitHub, SSH, SFTP, and the Clerk token fetch are all mocked at the
boundary; create_project is called directly, with the test owning the session
exactly like the route handler does.
"""

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models import Project, ProjectDeployKey, Server
from app.services.key_provider import get_key_provider
from app.services.project_service import (
    ClonePathOccupied,
    CloneVerificationFailed,
    DuplicateProject,
    ServerNotEligible,
    create_project,
)
from tests.conftest import FakeRedis, TEST_SESSION_ID, requires_db
from tests.project_mocks import (
    make_conn,
    make_github,
    make_ssh,
    ran_commands,
    result,
)

pytestmark = requires_db

REPO_ID = 777001
REPO_FULL_NAME = "Chukwuemekaeze/anibantsdotNG"


async def _make_server(
    db_session,
    user_id,
    *,
    status="verified",
    sudo_user_name="deploy",
    base_packages_installed=True,
):
    server = Server(
        user_id=user_id,
        name="web1",
        host="203.0.113.10",
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


async def _call(db_session, test_user, server, mocker, conn=None, github=None, **kw):
    conn = conn if conn is not None else make_conn(mocker)
    github = github if github is not None else make_github(mocker)
    mocker.patch(
        "app.services.project_service.get_github_oauth_token",
        mocker.AsyncMock(return_value="gho_test_token"),
    )
    defaults = dict(
        name_from_client="Anibants",
        server_id_from_client=server.id,
        github_repo_id_from_client=REPO_ID,
        github_repo_full_name_from_client=REPO_FULL_NAME,
        current_user=test_user,
        session_id=TEST_SESSION_ID,
        db=db_session,
        ssh=make_ssh(mocker, conn),
        redis=FakeRedis(),
        key_provider=get_key_provider(get_settings()),
        clerk=mocker.MagicMock(),
        github=github,
    )
    defaults.update(kw)
    return await create_project(**defaults), conn, github


async def test_happy_path(db_session, test_user, mocker):
    server = await _make_server(db_session, test_user.id)
    commit_spy = mocker.spy(db_session, "commit")
    conn = make_conn(mocker)
    github = make_github(mocker)

    (project, fingerprint), conn, github = await _call(
        db_session, test_user, server, mocker, conn=conn, github=github
    )

    assert project.slug == "anibants"
    assert project.clone_path == "/home/deploy/anibantsdotNG"
    assert project.github_repo_id == REPO_ID
    assert project.cloned_at is not None
    assert project.updated_at == project.cloned_at
    assert fingerprint.startswith("SHA256:")

    key_row = await db_session.scalar(
        select(ProjectDeployKey).where(ProjectDeployKey.project_id == project.id)
    )
    assert key_row is not None
    assert key_row.github_deploy_key_id == 4242
    assert key_row.deploy_key_fingerprint == fingerprint
    assert key_row.deploy_key_public_key.startswith("ssh-ed25519 ")
    # Private key stored encrypted, not as raw OpenSSH text.
    assert not key_row.encrypted_deploy_key_private_key.startswith(b"-----BEGIN")

    # GitHub key registered read-only with the project label before any VPS write.
    github.add_deploy_key.assert_awaited_once()
    call = github.add_deploy_key.await_args
    assert call.args[1] == REPO_FULL_NAME
    assert call.args[2] == "Abstract: Anibants"
    assert call.kwargs.get("read_only") is True

    # Private key written via SFTP (never echo/heredoc) and decryptable back
    # to the same bytes that went over the wire.
    assert conn.sftp.opened_paths == [".ssh/anibants-deploy"]
    provider = get_key_provider(get_settings())
    decrypted = await provider.decrypt(key_row.encrypted_deploy_key_private_key)
    assert conn.sftp.file.written == decrypted

    commands = ran_commands(conn)
    assert any("Host github-anibants" in c for c in commands)
    # GitHub's pinned host key is seeded into known_hosts before the clone.
    assert any(
        "known_hosts" in c and "github.com ssh-ed25519 AAAAghtestkey" in c
        for c in commands
    )
    assert any(
        "git clone" in c and "git@github-anibants:" in c for c in commands
    )
    # The service never commits; the caller owns the transaction.
    commit_spy.assert_not_called()
    github.delete_deploy_key.assert_not_awaited()


@pytest.mark.parametrize(
    "server_kwargs",
    [
        {"status": "pending_verification"},
        {"status": "key_mismatch"},
        {"sudo_user_name": None},
        {"base_packages_installed": False},
    ],
)
async def test_server_not_eligible(db_session, test_user, mocker, server_kwargs):
    server = await _make_server(db_session, test_user.id, **server_kwargs)
    with pytest.raises(ServerNotEligible):
        await _call(db_session, test_user, server, mocker)


async def test_server_not_eligible_when_git_missing(db_session, test_user, mocker):
    server = await _make_server(db_session, test_user.id)
    conn = make_conn(mocker, {"command -v git": result("no\n")})
    with pytest.raises(ServerNotEligible):
        await _call(db_session, test_user, server, mocker, conn=conn)


async def test_duplicate_project(db_session, test_user, mocker):
    server = await _make_server(db_session, test_user.id)
    db_session.add(
        Project(
            user_id=test_user.id,
            server_id=server.id,
            name="Existing",
            slug="existing",
            github_repo_full_name=REPO_FULL_NAME,
            github_repo_id=REPO_ID,
            clone_path="/home/deploy/anibantsdotNG",
        )
    )
    await db_session.commit()
    with pytest.raises(DuplicateProject):
        await _call(db_session, test_user, server, mocker)


async def test_clone_path_occupied(db_session, test_user, mocker):
    server = await _make_server(db_session, test_user.id)
    conn = make_conn(mocker, {"test -d": result("exists\n")})
    with pytest.raises(ClonePathOccupied):
        await _call(db_session, test_user, server, mocker, conn=conn)


async def test_no_cleanup_when_github_add_fails(db_session, test_user, mocker):
    server = await _make_server(db_session, test_user.id)
    github = make_github(mocker)
    github.add_deploy_key = mocker.AsyncMock(side_effect=RuntimeError("github down"))
    conn = make_conn(mocker)
    with pytest.raises(RuntimeError):
        await _call(db_session, test_user, server, mocker, conn=conn, github=github)
    # Nothing external was created, so nothing is cleaned up.
    github.delete_deploy_key.assert_not_awaited()
    assert not any("rm -" in c for c in ran_commands(conn))


async def test_github_key_deleted_when_sftp_fails(db_session, test_user, mocker):
    server = await _make_server(db_session, test_user.id)
    github = make_github(mocker)
    conn = make_conn(mocker)
    conn.start_sftp_client = mocker.AsyncMock(side_effect=OSError("sftp broke"))
    with pytest.raises(OSError):
        await _call(db_session, test_user, server, mocker, conn=conn, github=github)
    github.delete_deploy_key.assert_awaited_once_with(
        "gho_test_token", REPO_FULL_NAME, 4242
    )
    # The key file was never written, so no file cleanup runs.
    assert not any("rm -f" in c for c in ran_commands(conn))


async def test_full_cleanup_when_clone_fails(db_session, test_user, mocker):
    server = await _make_server(db_session, test_user.id)
    github = make_github(mocker)
    conn = make_conn(
        mocker,
        {"git clone": result(stderr="fatal: could not read", exit_status=128)},
    )
    with pytest.raises(CloneVerificationFailed):
        await _call(db_session, test_user, server, mocker, conn=conn, github=github)

    github.delete_deploy_key.assert_awaited_once()
    commands = ran_commands(conn)
    assert any("rm -f ~/.ssh/anibants-deploy" in c for c in commands)
    assert any("awk" in c and "Host github-anibants" in c for c in commands)
    assert any("rm -rf" in c and "anibantsdotNG" in c for c in commands)
    # known_hosts entries are shared across projects and never cleaned up.
    assert not any("known_hosts" in c and "rm " in c for c in commands)


async def test_cleanup_failures_never_mask_original_error(
    db_session, test_user, mocker
):
    server = await _make_server(db_session, test_user.id)
    github = make_github(mocker)
    github.delete_deploy_key = mocker.AsyncMock(
        side_effect=RuntimeError("cleanup exploded")
    )
    conn = make_conn(
        mocker,
        {
            "git clone": result(stderr="fatal: auth", exit_status=128),
            "rm -f": OSError("ssh dropped"),
        },
    )
    # The original clone failure propagates even though both the GitHub and the
    # file cleanup raise.
    with pytest.raises(CloneVerificationFailed):
        await _call(db_session, test_user, server, mocker, conn=conn, github=github)
    github.delete_deploy_key.assert_awaited_once()


async def test_slug_uniqueness_appends_suffix(db_session, test_user, mocker):
    server = await _make_server(db_session, test_user.id)
    db_session.add(
        Project(
            user_id=test_user.id,
            server_id=server.id,
            name="anibants",
            slug="anibants",
            github_repo_full_name="Chukwuemekaeze/other-repo",
            github_repo_id=REPO_ID + 1,
            clone_path="/home/deploy/other-repo",
        )
    )
    await db_session.commit()

    (project, _fingerprint), _conn, _github = await _call(
        db_session, test_user, server, mocker
    )
    assert project.slug == "anibants-2"


# -- pull_latest ----------------------------------------------------------------
# No DB needed: a SimpleNamespace stands in for the project row, matching how
# pull_latest only touches clone_path and updated_at.

CLONE_PATH = "/home/deploy/anibantsdotNG"


def _fake_pull_project():
    from datetime import datetime, timezone
    from types import SimpleNamespace

    return SimpleNamespace(
        clone_path=CLONE_PATH,
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


async def test_pull_latest_happy_path(mocker):
    from app.services.project_service import pull_latest

    conn = make_conn(
        mocker,
        {"rev-parse --short HEAD": [result("abc1234\n"), result("def5678\n")]},
    )
    project = _fake_pull_project()
    old_updated_at = project.updated_at

    pull = await pull_latest(conn=conn, project=project)

    assert pull.before_commit == "abc1234"
    assert pull.after_commit == "def5678"
    assert pull.already_up_to_date is False
    assert project.updated_at > old_updated_at

    commands = ran_commands(conn)
    git_commands = [c for c in commands if "&& git " in c]
    assert git_commands == [
        f"cd {CLONE_PATH} && git rev-parse --short HEAD",
        f"cd {CLONE_PATH} && git fetch origin --prune",
        f"cd {CLONE_PATH} && git remote set-head origin --auto",
        f"cd {CLONE_PATH} && git reset --hard refs/remotes/origin/HEAD",
        f"cd {CLONE_PATH} && git rev-parse --short HEAD",
    ]
    # The .git precondition check ran before any git command.
    assert f"{CLONE_PATH}/.git" in commands[0]


async def test_pull_latest_already_up_to_date(mocker):
    from app.services.project_service import pull_latest

    conn = make_conn(mocker, {"rev-parse --short HEAD": result("abc1234\n")})
    project = _fake_pull_project()
    old_updated_at = project.updated_at

    pull = await pull_latest(conn=conn, project=project)

    assert pull.already_up_to_date is True
    assert pull.before_commit == pull.after_commit == "abc1234"
    # A no-op pull is still a successful pull.
    assert project.updated_at > old_updated_at


async def test_pull_latest_fetch_failure_raises_before_reset(mocker):
    from app.services.project_service import PullFailed, pull_latest

    conn = make_conn(
        mocker,
        {
            "rev-parse --short HEAD": result("abc1234\n"),
            "git fetch": result(
                "", "fatal: Could not read from remote repository", 1
            ),
        },
    )
    project = _fake_pull_project()
    old_updated_at = project.updated_at

    with pytest.raises(PullFailed) as exc_info:
        await pull_latest(conn=conn, project=project)

    assert "Could not read from remote" in exc_info.value.captured_output
    assert not any("reset --hard" in c for c in ran_commands(conn))
    assert project.updated_at == old_updated_at


async def test_pull_latest_missing_clone_raises(mocker):
    from app.services.project_service import CloneMissing, pull_latest

    conn = make_conn(mocker, {f"{CLONE_PATH}/.git": result("no\n")})
    project = _fake_pull_project()

    with pytest.raises(CloneMissing):
        await pull_latest(conn=conn, project=project)

    assert not any("&& git " in c for c in ran_commands(conn))
