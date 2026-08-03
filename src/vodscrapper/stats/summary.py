"""Turning screen reads into a session summary.

Gold is reported as **endpoints plus a delta**, never as a single number.
Stash "value" in an extraction game is an estimated market price that drifts
on its own, and buying gear moves liquid into equipped items that may not
count toward the stash -- so a lone delta can be confidently wrong. Showing
both reads and the timestamps they came from makes an odd number visible
instead of authoritative.

The same instinct drives ``notes``: low-confidence reads, missing endpoints,
and skipped matches are stated rather than smoothed over.
"""

from __future__ import annotations

from pathlib import Path

from ..config import StatsConfig
from ..models import (
    Candidate,
    MatchResult,
    SessionSummary,
    Source,
    WealthReading,
)
from ..timing import clamp
from .ocr import FieldRead, OCREngine, read_screen
from .regions import RegionConfig
from .screens import ScreenHit, extract_frame

_LOW_CONFIDENCE = 0.6


def _mean_confidence(reads: dict[str, FieldRead]) -> float:
    values = [r.confidence for r in reads.values() if r.confidence > 0]
    return sum(values) / len(values) if values else 0.0


def collect(
    src: str | Path,
    hits: list[ScreenHit],
    config: RegionConfig,
    cfg: StatsConfig,
    work_dir: str | Path,
    ffmpeg: str = "ffmpeg",
) -> tuple[list[MatchResult], list[WealthReading], list[str]]:
    """OCR every detected screen. Returns (matches, wealth_reads, notes)."""
    engine = OCREngine(cfg)
    frames_dir = Path(work_dir) / "frames"
    matches: list[MatchResult] = []
    wealth: list[WealthReading] = []
    notes: list[str] = []

    for index, hit in enumerate(hits):
        screen = config.screens.get(hit.screen)
        if screen is None:
            continue

        frame_path = frames_dir / f"{hit.screen}-{index:04d}.png"
        try:
            extract_frame(src, hit.offset, frame_path, ffmpeg=ffmpeg)
            reads = read_screen(frame_path, screen, config, engine)
        except Exception as exc:
            notes.append(f"could not read {hit.screen} at {hit.offset:.0f}s: {exc}")
            continue

        confidence = _mean_confidence(reads)
        if confidence and confidence < _LOW_CONFIDENCE:
            notes.append(
                f"low OCR confidence ({confidence:.2f}) on {hit.screen} "
                f"at {hit.offset:.0f}s -- values may be wrong"
            )

        if hit.screen == "post_match":
            kills = reads.get("kills")
            survived = reads.get("survived")
            extra = {
                name: r.value
                for name, r in reads.items()
                if name not in {"kills", "survived"} and r.value is not None
            }
            matches.append(
                MatchResult(
                    at_offset=hit.offset,
                    kills=kills.value if kills and isinstance(kills.value, int) else None,
                    survived=survived.value if survived and isinstance(survived.value, bool) else None,
                    extra=extra,
                    confidence=confidence,
                    frame_path=str(frame_path),
                )
            )
        elif hit.screen == "stash":
            stash_value = reads.get("stash_value")
            liquid = reads.get("liquid")
            wealth.append(
                WealthReading(
                    at_offset=hit.offset,
                    stash_value=stash_value.value if stash_value and isinstance(stash_value.value, int) else None,
                    liquid=liquid.value if liquid and isinstance(liquid.value, int) else None,
                    confidence=confidence,
                    frame_path=str(frame_path),
                )
            )

    return matches, wealth, notes


def build_summary(
    matches: list[MatchResult],
    wealth: list[WealthReading],
    notes: list[str] | None = None,
) -> SessionSummary:
    summary = SessionSummary(notes=list(notes or []))
    summary.results = sorted(matches, key=lambda m: m.at_offset)
    summary.matches = len(summary.results)

    best_kills = -1
    for index, result in enumerate(summary.results, start=1):
        if result.survived is True:
            summary.extracted += 1
        elif result.survived is False:
            summary.died += 1
        if result.kills is not None:
            summary.total_kills += result.kills
            if result.kills > best_kills:
                best_kills = result.kills
                summary.best_match_index = index
                summary.best_match_kills = result.kills

    unknown = summary.matches - summary.extracted - summary.died
    if unknown:
        summary.notes.append(
            f"{unknown} match(es) had an unreadable outcome and are excluded "
            "from the extract/die counts"
        )

    usable = [w for w in sorted(wealth, key=lambda w: w.at_offset) if w.total is not None]
    if usable:
        summary.wealth_start = usable[0]
        if len(usable) > 1:
            summary.wealth_end = usable[-1]
        else:
            # Leaving wealth_end unset keeps the delta None. Pointing both
            # endpoints at the same reading would report a confident "+0",
            # which claims the session broke even when the truth is that we
            # only ever saw the stash once.
            summary.notes.append(
                "only one stash reading found, so no gold delta could be "
                "computed -- open the stash screen once early and once late"
            )
    else:
        summary.notes.append("no readable stash screen found; gold not tracked")

    return summary


def result_candidates(
    matches: list[MatchResult],
    duration: float,
    cfg: StatsConfig,
    max_score: float,
) -> list[Candidate]:
    """Turn strong match outcomes into clip candidates.

    This falls out of work already being done. Once match boundaries are
    known, a high kill count or a death carrying real value says the minute
    or so before the summary screen is probably worth watching -- so those
    moments surface even when no hotkey was pressed and chat was quiet.
    """
    if not cfg.result_signal_enabled:
        return []

    out: list[Candidate] = []
    for index, result in enumerate(matches, start=1):
        kills = result.kills or 0
        notable_kills = kills >= cfg.result_kill_threshold
        # A death is only interesting if something happened first; an early
        # empty-handed death is not a clip.
        notable_death = result.survived is False and kills >= max(1, cfg.result_kill_threshold - 2)
        if not (notable_kills or notable_death):
            continue

        # Scale with kills, but saturate so one huge match cannot dominate.
        strength = min(1.0, kills / max(1, cfg.result_kill_threshold * 2))
        score = max_score * (0.5 + 0.5 * strength)

        end = clamp(result.at_offset, 0.0, duration)
        start = clamp(end - cfg.result_signal_pre, 0.0, duration)
        if end - start < 5.0:
            continue

        out.append(
            Candidate(
                start=start,
                end=end,
                score=score,
                sources=[Source.MATCH_RESULT],
                evidence={
                    "kills": result.kills,
                    "survived": result.survived,
                    "match_index": index,
                    "spike_at": round(end - 10.0, 1),
                },
                match_index=index,
            )
        )
    return out


def format_summary(summary: SessionSummary) -> str:
    """Human-readable session recap."""
    lines: list[str] = []
    rate = summary.extract_rate
    lines.append(
        f"Matches: {summary.matches}   ·   Extracted: {summary.extracted}"
        + (f" ({rate:.0%})" if rate is not None else "")
        + f"   ·   Died: {summary.died}"
    )
    avg = summary.avg_kills
    best = (
        f", best {summary.best_match_kills} (match #{summary.best_match_index})"
        if summary.best_match_kills is not None
        else ""
    )
    lines.append(
        f"Kills: {summary.total_kills}"
        + (f" ({avg:.1f} avg{best})" if avg is not None else "")
    )

    start, end = summary.wealth_start, summary.wealth_end
    if start and end:
        lines.append(
            f"Gold start: {start.total:,} "
            f"(stash {start.stash_value or 0:,} + liquid {start.liquid or 0:,}) "
            f"at {start.at_offset / 60:.0f}m"
        )
        lines.append(
            f"Gold end:   {end.total:,} "
            f"(stash {end.stash_value or 0:,} + liquid {end.liquid or 0:,}) "
            f"at {end.at_offset / 60:.0f}m"
        )
        delta = summary.wealth_delta
        if delta is not None:
            lines.append(f"Net change: {delta:+,}")
    elif start:
        lines.append(
            f"Gold seen once: {start.total:,} "
            f"(stash {start.stash_value or 0:,} + liquid {start.liquid or 0:,}) "
            f"at {start.at_offset / 60:.0f}m -- no second reading, so no delta"
        )
    for note in summary.notes:
        lines.append(f"note: {note}")
    return "\n".join(lines)
