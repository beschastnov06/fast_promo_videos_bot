from pathlib import Path
import textwrap

from app.transcriber import SubtitleSegment


WIDTH = 1080
HEIGHT = 1920
BOTTOM_SAFE = 330
LEFT_RIGHT_SAFE = 160
FONT_SIZE = 48
MAX_LINES = 2
LINE_WIDTH = 30

AD_TOP_MARGIN = 90
AD_SLOT_HEIGHT = 190
AD_FONT_SIZE = 54
AD_MAX_LINES = 2
AD_LINE_WIDTH = 28


def write_ass_subtitles(
    segments: list[SubtitleSegment],
    output_path: Path,
    font_name: str = "DejaVu Sans",
) -> None:
    output_path.write_text(_build_ass(segments, font_name=font_name), encoding="utf-8")


def write_ass_ad_text(text: str, output_path: Path) -> None:
    output_path.write_text(_build_ad_ass(text), encoding="utf-8")


def _build_ass(segments: list[SubtitleSegment], font_name: str) -> str:
    margin_v = BOTTOM_SAFE
    margin_lr = LEFT_RIGHT_SAFE
    font_name = _escape_ass(font_name)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {WIDTH}
PlayResY: {HEIGHT}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{FONT_SIZE},&H00FFFFFF,&H000000FF,&HAA000000,&HAA000000,1,0,0,0,100,100,0,0,3,0,0,2,{margin_lr},{margin_lr},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events = [
        (
            f"Dialogue: 0,{_format_time(segment.start)},{_format_time(segment.end)},"
            f"Default,,0,0,0,,{_escape_ass(_wrap_text(segment.text))}"
        )
        for segment in segments
        if segment.end > segment.start
    ]

    return header + "\n".join(events) + "\n"


def _build_ad_ass(text: str) -> str:
    margin_v = AD_TOP_MARGIN + 42
    text = _escape_ass(_wrap_ad_text(text))

    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {WIDTH}
PlayResY: {HEIGHT}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Ad,DejaVu Sans,{AD_FONT_SIZE},&H00FFFFFF,&H000000FF,&HAA000000,&HAA000000,0,0,0,0,100,100,0,0,4,0,0,8,120,120,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,9:59:59.00,Ad,,0,0,0,,{text}
"""


def _wrap_text(text: str) -> str:
    lines = textwrap.wrap(
        " ".join(text.split()),
        width=LINE_WIDTH,
        max_lines=MAX_LINES,
        placeholder="...",
    )
    return "\\N".join(lines)


def _wrap_ad_text(text: str) -> str:
    normalized = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    normalized = normalized or "Реклама: @example"
    if "\n" in normalized:
        lines = normalized.splitlines()[:AD_MAX_LINES]
        return "\\N".join(lines)

    lines = textwrap.wrap(
        " ".join(normalized.split()),
        width=AD_LINE_WIDTH,
        max_lines=AD_MAX_LINES,
        placeholder="...",
    )
    return "\\N".join(lines)


def _format_time(seconds: float) -> str:
    centiseconds = int(round(seconds * 100))
    hours, rest = divmod(centiseconds, 360000)
    minutes, rest = divmod(rest, 6000)
    secs, cs = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _escape_ass(text: str) -> str:
    return text.replace("{", "\\{").replace("}", "\\}")
