"""Env file management: encrypted-at-rest dotenv files per project.

The caller owns the transaction; this module never commits. Values are
encrypted the moment they cross the request boundary and are only decrypted
by get_decrypted_variables, which exists for the run service alone and must
never feed an API response. Nothing in this module logs a value, encrypted
or plaintext.
"""

import posixpath
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, ProjectEnvFile, ProjectEnvVar
from app.schemas.env import validate_env_file_path
from app.services.key_provider import KeyProvider

__all__ = [
    "EnvFileServiceError",
    "EnvFilePathInvalid",
    "EnvFileAlreadyExists",
    "EnvFileNotFound",
    "DotenvParseError",
    "parse_dotenv",
    "validate_path_within_clone",
    "create_env_file",
    "update_env_file",
    "delete_env_file",
    "list_env_files",
    "get_env_file",
    "get_env_file_keys",
    "get_decrypted_variables",
]


class EnvFileServiceError(Exception):
    pass


class EnvFilePathInvalid(EnvFileServiceError):
    pass


class EnvFileAlreadyExists(EnvFileServiceError):
    pass


class EnvFileNotFound(EnvFileServiceError):
    pass


class DotenvParseError(EnvFileServiceError):
    def __init__(self, line_number: int, message: str):
        self.line_number = line_number
        super().__init__(f"line {line_number}: {message}")


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse dotenv text into a key-value dict.

    Comments and blank lines are skipped, an optional 'export ' prefix is
    stripped, and a single matching quote pair around the value is removed.
    Values are stored literally: no ${VAR} interpolation. On duplicate keys
    the last occurrence wins, matching dotenv convention.
    """
    variables: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            raise DotenvParseError(line_number, f"expected KEY=value, got {line!r}")
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            raise DotenvParseError(line_number, "empty key before '='")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        variables[key] = value
    return variables


def validate_path_within_clone(path: str, clone_path: str) -> str:
    """Re-validate at the service layer and check containment against the
    actual clone_path. Raises EnvFilePathInvalid, never ValueError, so routes
    map it to a clean 400."""
    try:
        path = validate_env_file_path(path)
    except ValueError as exc:
        raise EnvFilePathInvalid(str(exc)) from exc
    clone_root = posixpath.normpath(clone_path)
    resolved = posixpath.normpath(posixpath.join(clone_root, path))
    if not resolved.startswith(clone_root + "/"):
        raise EnvFilePathInvalid("path must resolve inside the project directory")
    return path


async def _path_taken(
    db: AsyncSession, project_id: UUID, path: str, exclude_id: UUID | None = None
) -> bool:
    query = select(ProjectEnvFile.id).where(
        ProjectEnvFile.project_id == project_id, ProjectEnvFile.path == path
    )
    if exclude_id is not None:
        query = query.where(ProjectEnvFile.id != exclude_id)
    return await db.scalar(query) is not None


async def create_env_file(
    *,
    db: AsyncSession,
    project: Project,
    path_from_client: str,
    variables_from_client: dict[str, str],
    key_provider: KeyProvider,
) -> ProjectEnvFile:
    path = validate_path_within_clone(path_from_client, project.clone_path)
    # Pre-check for a friendly 409; UNIQUE(project_id, path) backstops races.
    if await _path_taken(db, project.id, path):
        raise EnvFileAlreadyExists(f"an env file at {path!r} already exists")

    env_file = ProjectEnvFile(project_id=project.id, path=path)
    db.add(env_file)
    await db.flush()

    for key, value in variables_from_client.items():
        db.add(
            ProjectEnvVar(
                env_file_id=env_file.id,
                key=key,
                encrypted_value=await key_provider.encrypt(value.encode()),
                encryption_key_id=key_provider.key_id,
            )
        )
    await db.flush()
    return env_file


async def update_env_file(
    *,
    db: AsyncSession,
    project: Project,
    env_file: ProjectEnvFile,
    path_from_client: str | None,
    set_variables_from_client: dict[str, str] | None,
    remove_keys_from_client: list[str] | None,
    key_provider: KeyProvider,
) -> ProjectEnvFile:
    if path_from_client is not None and path_from_client != env_file.path:
        path = validate_path_within_clone(path_from_client, project.clone_path)
        if await _path_taken(db, project.id, path, exclude_id=env_file.id):
            raise EnvFileAlreadyExists(f"an env file at {path!r} already exists")
        env_file.path = path

    if remove_keys_from_client:
        remove_set = set(remove_keys_from_client)
        existing = (
            await db.scalars(
                select(ProjectEnvVar).where(
                    ProjectEnvVar.env_file_id == env_file.id,
                    ProjectEnvVar.key.in_(remove_set),
                )
            )
        ).all()
        for var in existing:
            await db.delete(var)
        await db.flush()

    if set_variables_from_client:
        existing_by_key = {
            var.key: var
            for var in (
                await db.scalars(
                    select(ProjectEnvVar).where(
                        ProjectEnvVar.env_file_id == env_file.id,
                        ProjectEnvVar.key.in_(set_variables_from_client.keys()),
                    )
                )
            ).all()
        }
        now = datetime.now(timezone.utc)
        for key, value in set_variables_from_client.items():
            encrypted = await key_provider.encrypt(value.encode())
            var = existing_by_key.get(key)
            if var is not None:
                var.encrypted_value = encrypted
                var.encryption_key_id = key_provider.key_id
                var.updated_at = now
            else:
                db.add(
                    ProjectEnvVar(
                        env_file_id=env_file.id,
                        key=key,
                        encrypted_value=encrypted,
                        encryption_key_id=key_provider.key_id,
                    )
                )

    env_file.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return env_file


async def delete_env_file(*, db: AsyncSession, env_file: ProjectEnvFile) -> None:
    await db.delete(env_file)
    await db.flush()


async def list_env_files(
    db: AsyncSession, project: Project
) -> list[tuple[ProjectEnvFile, int]]:
    """Env files with their variable counts, ordered by path."""
    rows = await db.execute(
        select(ProjectEnvFile, func.count(ProjectEnvVar.id))
        .outerjoin(ProjectEnvVar, ProjectEnvVar.env_file_id == ProjectEnvFile.id)
        .where(ProjectEnvFile.project_id == project.id)
        .group_by(ProjectEnvFile.id)
        .order_by(ProjectEnvFile.path)
    )
    return [(env_file, count) for env_file, count in rows.all()]


async def get_env_file(
    db: AsyncSession, project: Project, env_file_id: UUID
) -> ProjectEnvFile:
    env_file = await db.get(ProjectEnvFile, env_file_id)
    if env_file is None or env_file.project_id != project.id:
        raise EnvFileNotFound("Env file not found")
    return env_file


async def get_env_file_keys(db: AsyncSession, env_file: ProjectEnvFile) -> list[str]:
    keys = await db.scalars(
        select(ProjectEnvVar.key)
        .where(ProjectEnvVar.env_file_id == env_file.id)
        .order_by(ProjectEnvVar.key)
    )
    return list(keys.all())


async def get_decrypted_variables(
    db: AsyncSession, project: Project, key_provider: KeyProvider
) -> dict[str, dict[str, str]]:
    """{env_file_path: {key: plaintext_value}} for the run service ONLY.

    The result contains secrets: never log it, never serialize it into a
    response, never pass it anywhere but the VPS file writer.
    """
    rows = await db.execute(
        select(ProjectEnvFile.path, ProjectEnvVar.key, ProjectEnvVar.encrypted_value)
        .join(ProjectEnvVar, ProjectEnvVar.env_file_id == ProjectEnvFile.id)
        .where(ProjectEnvFile.project_id == project.id)
        .order_by(ProjectEnvFile.path, ProjectEnvVar.key)
    )
    decrypted: dict[str, dict[str, str]] = {}
    for path, key, encrypted_value in rows.all():
        decrypted.setdefault(path, {})[key] = (
            await key_provider.decrypt(encrypted_value)
        ).decode()
    # Files with zero variables still count: they must exist on the VPS.
    empty_files = await db.scalars(
        select(ProjectEnvFile.path).where(ProjectEnvFile.project_id == project.id)
    )
    for path in empty_files.all():
        decrypted.setdefault(path, {})
    return decrypted
