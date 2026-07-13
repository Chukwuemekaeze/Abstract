"""Env file service tests: dotenv parsing, path validation, encryption at
rest, upsert/remove semantics, and cascade deletes."""

import pytest

from app.config import get_settings
from app.models import ProjectEnvFile, ProjectEnvVar
from app.services.env_file_service import (
    DotenvParseError,
    EnvFileAlreadyExists,
    EnvFileNotFound,
    create_env_file,
    delete_env_file,
    get_decrypted_variables,
    get_env_file,
    get_env_file_keys,
    list_env_files,
    parse_dotenv,
    update_env_file,
    validate_path_within_clone,
    EnvFilePathInvalid,
)
from app.services.key_provider import get_key_provider
from sqlalchemy import select
from tests.conftest import requires_db
from tests.run_publish_mocks import CLONE_PATH, make_server, seed_project


# -- parse_dotenv (no DB) -----------------------------------------------------


def test_parse_dotenv_happy_path():
    text = "FOO=bar\nBAZ=qux\n"
    assert parse_dotenv(text) == {"FOO": "bar", "BAZ": "qux"}


def test_parse_dotenv_skips_comments_and_blank_lines():
    text = "# a comment\n\nFOO=bar\n   \n# another\nBAZ=qux"
    assert parse_dotenv(text) == {"FOO": "bar", "BAZ": "qux"}


def test_parse_dotenv_strips_export_prefix():
    assert parse_dotenv("export FOO=bar") == {"FOO": "bar"}


def test_parse_dotenv_strips_matching_quote_pairs():
    assert parse_dotenv('FOO="bar baz"') == {"FOO": "bar baz"}
    assert parse_dotenv("FOO='single'") == {"FOO": "single"}
    # Mismatched quotes are kept literally.
    assert parse_dotenv("FOO=\"half") == {"FOO": '"half'}


def test_parse_dotenv_splits_on_first_equals_only():
    assert parse_dotenv("URL=postgres://u:p@h/db?x=1") == {
        "URL": "postgres://u:p@h/db?x=1"
    }


def test_parse_dotenv_no_interpolation():
    assert parse_dotenv("A=1\nB=${A}") == {"A": "1", "B": "${A}"}


def test_parse_dotenv_malformed_line_raises_with_line_number():
    with pytest.raises(DotenvParseError) as exc_info:
        parse_dotenv("FOO=bar\nnot a kv pair\n")
    assert "line 2" in str(exc_info.value)
    assert exc_info.value.line_number == 2


def test_parse_dotenv_empty_key_raises():
    with pytest.raises(DotenvParseError) as exc_info:
        parse_dotenv("=value")
    assert exc_info.value.line_number == 1


# -- path validation (no DB) --------------------------------------------------


@pytest.mark.parametrize(
    "bad_path",
    [
        "/etc/passwd",
        "../outside",
        "a/../../outside",
        "",
        "   ",
        "a\x00b",
        "x" * 201,
    ],
)
def test_path_validation_rejects(bad_path):
    with pytest.raises(EnvFilePathInvalid):
        validate_path_within_clone(bad_path, CLONE_PATH)


@pytest.mark.parametrize("good_path", [".env", "backend/.env", "a/b/c.env"])
def test_path_validation_accepts(good_path):
    assert validate_path_within_clone(good_path, CLONE_PATH) == good_path


# -- DB backed CRUD -----------------------------------------------------------

pytestmark_db = requires_db


@pytest.fixture
def key_provider():
    return get_key_provider(get_settings())


@requires_db
async def test_encryption_roundtrip(db_session, test_user, key_provider):
    server = await make_server(db_session, test_user.id)
    project = await seed_project(db_session, test_user.id, server)

    await create_env_file(
        db=db_session,
        project=project,
        path_from_client=".env",
        variables_from_client={"SECRET_KEY": "s3cret-value", "PORT": "8000"},
        key_provider=key_provider,
    )
    await db_session.commit()

    # Values are ciphertext at rest.
    stored = (await db_session.scalars(select(ProjectEnvVar))).all()
    for var in stored:
        assert b"s3cret-value" not in var.encrypted_value
        assert var.encryption_key_id == key_provider.key_id

    decrypted = await get_decrypted_variables(db_session, project, key_provider)
    assert decrypted == {".env": {"SECRET_KEY": "s3cret-value", "PORT": "8000"}}


@requires_db
async def test_duplicate_path_raises(db_session, test_user, key_provider):
    server = await make_server(db_session, test_user.id)
    project = await seed_project(db_session, test_user.id, server)

    await create_env_file(
        db=db_session,
        project=project,
        path_from_client=".env",
        variables_from_client={},
        key_provider=key_provider,
    )
    with pytest.raises(EnvFileAlreadyExists):
        await create_env_file(
            db=db_session,
            project=project,
            path_from_client=".env",
            variables_from_client={},
            key_provider=key_provider,
        )


@requires_db
async def test_update_path_uniqueness_enforced(db_session, test_user, key_provider):
    server = await make_server(db_session, test_user.id)
    project = await seed_project(db_session, test_user.id, server)

    await create_env_file(
        db=db_session,
        project=project,
        path_from_client=".env",
        variables_from_client={},
        key_provider=key_provider,
    )
    second = await create_env_file(
        db=db_session,
        project=project,
        path_from_client="backend/.env",
        variables_from_client={},
        key_provider=key_provider,
    )
    with pytest.raises(EnvFileAlreadyExists):
        await update_env_file(
            db=db_session,
            project=project,
            env_file=second,
            path_from_client=".env",
            set_variables_from_client=None,
            remove_keys_from_client=None,
            key_provider=key_provider,
        )


@requires_db
async def test_set_variables_and_remove_keys(db_session, test_user, key_provider):
    server = await make_server(db_session, test_user.id)
    project = await seed_project(db_session, test_user.id, server)

    env_file = await create_env_file(
        db=db_session,
        project=project,
        path_from_client=".env",
        variables_from_client={"KEEP": "old", "CHANGE": "old", "DROP": "old"},
        key_provider=key_provider,
    )
    await update_env_file(
        db=db_session,
        project=project,
        env_file=env_file,
        path_from_client=None,
        set_variables_from_client={"CHANGE": "new", "ADDED": "fresh"},
        remove_keys_from_client=["DROP"],
        key_provider=key_provider,
    )
    await db_session.commit()

    decrypted = await get_decrypted_variables(db_session, project, key_provider)
    assert decrypted == {
        ".env": {"KEEP": "old", "CHANGE": "new", "ADDED": "fresh"}
    }
    keys = await get_env_file_keys(db_session, env_file)
    assert keys == ["ADDED", "CHANGE", "KEEP"]


@requires_db
async def test_delete_cascades_variables(db_session, test_user, key_provider):
    server = await make_server(db_session, test_user.id)
    project = await seed_project(db_session, test_user.id, server)

    env_file = await create_env_file(
        db=db_session,
        project=project,
        path_from_client=".env",
        variables_from_client={"A": "1", "B": "2"},
        key_provider=key_provider,
    )
    await db_session.commit()
    env_file_id = env_file.id

    await delete_env_file(db=db_session, env_file=env_file)
    await db_session.commit()

    remaining_files = (await db_session.scalars(select(ProjectEnvFile))).all()
    remaining_vars = (await db_session.scalars(select(ProjectEnvVar))).all()
    assert remaining_files == []
    assert remaining_vars == []
    with pytest.raises(EnvFileNotFound):
        await get_env_file(db_session, project, env_file_id)


@requires_db
async def test_list_env_files_with_counts(db_session, test_user, key_provider):
    server = await make_server(db_session, test_user.id)
    project = await seed_project(db_session, test_user.id, server)

    await create_env_file(
        db=db_session,
        project=project,
        path_from_client="backend/.env",
        variables_from_client={"A": "1", "B": "2"},
        key_provider=key_provider,
    )
    await create_env_file(
        db=db_session,
        project=project,
        path_from_client=".env",
        variables_from_client={},
        key_provider=key_provider,
    )
    await db_session.commit()

    rows = await list_env_files(db_session, project)
    assert [(f.path, count) for f, count in rows] == [
        (".env", 0),
        ("backend/.env", 2),
    ]

    # Empty files still show up for the run service so they exist on the VPS.
    decrypted = await get_decrypted_variables(db_session, project, key_provider)
    assert decrypted == {".env": {}, "backend/.env": {"A": "1", "B": "2"}}
