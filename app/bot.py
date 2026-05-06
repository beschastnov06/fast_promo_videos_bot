import asyncio
from dataclasses import dataclass, field
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
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)

from app.config import Config, load_config
from app.subtitles import write_ass_subtitles
from app.transcriber import TranscriptionError, extract_audio, transcribe_audio
from app.video_processor import (
    FFmpegNotFoundError,
    HEIGHT,
    VIDEO_FORMATS,
    VideoProcessingError,
    WIDTH,
    ensure_ffmpeg_available,
    process_video,
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
MAX_AD_TEXT_CHARS = 60
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
    input_path: Path
    audio_path: Path
    subtitles_path: Path
    output_path: Path
    video_count: int = 1
    settings: MontageSettings = field(default_factory=MontageSettings)
    ad_text: str | None = None
    ad_banner_path: Path | None = None
    ad_banner_name: str | None = None
    cleanup_ad_banner: bool = False
    ready_for_montage: bool = False


pending_videos: dict[int, PendingVideo] = {}
app_config: Config | None = None


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


dp.message.outer_middleware(UsernameWhitelistMiddleware())


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        f"Отправьте видео до {MAX_VIDEO_SIZE_MB} МБ, "
        "а я сделаю вертикальный ролик 1080x1920. "
        "После отправки видео я попрошу рекламный текст или баннер для верхней части макета, "
        "а субтитры снизу сделаю автоматически из аудио в видео."
    )


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
    pending.ad_banner_path = None
    pending.ad_banner_name = None
    pending.cleanup_ad_banner = False
    pending.ready_for_montage = True
    await _send_montage_settings(message, pending)


@dp.message(Command("clear_ad"))
async def clear_ad(message: Message) -> None:
    user_id = _user_id(message)
    pending = pending_videos.pop(user_id, None)
    if pending:
        _cleanup_pending(pending)
        if pending.cleanup_ad_banner and pending.ad_banner_path:
            pending.ad_banner_path.unlink(missing_ok=True)
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

    banner_path = pending.input_path.with_name(f"{pending.input_path.stem}_banner{suffix}")

    telegram_file = await bot.get_file(file_id)
    if not telegram_file.file_path:
        await message.answer("Ошибка: не удалось скачать баннер. Попробуй другую картинку.")
        return

    await message.answer("Рекламный контент обрабатывается", reply_markup=ReplyKeyboardRemove())
    await bot.download_file(telegram_file.file_path, destination=banner_path)

    await message.answer("Рекламный контент обработан")
    pending.ad_text = None
    pending.ad_banner_path = banner_path
    pending.ad_banner_name = display_name or banner_path.name
    pending.cleanup_ad_banner = True
    pending.ready_for_montage = True
    await _send_montage_settings(message, pending)


@dp.message(F.video)
async def handle_video(message: Message, bot: Bot) -> None:
    video = message.video

    if video.file_size and video.file_size > MAX_VIDEO_SIZE_BYTES:
        await message.answer(f"Ошибка: видео слишком большое. Максимальный размер сейчас — {MAX_VIDEO_SIZE_MB} МБ.")
        return

    job_id = uuid.uuid4().hex
    input_path = TMP_DIR / f"{job_id}_input.mp4"
    audio_path = TMP_DIR / f"{job_id}_audio.mp3"
    subtitles_path = TMP_DIR / f"{job_id}_subtitles.ass"
    output_path = TMP_DIR / f"{job_id}_output.mp4"

    user_id = _user_id(message)
    old_pending = pending_videos.pop(user_id, None)
    if old_pending:
        _cleanup_pending(old_pending)

    try:
        telegram_file = await bot.get_file(video.file_id)
        if not telegram_file.file_path:
            raise VideoProcessingError("Telegram did not return file_path for video")

        await bot.download_file(telegram_file.file_path, destination=input_path)

        pending_videos[user_id] = PendingVideo(
            input_path=input_path,
            audio_path=audio_path,
            subtitles_path=subtitles_path,
            output_path=output_path,
        )

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
        input_path.unlink(missing_ok=True)
    except Exception as exc:
        logger.exception("Unexpected error while preparing video message_id=%s", message.message_id)
        await message.answer(f"Ошибка: не удалось принять видео. {exc}")
        input_path.unlink(missing_ok=True)


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
        pending.ad_banner_path = None
        pending.ad_banner_name = None
        pending.cleanup_ad_banner = False
        pending.ready_for_montage = True
        await _send_montage_settings(message, pending)
        return

    if len(text) > MAX_AD_TEXT_CHARS:
        await message.answer(f"Ошибка: рекламный текст слишком длинный. Максимум — {MAX_AD_TEXT_CHARS} символов.")
        return

    await message.answer("Рекламный контент обрабатывается", reply_markup=ReplyKeyboardRemove())
    await message.answer("Рекламный контент обработан")
    pending.ad_text = text
    pending.ad_banner_path = None
    pending.ad_banner_name = None
    pending.cleanup_ad_banner = False
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
    pending.ad_banner_path = None
    pending.ad_banner_name = None
    pending.cleanup_ad_banner = False
    pending.ready_for_montage = True
    await _send_montage_settings(callback.message, pending)
    await callback.answer()


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
    global app_config

    config = load_config()
    app_config = config

    try:
        ensure_ffmpeg_available()
    except FFmpegNotFoundError:
        logger.exception("Startup check failed")
        raise

    TMP_DIR.mkdir(parents=True, exist_ok=True)

    session = _create_session(config)
    bot = Bot(token=config.bot_token, session=session) if session else Bot(token=config.bot_token)
    logger.info("Bot started")
    await dp.start_polling(bot)


def _create_session(config: Config) -> AiohttpSession | None:
    if not config.telegram_api_base:
        return None

    api = TelegramAPIServer.from_base(
        config.telegram_api_base,
        is_local=config.telegram_api_is_local,
    )
    return AiohttpSession(api=api)


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
    if pending.ad_banner_path is not None:
        return pending.ad_banner_name or pending.ad_banner_path.name

    return "нет"


async def _create_subtitles_file(
    input_path: Path,
    audio_path: Path,
    subtitles_path: Path,
    subtitle_font: str = DEFAULT_SUBTITLE_FONT,
    subtitle_color: str = DEFAULT_SUBTITLE_COLOR,
) -> Path | None:
    if not app_config or not app_config.openai_api_key:
        logger.warning("OPENAI_API_KEY is not set. Video will be processed without subtitles.")
        return None

    try:
        await extract_audio(input_path=input_path, output_path=audio_path)
        segments = await transcribe_audio(audio_path=audio_path, api_key=app_config.openai_api_key)
    except TranscriptionError:
        logger.exception("Transcription failed")
        return None
    except Exception:
        logger.exception("Unexpected transcription error")
        return None

    if not segments:
        logger.info("Transcription returned no subtitle segments")
        return None

    write_ass_subtitles(
        segments=segments,
        output_path=subtitles_path,
        font_name=subtitle_font,
        font_color=subtitle_color,
    )
    return subtitles_path


async def _process_pending_video(
    message: Message,
    user_id: int,
    pending: PendingVideo,
) -> None:
    pending_videos.pop(user_id, None)

    await message.answer("Начал монтировать видео")

    try:
        subtitles_file = await _create_subtitles_file(
            input_path=pending.input_path,
            audio_path=pending.audio_path,
            subtitles_path=pending.subtitles_path,
            subtitle_font=pending.settings.subtitle_font,
            subtitle_color=pending.settings.subtitle_color,
        )

        await process_video(
            input_path=pending.input_path,
            output_path=pending.output_path,
            ad_text=pending.ad_text,
            ad_banner_path=pending.ad_banner_path,
            subtitles_path=subtitles_file,
            output_format=pending.settings.video_format,
            fill_color=pending.settings.fill_color,
            video_speed=pending.settings.video_speed,
            mirror=pending.settings.mirror,
            strip_metadata=pending.settings.strip_metadata,
        )

        output_width, output_height = VIDEO_FORMATS.get(pending.settings.video_format, (WIDTH, HEIGHT))
        await message.answer_video(
            video=FSInputFile(pending.output_path),
            width=output_width,
            height=output_height,
            caption="Готово",
        )
    except VideoProcessingError as exc:
        logger.exception("Video processing failed for message_id=%s", message.message_id)
        await message.answer(f"Ошибка: не удалось смонтировать видео. {exc}")
    except Exception as exc:
        logger.exception("Unexpected error while processing pending video")
        await message.answer(f"Ошибка: не удалось смонтировать видео. {exc}")
    finally:
        _cleanup_pending(pending)
        if pending.cleanup_ad_banner and pending.ad_banner_path:
            pending.ad_banner_path.unlink(missing_ok=True)


def _cleanup_pending(pending: PendingVideo) -> None:
    pending.input_path.unlink(missing_ok=True)
    pending.audio_path.unlink(missing_ok=True)
    pending.subtitles_path.unlink(missing_ok=True)
    pending.output_path.unlink(missing_ok=True)


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
