from __future__ import annotations

import logging

from arq.connections import RedisSettings

from app.config import load_config
from app.queue import redis_settings

logger = logging.getLogger(__name__)


async def render_video(ctx: dict, job_id: str) -> None:
    # The next implementation step will move the current in-process montage flow here.
    logger.info("Received render job: job_id=%s", job_id)


async def startup(ctx: dict) -> None:
    config = load_config()
    ctx["config"] = config
    config.tmp_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Worker started")


async def shutdown(ctx: dict) -> None:
    logger.info("Worker stopped")


class WorkerSettings:
    config = load_config()
    redis_settings: RedisSettings = redis_settings(config)
    functions = [render_video]
    on_startup = startup
    on_shutdown = shutdown
    job_timeout = config.render_job_timeout_seconds
    max_jobs = config.max_concurrent_renders


if __name__ == "__main__":
    from arq.worker import run_worker

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_worker(WorkerSettings)
