import asyncio
import logging
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, Message

from app.config import Config, load_config
from app.video_processor import (
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


dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        f"Привет! Отправь мне видео до {MAX_VIDEO_SIZE_MB} МБ, "
        "а я сделаю вертикальный ролик 720x1280."
    )


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
    output_path = TMP_DIR / f"{job_id}_output.mp4"

    await message.answer("Видео принято, обрабатываю...")

    try:
        telegram_file = await bot.get_file(video.file_id)
        if not telegram_file.file_path:
            raise VideoProcessingError("Telegram did not return file_path for video")

        await bot.download_file(telegram_file.file_path, destination=input_path)

        await process_video(input_path=input_path, output_path=output_path)

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
        output_path.unlink(missing_ok=True)


@dp.message()
async def handle_other(message: Message) -> None:
    await message.answer("Пожалуйста, отправь видео файлом Telegram video.")


async def main() -> None:
    config = load_config()

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


if __name__ == "__main__":
    asyncio.run(main())
