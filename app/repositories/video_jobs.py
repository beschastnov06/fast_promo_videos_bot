from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import VideoJob, VideoJobSettings


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
        select(VideoJob).where(VideoJob.id == job_id)
    )
    return result.scalar_one_or_none()


async def mark_queued(session: AsyncSession, job: VideoJob, *, credits_charged: int) -> None:
    job.status = "queued"
    job.credits_charged = credits_charged
    job.queued_at = datetime.now(UTC)


async def mark_processing(session: AsyncSession, job: VideoJob) -> None:
    job.status = "processing"
    job.started_at = datetime.now(UTC)


async def mark_completed(session: AsyncSession, job: VideoJob) -> None:
    job.status = "completed"
    job.finished_at = datetime.now(UTC)
    job.error_message = None


async def mark_failed(session: AsyncSession, job: VideoJob, error_message: str) -> None:
    job.status = "failed"
    job.finished_at = datetime.now(UTC)
    job.error_message = error_message
