"""Route tests for GET /api/account/me.

DB backed (TEST_DATABASE_URL). The endpoint returns the signed-in user's own
identity so the frontend can tag its monitoring events with the same Postgres
user id the backend uses. These assert the HTTP contract and that auth is required.
"""

from tests.conftest import requires_db

pytestmark = requires_db


async def test_me_returns_current_user_identity(client, test_user):
    resp = await client.get("/api/account/me")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(test_user.id)
    assert body["email"] == test_user.email


async def test_me_no_auth_returns_401(unauthenticated_client):
    ac, _clerk = unauthenticated_client
    assert (await ac.get("/api/account/me")).status_code == 401
