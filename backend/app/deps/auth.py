"""Stubbed authentication.

For this milestone there is no real login. get_current_user always resolves to the
seeded dev user. The important property preserved here is that user identity comes
from the server side (settings + database), never from the client. Real auth will
replace this dependency without changing call sites.
"""

from uuid import UUID

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_db
from app.models import User


async def get_current_user(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    # Stub: always returns the dev user. Real auth comes later.
    user = await db.get(User, UUID(settings.dev_user_id))
    if user is None:
        raise HTTPException(
            500, "Dev user not seeded. Run python -m scripts.seed_dev_user"
        )
    return user
