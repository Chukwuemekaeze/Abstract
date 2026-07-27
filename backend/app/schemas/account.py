"""Response models for the account API."""

from pydantic import BaseModel


class DeleteAccountResponse(BaseModel):
    success: bool
