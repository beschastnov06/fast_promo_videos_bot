from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Config, load_config
from app.db import create_engine, create_session_factory
from app.repositories.admin_subscribers import upsert_admin_subscriber


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app_config: Config | None = None
db_engine: AsyncEngine | None = None
db_session_factory: async_sessionmaker[AsyncSession] | None = None

dp = Dispatcher()


class AdminUsernameMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        config = _app_config()
        username = (event.from_user.username if event.from_user else None) or ""
        if username.casefold() in config.admins_usernames:
            return await handler(event, data)

        logger.info(
            "Ignored admin bot message from unauthorized user: user_id=%s username=%s",
            event.from_user.id if event.from_user else None,
            username or None,
        )
        return None


dp.message.outer_middleware(AdminUsernameMiddleware())


@dp.message(CommandStart())
async def start(message: Message) -> None:
    if message.from_user is None or not message.from_user.username:
        return

    session_factory = _db_session_factory()
    async with session_factory() as session:
        async with session.begin():
            await upsert_admin_subscriber(
                session,
                telegram_user_id=message.from_user.id,
                telegram_chat_id=message.chat.id,
                telegram_username=message.from_user.username,
                first_name=message.from_user.first_name,
                last_name=message.from_user.last_name,
            )

    await message.answer(
        "Админ-бот подключен.\n\n"
        "Теперь сюда будут приходить служебные уведомления Fast Promo Videos Bot."
    )


@dp.message()
async def ignore_other_messages(message: Message) -> None:
    return None


async def main() -> None:
    global app_config, db_engine, db_session_factory

    config = load_config(require_bot_token=False)
    if not config.admin_bot_token:
        raise RuntimeError("ADMIN_BOT_TOKEN is not set.")
    if not config.admins_usernames:
        raise RuntimeError("ADMINS_USERNAME is not set.")

    app_config = config
    db_engine = create_engine(config)
    db_session_factory = create_session_factory(db_engine)

    session = _create_session(config)
    bot = Bot(token=config.admin_bot_token, session=session) if session else Bot(token=config.admin_bot_token)
    logger.info("Admin bot started: admins=%s", ",".join(sorted(config.admins_usernames)))
    await dp.start_polling(bot)


def _create_session(config: Config) -> AiohttpSession | None:
    if not config.telegram_api_base:
        return AiohttpSession(timeout=config.telegram_request_timeout_seconds)

    api = TelegramAPIServer.from_base(
        config.telegram_api_base,
        is_local=config.telegram_api_is_local,
    )
    return AiohttpSession(api=api, timeout=config.telegram_request_timeout_seconds)


def _app_config() -> Config:
    if app_config is None:
        raise RuntimeError("App config is not initialized")

    return app_config


def _db_session_factory() -> async_sessionmaker[AsyncSession]:
    if db_session_factory is None:
        raise RuntimeError("Database session factory is not initialized")

    return db_session_factory


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
