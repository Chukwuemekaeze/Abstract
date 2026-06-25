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
            return FakeProcResult(stdout="hello from deployment pipeline\n")
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

    conn = FakeConn()
    connect_mock = mocker.patch.object(
        ssh_mod.asyncssh, "connect", return_value=FakeConnect(conn)
    )
    mocker.patch.object(ssh_mod.asyncssh, "import_known_hosts", return_value=object())
    mocker.patch.object(ssh_mod.asyncssh, "import_private_key", return_value=object())

    service = SSHService()

    # Cache miss: loads key from db, decrypts, caches in redis, opens connection.
    c1 = await service.get_connection(server, user_id, redis, db, provider)
    assert c1 is conn
    assert connect_mock.call_count == 1
    assert db.scalar.call_count == 1
    assert await redis.get(f"ssh_key:{user_id}:dev-session") == b"PRIVATEKEYBYTES"

    # Cache hit: pooled connection reused, no new connect, no new db lookup.
    c2 = await service.get_connection(server, user_id, redis, db, provider)
    assert c2 is conn
    assert connect_mock.call_count == 1
    assert db.scalar.call_count == 1


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
        "echo 'hello world'",
        FakeRedis(),
        db,
        provider,
    )
    assert result.exit_status == 0
    assert "hello" in result.stdout
