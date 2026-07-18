"""Tests for the per-server app SSH key service.

Each server gets its own freshly generated keypair (blast radius isolation). These
tests run against the test DB so the FK cascade and UNIQUE(server_id) constraint are
exercised against real Postgres.
"""

import re

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.models import AppSshKey, Server
from app.services.app_key_service import (
    AppKeyMissing,
    create_key_for_server,
    get_key_for_server,
)
from app.services.key_provider import get_key_provider
from tests.conftest import requires_db

pytestmark = requires_db


async def _make_server(db_session, user_id, *, host="203.0.113.10"):
    server = Server(
        user_id=user_id,
        name="web1",
        host=host,
        port=22,
        username="root",
        status="pending_verification",
        verification_source="tofu",
    )
    db_session.add(server)
    await db_session.commit()
    await db_session.refresh(server)
    return server


async def test_two_servers_get_distinct_keys(db_session, test_user):
    provider = get_key_provider(get_settings())
    server_a = await _make_server(db_session, test_user.id, host="203.0.113.10")
    server_b = await _make_server(db_session, test_user.id, host="203.0.113.11")

    key_a = await create_key_for_server(server_a, db_session, provider)
    key_b = await create_key_for_server(server_b, db_session, provider)
    await db_session.commit()

    assert key_a.public_key != key_b.public_key
    assert key_a.server_id == server_a.id
    assert key_b.server_id == server_b.id


async def test_public_key_comment_matches_server_id(db_session, test_user):
    provider = get_key_provider(get_settings())
    server = await _make_server(db_session, test_user.id)

    key = await create_key_for_server(server, db_session, provider)
    await db_session.commit()

    # OpenSSH public key is "ssh-ed25519 <base64> <comment>". The comment is the last
    # whitespace separated field.
    comment = key.public_key.split()[-1]
    assert re.fullmatch(r"abstract-server-[0-9a-f]{8}", comment)
    assert comment == f"abstract-server-{server.id.hex[:8]}"


async def test_get_key_for_server_missing_raises(db_session, test_user):
    server = await _make_server(db_session, test_user.id)
    with pytest.raises(AppKeyMissing):
        await get_key_for_server(server, db_session)


async def test_deleting_server_cascades_to_key(db_session, test_user):
    provider = get_key_provider(get_settings())
    server = await _make_server(db_session, test_user.id)
    await create_key_for_server(server, db_session, provider)
    await db_session.commit()
    server_id = server.id

    await db_session.delete(server)
    await db_session.commit()

    remaining = await db_session.scalar(
        select(AppSshKey).where(AppSshKey.server_id == server_id)
    )
    assert remaining is None


async def test_unique_constraint_rejects_second_key_for_same_server(
    db_session, test_user
):
    provider = get_key_provider(get_settings())
    server = await _make_server(db_session, test_user.id)
    await create_key_for_server(server, db_session, provider)
    await db_session.commit()

    # A second key for the same server violates UNIQUE(server_id).
    with pytest.raises(IntegrityError):
        await create_key_for_server(server, db_session, provider)
        await db_session.commit()
    await db_session.rollback()
