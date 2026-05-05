import asyncio
import logging
import shutil
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
    ad_text: str | None = DEFAULT_AD_TEXT,
    ad_banner_path: Path | None = None,
    subtitles_path: Path | None = None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    process = None
    normalized_banner_path = (
        output_path.with_name(f"{output_path.stem}_banner.png") if ad_banner_path else None
    )

    try:
        if ad_banner_path and normalized_banner_path:
            await _normalize_banner(ad_banner_path, normalized_banner_path)
            ad_banner_path = normalized_banner_path

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
                    "[1:v]format=rgba,colorchannelmixer=aa=0.95[banner]",
                    (
                        f"[base][banner]overlay=x=0:y={AD_TOP_MARGIN}:"
                        f"eof_action=repeat:shortest=1[{final_ad_label}]"
                    ),
                ]
            )
            if subtitles_path:
                filter_complex += f";[with_ad]{_ass_subtitles_filter(subtitles_path)}[v]"
        elif ad_text is not None:
            ad_text_ass_path = await _prepare_ad_text_ass(ad_text, output_path)
            video_filters = [
                *base_filters,
                _ass_subtitles_filter(ad_text_ass_path),
            ]

            if subtitles_path:
                video_filters.append(_ass_subtitles_filter(subtitles_path))

            filter_complex = (
                f"[0:v]"
                + ",".join(video_filters)
                + "[v]"
            )
        else:
            video_filters = list(base_filters)

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
            cmd.extend(["-loop", "1", "-framerate", "30", "-i", str(ad_banner_path)])

        cmd.extend(
            [
                "-filter_threads",
                "1",
                "-filter_complex_threads",
                "1",
                "-filter_complex",
                filter_complex,
                "-map",
                "[v]",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "26",
                "-threads",
                "2",
                "-x264-params",
                "threads=2:lookahead_threads=1",
                "-profile:v",
                "main",
                "-level",
                "4.0",
                "-pix_fmt",
                "yuv420p",
                "-aspect",
                "9:16",
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
        if ad_text is not None:
            output_path.with_name(f"{output_path.stem}_ad.ass").unlink(missing_ok=True)
        if normalized_banner_path:
            normalized_banner_path.unlink(missing_ok=True)


async def _normalize_banner(input_path: Path, output_path: Path) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-i",
        str(input_path),
        "-vf",
        (
            "format=rgba,"
            f"scale={WIDTH}:{AD_SLOT_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={WIDTH}:{AD_SLOT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black@0,"
            "format=rgba"
        ),
        "-frames:v",
        "1",
        str(output_path),
    ]

    logger.info("Normalizing ad banner: input=%s output=%s", input_path, output_path)

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    if stdout:
        logger.debug("Banner normalize stdout:\n%s", stdout.decode("utf-8", errors="replace"))

    if process.returncode != 0:
        logger.error(
            "Banner normalize failed with code %s. Stderr:\n%s",
            process.returncode,
            stderr.decode("utf-8", errors="replace"),
        )
        raise VideoProcessingError("Failed to prepare ad banner")


def _escape_drawtext(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )


def _ass_subtitles_filter(subtitles_path: Path) -> str:
    return f"subtitles=filename='{_escape_drawtext(str(subtitles_path))}'"


async def _prepare_ad_text_ass(ad_text: str, output_path: Path) -> Path:
    from app.subtitles import write_ass_ad_text

    ad_text_ass_path = output_path.with_name(f"{output_path.stem}_ad.ass")
    write_ass_ad_text(ad_text, ad_text_ass_path)
    return ad_text_ass_path
