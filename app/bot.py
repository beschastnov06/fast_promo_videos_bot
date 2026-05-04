import asyncio
import logging
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, Message

from app.config import Config, load_config
from app.subtitles import write_ass_subtitles
from app.transcriber import TranscriptionError, extract_audio, transcribe_audio
from app.video_processor import (
    DEFAULT_AD_TEXT,
    FFmpegNotFoundError,
    VideoProcessingError,
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
TMP_DIR = Path("tmp")
AD_BANNERS_DIR = TMP_DIR / "ad_banners"

user_ad_texts: dict[int, str] = {}
user_ad_banners: dict[int, Path] = {}
app_config: Config | None = None


dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        f"Привет! Отправь мне видео до {MAX_VIDEO_SIZE_MB} МБ, "
        "а я сделаю вертикальный ролик 1080x1920. "
        "Верхнюю рекламу можно задать командой /ad или отправить картинку-баннер. "
        "Субтитры снизу сделаю автоматически из речи."
    )


@dp.message(Command("ad"))
async def set_ad_text(message: Message) -> None:
    text = _command_payload(message.text or "")
    if not text:
        await message.answer("Напиши текст после команды, например: /ad Реклама: @example")
        return

    user_id = _user_id(message)
    user_ad_texts[user_id] = text
    user_ad_banners.pop(user_id, None)

    await message.answer("Верхняя текстовая рекламная плашка сохранена. Теперь отправь видео.")


@dp.message(Command("clear_ad"))
async def clear_ad(message: Message) -> None:
    user_id = _user_id(message)
    banner_path = user_ad_banners.pop(user_id, None)
    if banner_path:
        banner_path.unlink(missing_ok=True)
    user_ad_texts.pop(user_id, None)

    await message.answer(f"Верхняя рекламная плашка сброшена. Будет использоваться: {DEFAULT_AD_TEXT}")


@dp.message(F.photo)
async def set_ad_banner(message: Message, bot: Bot) -> None:
    user_id = _user_id(message)
    AD_BANNERS_DIR.mkdir(parents=True, exist_ok=True)
    banner_path = AD_BANNERS_DIR / f"{user_id}.jpg"

    photo = message.photo[-1]
    telegram_file = await bot.get_file(photo.file_id)
    if not telegram_file.file_path:
        await message.answer("Не удалось скачать баннер. Попробуй другую картинку.")
        return

    await bot.download_file(telegram_file.file_path, destination=banner_path)

    user_ad_banners[user_id] = banner_path
    user_ad_texts.pop(user_id, None)

    await message.answer("Баннер сохранен как верхняя рекламная плашка. Теперь отправь видео.")


@dp.message(F.video)
async def handle_video(message: Message, bot: Bot) -> None:
    video = message.video

    if video.file_size and video.file_size > MAX_VIDEO_SIZE_BYTES:
        await message.answer(
            f"Видео слишком большое. Максимальный размер сейчас — {MAX_VIDEO_SIZE_MB} МБ."
        )
        return

    job_id = uuid.uuid4().hex
    input_path = TMP_DIR / f"{job_id}_input.mp4"
    audio_path = TMP_DIR / f"{job_id}_audio.mp3"
    subtitles_path = TMP_DIR / f"{job_id}_subtitles.ass"
    output_path = TMP_DIR / f"{job_id}_output.mp4"

    await message.answer("Видео принято, обрабатываю...")

    try:
        user_id = _user_id(message)
        ad_banner_path = user_ad_banners.get(user_id)
        if ad_banner_path and not ad_banner_path.exists():
            ad_banner_path = None

        telegram_file = await bot.get_file(video.file_id)
        if not telegram_file.file_path:
            raise VideoProcessingError("Telegram did not return file_path for video")

        await bot.download_file(telegram_file.file_path, destination=input_path)
        subtitles_file = await _create_subtitles_file(input_path, audio_path, subtitles_path)

        await process_video(
            input_path=input_path,
            output_path=output_path,
            ad_text=user_ad_texts.get(user_id, DEFAULT_AD_TEXT),
            ad_banner_path=ad_banner_path,
            subtitles_path=subtitles_file,
        )

        await message.answer_video(
            video=FSInputFile(output_path),
            caption="Готово!",
        )
    except VideoProcessingError:
        logger.exception("Video processing failed for message_id=%s", message.message_id)
        await message.answer("Не удалось обработать видео. Попробуй другой файл.")
    except Exception:
        logger.exception("Unexpected error while handling video message_id=%s", message.message_id)
        await message.answer("Произошла ошибка. Попробуй отправить видео еще раз.")
    finally:
        input_path.unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)
        subtitles_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)


@dp.message()
async def handle_other(message: Message) -> None:
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
    AD_BANNERS_DIR.mkdir(parents=True, exist_ok=True)

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


def _command_payload(text: str) -> str:
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _user_id(message: Message) -> int:
    return message.from_user.id if message.from_user else message.chat.id


if __name__ == "__main__":
    asyncio.run(main())
