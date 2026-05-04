import asyncio
import logging
import shutil
import textwrap
from pathlib import Path


logger = logging.getLogger(__name__)

WIDTH = 1080
HEIGHT = 1920
PROCESS_TIMEOUT_SECONDS = 300
FONT_FILE = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

DEFAULT_AD_TEXT = "Реклама: @example"
AD_TOP_MARGIN = 90
AD_SLOT_HEIGHT = 190
SUBTITLE_BOTTOM_SAFE = 330
SUBTITLE_BOX_HEIGHT = 170
SUBTITLE_SIDE_SAFE = 160
SUBTITLE_FONT_SIZE = 48
SUBTITLE_LINE_SPACING = 10
MAX_AD_TEXT_CHARS = 120
MAX_AD_TEXT_LINES = 2
AD_TEXT_LINE_WIDTH = 28
MAX_SUBTITLE_CHARS = 160
MAX_SUBTITLE_LINES = 2
SUBTITLE_LINE_WIDTH = 30


class FFmpegNotFoundError(RuntimeError):
    pass


class VideoProcessingError(RuntimeError):
    pass


def ensure_ffmpeg_available() -> None:
    if not shutil.which("ffmpeg"):
        raise FFmpegNotFoundError(
            "FFmpeg was not found. Install FFmpeg locally or run the bot through Docker."
        )


async def process_video(
    input_path: Path,
    output_path: Path,
    ad_text: str = DEFAULT_AD_TEXT,
    ad_banner_path: Path | None = None,
    subtitles_path: Path | None = None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    process = None
    ad_text_path = output_path.with_name(f"{output_path.stem}_ad.txt")
    ad_text_path.write_text(_prepare_ad_text(ad_text), encoding="utf-8")

    try:
        base_filters = [
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease",
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:black",
            "fps=30",
            "setsar=1",
        ]

        if ad_banner_path:
            final_ad_label = "with_ad" if subtitles_path else "v"

            filter_complex = ";".join(
                [
                    f"[0:v]{','.join(base_filters)}[base]",
                    (
                        f"[1:v]scale={WIDTH}:{AD_SLOT_HEIGHT}:"
                        "force_original_aspect_ratio=decrease,"
                        f"pad={WIDTH}:{AD_SLOT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
                        "format=rgba,colorchannelmixer=aa=0.95[banner]"
                    ),
                    f"[base][banner]overlay=x=0:y={AD_TOP_MARGIN}[{final_ad_label}]",
                ]
            )
            if subtitles_path:
                filter_complex += f";[with_ad]{_ass_subtitles_filter(subtitles_path)}[v]"
        else:
            video_filters = [
                *base_filters,
                (
                    "drawbox="
                    f"x=0:y={AD_TOP_MARGIN}:"
                    f"w=iw:h={AD_SLOT_HEIGHT}:color=black@0.55:t=fill"
                ),
                _drawtext(
                    textfile=ad_text_path,
                    y=str(AD_TOP_MARGIN + 46),
                    fontsize=54,
                    line_spacing=10,
                ),
            ]

            if subtitles_path:
                video_filters.append(_ass_subtitles_filter(subtitles_path))

            filter_complex = (
                f"[0:v]"
                + ",".join(video_filters)
                + "[v]"
            )

        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-i",
            str(input_path),
        ]

        if ad_banner_path:
            cmd.extend(["-loop", "1", "-i", str(ad_banner_path)])

        cmd.extend(
            [
                "-filter_complex",
                filter_complex,
                "-map",
                "[v]",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "26",
                "-profile:v",
                "main",
                "-level",
                "4.0",
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
        )

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
    finally:
        ad_text_path.unlink(missing_ok=True)


def _drawtext(
    textfile: Path,
    y: str,
    fontsize: int,
    line_spacing: int = 0,
) -> str:
    return (
        "drawtext="
        f"fontfile='{FONT_FILE}':"
        f"fontcolor=white:"
        f"fontsize={fontsize}:"
        f"textfile='{_escape_drawtext(str(textfile))}':"
        f"line_spacing={line_spacing}:"
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


def _prepare_ad_text(text: str) -> str:
    normalized = " ".join(text.split()) or DEFAULT_AD_TEXT
    normalized = normalized[:MAX_AD_TEXT_CHARS].strip()

    lines = textwrap.wrap(
        normalized,
        width=AD_TEXT_LINE_WIDTH,
        max_lines=MAX_AD_TEXT_LINES,
        placeholder="...",
    )
    return "\n".join(lines)


def _ass_subtitles_filter(subtitles_path: Path) -> str:
    return f"subtitles=filename='{_escape_drawtext(str(subtitles_path))}'"
