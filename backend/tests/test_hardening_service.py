"""HardeningService tests with asyncssh fully mocked. No network, no DB required.

The pooled connection is passed directly to each method as a FakeConn that records
commands and returns canned output. The verification sub-connections opened inside
create_sudo_user / disable_root_login go through asyncssh.connect, which is patched.
"""

from uuid import uuid4

import pytest

from app.services import hardening_service as hard_mod
from app.services import ssh_service as ssh_mod
from app.services.hardening_service import (
    DockerInstallFailed,
    FirewallConfigFailed,
    HardeningContext,
    HardeningService,
    NginxInstallFailed,
    PasswordAuthDisableFailed,
    RootLoginDisableFailed,
    RootLoginPrecheckFailed,
    SudoUserVerificationFailed,
    SwapConfigFailed,
    SystemUpdateFailed,
)


class FakeProc:
    def __init__(self, stdout="", stderr="", exit_status=0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status


class FakeConn:
    """Records commands. Canned output for the detection commands. Optional failure."""

    def __init__(self, *, user_exists=False, fail_on=None):
        self.commands: list[str] = []
        self.user_exists = user_exists
        self.fail_on = fail_on
        self.closed = False

    async def run(self, command, check=False, timeout=None):
        self.commands.append(command)
        if self.fail_on and self.fail_on in command:
            return FakeProc(stderr="boom", exit_status=1)
        if "id -u" in command:
            return FakeProc(stdout="exists\n" if self.user_exists else "absent\n")
        if "docker --version" in command:
            return FakeProc(stdout="Docker version 24.0.0\n")
        if "is-active nginx" in command:
            return FakeProc(stdout="active\n")
        if "is-enabled nginx" in command:
            return FakeProc(stdout="enabled\n")
        if "permitrootlogin" in command:
            return FakeProc(stdout="permitrootlogin no\n")
        if "passwordauthentication" in command:
            return FakeProc(stdout="passwordauthentication no\n")
        if "ufw status" in command:
            return FakeProc(stdout="Status: active\n")
        if "swapon --show" in command:
            return FakeProc(stdout="/swapfile file 512M 0B -2\n")
        return FakeProc()

    def close(self):
        self.closed = True


class FakeVerifyConn:
    """The fresh sub-connection used to verify the sudo user works."""

    def __init__(self, whoami, sudo_ok=True):
        self._whoami = whoami
        self._sudo_ok = sudo_ok

    async def run(self, command, check=False, timeout=None):
        if command.strip() == "whoami":
            return FakeProc(stdout=f"{self._whoami}\n")
        if command.strip() == "sudo -n true":
            return FakeProc(exit_status=0 if self._sudo_ok else 1)
        return FakeProc()


class FakeConnect:
    """Supports `async with asyncssh.connect(...)`."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class FakeServer:
    def __init__(self, username="root", docker_installed=False, sudo_user_name=None):
        self.id = uuid4()
        self.host = "203.0.113.10"
        self.port = 22
        self.username = username
        self.host_key = b"ssh-ed25519 AAAATESTKEY"
        self.status = "verified"
        self.docker_installed = docker_installed
        self.sudo_user_name = sudo_user_name
        self.root_login_disabled = False
        self.firewall_enabled = False
        self.swap_configured = False
        self.last_system_update_at = None
        self.password_auth_disabled = False
        self.base_packages_installed = False
        self.nginx_installed = False


def _ctx(mocker):
    return HardeningContext(
        user_id=uuid4(),
        session_id="sess_test",
        redis=mocker.MagicMock(),
        key_provider=mocker.MagicMock(),
        app_public_key="ssh-ed25519 AAAAAPPKEY app-deploy-x",
        app_private_key=b"PRIVATEKEYBYTES",
    )


def _service(mocker):
    ssh = mocker.MagicMock()
    ssh.evict_connection = mocker.MagicMock()
    return HardeningService(ssh), ssh


def _patch_subconnection(mocker, whoami, sudo_ok=True):
    mocker.patch.object(ssh_mod.asyncssh, "import_known_hosts", return_value=object())
    mocker.patch.object(hard_mod.asyncssh, "import_private_key", return_value=object())
    mocker.patch.object(
        hard_mod.asyncssh,
        "connect",
        return_value=FakeConnect(FakeVerifyConn(whoami, sudo_ok)),
    )


# -- update_system ---------------------------------------------------------


async def test_update_system_happy(mocker):
    service, _ = _service(mocker)
    conn = FakeConn()
    server = FakeServer()
    await service.update_system(conn, server, mocker.MagicMock())
    joined = "\n".join(conn.commands)
    assert "apt-get update" in joined
    assert "apt-get upgrade" in joined
    assert server.last_system_update_at is not None


async def test_update_system_failure_no_state(mocker):
    service, _ = _service(mocker)
    conn = FakeConn(fail_on="apt-get upgrade")
    server = FakeServer()
    with pytest.raises(SystemUpdateFailed):
        await service.update_system(conn, server, mocker.MagicMock())
    assert server.last_system_update_at is None


# -- install_base_packages -------------------------------------------------


async def test_install_base_packages_happy_sets_flag(mocker):
    service, _ = _service(mocker)
    conn = FakeConn()
    server = FakeServer()
    await service.install_base_packages(conn, server, mocker.MagicMock())
    assert "apt-get install" in "\n".join(conn.commands)
    assert server.base_packages_installed is True


async def test_install_base_packages_failure_no_state(mocker):
    service, _ = _service(mocker)
    conn = FakeConn(fail_on="apt-get install")
    server = FakeServer()
    with pytest.raises(SystemUpdateFailed):
        await service.install_base_packages(conn, server, mocker.MagicMock())
    assert server.base_packages_installed is False


# -- install_docker --------------------------------------------------------


async def test_install_docker_happy_sets_flag(mocker):
    service, _ = _service(mocker)
    conn = FakeConn()
    server = FakeServer()
    await service.install_docker(conn, server, mocker.MagicMock())
    assert server.docker_installed is True
    assert "docker --version" in "\n".join(conn.commands)


async def test_install_docker_failure_no_state(mocker):
    service, _ = _service(mocker)
    conn = FakeConn(fail_on="get.docker.com")
    server = FakeServer()
    with pytest.raises(DockerInstallFailed):
        await service.install_docker(conn, server, mocker.MagicMock())
    assert server.docker_installed is False


# -- install_nginx -----------------------------------------------------------


class EnabledSequenceConn(FakeConn):
    """FakeConn whose successive `is-enabled nginx` probes pop from a list."""

    def __init__(self, enabled_responses, **kwargs):
        super().__init__(**kwargs)
        self.enabled_responses = list(enabled_responses)

    async def run(self, command, check=False, timeout=None):
        if "is-enabled nginx" in command:
            self.commands.append(command)
            return FakeProc(stdout=self.enabled_responses.pop(0))
        return await super().run(command, check=check, timeout=timeout)


async def test_install_nginx_happy_sets_flag_and_never_commits(mocker):
    service, _ = _service(mocker)
    conn = FakeConn()
    server = FakeServer()
    db = mocker.MagicMock()
    await service.install_nginx(conn, server, db)
    joined = "\n".join(conn.commands)
    apt = joined.index("apt-get install -y nginx python3-certbot-nginx")
    active = joined.index("systemctl is-active nginx")
    enabled = joined.index("systemctl is-enabled nginx")
    assert apt < active < enabled
    assert server.nginx_installed is True
    db.commit.assert_not_called()


async def test_install_nginx_idempotent_rerun(mocker):
    service, _ = _service(mocker)
    conn = FakeConn()
    server = FakeServer()
    await service.install_nginx(conn, server, mocker.MagicMock())
    assert server.nginx_installed is True
    await service.install_nginx(conn, server, mocker.MagicMock())
    assert server.nginx_installed is True


async def test_install_nginx_apt_failure_no_state(mocker):
    service, _ = _service(mocker)
    conn = FakeConn(fail_on="apt-get install")
    server = FakeServer()
    with pytest.raises(NginxInstallFailed) as excinfo:
        await service.install_nginx(conn, server, mocker.MagicMock())
    assert "boom" in excinfo.value.captured_output
    assert server.nginx_installed is False


async def test_install_nginx_inactive_after_install_fails(mocker):
    service, _ = _service(mocker)

    class InactiveConn(FakeConn):
        async def run(self, command, check=False, timeout=None):
            if "is-active nginx" in command:
                self.commands.append(command)
                return FakeProc(stdout="inactive\n")
            return await super().run(command, check=check, timeout=timeout)

    conn = InactiveConn()
    server = FakeServer()
    with pytest.raises(NginxInstallFailed):
        await service.install_nginx(conn, server, mocker.MagicMock())
    assert server.nginx_installed is False


async def test_install_nginx_enable_recovery(mocker):
    service, _ = _service(mocker)
    conn = EnabledSequenceConn(["disabled\n", "enabled\n"])
    server = FakeServer()
    await service.install_nginx(conn, server, mocker.MagicMock())
    assert "systemctl enable nginx" in "\n".join(conn.commands)
    assert server.nginx_installed is True


async def test_install_nginx_enable_unrecoverable_fails(mocker):
    service, _ = _service(mocker)
    conn = EnabledSequenceConn(["disabled\n", "disabled\n"])
    server = FakeServer()
    with pytest.raises(NginxInstallFailed):
        await service.install_nginx(conn, server, mocker.MagicMock())
    assert server.nginx_installed is False


# -- create_sudo_user ------------------------------------------------------


async def test_create_sudo_user_happy_switches_identity_and_evicts(mocker):
    service, ssh = _service(mocker)
    _patch_subconnection(mocker, whoami="deploy")
    conn = FakeConn(user_exists=False)
    server = FakeServer()
    ctx = _ctx(mocker)

    await service.create_sudo_user(conn, server, mocker.MagicMock(), ctx, "deploy")

    joined = "\n".join(conn.commands)
    assert "adduser --disabled-password" in joined
    assert "usermod -aG sudo deploy" in joined
    assert "NOPASSWD:ALL" in joined
    assert server.sudo_user_name == "deploy"
    assert server.username == "deploy"
    ssh.evict_connection.assert_called_once_with(ctx.user_id, server.id)


async def test_create_sudo_user_idempotent_when_user_exists(mocker):
    service, _ = _service(mocker)
    _patch_subconnection(mocker, whoami="deploy")
    conn = FakeConn(user_exists=True)
    server = FakeServer()
    ctx = _ctx(mocker)

    await service.create_sudo_user(conn, server, mocker.MagicMock(), ctx, "deploy")

    joined = "\n".join(conn.commands)
    assert "adduser --disabled-password" not in joined  # skipped
    assert server.sudo_user_name == "deploy"  # still records the user


async def test_create_sudo_user_adds_docker_group_when_installed(mocker):
    service, _ = _service(mocker)
    _patch_subconnection(mocker, whoami="deploy")
    conn = FakeConn(user_exists=False)
    server = FakeServer(docker_installed=True)
    await service.create_sudo_user(
        conn, server, mocker.MagicMock(), _ctx(mocker), "deploy"
    )
    assert "usermod -aG docker deploy" in "\n".join(conn.commands)


async def test_create_sudo_user_verification_failure_does_not_switch(mocker):
    service, ssh = _service(mocker)
    # Sub-connection reports the wrong user, so verification fails.
    _patch_subconnection(mocker, whoami="someoneelse")
    conn = FakeConn(user_exists=False)
    server = FakeServer()
    with pytest.raises(SudoUserVerificationFailed):
        await service.create_sudo_user(
            conn, server, mocker.MagicMock(), _ctx(mocker), "deploy"
        )
    assert server.sudo_user_name is None
    assert server.username == "root"  # never lost root access
    ssh.evict_connection.assert_not_called()


# -- disable_root_login ----------------------------------------------------


async def test_disable_root_login_guardrail_no_sudo_user(mocker):
    service, _ = _service(mocker)
    conn = FakeConn()
    server = FakeServer(sudo_user_name=None)
    with pytest.raises(RootLoginPrecheckFailed):
        await service.disable_root_login(conn, server, mocker.MagicMock(), _ctx(mocker))
    assert conn.commands == []  # no SSH touched
    assert server.root_login_disabled is False


async def test_disable_root_login_happy(mocker):
    service, _ = _service(mocker)
    _patch_subconnection(mocker, whoami="deploy")
    conn = FakeConn()
    server = FakeServer(username="deploy", sudo_user_name="deploy")
    await service.disable_root_login(conn, server, mocker.MagicMock(), _ctx(mocker))
    joined = "\n".join(conn.commands)
    assert "PermitRootLogin no" in joined
    assert "reload" in joined
    assert server.root_login_disabled is True


async def test_disable_root_login_failure_when_runtime_disagrees(mocker):
    service, _ = _service(mocker)
    _patch_subconnection(mocker, whoami="deploy")

    class StubbornConn(FakeConn):
        async def run(self, command, check=False, timeout=None):
            if "permitrootlogin" in command:
                self.commands.append(command)
                return FakeProc(stdout="permitrootlogin yes\n")
            return await super().run(command, check=check, timeout=timeout)

    conn = StubbornConn()
    server = FakeServer(username="deploy", sudo_user_name="deploy")
    with pytest.raises(RootLoginDisableFailed):
        await service.disable_root_login(conn, server, mocker.MagicMock(), _ctx(mocker))
    assert server.root_login_disabled is False


# -- disable_password_auth -------------------------------------------------


async def test_disable_password_auth_happy(mocker):
    service, _ = _service(mocker)
    conn = FakeConn()
    server = FakeServer(username="deploy", sudo_user_name="deploy")
    await service.disable_password_auth(conn, server, mocker.MagicMock())
    joined = "\n".join(conn.commands)
    assert "PasswordAuthentication no" in joined
    assert "reload" in joined
    assert server.password_auth_disabled is True


async def test_disable_password_auth_failure_when_runtime_disagrees(mocker):
    service, _ = _service(mocker)

    class StubbornConn(FakeConn):
        async def run(self, command, check=False, timeout=None):
            if "passwordauthentication" in command:
                self.commands.append(command)
                return FakeProc(stdout="passwordauthentication yes\n")
            return await super().run(command, check=check, timeout=timeout)

    conn = StubbornConn()
    server = FakeServer()
    with pytest.raises(PasswordAuthDisableFailed):
        await service.disable_password_auth(conn, server, mocker.MagicMock())
    assert server.password_auth_disabled is False


# -- configure_firewall ----------------------------------------------------


async def test_configure_firewall_happy(mocker):
    service, _ = _service(mocker)
    conn = FakeConn()
    server = FakeServer()
    await service.configure_firewall(conn, server, mocker.MagicMock())
    joined = "\n".join(conn.commands)
    assert "ufw allow OpenSSH" in joined
    assert "ufw --force enable" in joined
    assert server.firewall_enabled is True


async def test_configure_firewall_failure_no_state(mocker):
    service, _ = _service(mocker)
    conn = FakeConn(fail_on="ufw --force enable")
    server = FakeServer()
    with pytest.raises(FirewallConfigFailed):
        await service.configure_firewall(conn, server, mocker.MagicMock())
    assert server.firewall_enabled is False


# -- create_swap -----------------------------------------------------------


async def test_create_swap_happy(mocker):
    service, _ = _service(mocker)
    conn = FakeConn()
    server = FakeServer()
    await service.create_swap(conn, server, mocker.MagicMock())
    assert server.swap_configured is True
    assert "swapon --show" in "\n".join(conn.commands)


async def test_create_swap_failure_no_state(mocker):
    service, _ = _service(mocker)

    class NoSwapConn(FakeConn):
        async def run(self, command, check=False, timeout=None):
            if "swapon --show" in command:
                self.commands.append(command)
                return FakeProc(stdout="", exit_status=1)  # verify fails
            return await super().run(command, check=check, timeout=timeout)

    conn = NoSwapConn()
    server = FakeServer()
    with pytest.raises(SwapConfigFailed):
        await service.create_swap(conn, server, mocker.MagicMock())
    assert server.swap_configured is False


# -- quick_harden ----------------------------------------------------------


async def test_quick_harden_runs_full_sequence(mocker):
    service, ssh = _service(mocker)
    _patch_subconnection(mocker, whoami="deploy")
    conn = FakeConn(user_exists=False)
    ssh.get_connection = mocker.AsyncMock(return_value=conn)
    server = FakeServer()

    await service.quick_harden(server, mocker.MagicMock(), _ctx(mocker), "deploy")

    assert server.last_system_update_at is not None
    assert server.base_packages_installed is True
    assert server.docker_installed is True
    assert server.nginx_installed is True
    # install_nginx runs after install_docker and before create_sudo_user.
    joined = "\n".join(conn.commands)
    docker_idx = joined.index("get.docker.com")
    nginx_idx = joined.index("apt-get install -y nginx")
    adduser_idx = joined.index("adduser --disabled-password")
    assert docker_idx < nginx_idx < adduser_idx
    assert server.sudo_user_name == "deploy"
    assert server.username == "deploy"
    assert server.firewall_enabled is True
    assert server.swap_configured is True
    assert server.password_auth_disabled is True
    assert server.root_login_disabled is True
    # Connection re-acquired after the identity switch.
    assert ssh.get_connection.await_count == 2
