import asyncio
import logging
import shutil
from pathlib import Path


logger = logging.getLogger(__name__)

WIDTH = 720
HEIGHT = 1280
PROCESS_TIMEOUT_SECONDS = 300
FONT_FILE = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

TOP_TEXT = "Смотри до конца"
BOTTOM_TEXT = "Реклама: @example"


class FFmpegNotFoundError(RuntimeError):
    pass


class VideoProcessingError(RuntimeError):
    pass


def ensure_ffmpeg_available() -> None:
    if not shutil.which("ffmpeg"):
        raise FFmpegNotFoundError(
            "FFmpeg was not found. Install FFmpeg locally or run the bot through Docker."
        )


async def process_video(input_path: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    process = None

    vf = ",".join(
        [
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease",
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:black",
            "fps=30",
            "setsar=1",
            "drawbox=x=0:y=50:w=iw:h=105:color=black@0.55:t=fill",
            _drawtext(
                text=TOP_TEXT,
                y="78",
                fontsize=42,
            ),
            "drawbox=x=0:y=ih-155:w=iw:h=105:color=black@0.55:t=fill",
            _drawtext(
                text=BOTTOM_TEXT,
                y="h-127",
                fontsize=38,
            ),
        ]
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "26",
        "-profile:v",
        "main",
        "-level",
        "3.1",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "44100",
        "-movflags",
        "+faststart",
        str(output_path),
    ]

    logger.info("Starting FFmpeg processing: input=%s output=%s", input_path, output_path)
    logger.debug("FFmpeg command: %s", " ".join(cmd))

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=PROCESS_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        if process and process.returncode is None:
            process.kill()
            await process.communicate()
        logger.exception("FFmpeg processing timed out after %s seconds", PROCESS_TIMEOUT_SECONDS)
        raise VideoProcessingError("FFmpeg processing timed out") from exc

    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()

    if stdout_text:
        logger.debug("FFmpeg stdout:\n%s", stdout_text)

    if process.returncode != 0:
        logger.error("FFmpeg failed with code %s. Stderr:\n%s", process.returncode, stderr_text)
        raise VideoProcessingError("FFmpeg failed to process video")

    if stderr_text:
        logger.info("FFmpeg stderr:\n%s", stderr_text)

    logger.info("FFmpeg processing finished: %s", output_path)


def _drawtext(text: str, y: str, fontsize: int) -> str:
    escaped = _escape_drawtext(text)
    return (
        "drawtext="
        f"fontfile='{FONT_FILE}':"
        f"fontcolor=white:"
        f"fontsize={fontsize}:"
        f"text='{escaped}':"
        "x=(w-text_w)/2:"
        f"y={y}"
    )


def _escape_drawtext(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )
