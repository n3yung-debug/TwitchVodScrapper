"""Burned-in captions from Whisper word timestamps.

ASS rather than SRT because the styling matters: short-form captions need a
heavy outline to stay readable over gameplay, and a margin that keeps them
clear of the platform's own UI overlay at the bottom of the screen.

``PlayResX``/``PlayResY`` are written to match the output frame so a font size
means the same thing in the 9:16 render as in the 16:9 one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import CaptionStyle
from ..detect.transcript import Word

_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{size},{primary},{primary},{outline_colour},&H64000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


@dataclass
class CaptionLine:
    start: float
    end: float
    text: str


def _timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    whole = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis == 100:  # rounding can tip a whole second
        centis = 0
        whole += 1
    return f"{hours}:{minutes:02d}:{whole:02d}.{centis:02d}"


def _escape(text: str) -> str:
    # Braces open override blocks in ASS; a stray one swallows the line.
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def group_words(
    words: list[Word],
    max_chars: int,
    max_gap: float = 0.8,
) -> list[CaptionLine]:
    """Group words into short caption lines.

    Lines break on length, and also on a pause: a gap longer than ``max_gap``
    means the previous line should clear rather than hang on screen while
    nothing is being said.
    """
    lines: list[CaptionLine] = []
    current: list[Word] = []

    def flush() -> None:
        if not current:
            return
        text = " ".join(w.text for w in current).strip()
        if text:
            lines.append(CaptionLine(current[0].start, current[-1].end, text))
        current.clear()

    for word in words:
        if not word.text:
            continue
        if current:
            gap = word.start - current[-1].end
            candidate_len = sum(len(w.text) + 1 for w in current) + len(word.text)
            if gap > max_gap or candidate_len > max_chars:
                flush()
        current.append(word)
    flush()
    return lines


def build_ass(
    words: list[Word],
    clip_start: float,
    clip_end: float,
    width: int,
    height: int,
    style: CaptionStyle,
) -> str:
    """Render an ASS subtitle file for one clip.

    Word timestamps arrive on the recording timeline; they are rebased to the
    clip so the subtitle file stands alone.
    """
    inside = [
        Word(start=w.start - clip_start, end=w.end - clip_start, text=w.text)
        for w in words
        if w.end > clip_start and w.start < clip_end
    ]
    duration = clip_end - clip_start
    for word in inside:
        word.start = max(0.0, word.start)
        word.end = min(duration, max(word.start + 0.05, word.end))

    header = _HEADER.format(
        width=width,
        height=height,
        font=style.font,
        size=style.font_size,
        primary=style.primary_color,
        outline_colour=style.outline_color,
        outline=style.outline,
        shadow=style.shadow,
        margin_v=style.margin_v,
    )

    events = []
    for line in group_words(inside, style.max_chars_per_line):
        text = line.text.upper() if style.uppercase else line.text
        events.append(
            f"Dialogue: 0,{_timestamp(line.start)},{_timestamp(line.end)},"
            f"Caption,,0,0,0,,{_escape(text)}"
        )
    return header + "\n".join(events) + "\n"


def write_ass(
    path: str | Path,
    words: list[Word],
    clip_start: float,
    clip_end: float,
    width: int,
    height: int,
    style: CaptionStyle,
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        build_ass(words, clip_start, clip_end, width, height, style),
        encoding="utf-8",
    )
    return target
