"""Response models for the account API."""

from uuid import UUID

from pydantic import BaseModel


class CurrentUserResponse(BaseModel):
    """The signed-in user's own identity.

    Exposes the Postgres user id so the frontend can attribute its monitoring
    events (Sentry) to the same id the backend tags its logs and errors with,
    lining both sides up on one key.
    """

    id: UUID
    email: str


class DeleteAccountResponse(BaseModel):
    success: bool
