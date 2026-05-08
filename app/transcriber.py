from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path

from openai import AsyncOpenAI


logger = logging.getLogger(__name__)

AUDIO_BITRATE = "32k"
AUDIO_SAMPLE_RATE = "16000"
TRANSCRIPTION_MODEL = "whisper-1"
MAX_TRANSCRIPTION_ATTEMPTS = 2
TRANSCRIPTION_RETRY_PROMPT = (
    "Расшифруй только реально произнесенную речь из видео. "
    "Не добавляй служебные фразы вроде 'Продолжение следует', "
    "'Спасибо за просмотр', 'Субтитры сделал...' или похожие фразы, "
    "если их нет в аудио."
)
MAX_SUBTITLE_WORDS = 7
MAX_SUBTITLE_DURATION_SECONDS = 4.5
MIN_SUBTITLE_DURATION_SECONDS = 1.0
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

    for attempt in range(1, MAX_TRANSCRIPTION_ATTEMPTS + 1):
        response = await _request_transcription(
            client=client,
            audio_path=audio_path,
            prompt=TRANSCRIPTION_RETRY_PROMPT if attempt > 1 else None,
        )
        result = _subtitle_segments_from_response(response)
        if result:
            logger.info("Transcription finished: %s segments, attempt=%s", len(result), attempt)
            return result

        if attempt < MAX_TRANSCRIPTION_ATTEMPTS:
            logger.info("Transcription returned no usable segments, retrying: attempt=%s", attempt)

    logger.info("Transcription finished: 0 segments after %s attempts", MAX_TRANSCRIPTION_ATTEMPTS)
    return []


async def _request_transcription(client: AsyncOpenAI, audio_path: Path, prompt: str | None):
    request = {
        "model": TRANSCRIPTION_MODEL,
        "response_format": "verbose_json",
        "timestamp_granularities": ["word", "segment"],
    }
    if prompt:
        request["prompt"] = prompt

    with audio_path.open("rb") as audio_file:
        return await client.audio.transcriptions.create(file=audio_file, **request)


def _subtitle_segments_from_response(response) -> list[SubtitleSegment]:
    word_segments = _subtitle_segments_from_words(getattr(response, "words", None) or [])
    if word_segments:
        return word_segments

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

        result.extend(_split_segment(SubtitleSegment(start=float(start), end=float(end), text=text)))

    return result


def _subtitle_segments_from_words(words) -> list[SubtitleSegment]:
    result: list[SubtitleSegment] = []
    chunk: list[tuple[float, float, str]] = []

    for word in words:
        start = _segment_value(word, "start")
        end = _segment_value(word, "end")
        text = str(_segment_value(word, "word") or "").strip()

        if start is None or end is None or not text:
            continue

        chunk.append((float(start), float(end), text))
        chunk_duration = chunk[-1][1] - chunk[0][0]
        should_flush = (
            len(chunk) >= MAX_SUBTITLE_WORDS
            or chunk_duration >= MAX_SUBTITLE_DURATION_SECONDS
            or _ends_sentence(text)
        )
        if should_flush:
            _append_word_chunk(result, chunk)
            chunk = []

    if chunk:
        _append_word_chunk(result, chunk)

    return [segment for segment in result if not _is_likely_hallucination(segment.text)]


def _append_word_chunk(result: list[SubtitleSegment], chunk: list[tuple[float, float, str]]) -> None:
    if not chunk:
        return

    start = chunk[0][0]
    end = max(chunk[-1][1], start + MIN_SUBTITLE_DURATION_SECONDS)
    result.append(SubtitleSegment(start=start, end=end, text=_join_words(chunk)))


def _join_words(words: list[tuple[float, float, str]]) -> str:
    return " ".join(word for _, _, word in words).strip()


def _ends_sentence(text: str) -> bool:
    return text.rstrip().endswith((".", "!", "?", "…"))


def _split_segment(segment: SubtitleSegment) -> list[SubtitleSegment]:
    duration = segment.end - segment.start
    if duration <= MAX_SUBTITLE_DURATION_SECONDS:
        return [segment]

    words = segment.text.split()
    if len(words) <= MAX_SUBTITLE_WORDS:
        return [
            SubtitleSegment(
                start=segment.start,
                end=min(segment.end, segment.start + MAX_SUBTITLE_DURATION_SECONDS),
                text=segment.text,
            )
        ]

    chunks = [
        words[index : index + MAX_SUBTITLE_WORDS]
        for index in range(0, len(words), MAX_SUBTITLE_WORDS)
    ]
    result: list[SubtitleSegment] = []
    cursor = segment.start
    for chunk in chunks:
        end = min(segment.end, cursor + MAX_SUBTITLE_DURATION_SECONDS)
        if end <= cursor:
            break
        result.append(SubtitleSegment(start=cursor, end=end, text=" ".join(chunk)))
        cursor = end

    return result


def _segment_value(segment, key: str):
    if isinstance(segment, dict):
        return segment.get(key)
    return getattr(segment, key, None)


def _is_likely_hallucination(text: str) -> bool:
    normalized = " ".join(text.casefold().split())
    marker_groups = SUBTITLE_CREDIT_MARKERS + TRANSCRIPTION_HALLUCINATION_MARKERS
    return any(all(marker in normalized for marker in markers) for markers in marker_groups)
