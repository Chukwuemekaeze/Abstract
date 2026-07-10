"""Shared fakes for project provisioning tests.

The SSH connection fake routes commands by substring so each test can script
just the step it cares about (git present, clone path occupied, clone failing)
while everything else succeeds. The SFTP fake records written bytes so tests
can assert the private key landed intact.
"""

from types import SimpleNamespace


def result(stdout: str = "", stderr: str = "", exit_status: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, exit_status=exit_status)


class FakeSftpFile:
    def __init__(self):
        self.written = b""

    async def write(self, data: bytes) -> None:
        self.written += data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeSftp:
    def __init__(self):
        self.file = FakeSftpFile()
        self.opened_paths: list[str] = []
        self.exited = False

    def open(self, path: str, mode: str):
        self.opened_paths.append(path)
        return self.file

    def exit(self) -> None:
        self.exited = True


def make_conn(mocker, overrides: dict | None = None):
    """A fake asyncssh connection whose run() is scripted by substring match.

    overrides maps a command substring to either a result(...) to return or an
    Exception to raise. First match wins; unmatched commands succeed with the
    defaults (git installed, clone path missing, clone verification passing).
    """
    overrides = overrides or {}
    sftp = FakeSftp()

    async def run(command: str, check: bool = False, timeout: int | None = None):
        for needle, outcome in overrides.items():
            if needle in command:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        if "command -v git" in command:
            return result("yes\n")
        if "/.git" in command:
            return result("yes\n")
        if "test -d" in command:
            return result("missing\n")
        return result("")

    conn = mocker.MagicMock()
    conn.run = mocker.AsyncMock(side_effect=run)
    conn.start_sftp_client = mocker.AsyncMock(return_value=sftp)
    conn.sftp = sftp
    return conn


def make_ssh(mocker, conn):
    ssh = mocker.MagicMock()
    ssh.get_connection = mocker.AsyncMock(return_value=conn)
    return ssh


def make_github(mocker, deploy_key_id: int = 4242):
    github = mocker.MagicMock()
    github.add_deploy_key = mocker.AsyncMock(return_value=deploy_key_id)
    github.delete_deploy_key = mocker.AsyncMock(return_value=None)
    github.list_admin_repos = mocker.AsyncMock(return_value=[])
    github.get_ssh_host_keys = mocker.AsyncMock(
        return_value=["ssh-ed25519 AAAAghtestkey"]
    )
    return github


def ran_commands(conn) -> list[str]:
    return [call.args[0] for call in conn.run.await_args_list]
