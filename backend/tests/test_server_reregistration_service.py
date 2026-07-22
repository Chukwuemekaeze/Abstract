"""Unit tests for the password-based re-registration engine.

asyncssh is fully faked, so these run with no network and no DB. They cover the prompt
classifier, the forced-change client, the access branches (A clean, B kbdint, B PTY,
C no password auth, disconnect-after-retype), verification-driven retries, and the
post-access helpers.
"""

import base64
import secrets
from uuid import uuid4

import pytest

from app.services import server_reregistration_service as srv
from app.services.key_provider import EnvKeyProvider

HOST_KEY_BYTES = b"ssh-ed25519 AAAAPENDINGKEY"


class FakeProc:
    def __init__(self, stdout="", stderr="", exit_status=0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status


class FakeStdout:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, _n):
        return self._chunks.pop(0) if self._chunks else ""


class FakeStdin:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data)

    def write_eof(self):
        pass


class FakeProcess:
    def __init__(self, chunks):
        self.stdout = FakeStdout(chunks)
        self.stdin = FakeStdin()
        self.closed = False

    def close(self):
        self.closed = True


class Conn:
    """An async-context-manager connection. whoami returns the configured identity;
    create_process yields the configured PTY process."""

    def __init__(self, whoami="root", whoami_exit=0, whoami_err="", process=None):
        self._whoami = whoami
        self._whoami_exit = whoami_exit
        self._whoami_err = whoami_err
        self.process = process
        self.commands = []
        self.closed = False

    async def run(self, command, check=False):
        self.commands.append(command)
        if command.strip() == "whoami":
            return FakeProc(
                stdout=f"{self._whoami}\n" if self._whoami else "",
                stderr=self._whoami_err,
                exit_status=self._whoami_exit,
            )
        return FakeProc(stdout="", stderr="", exit_status=0)

    async def create_process(self, term_type=None):
        return self.process

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    def close(self):
        self.closed = True


class FakeConnect:
    """Both awaitable (conn = await connect(...)) and an async CM (async with)."""

    def __init__(self, conn):
        self._conn = conn

    def __await__(self):
        async def _inner():
            return self._conn

        return _inner().__await__()

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        self._conn.closed = True
        return False


class FakeServer:
    def __init__(self):
        self.id = uuid4()
        self.host = "203.0.113.10"
        self.port = 22
        self.username = "root"
        self.pending_host_key = HOST_KEY_BYTES


@pytest.fixture(autouse=True)
def _patch_import_helpers(mocker):
    # These parse real key material; stub them since the fake conn ignores them.
    mocker.patch.object(srv.asyncssh, "import_known_hosts", return_value=object())
    mocker.patch.object(srv.asyncssh, "import_private_key", return_value=object())


# --- classifier ------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("(current) UNIX password:", "current"),
        ("Current password:", "current"),
        ("Password:", "current"),
        ("New password:", "new"),
        ("Enter new UNIX password:", "new"),
        ("Retype new password:", "retype"),
        ("Re-enter new password:", "retype"),
        ("New password again:", "retype"),
        ("  NEW PASSWORD:  ", "new"),
        ("RETYPE NEW PASSWORD:", "retype"),
    ],
)
def test_classify_prompt(prompt, expected):
    assert srv.classify_prompt(prompt) == expected


def test_forced_change_client_maps_prompts_regardless_of_order():
    client = srv._ForcedChangeClient("USERPASS", "GENPASS")
    # A single combined challenge, answered position by position.
    assert client.kbdint_challenge_received(
        "",
        "",
        "",
        [
            ("Current password:", False),
            ("New password:", False),
            ("Retype new password:", False),
        ],
    ) == ["USERPASS", "GENPASS", "GENPASS"]
    # An unexpected order still classifies by text, not position.
    assert client.kbdint_challenge_received(
        "", "", "", [("Retype new password:", False), ("Password:", False)]
    ) == ["GENPASS", "USERPASS"]
    assert client.change_requested is True


def test_forced_change_client_records_offered_methods():
    client = srv._ForcedChangeClient("u", "g")
    assert client.password_offered is False
    assert client.password_auth_requested() == "u"
    assert client.password_offered is True


def test_generate_bootstrap_password_meets_complexity():
    for _ in range(20):
        pw = srv.generate_bootstrap_password()
        assert len(pw) == 32
        assert any(c.isupper() for c in pw)
        assert any(c.islower() for c in pw)
        assert any(c.isdigit() for c in pw)
        assert any(not c.isalnum() for c in pw)


# --- access branches -------------------------------------------------------


async def test_branch_a_clean_login_returns_user_password(mocker):
    def connect(**kwargs):
        client = kwargs["client_factory"]()
        client.password_auth_requested()  # server offered password, no change
        return FakeConnect(Conn(whoami="root"))

    mocker.patch.object(srv.asyncssh, "connect", side_effect=connect)
    result = await srv._open_access(FakeServer(), "userpass", "genpass")
    assert result.changed is False
    assert result.working_password == "userpass"


async def test_branch_b_kbdint_change_returns_generated(mocker):
    def connect(**kwargs):
        client = kwargs["client_factory"]()
        # The server drives a change during keyboard-interactive auth.
        client.kbdint_challenge_received(
            "", "", "", [("New password:", False), ("Retype new password:", False)]
        )
        return FakeConnect(Conn(whoami="root"))

    mocker.patch.object(srv.asyncssh, "connect", side_effect=connect)
    result = await srv._open_access(FakeServer(), "userpass", "genpass")
    assert result.changed is True
    assert result.working_password == "genpass"


async def test_branch_b_pty_change_returns_generated(mocker):
    process = FakeProcess(
        [
            "You are required to change your password immediately.\r\n"
            "(current) UNIX password: ",
            "\r\nNew password: ",
            "\r\nRetype new password: ",
            "\r\npasswd: password updated successfully\r\n",
        ]
    )
    # Auth succeeds on the old password; the login shell forces the change, so whoami
    # comes back with the expiry signature and the PTY drives it.
    conn = Conn(whoami="", whoami_exit=1, whoami_err="Your password has expired", process=process)

    def connect(**kwargs):
        client = kwargs["client_factory"]()
        client.password_auth_requested()
        return FakeConnect(conn)

    mocker.patch.object(srv.asyncssh, "connect", side_effect=connect)
    result = await srv._open_access(FakeServer(), "expired", "genpass")
    assert result.changed is True
    assert result.working_password == "genpass"
    assert process.stdin.writes == ["expired\n", "genpass\n", "genpass\n"]


async def test_branch_c_password_auth_unavailable(mocker):
    def connect(**kwargs):
        # The client hooks are never called: the server offered only publickey.
        raise srv.asyncssh.PermissionDenied("no matching auth")

    mocker.patch.object(srv.asyncssh, "connect", side_effect=connect)
    with pytest.raises(srv.ReregistrationError) as exc:
        await srv._open_access(FakeServer(), "userpass", "genpass")
    assert exc.value.code == "PASSWORD_AUTH_UNAVAILABLE"


async def test_auth_failed_when_password_offered_but_rejected(mocker):
    def connect(**kwargs):
        client = kwargs["client_factory"]()
        client.password_auth_requested()  # offered, but wrong password
        raise srv.asyncssh.PermissionDenied("bad password")

    mocker.patch.object(srv.asyncssh, "connect", side_effect=connect)
    with pytest.raises(srv.ReregistrationError) as exc:
        await srv._open_access(FakeServer(), "wrongpass", "genpass")
    assert exc.value.code == "AUTH_FAILED"


async def test_disconnect_after_change_is_probable_success(mocker):
    def connect(**kwargs):
        client = kwargs["client_factory"]()
        # The change was answered during kbdint, then the server dropped the connection.
        client.kbdint_challenge_received("", "", "", [("New password:", False)])
        raise srv.asyncssh.ConnectionLost("closed after change")

    mocker.patch.object(srv.asyncssh, "connect", side_effect=connect)
    result = await srv._open_access(FakeServer(), "userpass", "genpass")
    assert result.changed is True
    assert result.working_password == "genpass"


# --- verification-driven retries ------------------------------------------


async def test_run_exchange_retries_when_change_did_not_take(mocker):
    mocker.patch.object(
        srv,
        "_open_access",
        mocker.AsyncMock(return_value=srv.AccessResult("genpass", changed=True)),
    )
    # First verify(generated) fails, verify(user) works -> retry; second verify(generated)
    # works.
    verify = mocker.patch.object(
        srv, "_verify_password", mocker.AsyncMock(side_effect=[False, True, True])
    )
    result = await srv.run_exchange_and_verify(FakeServer(), "userpass", "genpass")
    assert result == "genpass"
    assert verify.await_count == 3


async def test_run_exchange_locked_out_when_neither_password_works(mocker):
    mocker.patch.object(
        srv,
        "_open_access",
        mocker.AsyncMock(return_value=srv.AccessResult("genpass", changed=True)),
    )
    mocker.patch.object(
        srv, "_verify_password", mocker.AsyncMock(return_value=False)
    )
    with pytest.raises(srv.ReregistrationError) as exc:
        await srv.run_exchange_and_verify(FakeServer(), "userpass", "genpass")
    assert exc.value.code == "LOCKED_OUT"


async def test_run_exchange_branch_a_verifies_user_password(mocker):
    mocker.patch.object(
        srv,
        "_open_access",
        mocker.AsyncMock(return_value=srv.AccessResult("userpass", changed=False)),
    )
    mocker.patch.object(
        srv, "_verify_password", mocker.AsyncMock(return_value=True)
    )
    result = await srv.run_exchange_and_verify(FakeServer(), "userpass", "genpass")
    assert result == "userpass"


# --- host key recheck and resume -------------------------------------------


async def test_recheck_pending_host_key_detects_swap(mocker):
    from app.services.ssh_service import ProbeResult

    ssh = mocker.MagicMock()
    ssh.probe = mocker.AsyncMock(
        return_value=ProbeResult(
            host_key=b"ssh-ed25519 DIFFERENT",
            host_key_type="ssh-ed25519",
            fingerprint_sha256="SHA256:x",
        )
    )
    with pytest.raises(srv.ReregistrationError) as exc:
        await srv.recheck_pending_host_key(FakeServer(), ssh)
    assert exc.value.code == "HOST_KEY_CHANGED_AGAIN"


async def test_recheck_pending_host_key_maps_probe_error(mocker):
    from app.services.ssh_service import ProbeError

    ssh = mocker.MagicMock()
    ssh.probe = mocker.AsyncMock(side_effect=ProbeError("unreachable"))
    with pytest.raises(srv.ReregistrationError) as exc:
        await srv.recheck_pending_host_key(FakeServer(), ssh)
    assert exc.value.code == "NETWORK_UNREACHABLE"
    assert exc.value.retryable is True


async def test_try_resume_with_pending_key_true_on_whoami_root(mocker):
    mocker.patch.object(
        srv.asyncssh, "connect", side_effect=lambda **k: FakeConnect(Conn(whoami="root"))
    )
    assert await srv.try_resume_with_pending_key(FakeServer(), b"PRIV") is True


async def test_verify_password_false_on_connection_error(mocker):
    mocker.patch.object(
        srv.asyncssh, "connect", side_effect=OSError("refused")
    )
    assert await srv.verify_password_for_resume(FakeServer(), "pw") is False


# --- post-access helpers ---------------------------------------------------


async def test_install_public_key_appends_idempotently(mocker):
    conn = Conn(whoami="root")
    mocker.patch.object(
        srv.asyncssh, "connect", side_effect=lambda **k: FakeConnect(conn)
    )
    await srv.install_public_key(FakeServer(), "genpass", "ssh-ed25519 AAAAKEY comment")
    joined = "\n".join(conn.commands)
    assert "mkdir -p ~/.ssh" in joined
    assert "authorized_keys" in joined
    assert "chmod 600 ~/.ssh/authorized_keys" in joined


async def test_evict_stale_ssh_state_clears_pool_and_cache(mocker):
    class FakeRedis:
        def __init__(self):
            self.store = {}

        async def scan_iter(self, match=None):
            prefix = match.rstrip("*")
            for key in list(self.store):
                if key.startswith(prefix):
                    yield key

        async def delete(self, key):
            self.store.pop(key, None)

    server = FakeServer()
    redis = FakeRedis()
    redis.store[f"ssh_key:{server.id}:sess_a"] = b"x"
    redis.store[f"ssh_key:{server.id}:sess_b"] = b"y"
    redis.store["ssh_key:other:sess"] = b"z"

    ssh = mocker.MagicMock()
    await srv.evict_stale_ssh_state(server, uuid4(), "sess_a", redis, ssh)

    ssh.evict_connection.assert_called_once()
    assert f"ssh_key:{server.id}:sess_a" not in redis.store
    assert f"ssh_key:{server.id}:sess_b" not in redis.store
    assert "ssh_key:other:sess" in redis.store  # a different server is untouched


def test_error_codes_have_user_copy_without_leaky_terms():
    for factory in (
        srv._host_key_changed_again,
        srv._auth_failed,
        srv._password_auth_unavailable,
        srv._change_incomplete,
        srv._locked_out,
        srv._network_unreachable,
    ):
        err = factory()
        lowered = err.message.lower()
        for leak in ("ssh key", "pam", "keyboard-interactive", "kbdint"):
            assert leak not in lowered
    assert srv._network_unreachable().retryable is True


def _provider():
    master_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    return EnvKeyProvider(master_key)
