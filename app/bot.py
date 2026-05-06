import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Config, load_config
from app.db import create_engine, create_session_factory
from app.models import VideoJobSettings
from app.queue import enqueue_render_job
from app.repositories.credits import InsufficientCreditsError, add_credits, charge_credits, get_balance
from app.repositories.users import get_or_create_user
from app.repositories.video_jobs import (
    create_draft_job,
    get_job,
    get_latest_active_job_for_telegram_user,
    get_queue_position,
    list_queued_jobs,
    mark_cancelled,
    mark_failed,
    mark_queued,
    mark_refunded,
    set_status_message_id,
)
from app.video_processor import (
    FFmpegNotFoundError,
    VIDEO_FORMATS,
    VideoProcessingError,
    ensure_ffmpeg_available,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

MAX_VIDEO_SIZE_MB = 20
MAX_VIDEO_SIZE_BYTES = MAX_VIDEO_SIZE_MB * 1024 * 1024
MAX_BANNER_SIZE_MB = 5
MAX_BANNER_SIZE_BYTES = MAX_BANNER_SIZE_MB * 1024 * 1024
TMP_DIR = Path("tmp")
NO_CONTENT_TEXT = "Без контента"
CANCEL_MONTAGE_TEXT = "Отменить монтаж"
NEW_VIDEO_CALLBACK = "flow:new_video"
START_MONTAGE_CALLBACK = "flow:start_montage"
MENU_CALLBACK = "flow:menu"
BUY_PACKAGE_CALLBACK_PREFIX = "billing:buy:"
MAX_AD_TEXT_CHARS = 60
INTRO_BONUS_VIDEOS = 3
RENDER_COST_VIDEOS = 1
DEFAULT_VIDEO_FORMAT = "9:16"
DEFAULT_FILL_COLOR = "black"
DEFAULT_SUBTITLE_FONT = "DejaVu Sans"
DEFAULT_SUBTITLE_COLOR = "white"
DEFAULT_STRIP_METADATA = True
DEFAULT_VIDEO_SPEED = 1.0
FILL_COLORS = {
    "black": "черное",
    "white": "белое",
}
SUBTITLE_COLORS = {
    "white": "белый",
    "black": "черный",
}
SUBTITLE_FONTS = {
    "DejaVu Sans": "DejaVu Sans",
    "Inter": "Inter",
    "Roboto": "Roboto",
    "DejaVu Sans Mono": "DejaVu Sans Mono",
}
VIDEO_SPEEDS = {
    1.0: "нет",
    1.10: "1.10x",
    1.25: "1.25x",
    1.50: "1.50x",
    2.00: "2.00x",
}
TARIFF_PACKAGES = (
    (10, "10 видео", "99 ₽", None),
    (25, "25 видео", "229 ₽", "скидка 7%"),
    (50, "50 видео", "399 ₽", "скидка 19%"),
    (100, "100 видео", "699 ₽", "скидка 29%"),
)


@dataclass
class MontageSettings:
    video_format: str = DEFAULT_VIDEO_FORMAT
    fill_color: str = DEFAULT_FILL_COLOR
    subtitle_font: str = DEFAULT_SUBTITLE_FONT
    subtitle_color: str = DEFAULT_SUBTITLE_COLOR
    video_speed: float = DEFAULT_VIDEO_SPEED
    mirror: bool = False
    strip_metadata: bool = DEFAULT_STRIP_METADATA


@dataclass
class PendingVideo:
    job_id: uuid.UUID
    telegram_video_file_id: str
    telegram_video_file_unique_id: str | None = None
    video_count: int = 1
    settings: MontageSettings = field(default_factory=MontageSettings)
    ad_text: str | None = None
    ad_banner_file_id: str | None = None
    ad_banner_file_unique_id: str | None = None
    ad_banner_name: str | None = None
    ready_for_montage: bool = False


pending_videos: dict[int, PendingVideo] = {}
app_config: Config | None = None
db_engine: AsyncEngine | None = None
db_session_factory: async_sessionmaker[AsyncSession] | None = None


dp = Dispatcher()


class UsernameWhitelistMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if _is_allowed_user(event):
            return await handler(event, data)

        await event.answer("на этапе разработки")
        return None


class ActiveRenderMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if (event.text or "").strip() == CANCEL_MONTAGE_TEXT:
            return await handler(event, data)

        if await _has_active_render_job(_user_id(event)):
            return None

        return await handler(event, data)


dp.message.outer_middleware(UsernameWhitelistMiddleware())
dp.message.outer_middleware(ActiveRenderMiddleware())


@dp.message(CommandStart())
async def start(message: Message) -> None:
    intro_bonus = await _ensure_intro_bonus(message)
    await message.answer(_welcome_text())
    await message.answer(
        _how_it_works_text(),
        reply_markup=_start_keyboard(),
    )
    if intro_bonus:
        added_videos, balance_value = intro_bonus
        await message.answer(_balance_added_text(added_videos=added_videos, balance_value=balance_value))


@dp.message(Command("balance"))
async def balance(message: Message) -> None:
    balance_value = await _user_video_balance(message)
    await message.answer(_menu_text(balance_value), reply_markup=_menu_keyboard())


@dp.message(Command("buy"))
async def buy(message: Message) -> None:
    balance_value = await _user_video_balance(message)
    await message.answer(_menu_text(balance_value), reply_markup=_menu_keyboard())


@dp.message(Command("ad"))
async def set_ad_text(message: Message) -> None:
    user_id = _user_id(message)
    pending = pending_videos.get(user_id)
    if not pending:
        await message.answer("Сначала отправь видео, а потом рекламный текст или баннер для него.")
        return

    text = _command_payload(message.text or "")
    if not text:
        await message.answer("Напиши текст после команды, например: /ad Реклама: @example")
        return
    if len(text) > MAX_AD_TEXT_CHARS:
        await message.answer(f"Ошибка: рекламный текст слишком длинный. Максимум — {MAX_AD_TEXT_CHARS} символов.")
        return

    await message.answer("Рекламный контент обрабатывается", reply_markup=ReplyKeyboardRemove())
    await message.answer("Рекламный контент обработан")
    pending.ad_text = text
    pending.ad_banner_file_id = None
    pending.ad_banner_file_unique_id = None
    pending.ad_banner_name = None
    pending.ready_for_montage = True
    await _send_montage_settings(message, pending)


@dp.message(Command("clear_ad"))
async def clear_ad(message: Message) -> None:
    user_id = _user_id(message)
    pending = pending_videos.pop(user_id, None)
    if pending:
        await _mark_pending_cancelled(pending)
        await message.answer("Текущая обработка отменена.", reply_markup=ReplyKeyboardRemove())
        return

    await message.answer("Нет активного видео для отмены.")


@dp.message(F.photo)
async def set_ad_banner(message: Message, bot: Bot) -> None:
    photo = message.photo[-1]
    await _handle_ad_banner_file(
        message=message,
        bot=bot,
        file_id=photo.file_id,
        suffix=".jpg",
        file_size=photo.file_size,
        display_name="banner.jpg",
    )


@dp.message(F.document)
async def set_ad_banner_document(message: Message, bot: Bot) -> None:
    document = message.document
    if not document:
        await handle_other(message)
        return

    if not _is_image_document(document.mime_type, document.file_name):
        await handle_other(message)
        return

    await _handle_ad_banner_file(
        message=message,
        bot=bot,
        file_id=document.file_id,
        suffix=_image_suffix(document.mime_type, document.file_name),
        file_size=document.file_size,
        display_name=document.file_name,
    )


async def _handle_ad_banner_file(
    message: Message,
    bot: Bot,
    file_id: str,
    suffix: str,
    file_size: int | None,
    display_name: str | None,
) -> None:
    if file_size and file_size > MAX_BANNER_SIZE_BYTES:
        await message.answer(f"Ошибка: баннер слишком большой. Максимум — {MAX_BANNER_SIZE_MB} МБ.")
        return

    user_id = _user_id(message)
    pending = pending_videos.get(user_id)
    if not pending:
        await message.answer("Сначала отправь видео, а потом рекламный текст или баннер для него.")
        return

    await message.answer("Рекламный контент обрабатывается", reply_markup=ReplyKeyboardRemove())

    await message.answer("Рекламный контент обработан")
    pending.ad_text = None
    pending.ad_banner_file_id = file_id
    pending.ad_banner_file_unique_id = None
    pending.ad_banner_name = display_name or f"banner{suffix}"
    pending.ready_for_montage = True
    await _send_montage_settings(message, pending)


@dp.message(F.text == CANCEL_MONTAGE_TEXT)
async def handle_cancel_montage(message: Message, bot: Bot) -> None:
    user_id = _user_id(message)
    pending = pending_videos.pop(user_id, None)
    if pending:
        await _mark_pending_cancelled(pending)
        balance_value = await _user_video_balance(message)
        await message.answer("Монтаж отменен", reply_markup=ReplyKeyboardRemove())
        await message.answer(_menu_text(balance_value), reply_markup=_menu_keyboard())
        return

    _, balance_value, note, status_chat_id, status_message_id = await _cancel_latest_active_job(message)
    if status_chat_id is not None and status_message_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=status_chat_id,
                message_id=status_message_id,
                text="Монтаж отменен",
            )
        except Exception:
            logger.exception("Failed to edit cancelled job status message")
        await _refresh_queue_positions(bot)
    await message.answer(note, reply_markup=ReplyKeyboardRemove())
    await message.answer(_menu_text(balance_value), reply_markup=_menu_keyboard())


@dp.message(F.video)
async def handle_video(message: Message, bot: Bot) -> None:
    video = message.video

    if video.file_size and video.file_size > MAX_VIDEO_SIZE_BYTES:
        await message.answer(f"Ошибка: видео слишком большое. Максимальный размер сейчас — {MAX_VIDEO_SIZE_MB} МБ.")
        return

    user_id = _user_id(message)
    old_pending = pending_videos.pop(user_id, None)
    if old_pending:
        await _mark_pending_cancelled(old_pending)

    try:
        job = await _create_pending_job(message, video.file_id, video.file_unique_id)
        pending = PendingVideo(
            job_id=job.id,
            telegram_video_file_id=video.file_id,
            telegram_video_file_unique_id=video.file_unique_id,
        )
        pending_videos[user_id] = pending

        await message.answer(
            "Видео принято 👌\n\n"
            "Вы можете добавить рекламный контент (текст или баннер) сверху макета - "
            "отправьте то что необходимо\n\n"
            "❗️формат баннеров .png необходимо отправить в формате \"без сжатия\"\n\n"
            "Если дополнительного контента нет, нажмите кнопку ниже",
            reply_markup=_ad_content_keyboard(),
        )
    except VideoProcessingError as exc:
        logger.exception("Video preparation failed for message_id=%s", message.message_id)
        await message.answer(f"Ошибка: не удалось принять видео. {exc}")
    except Exception as exc:
        logger.exception("Unexpected error while preparing video message_id=%s", message.message_id)
        await message.answer(f"Ошибка: не удалось принять видео. {exc}")


@dp.message(F.text)
async def handle_ad_text(message: Message) -> None:
    user_id = _user_id(message)
    pending = pending_videos.get(user_id)
    if not pending:
        await message.answer("Пожалуйста, отправь видео файлом Telegram video.")
        return
    if pending.ready_for_montage:
        await message.answer("Используй кнопки под сообщением с параметрами монтажа.")
        return

    text = (message.text or "").strip()
    if text.casefold() == NO_CONTENT_TEXT.casefold():
        await message.answer("Видео будет без рекламного контента", reply_markup=ReplyKeyboardRemove())
        pending.ad_text = None
        pending.ad_banner_file_id = None
        pending.ad_banner_file_unique_id = None
        pending.ad_banner_name = None
        pending.ready_for_montage = True
        await _send_montage_settings(message, pending)
        return

    if len(text) > MAX_AD_TEXT_CHARS:
        await message.answer(f"Ошибка: рекламный текст слишком длинный. Максимум — {MAX_AD_TEXT_CHARS} символов.")
        return

    await message.answer("Рекламный контент обрабатывается", reply_markup=ReplyKeyboardRemove())
    await message.answer("Рекламный контент обработан")
    pending.ad_text = text
    pending.ad_banner_file_id = None
    pending.ad_banner_file_unique_id = None
    pending.ad_banner_name = None
    pending.ready_for_montage = True
    await _send_montage_settings(message, pending)


@dp.callback_query(F.data == "content:none")
async def handle_no_content_callback(callback: CallbackQuery) -> None:
    if not _is_allowed_callback(callback):
        await callback.answer("на этапе разработки", show_alert=True)
        return

    user_id = callback.from_user.id
    pending = pending_videos.get(user_id)
    if not pending or pending.ready_for_montage:
        await callback.answer("Видео для монтажа не найдено", show_alert=True)
        return
    if not callback.message:
        await callback.answer()
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Видео будет без рекламного контента")
    pending.ad_text = None
    pending.ad_banner_file_id = None
    pending.ad_banner_file_unique_id = None
    pending.ad_banner_name = None
    pending.ready_for_montage = True
    await _send_montage_settings(callback.message, pending)
    await callback.answer()


@dp.callback_query(F.data == NEW_VIDEO_CALLBACK)
async def handle_new_video_callback(callback: CallbackQuery) -> None:
    if not _is_allowed_callback(callback):
        await callback.answer("на этапе разработки", show_alert=True)
        return

    if callback.message:
        await callback.message.answer(f"Отправьте видео до {MAX_VIDEO_SIZE_MB} МБ для нового монтажа")
    await callback.answer()


@dp.callback_query(F.data == START_MONTAGE_CALLBACK)
async def handle_start_montage_callback(callback: CallbackQuery) -> None:
    if not _is_allowed_callback(callback):
        await callback.answer("на этапе разработки", show_alert=True)
        return

    if callback.message:
        await callback.message.answer(f"Отправьте видео до {MAX_VIDEO_SIZE_MB} МБ для нового монтажа")
    await callback.answer()


@dp.callback_query(F.data == MENU_CALLBACK)
async def handle_menu_callback(callback: CallbackQuery) -> None:
    if not _is_allowed_callback(callback):
        await callback.answer("на этапе разработки", show_alert=True)
        return

    if callback.message:
        balance_value = await _user_video_balance_from_telegram_user(callback.from_user)
        await callback.message.answer(_menu_text(balance_value), reply_markup=_menu_keyboard())
    await callback.answer()


@dp.callback_query(F.data.startswith(BUY_PACKAGE_CALLBACK_PREFIX))
async def handle_buy_package_callback(callback: CallbackQuery) -> None:
    if not _is_allowed_callback(callback):
        await callback.answer("на этапе разработки", show_alert=True)
        return

    package_videos = callback.data.removeprefix(BUY_PACKAGE_CALLBACK_PREFIX) if callback.data else ""
    await callback.answer(
        f"Скоро здесь будет оплата пакета на {package_videos} видео",
        show_alert=True,
    )


@dp.callback_query(F.data.startswith("settings:"))
async def handle_settings_callback(callback: CallbackQuery) -> None:
    if not _is_allowed_callback(callback):
        await callback.answer("на этапе разработки", show_alert=True)
        return

    user_id = callback.from_user.id
    pending = pending_videos.get(user_id)
    if not pending or not pending.ready_for_montage:
        await callback.answer("Видео для монтажа не найдено", show_alert=True)
        return
    if not callback.message:
        await callback.answer()
        return

    action = callback.data or ""

    if action == "settings:main":
        await _edit_montage_settings(callback.message, pending, _montage_settings_keyboard(pending))
    elif action == "settings:format":
        await _edit_montage_settings(callback.message, pending, _format_keyboard())
    elif action.startswith("settings:format:"):
        video_format = _decode_format_callback(action.removeprefix("settings:format:"))
        if video_format in VIDEO_FORMATS:
            pending.settings.video_format = video_format
        await _edit_montage_settings(callback.message, pending, _montage_settings_keyboard(pending))
    elif action == "settings:fill":
        await _edit_montage_settings(callback.message, pending, _fill_color_keyboard())
    elif action.startswith("settings:fill:"):
        fill_color = action.removeprefix("settings:fill:")
        if fill_color in FILL_COLORS:
            pending.settings.fill_color = fill_color
        await _edit_montage_settings(callback.message, pending, _montage_settings_keyboard(pending))
    elif action == "settings:font":
        await _edit_montage_settings(callback.message, pending, _subtitle_font_keyboard())
    elif action.startswith("settings:font:"):
        subtitle_font = action.removeprefix("settings:font:")
        if subtitle_font in SUBTITLE_FONTS:
            pending.settings.subtitle_font = subtitle_font
        await _edit_montage_settings(callback.message, pending, _montage_settings_keyboard(pending))
    elif action == "settings:subtitle_color":
        await _edit_montage_settings(callback.message, pending, _subtitle_color_keyboard())
    elif action.startswith("settings:subtitle_color:"):
        subtitle_color = action.removeprefix("settings:subtitle_color:")
        if subtitle_color in SUBTITLE_COLORS:
            pending.settings.subtitle_color = subtitle_color
        await _edit_montage_settings(callback.message, pending, _montage_settings_keyboard(pending))
    elif action == "settings:speed":
        await _edit_montage_settings(callback.message, pending, _video_speed_keyboard())
    elif action.startswith("settings:speed:"):
        video_speed = _decode_speed_callback(action.removeprefix("settings:speed:"))
        if video_speed in VIDEO_SPEEDS:
            pending.settings.video_speed = video_speed
        await _edit_montage_settings(callback.message, pending, _montage_settings_keyboard(pending))
    elif action == "settings:mirror":
        pending.settings.mirror = not pending.settings.mirror
        await _edit_montage_settings(callback.message, pending, _montage_settings_keyboard(pending))
    elif action == "settings:metadata":
        pending.settings.strip_metadata = not pending.settings.strip_metadata
        await _edit_montage_settings(callback.message, pending, _montage_settings_keyboard(pending))
    elif action == "settings:render":
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=None)
        await _process_pending_video(
            message=callback.message,
            user_id=user_id,
            pending=pending,
        )
        return

    await callback.answer()


@dp.message()
async def handle_other(message: Message) -> None:
    user_id = _user_id(message)
    if pending_videos.get(user_id):
        await message.answer("Отправь рекламный текст сообщением, картинку-баннер или нажми «Без контента».")
        return

    await message.answer("Пожалуйста, отправь видео файлом Telegram video.")


async def main() -> None:
    global app_config, db_engine, db_session_factory

    config = load_config()
    app_config = config
    db_engine = create_engine(config)
    db_session_factory = create_session_factory(db_engine)

    try:
        ensure_ffmpeg_available()
    except FFmpegNotFoundError:
        logger.exception("Startup check failed")
        raise

    TMP_DIR.mkdir(parents=True, exist_ok=True)

    session = _create_session(config)
    bot = Bot(token=config.bot_token, session=session) if session else Bot(token=config.bot_token)
    logger.info(
        "Bot started: render_job_timeout=%s telegram_request_timeout=%s",
        config.render_job_timeout_seconds,
        config.telegram_request_timeout_seconds,
    )
    await dp.start_polling(bot)


def _create_session(config: Config) -> AiohttpSession | None:
    if not config.telegram_api_base:
        return AiohttpSession(timeout=config.telegram_request_timeout_seconds)

    api = TelegramAPIServer.from_base(
        config.telegram_api_base,
        is_local=config.telegram_api_is_local,
    )
    return AiohttpSession(api=api, timeout=config.telegram_request_timeout_seconds)


async def _send_montage_settings(message: Message, pending: PendingVideo) -> None:
    await message.answer(
        _montage_settings_text(pending),
        reply_markup=_montage_settings_keyboard(pending),
    )


async def _edit_montage_settings(
    message: Message,
    pending: PendingVideo,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    await message.edit_text(
        _montage_settings_text(pending),
        reply_markup=reply_markup,
    )


def _montage_settings_text(pending: PendingVideo) -> str:
    settings = pending.settings
    return (
        f"Кол-во видео - {pending.video_count}\n"
        f"Рекламный контент - {_ad_content_label(pending)}\n\n"
        "Параметры для монтажа:\n\n"
        f"Формат: {_format_label(settings.video_format)}\n"
        f"Заполнение пустоты: {FILL_COLORS[settings.fill_color]}\n"
        f"Шрифт субтитров: {settings.subtitle_font}\n"
        f"Цвет субтитров: {SUBTITLE_COLORS[settings.subtitle_color]}\n"
        f"Ускорение видео: {VIDEO_SPEEDS[settings.video_speed]}\n"
        f"Зеркальность видео: {'да' if settings.mirror else 'нет'}\n"
        f"Удаление метаданных: {'да' if settings.strip_metadata else 'нет'}\n\n"
        "Если все подходит, нажмите \"Отправить в монтаж\""
    )


def _montage_settings_keyboard(pending: PendingVideo) -> InlineKeyboardMarkup:
    mirror_text = "Выключить зеркальность" if pending.settings.mirror else "Включить зеркальность"
    metadata_text = "Оставить метаданные" if pending.settings.strip_metadata else "Удалить метаданные"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Изменить формат", callback_data="settings:format")],
            [InlineKeyboardButton(text="Изменить цвет заполнения", callback_data="settings:fill")],
            [InlineKeyboardButton(text="Изменить шрифт субтитров", callback_data="settings:font")],
            [InlineKeyboardButton(text="Изменить цвет субтитров", callback_data="settings:subtitle_color")],
            [InlineKeyboardButton(text="Ускорение", callback_data="settings:speed")],
            [InlineKeyboardButton(text=mirror_text, callback_data="settings:mirror")],
            [InlineKeyboardButton(text=metadata_text, callback_data="settings:metadata")],
            [InlineKeyboardButton(text="✅ Отправить в монтаж", callback_data="settings:render")],
        ]
    )


def _format_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="9:16 — без растягивания", callback_data="settings:format:9_16")],
            [InlineKeyboardButton(text="9:16 — с небольшим приближением", callback_data="settings:format:9_16_soft_zoom")],
            [InlineKeyboardButton(text="9:16 — растянутый, без полей", callback_data="settings:format:9_16_cover")],
            [InlineKeyboardButton(text="Назад", callback_data="settings:main")],
        ]
    )


def _fill_color_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Черное", callback_data="settings:fill:black")],
            [InlineKeyboardButton(text="Белое", callback_data="settings:fill:white")],
            [InlineKeyboardButton(text="Назад", callback_data="settings:main")],
        ]
    )


def _subtitle_font_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=name, callback_data=f"settings:font:{name}")]
            for name in SUBTITLE_FONTS
        ]
        + [[InlineKeyboardButton(text="Назад", callback_data="settings:main")]]
    )


def _subtitle_color_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Белый", callback_data="settings:subtitle_color:white")],
            [InlineKeyboardButton(text="Черный", callback_data="settings:subtitle_color:black")],
            [InlineKeyboardButton(text="Назад", callback_data="settings:main")],
        ]
    )


def _video_speed_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Без ускорения", callback_data="settings:speed:none")],
            [InlineKeyboardButton(text="1.10x", callback_data="settings:speed:1_10")],
            [InlineKeyboardButton(text="1.25x", callback_data="settings:speed:1_25")],
            [InlineKeyboardButton(text="1.50x", callback_data="settings:speed:1_50")],
            [InlineKeyboardButton(text="2.00x", callback_data="settings:speed:2_00")],
            [InlineKeyboardButton(text="Назад", callback_data="settings:main")],
        ]
    )


def _format_label(video_format: str) -> str:
    labels = {
        "9:16": "9:16, без растягивания",
        "9:16_soft_zoom": "9:16, с небольшим приближением",
        "9:16_cover": "9:16, растянутый, без полей",
    }
    return labels.get(video_format, labels[DEFAULT_VIDEO_FORMAT])


def _decode_format_callback(value: str) -> str:
    if value == "9_16_soft_zoom":
        return "9:16_soft_zoom"
    if value == "9_16_cover":
        return "9:16_cover"

    return value.replace("_", ":")


def _decode_speed_callback(value: str) -> float:
    if value == "none":
        return DEFAULT_VIDEO_SPEED

    try:
        return float(value.replace("_", "."))
    except ValueError:
        return DEFAULT_VIDEO_SPEED


def _ad_content_label(pending: PendingVideo) -> str:
    if pending.ad_text is not None:
        return f"«{pending.ad_text}»"
    if pending.ad_banner_file_id is not None:
        return pending.ad_banner_name or "banner"

    return "нет"


async def _process_pending_video(
    message: Message,
    user_id: int,
    pending: PendingVideo,
) -> None:
    pending_videos.pop(user_id, None)

    try:
        queue_position = await _persist_pending_for_render(pending)
        await message.answer("Видео поставлено в очередь на монтаж")
        status_message = await message.answer(
            _queue_position_text(queue_position),
            reply_markup=_cancel_montage_keyboard(),
        )
        await _set_job_status_message_id(pending.job_id, status_message.message_id)
        await enqueue_render_job(_app_config(), str(pending.job_id))
    except InsufficientCreditsError:
        pending_videos[user_id] = pending
        await message.answer(
            "Недостаточно видео на счете.\n\n"
            "Пополните баланс, чтобы отправить ролик в монтаж.\n\n"
            f"{_tariffs_text()}",
            reply_markup=_menu_keyboard(),
        )
    except Exception as exc:
        logger.exception("Failed to enqueue render job: job_id=%s", pending.job_id)
        await _mark_enqueue_failed_and_refund(pending.job_id, str(exc))
        await message.answer(f"Ошибка: не удалось поставить видео в очередь. {exc}")


async def _create_pending_job(
    message: Message,
    telegram_video_file_id: str,
    telegram_video_file_unique_id: str | None,
):
    session_factory = _db_session_factory()
    telegram_user = message.from_user
    async with session_factory() as session:
        async with session.begin():
            user = await get_or_create_user(
                session,
                telegram_user_id=_user_id(message),
                telegram_username=telegram_user.username if telegram_user else None,
                first_name=telegram_user.first_name if telegram_user else None,
                last_name=telegram_user.last_name if telegram_user else None,
            )
            return await create_draft_job(
                session,
                user_id=user.id,
                telegram_chat_id=message.chat.id,
                telegram_message_id=message.message_id,
                telegram_video_file_id=telegram_video_file_id,
                telegram_video_file_unique_id=telegram_video_file_unique_id,
            )


async def _persist_pending_for_render(pending: PendingVideo) -> int:
    session_factory = _db_session_factory()
    async with session_factory() as session:
        async with session.begin():
            job = await get_job(session, pending.job_id)
            if job is None:
                raise VideoProcessingError("Video job was not found")

            await charge_credits(
                session,
                user_id=job.user_id,
                amount=RENDER_COST_VIDEOS,
                reason="render_started",
                source="bot",
                related_job_id=job.id,
            )
            job.ad_content_type = _ad_content_type(pending)
            job.ad_text = pending.ad_text
            job.ad_banner_file_id = pending.ad_banner_file_id
            job.ad_banner_file_unique_id = pending.ad_banner_file_unique_id
            job.ad_banner_name = pending.ad_banner_name

            if job.settings is None:
                job.settings = VideoJobSettings(job_id=job.id)

            _copy_pending_settings(pending, job.settings)
            await mark_queued(session, job, credits_charged=RENDER_COST_VIDEOS)
            return await get_queue_position(session, job)


async def _set_job_status_message_id(job_id: uuid.UUID, message_id: int) -> None:
    session_factory = _db_session_factory()
    async with session_factory() as session:
        async with session.begin():
            job = await get_job(session, job_id)
            if job:
                await set_status_message_id(session, job, message_id)


async def _mark_enqueue_failed_and_refund(job_id: uuid.UUID, error_message: str) -> None:
    session_factory = _db_session_factory()
    async with session_factory() as session:
        async with session.begin():
            job = await get_job(session, job_id)
            if job is None or job.status in {"completed", "failed", "cancelled"}:
                return
            if job.credits_charged > 0:
                await add_credits(
                    session,
                    user_id=job.user_id,
                    amount=job.credits_charged,
                    reason="enqueue_failed_refund",
                    source="bot",
                    related_job_id=job.id,
                )
                await mark_refunded(session, job)
            await mark_failed(session, job, error_message)


async def _refresh_queue_positions(bot: Bot) -> None:
    session_factory = _db_session_factory()
    async with session_factory() as session:
        queued_jobs = await list_queued_jobs(session)

    for position, job in enumerate(queued_jobs, start=1):
        if job.telegram_status_message_id is None:
            continue
        try:
            await bot.edit_message_text(
                chat_id=job.telegram_chat_id,
                message_id=job.telegram_status_message_id,
                text=_queue_position_text(position),
            )
        except Exception:
            logger.exception("Failed to refresh queue status message: job_id=%s", job.id)


async def _cancel_latest_active_job(message: Message) -> tuple[bool, int, str, int | None, int | None]:
    session_factory = _db_session_factory()
    telegram_user_id = _user_id(message)
    async with session_factory() as session:
        async with session.begin():
            user = await get_or_create_user(
                session,
                telegram_user_id=telegram_user_id,
                telegram_username=message.from_user.username if message.from_user else None,
                first_name=message.from_user.first_name if message.from_user else None,
                last_name=message.from_user.last_name if message.from_user else None,
            )
            job = await get_latest_active_job_for_telegram_user(session, telegram_user_id=telegram_user_id)
            if job is None:
                balance_value = await get_balance(session, user_id=user.id)
                return False, balance_value, "Активного монтажа для отмены нет", None, None

            if job.status != "queued":
                balance_value = await get_balance(session, user_id=user.id)
                return (
                    False,
                    balance_value,
                    "Видео уже отправлено на монтаж, отменить сейчас нельзя",
                    None,
                    None,
                )

            if job.credits_charged > 0:
                await add_credits(
                    session,
                    user_id=job.user_id,
                    amount=job.credits_charged,
                    reason="render_cancelled_refund",
                    source="bot",
                    related_job_id=job.id,
                )
                await mark_refunded(session, job)
            await mark_cancelled(session, job)
            balance_value = await get_balance(session, user_id=user.id)
            return True, balance_value, "Монтаж отменен", job.telegram_chat_id, job.telegram_status_message_id


async def _has_active_render_job(telegram_user_id: int) -> bool:
    if db_session_factory is None:
        return False

    async with db_session_factory() as session:
        job = await get_latest_active_job_for_telegram_user(session, telegram_user_id=telegram_user_id)
        if job is None:
            return False

        active_since = job.started_at or job.queued_at or job.created_at
        if active_since is None:
            return False
        if active_since.tzinfo is None:
            active_since = active_since.replace(tzinfo=UTC)

        max_active_seconds = _app_config().render_job_timeout_seconds
        return (datetime.now(UTC) - active_since).total_seconds() <= max_active_seconds


async def _mark_pending_cancelled(pending: PendingVideo) -> None:
    try:
        session_factory = _db_session_factory()
    except RuntimeError:
        return

    async with session_factory() as session:
        async with session.begin():
            job = await get_job(session, pending.job_id)
            if job and job.status == "draft":
                job.status = "cancelled"


def _copy_pending_settings(pending: PendingVideo, settings: VideoJobSettings) -> None:
    settings.video_count = pending.video_count
    settings.video_format = pending.settings.video_format
    settings.fill_color = pending.settings.fill_color
    settings.subtitle_font = pending.settings.subtitle_font
    settings.subtitle_color = pending.settings.subtitle_color
    settings.video_speed = pending.settings.video_speed
    settings.mirror = pending.settings.mirror
    settings.strip_metadata = pending.settings.strip_metadata


def _ad_content_type(pending: PendingVideo) -> str:
    if pending.ad_text is not None:
        return "text"
    if pending.ad_banner_file_id is not None:
        return "banner"

    return "none"


def _app_config() -> Config:
    if app_config is None:
        raise RuntimeError("App config is not initialized")

    return app_config


def _db_session_factory() -> async_sessionmaker[AsyncSession]:
    if db_session_factory is None:
        raise RuntimeError("Database session factory is not initialized")

    return db_session_factory


async def _ensure_intro_bonus(message: Message) -> tuple[int, int] | None:
    session_factory = _db_session_factory()
    telegram_user = message.from_user
    async with session_factory() as session:
        async with session.begin():
            user = await get_or_create_user(
                session,
                telegram_user_id=_user_id(message),
                telegram_username=telegram_user.username if telegram_user else None,
                first_name=telegram_user.first_name if telegram_user else None,
                last_name=telegram_user.last_name if telegram_user else None,
            )
            if user.intro_bonus_granted:
                return None

            await add_credits(
                session,
                user_id=user.id,
                amount=INTRO_BONUS_VIDEOS,
                reason="intro_bonus",
                source="bot",
            )
            user.intro_bonus_granted = True
            balance_value = await get_balance(session, user_id=user.id)
            return INTRO_BONUS_VIDEOS, balance_value


async def _user_video_balance(message: Message) -> int:
    return await _user_video_balance_from_telegram_user(message.from_user, fallback_id=_user_id(message))


async def _user_video_balance_from_telegram_user(telegram_user, fallback_id: int | None = None) -> int:
    session_factory = _db_session_factory()
    telegram_user_id = telegram_user.id if telegram_user else fallback_id
    if telegram_user_id is None:
        raise RuntimeError("Telegram user id is missing")

    async with session_factory() as session:
        async with session.begin():
            user = await get_or_create_user(
                session,
                telegram_user_id=telegram_user_id,
                telegram_username=telegram_user.username if telegram_user else None,
                first_name=telegram_user.first_name if telegram_user else None,
                last_name=telegram_user.last_name if telegram_user else None,
            )
            return await get_balance(session, user_id=user.id)


def _welcome_text() -> str:
    return (
        "Бот помогает быстро готовить рекламные видео-нарезки для вертикальных форматов.\n\n"
        "Отправьте видео, добавьте рекламный текст или баннер, а бот автоматически соберет ролик "
        "1080x1920, адаптирует формат, добавит рекламный блок и субтитры из речи."
    )


def _how_it_works_text() -> str:
    return (
        "Краткая информация о работе c ботом:\n"
        f"1. 🎞 Отправляете видео (до {MAX_VIDEO_SIZE_MB} МБ), которое необходимо смонтировать\n"
        "2. 📌 Если необходимо, отправляете рекламный контент (текст или баннер)\n"
        "3. ⚙️ Выбираете настройки для монтажа\n"
        "4. ✅ Получаете готовое видео"
    )


def _balance_added_text(*, added_videos: int, balance_value: int) -> str:
    return (
        f"➕ Начислено {added_videos} видео на ваш счет\n"
        f"Доступно: {balance_value} видео"
    )


def _tariffs_text() -> str:
    lines = ["Стоимость:"]
    for _, title, price, discount in TARIFF_PACKAGES:
        line = f"🔹 {title} — {price}"
        if discount:
            line += f" · {discount}"
        lines.append(line)

    return "\n".join(lines)


def _menu_text(balance_value: int) -> str:
    return (
        f"На вашем счете: {balance_value} видео\n\n"
        f"{_tariffs_text()}"
    )


def _start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Меню", callback_data=MENU_CALLBACK),
                InlineKeyboardButton(text="Начать монтаж", callback_data=START_MONTAGE_CALLBACK),
            ],
        ]
    )


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Купить {title}",
                    callback_data=f"{BUY_PACKAGE_CALLBACK_PREFIX}{videos_count}",
                )
            ]
            for videos_count, title, _, _ in TARIFF_PACKAGES
        ]
        + [
            [InlineKeyboardButton(text="Начать монтаж", callback_data=START_MONTAGE_CALLBACK)],
        ]
    )


def _queue_position_text(position: int) -> str:
    return f"Вы {position} в очереди"


def _cancel_montage_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=CANCEL_MONTAGE_TEXT)]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _command_payload(text: str) -> str:
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _is_image_document(mime_type: str | None, file_name: str | None) -> bool:
    if mime_type and mime_type.startswith("image/"):
        return True

    suffix = Path(file_name or "").suffix.casefold()
    return suffix in {".jpg", ".jpeg", ".png", ".webp"}


def _image_suffix(mime_type: str | None, file_name: str | None) -> str:
    suffix = Path(file_name or "").suffix.casefold()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return suffix

    if mime_type == "image/png":
        return ".png"
    if mime_type == "image/webp":
        return ".webp"

    return ".jpg"


def _is_allowed_user(message: Message) -> bool:
    if app_config is None:
        return False

    username = message.from_user.username if message.from_user else None
    if not username:
        return False

    return username.casefold() in app_config.allowed_telegram_usernames


def _is_allowed_callback(callback: CallbackQuery) -> bool:
    if app_config is None or not callback.from_user.username:
        return False

    return callback.from_user.username.casefold() in app_config.allowed_telegram_usernames


def _user_id(message: Message) -> int:
    return message.from_user.id if message.from_user else message.chat.id


def _ad_content_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=NO_CONTENT_TEXT, callback_data="content:none")],
        ]
    )


if __name__ == "__main__":
    asyncio.run(main())
