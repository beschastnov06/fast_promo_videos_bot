from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CreditAccount, User


async def get_or_create_user(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    telegram_username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> User:
    result = await session.execute(
        select(User).where(User.telegram_user_id == telegram_user_id)
    )
    user = result.scalar_one_or_none()

    if user is None:
        user = User(
            telegram_user_id=telegram_user_id,
            telegram_username=telegram_username,
            first_name=first_name,
            last_name=last_name,
        )
        session.add(user)
        await session.flush()
        session.add(CreditAccount(user_id=user.id))
        return user

    user.telegram_username = telegram_username
    user.first_name = first_name
    user.last_name = last_name
    return user
