from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from pathlib import Path

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Config, load_config
from app.db import create_engine, create_session_factory
from app.queue import redis_settings
from app.repositories.credits import add_credits, get_balance
from app.repositories.video_jobs import (
    get_job,
    list_queued_jobs,
    mark_completed,
    mark_failed,
    mark_processing,
    mark_refunded,
    set_render_stage,
    set_status_message_id,
)
from app.subtitles import write_ass_subtitles
from app.transcriber import TranscriptionError, extract_audio, transcribe_audio
from app.video_processor import (
    VideoProcessingError,
    ensure_ffmpeg_available,
    process_video,
    resolve_output_dimensions,
)

logger = logging.getLogger(__name__)
NEW_VIDEO_CALLBACK = "flow:new_video"
MENU_CALLBACK = "flow:menu"
SEND_VIDEO_TIMEOUT_SECONDS = 120


async def render_video(ctx: dict, job_id: str, **kwargs) -> None:
    config: Config = ctx["config"]
    bot: Bot = ctx["bot"]
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    job_uuid = uuid.UUID(job_id)
    job_dir = config.tmp_dir / job_id

    logger.info("Received render job: job_id=%s", job_id)

    async with session_factory() as session:
        async with session.begin():
            job = await get_job(session, job_uuid)
            if job is None:
                logger.warning("Render job not found: job_id=%s", job_id)
                return
            if job.status == "cancelled":
                logger.info("Render job was cancelled before processing: job_id=%s", job_id)
                await _refresh_queue_positions(bot=bot, session=session)
                return
            if job.status != "queued":
                logger.info("Render job is not queued, skipping: job_id=%s status=%s", job_id, job.status)
                return
            await mark_processing(session, job)
            status_chat_id = job.telegram_chat_id
            status_message_id = job.telegram_status_message_id

    await _replace_queue_status_with_processing_message(
        bot=bot,
        session_factory=session_factory,
        job_id=job_uuid,
        chat_id=status_chat_id,
        old_message_id=status_message_id,
    )

    async with session_factory() as session:
        await _refresh_queue_positions(bot=bot, session=session)

    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        input_path = job_dir / "input.mp4"
        audio_path = job_dir / "audio.mp3"
        subtitles_path = job_dir / "subtitles.ass"
        output_path = job_dir / "output.mp4"

        async with session_factory() as session:
            job = await get_job(session, job_uuid)
            if job is None:
                raise VideoProcessingError("Video job was not found")

        if not job.telegram_video_file_id:
            raise VideoProcessingError("Telegram video file_id is missing")

        await _download_telegram_file(bot, file_id=job.telegram_video_file_id, destination=input_path)
        ad_banner_path = await _download_ad_banner(bot, job, job_dir)

        settings = job.settings
        output_format = settings.video_format if settings else "9:16"
        output_width, output_height = await resolve_output_dimensions(input_path, output_format)
        await _update_render_stage(session_factory, bot, job_uuid, "subtitles")
        subtitles_file = await _create_subtitles_file(
            config=config,
            input_path=input_path,
            audio_path=audio_path,
            subtitles_path=subtitles_path,
            subtitle_font=settings.subtitle_font if settings else "DejaVu Sans",
            subtitle_color=settings.subtitle_color if settings else "white",
            output_width=output_width,
            output_height=output_height,
        )

        await _update_render_stage(session_factory, bot, job_uuid, "render")
        output_width, output_height = await process_video(
            input_path=input_path,
            output_path=output_path,
            ad_text=job.ad_text if job.ad_content_type == "text" else None,
            ad_banner_path=ad_banner_path,
            subtitles_path=subtitles_file,
            output_format=output_format,
            fill_color=settings.fill_color if settings else "black",
            video_speed=float(settings.video_speed) if settings else 1.0,
            mirror=settings.mirror if settings else False,
            strip_metadata=settings.strip_metadata if settings else True,
        )

        await _update_render_stage(session_factory, bot, job_uuid, "upload")
        await bot.send_message(chat_id=job.telegram_chat_id, text="Видео смонтировано отправляем вам")

        await _send_ready_video(
            bot=bot,
            chat_id=job.telegram_chat_id,
            output_path=output_path,
            output_width=output_width,
            output_height=output_height,
            balance_value=await _job_balance(session_factory, job_uuid),
        )
        await _delete_status_message(
            bot=bot,
            chat_id=job.telegram_chat_id,
            message_id=job.telegram_status_message_id,
        )

        async with session_factory() as session:
            async with session.begin():
                finished_job = await get_job(session, job_uuid)
                if finished_job:
                    await mark_completed(session, finished_job)

    except Exception as exc:
        logger.exception("Render job failed: job_id=%s", job_id)
        failed_chat_id = None
        async with session_factory() as session:
            async with session.begin():
                failed_job = await get_job(session, job_uuid)
                if failed_job:
                    if failed_job.credits_charged > 0:
                        await add_credits(
                            session,
                            user_id=failed_job.user_id,
                            amount=failed_job.credits_charged,
                            reason="render_failed_refund",
                            source="worker",
                            related_job_id=failed_job.id,
                        )
                        await mark_refunded(session, failed_job)
                    await mark_failed(session, failed_job, str(exc))
                    failed_chat_id = failed_job.telegram_chat_id
        if failed_chat_id is not None:
            try:
                await bot.send_message(
                    chat_id=failed_chat_id,
                    text=(
                        "Ошибка: не удалось смонтировать или отправить видео.\n\n"
                        "С вашего счета ничего не списалось: видео возвращено на баланс.\n"
                        "Попробуйте отправить файл заново."
                    ),
                )
            except Exception:
                logger.exception("Failed to send render failure message: job_id=%s", job_id)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def startup(ctx: dict) -> None:
    config = load_config()
    ensure_ffmpeg_available()
    engine = create_engine(config)
    session_factory = create_session_factory(engine)
    bot = _create_bot(config)

    ctx["config"] = config
    ctx["engine"] = engine
    ctx["session_factory"] = session_factory
    ctx["bot"] = bot
    config.tmp_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Worker started: render_job_timeout=%s telegram_request_timeout=%s max_jobs=%s",
        config.render_job_timeout_seconds,
        config.telegram_request_timeout_seconds,
        config.max_concurrent_renders,
    )


async def shutdown(ctx: dict) -> None:
    bot: Bot | None = ctx.get("bot")
    engine = ctx.get("engine")
    if bot:
        await bot.session.close()
    if engine:
        await engine.dispose()
    logger.info("Worker stopped")


class WorkerSettings:
    config = load_config()
    redis_settings: RedisSettings = redis_settings(config)
    functions = [render_video]
    on_startup = startup
    on_shutdown = shutdown
    job_timeout = config.render_job_timeout_seconds
    max_jobs = config.max_concurrent_renders


def _create_bot(config: Config) -> Bot:
    session = _create_session(config)
    return Bot(token=config.bot_token, session=session) if session else Bot(token=config.bot_token)


def _create_session(config: Config) -> AiohttpSession | None:
    if not config.telegram_api_base:
        return AiohttpSession(timeout=config.telegram_request_timeout_seconds)

    api = TelegramAPIServer.from_base(
        config.telegram_api_base,
        is_local=config.telegram_api_is_local,
    )
    return AiohttpSession(api=api, timeout=config.telegram_request_timeout_seconds)


async def _download_telegram_file(bot: Bot, *, file_id: str, destination: Path) -> None:
    telegram_file = await bot.get_file(file_id)
    if not telegram_file.file_path:
        raise VideoProcessingError("Telegram did not return file_path")

    await bot.download_file(telegram_file.file_path, destination=destination)


async def _download_ad_banner(bot: Bot, job, job_dir: Path) -> Path | None:
    if job.ad_content_type != "banner" or not job.ad_banner_file_id:
        return None

    suffix = Path(job.ad_banner_name or "").suffix.casefold()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".jpg"

    banner_path = job_dir / f"banner{suffix}"
    await _download_telegram_file(bot, file_id=job.ad_banner_file_id, destination=banner_path)
    return banner_path


async def _edit_status_message(
    *,
    bot: Bot,
    chat_id: int,
    message_id: int | None,
    text: str,
) -> None:
    if message_id is None:
        return

    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
    except Exception as exc:
        if "message is not modified" in str(exc).casefold():
            return
        logger.warning(
            "Failed to edit render status message, sending a new one: chat_id=%s message_id=%s error=%s",
            chat_id,
            message_id,
            exc,
        )
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            logger.exception("Failed to send fallback render status message: chat_id=%s", chat_id)


async def _replace_queue_status_with_processing_message(
    *,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    chat_id: int,
    old_message_id: int | None,
) -> None:
    if old_message_id is not None:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old_message_id)
        except Exception:
            logger.exception("Failed to delete queue status message: chat_id=%s message_id=%s", chat_id, old_message_id)

    try:
        message = await bot.send_message(
            chat_id=chat_id,
            text=await _render_status_text(session_factory, job_id),
        )
    except Exception:
        logger.exception("Failed to send processing status message: job_id=%s", job_id)
        return

    async with session_factory() as session:
        async with session.begin():
            job = await get_job(session, job_id)
            if job:
                await set_status_message_id(session, job, message.message_id)


async def _refresh_queue_positions(*, bot: Bot, session: AsyncSession) -> None:
    queued_jobs = await list_queued_jobs(session)
    for position, queued_job in enumerate(queued_jobs, start=1):
        await _edit_status_message(
            bot=bot,
            chat_id=queued_job.telegram_chat_id,
            message_id=queued_job.telegram_status_message_id,
            text=f"Вы {position} в очереди",
        )


async def _update_render_stage(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    job_id: uuid.UUID,
    stage: str,
) -> None:
    status_chat_id = None
    status_message_id = None
    status_text = None
    async with session_factory() as session:
        async with session.begin():
            job = await get_job(session, job_id)
            if job:
                await set_render_stage(session, job, stage)
                status_chat_id = job.telegram_chat_id
                status_message_id = job.telegram_status_message_id
                status_text = _render_status_text_for_job(job)

    if status_chat_id is not None and status_text is not None:
        await _edit_status_message(
            bot=bot,
            chat_id=status_chat_id,
            message_id=status_message_id,
            text=status_text,
        )


async def _render_status_text(session_factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> str:
    async with session_factory() as session:
        job = await get_job(session, job_id)
    if job is None:
        return _render_status_text_for_stage("download")

    return _render_status_text_for_job(job)


def _render_status_text_for_job(job) -> str:
    return _render_status_text_for_stage(
        job.render_stage or "download",
        ad_content_type=job.ad_content_type,
        video_speed=float(job.settings.video_speed) if job.settings else 1.0,
        mirror=job.settings.mirror if job.settings else False,
    )


def _render_status_text_for_stage(
    stage: str,
    *,
    ad_content_type: str = "none",
    video_speed: float = 1.0,
    mirror: bool = False,
) -> str:
    stage_order = ("download", "subtitles", "render", "upload")
    stage_labels = {
        "download": "Получение видео",
        "subtitles": "Субтитры",
        "render": _render_stage_label(ad_content_type=ad_content_type, video_speed=video_speed, mirror=mirror),
        "upload": "Отправка видео",
    }
    current_index = stage_order.index(stage) if stage in stage_order else len(stage_order)

    lines = ["Статус:"]
    for index, stage_key in enumerate(stage_order):
        if stage in {"completed", "failed", "cancelled"} or index < current_index:
            icon = "✅"
        elif index == current_index:
            icon = "🕜"
        else:
            icon = "⬜️"
        lines.append(f"{icon} {stage_labels[stage_key]}")

    return "\n".join(lines)


def _render_stage_label(*, ad_content_type: str, video_speed: float, mirror: bool) -> str:
    details = ["формат"]
    if ad_content_type in {"text", "banner"}:
        details.append("реклама")
    if video_speed != 1.0:
        details.append("ускорение")
    if mirror:
        details.append("зеркальность")
    details.append("сборка")
    return f"Монтаж видео ({', '.join(details)})"


async def _delete_status_message(*, bot: Bot, chat_id: int, message_id: int | None) -> None:
    if message_id is None:
        return

    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as exc:
        logger.warning("Failed to delete render status message: chat_id=%s message_id=%s error=%s", chat_id, message_id, exc)


async def _send_ready_video(
    *,
    bot: Bot,
    chat_id: int,
    output_path: Path,
    output_width: int,
    output_height: int,
    balance_value: int,
) -> None:
    file_size_mb = output_path.stat().st_size / 1024 / 1024
    logger.info("Sending rendered video: path=%s size=%.2fMB", output_path, file_size_mb)
    caption = f"Готово\n\nОстаток: {balance_value} видео"

    try:
        await asyncio.wait_for(
            bot.send_video(
                chat_id=chat_id,
                video=FSInputFile(output_path),
                width=output_width,
                height=output_height,
                caption=caption,
                reply_markup=_new_video_keyboard(),
            ),
            timeout=SEND_VIDEO_TIMEOUT_SECONDS,
        )
        logger.info("Rendered video sent as video: path=%s", output_path)
        return
    except asyncio.TimeoutError:
        logger.warning("Sending rendered video timed out, trying document fallback: path=%s", output_path)

    await asyncio.wait_for(
        bot.send_document(
            chat_id=chat_id,
            document=FSInputFile(output_path),
            caption=caption,
            reply_markup=_new_video_keyboard(),
        ),
        timeout=SEND_VIDEO_TIMEOUT_SECONDS,
    )
    logger.info("Rendered video sent as document fallback: path=%s", output_path)


async def _create_subtitles_file(
    *,
    config: Config,
    input_path: Path,
    audio_path: Path,
    subtitles_path: Path,
    subtitle_font: str,
    subtitle_color: str,
    output_width: int,
    output_height: int,
) -> Path | None:
    if not config.openai_api_key:
        logger.warning("OPENAI_API_KEY is not set. Video will be processed without subtitles.")
        return None

    try:
        await extract_audio(input_path=input_path, output_path=audio_path)
        segments = await transcribe_audio(audio_path=audio_path, api_key=config.openai_api_key)
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
        width=output_width,
        height=output_height,
    )
    return subtitles_path


async def _job_balance(session_factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> int:
    async with session_factory() as session:
        job = await get_job(session, job_id)
        if job is None:
            return 0
        return await get_balance(session, user_id=job.user_id)


def _new_video_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Пополнить счет", callback_data=MENU_CALLBACK)],
            [InlineKeyboardButton(text="Смонтировать новое видео 🆕", callback_data=NEW_VIDEO_CALLBACK)],
        ]
    )


if __name__ == "__main__":
    from arq.worker import run_worker

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_worker(WorkerSettings)
