from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import User, VideoJob, VideoJobSettings


async def create_draft_job(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    telegram_chat_id: int,
    telegram_message_id: int | None,
    telegram_video_file_id: str | None,
    telegram_video_file_unique_id: str | None,
) -> VideoJob:
    job = VideoJob(
        user_id=user_id,
        telegram_chat_id=telegram_chat_id,
        telegram_message_id=telegram_message_id,
        telegram_video_file_id=telegram_video_file_id,
        telegram_video_file_unique_id=telegram_video_file_unique_id,
    )
    session.add(job)
    await session.flush()
    session.add(VideoJobSettings(job_id=job.id))
    return job


async def get_job(session: AsyncSession, job_id: uuid.UUID) -> VideoJob | None:
    result = await session.execute(
        select(VideoJob)
        .options(selectinload(VideoJob.settings))
        .where(VideoJob.id == job_id)
    )
    return result.scalar_one_or_none()


async def mark_queued(session: AsyncSession, job: VideoJob, *, credits_charged: int) -> None:
    job.status = "queued"
    job.render_stage = "queued"
    job.credits_charged = credits_charged
    job.queued_at = datetime.now(UTC)


async def mark_processing(session: AsyncSession, job: VideoJob) -> None:
    job.status = "processing"
    job.render_stage = "download"
    job.started_at = datetime.now(UTC)


async def mark_completed(session: AsyncSession, job: VideoJob) -> None:
    job.status = "completed"
    job.render_stage = "completed"
    job.finished_at = datetime.now(UTC)
    job.error_message = None


async def mark_failed(session: AsyncSession, job: VideoJob, error_message: str) -> None:
    job.status = "failed"
    job.render_stage = "failed"
    job.finished_at = datetime.now(UTC)
    job.error_message = error_message


async def mark_refunded(session: AsyncSession, job: VideoJob) -> None:
    job.credits_charged = 0


async def set_status_message_id(session: AsyncSession, job: VideoJob, message_id: int) -> None:
    job.telegram_status_message_id = message_id


async def set_render_stage(session: AsyncSession, job: VideoJob, stage: str) -> None:
    job.render_stage = stage


async def get_queue_position(session: AsyncSession, job: VideoJob) -> int:
    if job.queued_at is None:
        return 1

    result = await session.execute(
        select(func.count(VideoJob.id)).where(
            VideoJob.status == "queued",
            VideoJob.queued_at <= job.queued_at,
        )
    )
    return int(result.scalar_one() or 1)


async def list_queued_jobs(session: AsyncSession) -> list[VideoJob]:
    result = await session.execute(
        select(VideoJob)
        .where(VideoJob.status == "queued")
        .order_by(VideoJob.queued_at.asc(), VideoJob.created_at.asc())
    )
    return list(result.scalars().all())


async def get_latest_active_job_for_telegram_user(
    session: AsyncSession,
    *,
    telegram_user_id: int,
) -> VideoJob | None:
    result = await session.execute(
        select(VideoJob)
        .join(User, User.id == VideoJob.user_id)
        .where(
            User.telegram_user_id == telegram_user_id,
            VideoJob.status.in_(("queued", "processing")),
        )
        .order_by(VideoJob.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def mark_cancelled(session: AsyncSession, job: VideoJob) -> None:
    job.status = "cancelled"
    job.render_stage = "cancelled"
    job.finished_at = datetime.now(UTC)
