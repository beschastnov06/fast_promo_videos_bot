import asyncio
from dataclasses import dataclass
import logging
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove

from app.config import Config, load_config
from app.subtitles import write_ass_subtitles
from app.transcriber import TranscriptionError, extract_audio, transcribe_audio
from app.video_processor import (
    FFmpegNotFoundError,
    HEIGHT,
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


@dataclass
class PendingVideo:
    input_path: Path
    audio_path: Path
    subtitles_path: Path
    output_path: Path


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
    await _process_pending_video(
        message=message,
        pending=pending,
        ad_text=text,
    )


@dp.message(Command("clear_ad"))
async def clear_ad(message: Message) -> None:
    user_id = _user_id(message)
    pending = pending_videos.pop(user_id, None)
    if pending:
        _cleanup_pending(pending)
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
    )


async def _handle_ad_banner_file(
    message: Message,
    bot: Bot,
    file_id: str,
    suffix: str,
    file_size: int | None,
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
    await _process_pending_video(
        message=message,
        pending=pending,
        ad_banner_path=banner_path,
        cleanup_ad_banner=True,
    )


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
            "Видео принято.\n\n"
            "Вы можете добавить рекламный контент (текст или баннер) сверху макета - "
            "отправьте то что необходимо.\n\n"
            f"Если дополнительного контента нет отправьте сообщение \"{NO_CONTENT_TEXT}\"",
            reply_markup=_no_content_keyboard(),
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

    text = (message.text or "").strip()
    if text.casefold() == NO_CONTENT_TEXT.casefold():
        await message.answer("Видео будет без рекламного контента", reply_markup=ReplyKeyboardRemove())
        await _process_pending_video(
            message=message,
            pending=pending,
            ad_text=None,
        )
        return

    if len(text) > MAX_AD_TEXT_CHARS:
        await message.answer(f"Ошибка: рекламный текст слишком длинный. Максимум — {MAX_AD_TEXT_CHARS} символов.")
        return

    await message.answer("Рекламный контент обрабатывается", reply_markup=ReplyKeyboardRemove())
    await message.answer("Рекламный контент обработан")
    await _process_pending_video(
        message=message,
        pending=pending,
        ad_text=text,
    )


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


async def _create_subtitles_file(
    input_path: Path,
    audio_path: Path,
    subtitles_path: Path,
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

    write_ass_subtitles(segments=segments, output_path=subtitles_path)
    return subtitles_path


async def _process_pending_video(
    message: Message,
    pending: PendingVideo,
    ad_text: str | None = None,
    ad_banner_path: Path | None = None,
    cleanup_ad_banner: bool = False,
) -> None:
    user_id = _user_id(message)
    pending_videos.pop(user_id, None)

    await message.answer("Начал монтировать видео")

    try:
        subtitles_file = await _create_subtitles_file(
            input_path=pending.input_path,
            audio_path=pending.audio_path,
            subtitles_path=pending.subtitles_path,
        )

        await process_video(
            input_path=pending.input_path,
            output_path=pending.output_path,
            ad_text=ad_text,
            ad_banner_path=ad_banner_path,
            subtitles_path=subtitles_file,
        )

        await message.answer_video(
            video=FSInputFile(pending.output_path),
            width=WIDTH,
            height=HEIGHT,
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
        if cleanup_ad_banner and ad_banner_path:
            ad_banner_path.unlink(missing_ok=True)


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


def _user_id(message: Message) -> int:
    return message.from_user.id if message.from_user else message.chat.id


def _no_content_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=NO_CONTENT_TEXT)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder=NO_CONTENT_TEXT,
    )


if __name__ == "__main__":
    asyncio.run(main())
