"""Seed the stubbed dev user.

Run once after migrations:
    python -m scripts.seed_dev_user

Idempotent: does nothing if the dev user already exists.
"""

import asyncio
from uuid import UUID

from sqlalchemy import select

from app.config import get_settings
from app.db import async_session_factory
from app.models import User


async def seed() -> None:
    settings = get_settings()
    dev_user_id = UUID(settings.dev_user_id)

    async with async_session_factory() as session:
        existing = await session.scalar(select(User).where(User.id == dev_user_id))
        if existing is not None:
            print(f"Dev user already present: {existing.email} ({existing.id})")
            return

        user = User(id=dev_user_id, email=settings.dev_user_email)
        session.add(user)
        await session.commit()
        print(f"Seeded dev user: {settings.dev_user_email} ({dev_user_id})")


if __name__ == "__main__":
    asyncio.run(seed())
