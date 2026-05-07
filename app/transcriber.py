import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path

from openai import AsyncOpenAI


logger = logging.getLogger(__name__)

AUDIO_BITRATE = "32k"
AUDIO_SAMPLE_RATE = "16000"
TRANSCRIPTION_MODEL = "whisper-1"
SUBTITLE_CREDIT_MARKERS = (
    ("редактор", "субтитр"),
    ("корректор",),
    ("субтитры", "сделал"),
    ("субтитры", "делал"),
    ("субтитры", "автор"),
    ("subtitles", "by"),
    ("caption", "by"),
)
TRANSCRIPTION_HALLUCINATION_MARKERS = (
    ("продолжение", "следует"),
    ("спасибо", "за", "просмотр"),
    ("спасибо", "что", "посмотрели"),
    ("подписывайтесь", "канал"),
    ("ставьте", "лайки"),
    ("thanks", "watching"),
    ("to", "be", "continued"),
)


@dataclass(frozen=True)
class SubtitleSegment:
    start: float
    end: float
    text: str


class TranscriptionError(RuntimeError):
    pass


async def extract_audio(input_path: Path, output_path: Path) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-i",
        str(input_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        AUDIO_SAMPLE_RATE,
        "-b:a",
        AUDIO_BITRATE,
        str(output_path),
    ]

    logger.info("Extracting audio for transcription: input=%s output=%s", input_path, output_path)

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    if stdout:
        logger.debug("Audio extract stdout:\n%s", stdout.decode("utf-8", errors="replace"))

    if process.returncode != 0:
        logger.error(
            "Audio extraction failed with code %s. Stderr:\n%s",
            process.returncode,
            stderr.decode("utf-8", errors="replace"),
        )
        raise TranscriptionError("Failed to extract audio from video")


async def transcribe_audio(audio_path: Path, api_key: str) -> list[SubtitleSegment]:
    client = AsyncOpenAI(api_key=api_key)

    logger.info("Starting transcription: %s", audio_path)

    with audio_path.open("rb") as audio_file:
        response = await client.audio.transcriptions.create(
            model=TRANSCRIPTION_MODEL,
            file=audio_file,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
        )

    segments = getattr(response, "segments", None) or []
    result: list[SubtitleSegment] = []

    for segment in segments:
        start = _segment_value(segment, "start")
        end = _segment_value(segment, "end")
        text = str(_segment_value(segment, "text") or "").strip()

        if start is None or end is None or not text:
            continue

        if _is_likely_hallucination(text):
            logger.info("Filtered likely transcription hallucination: %s", text)
            continue

        result.append(
            SubtitleSegment(
                start=float(start),
                end=float(end),
                text=text,
            )
        )

    logger.info("Transcription finished: %s segments", len(result))
    return result


def _segment_value(segment, key: str):
    if isinstance(segment, dict):
        return segment.get(key)
    return getattr(segment, key, None)


def _is_likely_hallucination(text: str) -> bool:
    normalized = " ".join(text.casefold().split())
    marker_groups = SUBTITLE_CREDIT_MARKERS + TRANSCRIPTION_HALLUCINATION_MARKERS
    return any(all(marker in normalized for marker in markers) for markers in marker_groups)
