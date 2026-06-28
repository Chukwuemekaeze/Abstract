"""get_owned_server enforces ownership and returns 404 on mismatch."""

import pytest
from fastapi import HTTPException

from app.deps.server_ownership import get_owned_server
from app.models import Server
from tests.conftest import requires_db


async def _make_server(db_session, user_id, status="verified") -> Server:
    server = Server(
        user_id=user_id,
        name="box",
        host="203.0.113.10",
        port=22,
        username="root",
        status=status,
        verification_source="tofu",
    )
    db_session.add(server)
    await db_session.commit()
    await db_session.refresh(server)
    return server


@requires_db
async def test_returns_server_for_owner(db_session, test_user):
    server = await _make_server(db_session, test_user.id)
    result = await get_owned_server(
        server_id=server.id, current_user=test_user, db=db_session
    )
    assert result.id == server.id


@requires_db
async def test_returns_404_for_other_users_server(db_session, test_user, other_test_user):
    server = await _make_server(db_session, other_test_user.id)
    with pytest.raises(HTTPException) as exc_info:
        await get_owned_server(
            server_id=server.id, current_user=test_user, db=db_session
        )
    assert exc_info.value.status_code == 404


@requires_db
async def test_returns_404_for_missing_server(db_session, test_user):
    from uuid import uuid4

    with pytest.raises(HTTPException) as exc_info:
        await get_owned_server(
            server_id=uuid4(), current_user=test_user, db=db_session
        )
    assert exc_info.value.status_code == 404
