import asyncio
import logging
import shutil
from pathlib import Path


logger = logging.getLogger(__name__)

WIDTH = 1080
HEIGHT = 1920
VIDEO_FORMATS = {
    "9:16": (1080, 1920),
    "9:16_soft_zoom": (1080, 1920),
    "9:16_cover": (1080, 1920),
}
VIDEO_FORMATS_720 = {
    "9:16": (720, 1280),
    "9:16_soft_zoom": (720, 1280),
    "9:16_cover": (720, 1280),
}
VIDEO_BITRATES = {
    720: ("2200k", "2800k", "4400k"),
    1080: ("4000k", "5000k", "8000k"),
}
VIDEO_SPEEDS = {1.0, 1.10, 1.25, 1.50, 2.00}
SOFT_ZOOM_FACTOR = 1.12
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
    output_format: str = "9:16",
    fill_color: str = "black",
    video_speed: float = 1.0,
    mirror: bool = False,
    strip_metadata: bool = True,
) -> tuple[int, int]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    process = None
    output_width, output_height = await resolve_output_dimensions(input_path, output_format)
    video_bitrate, maxrate, bufsize = _video_bitrate(output_width)
    layout_scale = output_height / HEIGHT
    ad_top_margin = _scale(AD_TOP_MARGIN, layout_scale)
    ad_slot_height = _scale(AD_SLOT_HEIGHT, layout_scale)
    video_speed = _normalize_video_speed(video_speed)
    normalized_banner_path = (
        output_path.with_name(f"{output_path.stem}_banner.png") if ad_banner_path else None
    )

    try:
        if ad_banner_path and normalized_banner_path:
            await _normalize_banner(
                ad_banner_path,
                normalized_banner_path,
                output_width=output_width,
                ad_slot_height=ad_slot_height,
            )
            ad_banner_path = normalized_banner_path

        if output_format == "9:16_cover":
            base_filters = [
                f"scale={output_width}:{output_height}:force_original_aspect_ratio=increase",
                f"crop={output_width}:{output_height}:(iw-ow)/2:(ih-oh)/2",
                "fps=30",
                "setsar=1",
            ]
        elif output_format == "9:16_soft_zoom":
            base_filters = [
                f"scale={output_width}:{output_height}:force_original_aspect_ratio=decrease",
                f"scale=trunc(iw*{SOFT_ZOOM_FACTOR}/2)*2:trunc(ih*{SOFT_ZOOM_FACTOR}/2)*2",
                f"crop=min(iw\\,{output_width}):min(ih\\,{output_height}):(iw-ow)/2:(ih-oh)/2",
                f"pad={output_width}:{output_height}:(ow-iw)/2:(oh-ih)/2:{fill_color}",
                "fps=30",
                "setsar=1",
            ]
        else:
            base_filters = [
                f"scale={output_width}:{output_height}:force_original_aspect_ratio=decrease",
                f"pad={output_width}:{output_height}:(ow-iw)/2:(oh-ih)/2:{fill_color}",
                "fps=30",
                "setsar=1",
            ]
        if mirror:
            base_filters.append("hflip")

        if ad_banner_path:
            filter_parts = [
                f"[0:v]{','.join(base_filters)}[base]",
                "[1:v]format=rgba,colorchannelmixer=aa=0.95[banner]",
                (
                    f"[base][banner]overlay=x=0:y={ad_top_margin}:"
                    "eof_action=repeat:shortest=1[with_ad]"
                ),
            ]
            current_label = "with_ad"
            if subtitles_path:
                filter_parts.append(f"[{current_label}]{_ass_subtitles_filter(subtitles_path)}[with_subtitles]")
                current_label = "with_subtitles"

            filter_parts.append(_finish_video_filter(current_label, video_speed))
            filter_complex = ";".join(filter_parts)
        elif ad_text is not None:
            ad_text_ass_path = await _prepare_ad_text_ass(
                ad_text,
                output_path,
                output_width=output_width,
                output_height=output_height,
            )
            video_filters = [
                *base_filters,
                _ass_subtitles_filter(ad_text_ass_path),
            ]

            if subtitles_path:
                video_filters.append(_ass_subtitles_filter(subtitles_path))

            video_filters.append(_video_speed_filter(video_speed))

            filter_complex = (
                f"[0:v]"
                + ",".join(video_filters)
                + "[v]"
            )
        else:
            video_filters = list(base_filters)

            if subtitles_path:
                video_filters.append(_ass_subtitles_filter(subtitles_path))

            video_filters.append(_video_speed_filter(video_speed))

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
                "-b:v",
                video_bitrate,
                "-maxrate",
                maxrate,
                "-bufsize",
                bufsize,
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
                _aspect_ratio(output_format),
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

        if video_speed != 1.0:
            output_arg = cmd.pop()
            cmd.extend(["-filter:a", f"atempo={_format_speed(video_speed)}", output_arg])

        if strip_metadata:
            output_arg = cmd.pop()
            cmd.extend(
                [
                    "-map_metadata",
                    "-1",
                    "-map_chapters",
                    "-1",
                    "-metadata",
                    "encoder=",
                    "-metadata",
                    "comment=",
                    output_arg,
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
        return output_width, output_height
    finally:
        if ad_text is not None:
            output_path.with_name(f"{output_path.stem}_ad.ass").unlink(missing_ok=True)
        if normalized_banner_path:
            normalized_banner_path.unlink(missing_ok=True)


async def resolve_output_dimensions(input_path: Path, output_format: str = "9:16") -> tuple[int, int]:
    input_width, input_height = await _probe_video_dimensions(input_path)
    max_source_edge = max(input_width, input_height)
    if max_source_edge <= 1280:
        return VIDEO_FORMATS_720.get(output_format, (720, 1280))

    return VIDEO_FORMATS.get(output_format, (WIDTH, HEIGHT))


async def probe_video_duration(input_path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(input_path),
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise VideoProcessingError(
            f"Could not read video duration: {stderr.decode('utf-8', errors='replace')}"
        )

    try:
        return float(stdout.decode("utf-8", errors="replace").strip())
    except ValueError as exc:
        raise VideoProcessingError("Could not parse video duration") from exc


async def _probe_video_dimensions(input_path: Path) -> tuple[int, int]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=s=x:p=0",
        str(input_path),
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        logger.warning("ffprobe failed, using 1080 output. Stderr: %s", stderr.decode("utf-8", errors="replace"))
        return WIDTH, HEIGHT

    raw_dimensions = stdout.decode("utf-8", errors="replace").strip()
    try:
        raw_dimensions = raw_dimensions.splitlines()[0]
        width, height = raw_dimensions.split("x", maxsplit=1)
        return int(width), int(height)
    except (ValueError, IndexError):
        logger.warning("Could not parse ffprobe dimensions %r, using 1080 output", raw_dimensions)
        return WIDTH, HEIGHT


async def _normalize_banner(
    input_path: Path,
    output_path: Path,
    output_width: int = WIDTH,
    ad_slot_height: int = AD_SLOT_HEIGHT,
) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-i",
        str(input_path),
        "-vf",
        (
            "format=rgba,"
            f"scale={output_width}:{ad_slot_height}:force_original_aspect_ratio=decrease,"
            f"pad={output_width}:{ad_slot_height}:(ow-iw)/2:(oh-ih)/2:color=black@0,"
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


def _aspect_ratio(output_format: str) -> str:
    if output_format in {"9:16_soft_zoom", "9:16_cover"}:
        return "9:16"

    return output_format


def _normalize_video_speed(video_speed: float) -> float:
    rounded_speed = round(video_speed, 2)
    if rounded_speed not in VIDEO_SPEEDS:
        raise VideoProcessingError(f"Unsupported video speed: {video_speed}")

    return rounded_speed


def _video_speed_filter(video_speed: float) -> str:
    if video_speed == 1.0:
        return "null"

    return f"setpts=PTS/{_format_speed(video_speed)}"


def _finish_video_filter(input_label: str, video_speed: float) -> str:
    return f"[{input_label}]{_video_speed_filter(video_speed)}[v]"


def _format_speed(video_speed: float) -> str:
    return f"{video_speed:.2f}".rstrip("0").rstrip(".")


def _ass_subtitles_filter(subtitles_path: Path) -> str:
    return f"subtitles=filename='{_escape_drawtext(str(subtitles_path))}'"


async def _prepare_ad_text_ass(
    ad_text: str,
    output_path: Path,
    output_width: int = WIDTH,
    output_height: int = HEIGHT,
) -> Path:
    from app.subtitles import write_ass_ad_text

    ad_text_ass_path = output_path.with_name(f"{output_path.stem}_ad.ass")
    write_ass_ad_text(ad_text, ad_text_ass_path, width=output_width, height=output_height)
    return ad_text_ass_path


def _video_bitrate(output_width: int) -> tuple[str, str, str]:
    return VIDEO_BITRATES[720 if output_width <= 720 else 1080]


def _scale(value: int, factor: float) -> int:
    return max(1, round(value * factor))
