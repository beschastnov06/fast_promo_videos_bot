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


def write_ass_subtitles(segments: list[SubtitleSegment], output_path: Path) -> None:
    output_path.write_text(_build_ass(segments), encoding="utf-8")


def _build_ass(segments: list[SubtitleSegment]) -> str:
    margin_v = BOTTOM_SAFE
    margin_lr = LEFT_RIGHT_SAFE

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {WIDTH}
PlayResY: {HEIGHT}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,{FONT_SIZE},&H00FFFFFF,&H000000FF,&HAA000000,&HAA000000,1,0,0,0,100,100,0,0,3,0,0,2,{margin_lr},{margin_lr},{margin_v},1

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


def _wrap_text(text: str) -> str:
    lines = textwrap.wrap(
        " ".join(text.split()),
        width=LINE_WIDTH,
        max_lines=MAX_LINES,
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
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
