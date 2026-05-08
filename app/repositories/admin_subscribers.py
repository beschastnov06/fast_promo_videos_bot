from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminSubscriber


async def upsert_admin_subscriber(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    telegram_chat_id: int,
    telegram_username: str,
    first_name: str | None,
    last_name: str | None,
) -> AdminSubscriber:
    result = await session.execute(
        select(AdminSubscriber).where(AdminSubscriber.telegram_user_id == telegram_user_id)
    )
    subscriber = result.scalar_one_or_none()

    if subscriber is None:
        subscriber = AdminSubscriber(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            telegram_username=telegram_username,
            first_name=first_name,
            last_name=last_name,
            is_active=True,
        )
        session.add(subscriber)
        await session.flush()
        return subscriber

    subscriber.telegram_chat_id = telegram_chat_id
    subscriber.telegram_username = telegram_username
    subscriber.first_name = first_name
    subscriber.last_name = last_name
    subscriber.is_active = True
    return subscriber


async def list_active_admin_subscribers(session: AsyncSession) -> list[AdminSubscriber]:
    result = await session.execute(
        select(AdminSubscriber).where(AdminSubscriber.is_active.is_(True))
    )
    return list(result.scalars())
