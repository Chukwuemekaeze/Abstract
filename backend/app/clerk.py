"""Clerk client factory.

A single Clerk client, constructed lazily from settings. Used by the auth
dependency for token verification and lazy user sync, and available to any future
code that needs Clerk API access.
"""

from clerk_backend_api import Clerk

from app.config import get_settings


def get_clerk_client() -> Clerk:
    settings = get_settings()
    return Clerk(bearer_auth=settings.clerk_secret_key)
