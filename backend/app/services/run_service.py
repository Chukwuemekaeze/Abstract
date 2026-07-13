"""Run a project's app with docker compose over the pooled SSH connection.

The caller owns the transaction; this module never commits. Env files are
written to the VPS via SFTP (values must never pass through shell quoting)
with mode 600, then `docker compose up -d --build` runs in the clone
directory. On failure we deliberately do NOT `docker compose down`: the user
may have containers running from a previous successful start, and tearing
them down would turn a failed retry into an outage. The DB rollback is the
caller's job; env files left on the VPS are harmless and idempotent to
rewrite.

Nothing in this module logs an env var value, decrypted dict, or file body.
Build output is treated the same way: it may contain secrets a user echoed
during a build step, so it is returned to the caller but never logged.
"""

import json
import posixpath
import shlex
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import asyncssh
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_config import logger
from app.models import Project
from app.services import env_file_service
from app.services.key_provider import KeyProvider

__all__ = [
    "COMPOSE_FILE_CANDIDATES",
    "DANGEROUS_PORTS",
    "RunServiceError",
    "ComposeFileNotFound",
    "EnvFileKeyCollision",
    "ComposeUpFailed",
    "ContainerNotRunning",
    "ComposeConfigInvalid",
    "DetectedPort",
    "RunResult",
    "BUILD_OUTPUT_MAX_BYTES",
    "truncate_build_output",
    "detect_compose_file",
    "write_env_files_to_vps",
    "run_compose_up",
    "verify_containers_running",
    "start_project",
    "refresh_status",
    "get_detected_ports",
]

COMPOSE_FILE_CANDIDATES = [
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
]

# Ports that look like databases or caches: exposed publicly by accident far
# more often than on purpose. The API still reports them (is_dangerous=True);
# the UI hides them behind manual entry.
DANGEROUS_PORTS = frozenset({5432, 3306, 27017, 6379, 11211, 9200})

_TIMEOUT_CHECK = 30
_TIMEOUT_COMPOSE_UP = 900  # 15 minutes: first builds on small VPSes are slow
_TIMEOUT_PS = 60
_TIMEOUT_LOGS = 30

# Cap the build transcript returned to the client. The tail is kept because
# build errors land at the end; a marker tells the user output was cut.
BUILD_OUTPUT_MAX_BYTES = 200 * 1024


def truncate_build_output(output: str) -> str:
    """Keep the last BUILD_OUTPUT_MAX_BYTES of the transcript, prefixed with a
    marker when truncated. Byte-based (utf-8 chars vary in width); decoding the
    tail with errors="replace" safely handles a multi-byte char split at the
    cut point."""
    encoded = output.encode("utf-8", errors="replace")
    if len(encoded) <= BUILD_OUTPUT_MAX_BYTES:
        return output
    tail = encoded[-BUILD_OUTPUT_MAX_BYTES:].decode("utf-8", errors="replace")
    marker = (
        f"[Output truncated. Showing last {BUILD_OUTPUT_MAX_BYTES // 1024}KB "
        f"of {len(encoded) // 1024}KB total.]\n\n"
    )
    return marker + tail


class RunServiceError(Exception):
    pass


class ComposeFileNotFound(RunServiceError):
    pass


class EnvFileKeyCollision(RunServiceError):
    def __init__(self, key: str, files: list[str]):
        self.key = key
        self.files = files
        super().__init__(
            f"variable {key!r} is defined with different values in "
            f"{', '.join(sorted(files))}; merged root .env would be ambiguous"
        )


class ComposeUpFailed(RunServiceError):
    def __init__(self, captured_output: str):
        self.captured_output = captured_output
        super().__init__("docker compose up failed")


class ContainerNotRunning(RunServiceError):
    def __init__(self, captured_output: str):
        self.captured_output = captured_output
        super().__init__("one or more containers are not running")


class ComposeConfigInvalid(RunServiceError):
    def __init__(self, captured_output: str):
        self.captured_output = captured_output
        super().__init__("docker compose could not read the compose file")


@dataclass
class DetectedPort:
    service: str
    host_port: int
    container_port: int
    is_dangerous: bool


@dataclass
class RunResult:
    runtime_status: str
    started_at: datetime | None
    captured_output: str | None
    build_output: str | None = None


async def _run(
    conn: asyncssh.SSHClientConnection, command: str, timeout: int = _TIMEOUT_CHECK
) -> asyncssh.SSHCompletedProcess:
    """Run as the login (sudo) user. Never sudo: the clone dir and docker
    socket access both belong to that user."""
    return await conn.run(command, check=False, timeout=timeout)


def _compose_prefix(compose_file: str) -> str:
    """`docker compose` for the default names (compose resolves them itself),
    `docker compose -f <file>` for overrides."""
    if compose_file in COMPOSE_FILE_CANDIDATES:
        return "docker compose"
    return f"docker compose -f {shlex.quote(compose_file)}"


async def detect_compose_file(
    conn: asyncssh.SSHClientConnection,
    clone_path: str,
    override_path_from_db: str | None,
) -> str:
    candidates = [override_path_from_db] if override_path_from_db else COMPOSE_FILE_CANDIDATES
    for candidate in candidates:
        full = shlex.quote(posixpath.join(clone_path, candidate))
        result = await _run(conn, f"test -f {full} && echo yes || echo no")
        if (result.stdout or "").strip() == "yes":
            return candidate
    if override_path_from_db:
        raise ComposeFileNotFound(
            f"the configured compose file {override_path_from_db!r} does not exist "
            "in the project directory"
        )
    raise ComposeFileNotFound(
        "no compose file found; expected one of " + ", ".join(COMPOSE_FILE_CANDIDATES)
    )


def _check_key_collisions(decrypted_vars: dict[str, dict[str, str]]) -> None:
    seen: dict[str, tuple[str, str]] = {}
    for file_path, variables in decrypted_vars.items():
        for key, value in variables.items():
            if key in seen and seen[key][1] != value:
                raise EnvFileKeyCollision(key, [seen[key][0], file_path])
            seen.setdefault(key, (file_path, value))


def _render_dotenv(variables: dict[str, str]) -> bytes:
    # KEY=value, one per line, no quoting: docker compose reads this reliably
    # and newlines in values were rejected at the validation boundary.
    return "".join(f"{key}={value}\n" for key, value in variables.items()).encode()


async def _sftp_write(
    conn: asyncssh.SSHClientConnection, remote_path: str, content: bytes
) -> None:
    sftp = await conn.start_sftp_client()
    try:
        async with sftp.open(remote_path, "wb") as f:
            await f.write(content)
    finally:
        sftp.exit()


async def write_env_files_to_vps(
    conn: asyncssh.SSHClientConnection,
    project: Project,
    decrypted_vars: dict[str, dict[str, str]],
) -> None:
    """Write every env file, then a merged root .env if the user has none.

    The merged .env exists so ${VAR} substitution in docker-compose.yml works
    without the user thinking about which file compose implicitly loads.
    """
    _check_key_collisions(decrypted_vars)

    clone_path = project.clone_path
    to_write = dict(decrypted_vars)
    if ".env" not in to_write:
        union: dict[str, str] = {}
        for variables in decrypted_vars.values():
            union.update(variables)
        if union:
            to_write[".env"] = union

    for file_path, variables in to_write.items():
        # Re-validate right before touching the filesystem; the DB rows were
        # validated at write time but this is the last line of defense.
        env_file_service.validate_path_within_clone(file_path, clone_path)
        absolute = posixpath.join(clone_path, file_path)
        quoted = shlex.quote(absolute)
        parent = shlex.quote(posixpath.dirname(absolute))
        await _run(conn, f"mkdir -p {parent}")
        existed = await _run(conn, f"test -f {quoted} && echo yes || echo no")
        if (existed.stdout or "").strip() == "yes":
            logger.warning(
                "Overwriting existing file {} on server for project {}",
                file_path,
                project.id,
            )
        await _sftp_write(conn, absolute, _render_dotenv(variables))
        await _run(conn, f"chmod 600 {quoted}")


async def run_compose_up(
    conn: asyncssh.SSHClientConnection, clone_path: str, compose_file: str
) -> tuple[int, str]:
    """Run `docker compose up -d --build`, returning (exit_code, transcript).

    A nonzero build exit is a normal result the caller decides on, so it is
    returned rather than raised; only preconditions (docker missing) and
    transport failures raise ComposeUpFailed. stderr is folded into stdout at
    the SSH channel so the transcript keeps its interleaved line ordering.
    """
    precheck = await _run(conn, "docker compose version")
    if precheck.exit_status not in (0, None):
        raise ComposeUpFailed(
            truncate_build_output(
                "docker compose is not available for this user. Make sure Docker "
                "is installed via hardening and the sudo user is in the docker "
                "group (re-running the Docker hardening step fixes this).\n"
                f"{precheck.stdout or ''}{precheck.stderr or ''}".rstrip()
            )
        )

    # --remove-orphans makes the current compose file the source of truth:
    # containers from services since removed from the file are cleaned up.
    # It only touches this compose project and is a no-op when there are none.
    command = (
        f"cd {shlex.quote(clone_path)} && "
        f"{_compose_prefix(compose_file)} up -d --build --remove-orphans"
    )
    started = time.monotonic()
    try:
        result = await conn.run(
            command,
            check=False,
            timeout=_TIMEOUT_COMPOSE_UP,
            stderr=asyncssh.STDOUT,
        )
    except (TimeoutError, asyncssh.Error, OSError) as exc:
        raise ComposeUpFailed(
            truncate_build_output(f"docker compose up did not complete: {exc}")
        ) from exc
    duration = time.monotonic() - started
    # Prod folds stderr into stdout; the concat also reconstructs the fake
    # conn's separate streams in tests. The transcript itself is never logged.
    output = f"{result.stdout or ''}{result.stderr or ''}".rstrip()
    exit_code = result.exit_status if result.exit_status is not None else 0
    logger.info(
        "Compose up finished for {}: exit={} duration={:.1f}s",
        clone_path,
        exit_code,
        duration,
    )
    return exit_code, output


def _parse_compose_ps(stdout: str) -> list[dict]:
    """Handle both output shapes of `docker compose ps --format json`: newer
    versions emit NDJSON (one object per line), older ones a JSON array."""
    text = (stdout or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else [parsed]
    services: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, list):
            services.extend(e for e in entry if isinstance(e, dict))
        elif isinstance(entry, dict):
            services.append(entry)
    return services


def _service_name(entry: dict) -> str:
    return entry.get("Service") or entry.get("Name") or "unknown"


async def _compose_ps(
    conn: asyncssh.SSHClientConnection, clone_path: str, compose_file: str
) -> tuple[list[dict], str]:
    command = (
        f"cd {shlex.quote(clone_path)} && "
        f"{_compose_prefix(compose_file)} ps -a --format json"
    )
    result = await _run(conn, command, timeout=_TIMEOUT_PS)
    output = f"{result.stdout or ''}{result.stderr or ''}".rstrip()
    if result.exit_status not in (0, None):
        raise ContainerNotRunning(output)
    return _parse_compose_ps(result.stdout or ""), output


async def _defined_services(
    conn: asyncssh.SSHClientConnection, clone_path: str, compose_file: str
) -> set[str]:
    """The services the current compose file DEFINES (its source of truth),
    via `docker compose config --services`. Raises ComposeConfigInvalid if the
    file cannot be read so the caller can surface a clear message rather than a
    confusing verification failure."""
    command = (
        f"cd {shlex.quote(clone_path)} && "
        f"{_compose_prefix(compose_file)} config --services"
    )
    result = await _run(conn, command, timeout=_TIMEOUT_CHECK)
    output = f"{result.stdout or ''}{result.stderr or ''}".rstrip()
    if result.exit_status not in (0, None):
        raise ComposeConfigInvalid(truncate_build_output(output))
    return {line.strip() for line in (result.stdout or "").splitlines() if line.strip()}


def _evaluate_services(
    defined: set[str], entries: list[dict]
) -> tuple[dict[str, dict], list[str], list[str]]:
    """Judge ps output against the defined services. Entries whose Service is
    not defined (orphans from removed services, or other compose projects) are
    dropped. Returns (by_service, missing, not_running)."""
    by_service = {
        entry["Service"]: entry
        for entry in entries
        if entry.get("Service") in defined
    }
    missing = sorted(defined - set(by_service))
    not_running = sorted(
        name for name, entry in by_service.items() if entry.get("State") != "running"
    )
    return by_service, missing, not_running


async def verify_containers_running(
    conn: asyncssh.SSHClientConnection, clone_path: str, compose_file: str
) -> tuple[bool, str | None]:
    defined = await _defined_services(conn, clone_path, compose_file)
    entries, _ = await _compose_ps(conn, clone_path, compose_file)
    by_service, missing, not_running = _evaluate_services(defined, entries)
    if not missing and not not_running:
        return True, None

    chunks: list[str] = []
    for name in missing:
        chunks.append(f"Service {name} is defined in compose but was not created.")
    for name in not_running:
        entry = by_service[name]
        chunks.append(f"service {name}: state={entry.get('State', 'unknown')}")
        # Logs are fetched only for defined, running-expected services, so a
        # stale orphan never triggers a "no such service" error. Guard anyway.
        try:
            logs = await _run(
                conn,
                f"cd {shlex.quote(clone_path)} && "
                f"{_compose_prefix(compose_file)} logs --tail 50 {shlex.quote(name)}",
                timeout=_TIMEOUT_LOGS,
            )
            body = f"{logs.stdout or ''}{logs.stderr or ''}".rstrip()
            if logs.exit_status not in (0, None) or "no such service" in body.lower():
                chunks.append(f"[Could not fetch logs for service {name}: {body}]")
            else:
                chunks.append(body)
        except (TimeoutError, asyncssh.Error, OSError) as exc:
            chunks.append(f"[Could not fetch logs for service {name}: {exc}]")
    return False, "\n".join(chunks)


async def start_project(
    *,
    conn: asyncssh.SSHClientConnection,
    project: Project,
    db: AsyncSession,
    key_provider: KeyProvider,
) -> RunResult:
    """Detect compose file, write env files, compose up, verify, mark running.

    Raises ComposeFileNotFound, EnvFileKeyCollision, ComposeUpFailed, or
    ContainerNotRunning; the caller's rollback keeps runtime_status at
    whatever it was. Env files already written to the VPS stay: rewriting
    them on retry is idempotent and they are readable only by the app user.
    """
    compose_file = await detect_compose_file(
        conn, project.clone_path, project.compose_file_path
    )
    decrypted_vars = await env_file_service.get_decrypted_variables(
        db, project, key_provider
    )
    await write_env_files_to_vps(conn, project, decrypted_vars)
    exit_code, build_output = await run_compose_up(
        conn, project.clone_path, compose_file
    )
    if exit_code != 0:
        # The build failed; the transcript is the whole story. Do not also
        # fetch container logs.
        raise ComposeUpFailed(truncate_build_output(build_output))

    ok, container_logs = await verify_containers_running(
        conn, project.clone_path, compose_file
    )
    if not ok:
        # Build succeeded but a container did not come up: the user needs both
        # the build transcript and the container logs to see why.
        combined = build_output
        if container_logs:
            combined = (
                f"{build_output}\n\n--- container logs ---\n\n{container_logs}"
            )
        raise ContainerNotRunning(truncate_build_output(combined))

    now = datetime.now(timezone.utc)
    project.runtime_status = "running"
    project.started_at = now
    project.updated_at = now
    return RunResult(
        runtime_status="running",
        started_at=now,
        captured_output=None,
        build_output=truncate_build_output(build_output),
    )


async def refresh_status(
    *,
    conn: asyncssh.SSHClientConnection,
    project: Project,
) -> Project:
    """Re-derive runtime_status from `docker compose ps`. A project that was
    never started stays never_started when nothing is running; any error
    talking to docker maps to failed because we can no longer vouch for it."""
    evaluated: tuple[dict[str, dict], list[str], list[str]] | None
    try:
        compose_file = await detect_compose_file(
            conn, project.clone_path, project.compose_file_path
        )
        defined = await _defined_services(conn, project.clone_path, compose_file)
        entries, _ = await _compose_ps(conn, project.clone_path, compose_file)
        evaluated = _evaluate_services(defined, entries)
    except (
        ComposeFileNotFound,
        ContainerNotRunning,
        ComposeConfigInvalid,
        TimeoutError,
        asyncssh.Error,
        OSError,
    ):
        evaluated = None

    if evaluated is None:
        new_status = "failed"
    else:
        by_service, missing, not_running = evaluated
        if by_service and not missing and not not_running:
            new_status = "running"
        elif not by_service:
            # Nothing from this compose project is present: down, or never up.
            new_status = (
                "never_started"
                if project.runtime_status == "never_started"
                else "failed"
            )
        else:
            new_status = "failed"

    if project.runtime_status != new_status:
        project.runtime_status = new_status
        project.updated_at = datetime.now(timezone.utc)
    return project


async def get_detected_ports(
    *,
    conn: asyncssh.SSHClientConnection,
    project: Project,
) -> list[DetectedPort]:
    """Host-published ports of the running services. Dangerous (database-ish)
    ports are flagged, not filtered: the UI decides what to hide."""
    compose_file = await detect_compose_file(
        conn, project.clone_path, project.compose_file_path
    )
    services, _ = await _compose_ps(conn, project.clone_path, compose_file)

    ports: list[DetectedPort] = []
    seen: set[tuple[str, int]] = set()
    for entry in services:
        name = _service_name(entry)
        for publisher in entry.get("Publishers") or []:
            if not isinstance(publisher, dict):
                continue
            url = publisher.get("URL") or ""
            if url not in ("", "0.0.0.0", "::") and not url.startswith("0.0.0.0"):
                continue
            host_port = publisher.get("PublishedPort") or 0
            container_port = publisher.get("TargetPort") or 0
            if not host_port or (name, host_port) in seen:
                continue
            seen.add((name, host_port))
            ports.append(
                DetectedPort(
                    service=name,
                    host_port=host_port,
                    container_port=container_port,
                    is_dangerous=host_port in DANGEROUS_PORTS,
                )
            )
    ports.sort(key=lambda p: (p.service, p.host_port))
    return ports
