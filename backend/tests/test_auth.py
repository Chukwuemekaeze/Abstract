"""Auth dependency tests: lazy sync of Clerk users into Postgres.

The Clerk SDK is mocked at the get_clerk_client boundary so these tests never hit
Clerk's real API. The lazy sync and idempotency tests use the test DB; the race
condition test is fully mocked and runs anywhere.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.deps.auth import ClerkAuthState, get_current_user
from app.models import User
from tests.conftest import requires_db


def _fake_clerk_user(email: str, user_id: str = "user_new_789") -> SimpleNamespace:
    """A minimal stand in for clerk_backend_api.models.user.User."""
    address = SimpleNamespace(id="idn_1", email_address=email)
    return SimpleNamespace(
        id=user_id,
        primary_email_address_id="idn_1",
        email_addresses=[address],
    )


@requires_db
async def test_lazy_sync_creates_user(db_session, mocker):
    clerk = mocker.MagicMock()
    clerk.users.get_async = mocker.AsyncMock(
        return_value=_fake_clerk_user("new@example.com")
    )
    auth_state = ClerkAuthState(clerk_user_id="user_new_789", session_id="sess_1")

    user = await get_current_user(auth_state=auth_state, db=db_session, clerk=clerk)

    assert user.clerk_user_id == "user_new_789"
    assert user.email == "new@example.com"
    clerk.users.get_async.assert_awaited_once_with(user_id="user_new_789")


@requires_db
async def test_lazy_sync_is_idempotent(db_session, mocker):
    clerk = mocker.MagicMock()
    clerk.users.get_async = mocker.AsyncMock(
        return_value=_fake_clerk_user("dup@example.com")
    )
    auth_state = ClerkAuthState(clerk_user_id="user_new_789", session_id="sess_1")

    first = await get_current_user(auth_state=auth_state, db=db_session, clerk=clerk)
    second = await get_current_user(auth_state=auth_state, db=db_session, clerk=clerk)

    assert first.id == second.id
    count = await db_session.scalar(
        select(func.count()).select_from(User).where(
            User.clerk_user_id == "user_new_789"
        )
    )
    assert count == 1
    # Clerk is only consulted on the first request; the second is a plain lookup.
    clerk.users.get_async.assert_awaited_once()


@requires_db
async def test_lazy_sync_no_email_raises_400(db_session, mocker):
    from fastapi import HTTPException

    clerk = mocker.MagicMock()
    no_email_user = SimpleNamespace(
        id="user_noemail", primary_email_address_id=None, email_addresses=[]
    )
    clerk.users.get_async = mocker.AsyncMock(return_value=no_email_user)
    auth_state = ClerkAuthState(clerk_user_id="user_noemail", session_id="sess_1")

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(auth_state=auth_state, db=db_session, clerk=clerk)
    assert exc_info.value.status_code == 400


async def test_lazy_sync_recovers_from_integrity_error(mocker):
    # Simulate two concurrent first requests: the select sees no row, the insert
    # loses the race (IntegrityError), and the recovery re-query returns the winner.
    existing = User(clerk_user_id="user_race", email="race@example.com")

    miss = mocker.MagicMock()
    miss.scalar_one_or_none.return_value = None
    hit = mocker.MagicMock()
    hit.scalar_one_or_none.return_value = existing

    db = mocker.MagicMock()
    db.execute = mocker.AsyncMock(side_effect=[miss, hit])
    db.add = mocker.MagicMock()
    db.commit = mocker.AsyncMock(
        side_effect=IntegrityError("insert", {}, Exception("duplicate key"))
    )
    db.rollback = mocker.AsyncMock()
    db.refresh = mocker.AsyncMock()

    clerk = mocker.MagicMock()
    clerk.users.get_async = mocker.AsyncMock(
        return_value=_fake_clerk_user("race@example.com", user_id="user_race")
    )
    auth_state = ClerkAuthState(clerk_user_id="user_race", session_id="sess_1")

    result = await get_current_user(auth_state=auth_state, db=db, clerk=clerk)

    assert result is existing
    db.rollback.assert_awaited_once()
    assert db.execute.await_count == 2
