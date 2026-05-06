from __future__ import annotations

from urllib.parse import unquote, urlparse

from arq import create_pool
from arq.connections import RedisSettings

from app.config import Config

RENDER_VIDEO_JOB = "render_video"


def redis_settings(config: Config) -> RedisSettings:
    if not config.redis_url:
        raise RuntimeError("REDIS_URL is not set.")

    parsed_url = urlparse(config.redis_url)
    database = int(parsed_url.path.lstrip("/") or "0")
    return RedisSettings(
        host=parsed_url.hostname or "localhost",
        port=parsed_url.port or 6379,
        database=database,
        username=unquote(parsed_url.username) if parsed_url.username else None,
        password=unquote(parsed_url.password) if parsed_url.password else None,
        ssl=parsed_url.scheme == "rediss",
    )


async def enqueue_render_job(config: Config, job_id: str) -> None:
    redis = await create_pool(redis_settings(config))
    try:
        await redis.enqueue_job(
            RENDER_VIDEO_JOB,
            job_id,
            _job_timeout=config.render_job_timeout_seconds,
        )
    finally:
        await redis.close()
