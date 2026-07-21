"""SSHService tests with asyncssh fully mocked. No network, no DB required."""

import base64
import secrets
from uuid import uuid4

import pytest

from app.services import ssh_service as ssh_mod
from app.services.key_provider import EnvKeyProvider
from app.services.ssh_service import SSHService, clear_pool

HOST_KEY_BYTES = b"ssh-ed25519 AAAATESTKEY"
FINGERPRINT = "SHA256:testfingerprintvalue"


class FakeKey:
    def export_public_key(self) -> bytes:
        return HOST_KEY_BYTES

    def get_algorithm(self) -> str:
        return "ssh-ed25519"

    def get_fingerprint(self) -> str:
        return FINGERPRINT


class FakeProcResult:
    def __init__(self, stdout="", stderr="", exit_status=0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status


class FakeConn:
    """Records commands. Returns sensible output for whoami and sshd -T."""

    def __init__(self, whoami="root"):
        self.commands: list[str] = []
        self._whoami = whoami
        self.closed = False

    async def run(self, command, check=False):
        self.commands.append(command)
        stripped = command.strip()
        if stripped == "whoami":
            return FakeProcResult(stdout=f"{self._whoami}\n")
        if "sshd -T" in command:
            return FakeProcResult(stdout="passwordauthentication no\n")
        if stripped.startswith("echo 'hello"):
            return FakeProcResult(stdout="hello from Abstract\n")
        return FakeProcResult(stdout="", stderr="", exit_status=0)

    def close(self):
        self.closed = True


class FakeConnect:
    """Supports both `await asyncssh.connect(...)` and `async with ...`."""

    def __init__(self, conn: FakeConn):
        self._conn = conn

    def __await__(self):
        async def _inner():
            return self._conn

        return _inner().__await__()

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class FakeServer:
    def __init__(self):
        self.id = uuid4()
        self.host = "203.0.113.10"
        self.port = 22
        self.username = "root"
        self.host_key = HOST_KEY_BYTES
        self.status = "verified"


@pytest.fixture(autouse=True)
def _clear_pool():
    clear_pool()
    yield
    clear_pool()


async def test_probe_returns_fingerprint_format(mocker):
    mocker.patch.object(
        ssh_mod.asyncssh, "get_server_host_key", mocker.AsyncMock(return_value=FakeKey())
    )
    result = await SSHService().probe("203.0.113.10", 22, "root")
    assert result.fingerprint_sha256 == FINGERPRINT
    assert result.fingerprint_sha256.startswith("SHA256:")
    assert result.host_key == HOST_KEY_BYTES
    assert result.host_key_type == "ssh-ed25519"


async def test_probe_unreachable_raises_probe_error(mocker):
    mocker.patch.object(
        ssh_mod.asyncssh,
        "get_server_host_key",
        mocker.AsyncMock(side_effect=OSError("no route to host")),
    )
    with pytest.raises(ssh_mod.ProbeError):
        await SSHService().probe("203.0.113.10", 22, "root")


async def test_install_key_runs_full_sequence_with_hardening(mocker):
    conn = FakeConn(whoami="root")
    mocker.patch.object(
        ssh_mod.asyncssh, "get_server_host_key", mocker.AsyncMock(return_value=FakeKey())
    )
    mocker.patch.object(
        ssh_mod.asyncssh, "connect", return_value=FakeConnect(conn)
    )
    mocker.patch.object(ssh_mod.asyncssh, "import_known_hosts", return_value=object())
    mocker.patch.object(ssh_mod.asyncssh, "import_private_key", return_value=object())

    server = FakeServer()
    await SSHService().install_key(
        server=server,
        password_from_client="hunter2",
        app_public_key="ssh-ed25519 AAAAAPPKEY app-deploy-x",
        app_private_key=b"PRIVATEKEYBYTES",
        disable_password_auth=True,
    )

    joined = "\n".join(conn.commands)
    assert "mkdir -p ~/.ssh" in joined
    assert "authorized_keys" in joined
    assert "chmod 600 ~/.ssh/authorized_keys" in joined
    assert "PasswordAuthentication" in joined  # sed hardening line present
    assert "reload" in joined  # reload chain present
    assert "sshd -T" in joined  # runtime verification present
    assert "whoami" in conn.commands  # final key based smoke test


async def test_install_key_skips_hardening_when_disabled(mocker):
    conn = FakeConn(whoami="root")
    mocker.patch.object(
        ssh_mod.asyncssh, "get_server_host_key", mocker.AsyncMock(return_value=FakeKey())
    )
    mocker.patch.object(ssh_mod.asyncssh, "connect", return_value=FakeConnect(conn))
    mocker.patch.object(ssh_mod.asyncssh, "import_known_hosts", return_value=object())
    mocker.patch.object(ssh_mod.asyncssh, "import_private_key", return_value=object())

    server = FakeServer()
    await SSHService().install_key(
        server=server,
        password_from_client="hunter2",
        app_public_key="ssh-ed25519 AAAAAPPKEY app-deploy-x",
        app_private_key=b"PRIVATEKEYBYTES",
        disable_password_auth=False,
    )
    joined = "\n".join(conn.commands)
    assert "PasswordAuthentication" not in joined
    assert "sshd -T" not in joined
    assert "whoami" in conn.commands


async def test_install_key_aborts_on_host_key_change(mocker):
    # Re-probe returns a different key than the one stored on the server.
    class ChangedKey(FakeKey):
        def export_public_key(self) -> bytes:
            return b"ssh-ed25519 DIFFERENTKEY"

    mocker.patch.object(
        ssh_mod.asyncssh,
        "get_server_host_key",
        mocker.AsyncMock(return_value=ChangedKey()),
    )
    server = FakeServer()
    with pytest.raises(ssh_mod.HostKeyChangedDuringInstall):
        await SSHService().install_key(
            server=server,
            password_from_client="hunter2",
            app_public_key="ssh-ed25519 AAAAAPPKEY",
            app_private_key=b"PRIVATEKEYBYTES",
            disable_password_auth=True,
        )


async def test_install_key_verification_fails_on_wrong_whoami(mocker):
    conn = FakeConn(whoami="ubuntu")  # server.username is root, mismatch
    mocker.patch.object(
        ssh_mod.asyncssh, "get_server_host_key", mocker.AsyncMock(return_value=FakeKey())
    )
    mocker.patch.object(ssh_mod.asyncssh, "connect", return_value=FakeConnect(conn))
    mocker.patch.object(ssh_mod.asyncssh, "import_known_hosts", return_value=object())
    mocker.patch.object(ssh_mod.asyncssh, "import_private_key", return_value=object())

    server = FakeServer()
    with pytest.raises(ssh_mod.KeyInstallVerificationFailed):
        await SSHService().install_key(
            server=server,
            password_from_client="hunter2",
            app_public_key="ssh-ed25519 AAAAAPPKEY",
            app_private_key=b"PRIVATEKEYBYTES",
            disable_password_auth=False,
        )


async def test_get_connection_cache_miss_then_hit(mocker):
    from tests.conftest import FakeRedis

    master_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    provider = EnvKeyProvider(master_key)
    encrypted = await provider.encrypt(b"PRIVATEKEYBYTES")

    fake_app_key = mocker.MagicMock()
    fake_app_key.encrypted_private_key = encrypted

    db = mocker.MagicMock()
    db.scalar = mocker.AsyncMock(return_value=fake_app_key)
    db.commit = mocker.AsyncMock()

    redis = FakeRedis()
    server = FakeServer()
    user_id = uuid4()
    session_id = "sess_test"

    conn = FakeConn()
    connect_mock = mocker.patch.object(
        ssh_mod.asyncssh, "connect", return_value=FakeConnect(conn)
    )
    mocker.patch.object(ssh_mod.asyncssh, "import_known_hosts", return_value=object())
    mocker.patch.object(ssh_mod.asyncssh, "import_private_key", return_value=object())

    service = SSHService()

    # Cache miss: loads key from db, decrypts, caches in redis, opens connection.
    c1 = await service.get_connection(server, user_id, session_id, redis, db, provider)
    assert c1 is conn
    assert connect_mock.call_count == 1
    assert db.scalar.call_count == 1
    assert await redis.get(f"ssh_key:{server.id}:{session_id}") == b"PRIVATEKEYBYTES"

    # Cache hit: pooled connection reused, no new connect, no new db lookup.
    c2 = await service.get_connection(server, user_id, session_id, redis, db, provider)
    assert c2 is conn
    assert connect_mock.call_count == 1
    assert db.scalar.call_count == 1


async def test_get_connection_reconnects_when_username_changed(mocker):
    """A pooled connection whose username no longer matches is dropped and reopened.

    Models a rolled-back sudo-user switch: the pooled connection was opened as one
    user but the server now reports a different one, so reusing it would run commands
    with the wrong identity.
    """
    from tests.conftest import FakeRedis

    master_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    provider = EnvKeyProvider(master_key)
    encrypted = await provider.encrypt(b"PRIVATEKEYBYTES")
    fake_app_key = mocker.MagicMock(encrypted_private_key=encrypted)

    db = mocker.MagicMock()
    db.scalar = mocker.AsyncMock(return_value=fake_app_key)
    db.commit = mocker.AsyncMock()

    redis = FakeRedis()
    server = FakeServer()
    user_id = uuid4()

    first_conn = FakeConn()
    second_conn = FakeConn()
    connect_mock = mocker.patch.object(
        ssh_mod.asyncssh,
        "connect",
        side_effect=[FakeConnect(first_conn), FakeConnect(second_conn)],
    )
    mocker.patch.object(ssh_mod.asyncssh, "import_known_hosts", return_value=object())
    mocker.patch.object(ssh_mod.asyncssh, "import_private_key", return_value=object())

    service = SSHService()

    c1 = await service.get_connection(server, user_id, "sess", redis, db, provider)
    assert c1 is first_conn
    assert connect_mock.call_count == 1

    # Identity changes (as create_sudo_user would do, then a rollback reverts the DB).
    server.username = "deploy"
    c2 = await service.get_connection(server, user_id, "sess", redis, db, provider)
    assert c2 is second_conn  # reconnected, not the stale root connection
    assert connect_mock.call_count == 2
    assert first_conn.closed is True  # stale connection was closed


EXPIRED_WARNING = (
    "WARNING: Your password has expired. "
    "Password change required but no TTY available."
)


class ExpiredConn(FakeConn):
    """A box whose password is expired: every command is refused until it is changed,
    so the first install command (mkdir) fails with the PAM expiry warning."""

    async def run(self, command, check=False):
        if "mkdir" in command:
            return FakeProcResult(stderr=EXPIRED_WARNING, exit_status=1)
        return await super().run(command, check=check)


def test_password_change_client_maps_prompts_to_old_and_new():
    client = ssh_mod._PasswordChangeClient("OLDPASS", "NEWPASS")
    # Plain password method: the server asks for a change during auth.
    assert client.password_change_requested("New password: ", "") == (
        "OLDPASS",
        "NEWPASS",
    )
    assert client.kbdint_auth_requested() == ""
    # The login prompt and the "current password" re-prompt take the old password.
    assert client.kbdint_challenge_received("", "", "", [("Password:", False)]) == [
        "OLDPASS"
    ]
    assert client.kbdint_challenge_received(
        "", "", "", [("(current) UNIX password:", False)]
    ) == ["OLDPASS"]
    # The chauthtok prompts take the new password.
    assert client.kbdint_challenge_received(
        "", "", "", [("New password:", False)]
    ) == ["NEWPASS"]
    assert client.kbdint_challenge_received(
        "", "", "", [("Retype new password:", False)]
    ) == ["NEWPASS"]
    # A combined challenge is answered position by position.
    assert client.kbdint_challenge_received(
        "",
        "",
        "",
        [
            ("Current password:", False),
            ("New password:", False),
            ("Retype new password:", False),
        ],
    ) == ["OLDPASS", "NEWPASS", "NEWPASS"]


async def test_install_key_detects_expired_password(mocker):
    conn = ExpiredConn()
    mocker.patch.object(
        ssh_mod.asyncssh, "get_server_host_key", mocker.AsyncMock(return_value=FakeKey())
    )
    mocker.patch.object(ssh_mod.asyncssh, "connect", return_value=FakeConnect(conn))
    mocker.patch.object(ssh_mod.asyncssh, "import_known_hosts", return_value=object())
    mocker.patch.object(ssh_mod.asyncssh, "import_private_key", return_value=object())

    server = FakeServer()
    with pytest.raises(ssh_mod.PasswordChangeRequired):
        await SSHService().install_key(
            server=server,
            password_from_client="expired",
            app_public_key="ssh-ed25519 AAAAAPPKEY app-deploy-x",
            app_private_key=b"PRIVATEKEYBYTES",
            disable_password_auth=False,
        )


async def test_install_key_with_new_password_uses_kbdint_change_connection(mocker):
    conn = FakeConn(whoami="root")
    mocker.patch.object(
        ssh_mod.asyncssh, "get_server_host_key", mocker.AsyncMock(return_value=FakeKey())
    )
    connect_mock = mocker.patch.object(
        ssh_mod.asyncssh, "connect", return_value=FakeConnect(conn)
    )
    mocker.patch.object(ssh_mod.asyncssh, "import_known_hosts", return_value=object())
    mocker.patch.object(ssh_mod.asyncssh, "import_private_key", return_value=object())

    server = FakeServer()
    await SSHService().install_key(
        server=server,
        password_from_client="expired",
        app_public_key="ssh-ed25519 AAAAAPPKEY app-deploy-x",
        app_private_key=b"PRIVATEKEYBYTES",
        disable_password_auth=False,
        new_password="freshpass",
    )

    # The change connection leaves both auth methods enabled (so the server can drive the
    # change via either the password method or keyboard-interactive) and wires up the PAM
    # prompt handler that answers old/new to both.
    change_call = connect_mock.call_args_list[0]
    assert change_call.kwargs.get("password_auth") is not False
    assert change_call.kwargs["kbdint_auth"] is True
    assert callable(change_call.kwargs["client_factory"])
    factory = change_call.kwargs["client_factory"]()
    assert isinstance(factory, ssh_mod._PasswordChangeClient)

    # The change completed during auth, so the install runs on this session (no PTY,
    # no reconnect) and key login is verified.
    joined = "\n".join(conn.commands)
    assert "authorized_keys" in joined
    assert "whoami" in conn.commands


async def test_install_key_with_new_password_surfaces_rejection(mocker):
    mocker.patch.object(
        ssh_mod.asyncssh, "get_server_host_key", mocker.AsyncMock(return_value=FakeKey())
    )
    mocker.patch.object(
        ssh_mod.asyncssh,
        "connect",
        side_effect=ssh_mod.asyncssh.PermissionDenied("new password rejected"),
    )
    mocker.patch.object(ssh_mod.asyncssh, "import_known_hosts", return_value=object())
    mocker.patch.object(ssh_mod.asyncssh, "import_private_key", return_value=object())

    server = FakeServer()
    with pytest.raises(ssh_mod.KeyInstallVerificationFailed):
        await SSHService().install_key(
            server=server,
            password_from_client="expired",
            app_public_key="ssh-ed25519 AAAAAPPKEY",
            app_private_key=b"PRIVATEKEYBYTES",
            disable_password_auth=False,
            new_password="weak",
        )


class FakeStdout:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, _n):
        return self._chunks.pop(0) if self._chunks else ""


class FakeStdin:
    def __init__(self):
        self.writes: list[str] = []

    def write(self, data):
        self.writes.append(data)

    def write_eof(self):
        pass


class FakeProcess:
    """Stands in for the PTY session asyncssh.create_process returns, replaying the
    prompts a login-shell forced password change emits."""

    def __init__(self, chunks):
        self.stdout = FakeStdout(chunks)
        self.stdin = FakeStdin()
        self.closed = False

    def close(self):
        self.closed = True


class SessionChauthtokConn(FakeConn):
    """A box that authenticates the old password but defers the expired-password change
    to the login shell: the first install command fails with the expiry signature, and
    the change is driven over the PTY returned by create_process."""

    def __init__(self, process):
        super().__init__()
        self.process = process

    async def run(self, command, check=False):
        if "mkdir" in command:
            return FakeProcResult(stderr=EXPIRED_WARNING, exit_status=1)
        return await super().run(command, check=check)

    async def create_process(self, term_type=None):
        return self.process


async def test_install_key_drives_session_level_change_over_pty(mocker):
    process = FakeProcess(
        [
            "You are required to change your password immediately.\r\n"
            "(current) UNIX password: ",
            "\r\nNew password: ",
            "\r\nRetype new password: ",
            "\r\npasswd: password updated successfully\r\n",
        ]
    )
    expired = SessionChauthtokConn(process)
    clean = FakeConn(whoami="root")  # reconnect + key-verify land here
    mocker.patch.object(
        ssh_mod.asyncssh, "get_server_host_key", mocker.AsyncMock(return_value=FakeKey())
    )
    connect_mock = mocker.patch.object(
        ssh_mod.asyncssh,
        "connect",
        side_effect=[FakeConnect(expired), FakeConnect(clean), FakeConnect(clean)],
    )
    mocker.patch.object(ssh_mod.asyncssh, "import_known_hosts", return_value=object())
    mocker.patch.object(ssh_mod.asyncssh, "import_private_key", return_value=object())

    await SSHService().install_key(
        server=FakeServer(),
        password_from_client="expired",
        app_public_key="ssh-ed25519 AAAAAPPKEY app-deploy-x",
        app_private_key=b"PRIVATEKEYBYTES",
        disable_password_auth=False,
        new_password="freshpass",
    )

    # The PTY conversation answered current -> old, new/retype -> new.
    assert process.stdin.writes == ["expired\n", "freshpass\n", "freshpass\n"]
    assert process.closed is True
    # After the change we reconnect as the *new* password and install there.
    reconnect_call = connect_mock.call_args_list[1]
    assert reconnect_call.kwargs["password"] == "freshpass"
    assert "authorized_keys" in "\n".join(clean.commands)
    assert "whoami" in clean.commands


def test_looks_like_shell_prompt():
    assert ssh_mod._looks_like_shell_prompt("root@ubuntu-s-1vcpu-512mb:~# ")
    assert ssh_mod._looks_like_shell_prompt("deploy@web1:~$")
    # A passwd prompt is not a shell prompt.
    assert not ssh_mod._looks_like_shell_prompt("(current) unix password: ")
    assert not ssh_mod._looks_like_shell_prompt("new password: ")


class FalsePositiveExpiryConn(FakeConn):
    """The box is healthy (auth landed us at a shell) but the first install command
    happened to fail with an expiry-looking message. Opening a PTY yields only a ready
    shell prompt, so no change is actually being forced."""

    def __init__(self, process):
        super().__init__()
        self.process = process

    async def run(self, command, check=False):
        if "mkdir" in command:
            return FakeProcResult(
                stderr="You are required to change your password", exit_status=1
            )
        return await super().run(command, check=check)

    async def create_process(self, term_type=None):
        return self.process


async def test_install_key_reraises_when_pty_finds_a_ready_shell(mocker):
    # The PTY drops straight to a shell prompt: no forced change, so we must surface the
    # real install failure instead of hanging or falsely reconnecting as the new password.
    process = FakeProcess(
        ["\r\n\x1b[?2004hroot@ubuntu-s-1vcpu-512mb-10gb-fra1:~# "]
    )
    conn = FalsePositiveExpiryConn(process)
    mocker.patch.object(
        ssh_mod.asyncssh, "get_server_host_key", mocker.AsyncMock(return_value=FakeKey())
    )
    connect_mock = mocker.patch.object(
        ssh_mod.asyncssh, "connect", return_value=FakeConnect(conn)
    )
    mocker.patch.object(ssh_mod.asyncssh, "import_known_hosts", return_value=object())
    mocker.patch.object(ssh_mod.asyncssh, "import_private_key", return_value=object())

    with pytest.raises(ssh_mod.KeyInstallVerificationFailed):
        await SSHService().install_key(
            server=FakeServer(),
            password_from_client="expired",
            app_public_key="ssh-ed25519 AAAAAPPKEY app-deploy-x",
            app_private_key=b"PRIVATEKEYBYTES",
            disable_password_auth=False,
            new_password="freshpass",
        )

    # No reconnect as the new password happened, and we did not answer any prompt.
    assert connect_mock.call_count == 1
    assert process.stdin.writes == []


async def test_get_connection_flags_key_mismatch_on_host_key_change(mocker):
    """A changed host key surfaces as HostKeyNotVerifiable. get_connection must flip the
    server to key_mismatch, commit, and raise HostKeyMismatch so the operation aborts and
    every later operation stays blocked until the user re-establishes trust."""
    from tests.conftest import FakeRedis

    master_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    provider = EnvKeyProvider(master_key)
    encrypted = await provider.encrypt(b"PRIVATEKEYBYTES")
    fake_app_key = mocker.MagicMock(encrypted_private_key=encrypted)

    db = mocker.MagicMock()
    db.scalar = mocker.AsyncMock(return_value=fake_app_key)
    db.commit = mocker.AsyncMock()

    mocker.patch.object(
        ssh_mod.asyncssh,
        "connect",
        side_effect=ssh_mod.asyncssh.HostKeyNotVerifiable("host key changed"),
    )
    mocker.patch.object(ssh_mod.asyncssh, "import_known_hosts", return_value=object())
    mocker.patch.object(ssh_mod.asyncssh, "import_private_key", return_value=object())

    server = FakeServer()
    assert server.status == "verified"
    with pytest.raises(ssh_mod.HostKeyMismatch):
        await SSHService().get_connection(
            server, uuid4(), "sess", FakeRedis(), db, provider
        )
    assert server.status == "key_mismatch"
    db.commit.assert_awaited_once()


async def test_run_command_returns_result(mocker):
    from tests.conftest import FakeRedis

    master_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    provider = EnvKeyProvider(master_key)
    encrypted = await provider.encrypt(b"PRIVATEKEYBYTES")
    fake_app_key = mocker.MagicMock(encrypted_private_key=encrypted)

    db = mocker.MagicMock()
    db.scalar = mocker.AsyncMock(return_value=fake_app_key)
    db.commit = mocker.AsyncMock()

    conn = FakeConn()
    mocker.patch.object(ssh_mod.asyncssh, "connect", return_value=FakeConnect(conn))
    mocker.patch.object(ssh_mod.asyncssh, "import_known_hosts", return_value=object())
    mocker.patch.object(ssh_mod.asyncssh, "import_private_key", return_value=object())

    result = await SSHService().run_command(
        FakeServer(),
        uuid4(),
        "sess_test",
        "echo 'hello world'",
        FakeRedis(),
        db,
        provider,
    )
    assert result.exit_status == 0
    assert "hello" in result.stdout
