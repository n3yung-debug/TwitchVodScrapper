"""Native Twitch clips from reviewed candidates.

This is the other half of the output story. A rendered file is what gets
uploaded to YouTube or TikTok; a native Twitch clip is what lives on the
channel, shows up in Twitch's own discovery surfaces, and can be dropped
into a Discord message as a link. Both come from the same reviewed bounds,
so the decision made once in the review UI drives both.

Two things make this more than a thin API wrapper, and both are handled
here rather than at the call site:

* **Timeline translation.** Everything upstream speaks in seconds from the
  start of the *local recording*. The clip API addresses the *VOD*. Those
  two clocks start at different moments -- Nick may hit record before going
  live, or go live before hitting record -- so every candidate has to be
  converted through wall clock, and some of them legitimately fall outside
  the VOD entirely.
* **Twitch's clip length rules.** Clips are 5-60 seconds. A 3-second reaction
  and a 90-second story both have to be reshaped to fit, and silently
  producing a clip with different bounds than the ones reviewed is exactly
  the kind of error that is invisible until you watch the result. Every
  adjustment is recorded on the plan and printed before anything is created.

Planning is pure and separated from creation so the reshaping rules can be
tested without touching the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Iterator

from .config import TwitchConfig
from .ingest.twitch import TwitchClient, TwitchError
from .timing import recording_offset_to_vod_offset

# Twitch's own bounds for a clip. The upper bound is also configurable
# downwards (twitch.max_native_clip_seconds) but never upwards.
TWITCH_MIN_CLIP = 5.0
TWITCH_MAX_CLIP = 60.0


@dataclass
class ClipPlan:
    """One intended native clip, already reshaped to Twitch's rules."""

    index: int
    title: str
    start: float           # recording offset, after reshaping
    end: float             # recording offset, after reshaping
    vod_offset: float      # where the clip should BEGIN inside the VOD
    notes: list[str] = field(default_factory=list)
    skip_reason: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def usable(self) -> bool:
        return not self.skip_reason


@dataclass
class ClipResult:
    plan: ClipPlan
    clip_id: str = ""
    edit_url: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.clip_id) and not self.error

    @property
    def url(self) -> str:
        return f"https://clips.twitch.tv/{self.clip_id}" if self.clip_id else ""


def select_candidates(
    candidates: list[dict[str, Any]],
    *,
    approved_only: bool = True,
    top: int | None = None,
    indices: Iterable[int] | None = None,
    include_clipped: bool = False,
) -> list[int]:
    """Decide which candidate indices to clip.

    Defaults to the ones Nick actually approved in the review UI, because
    creating clips from unreviewed detections would publish detector noise
    to the channel.
    """
    if indices is not None:
        chosen = [i for i in indices if 0 <= i < len(candidates)]
    else:
        chosen = [
            i for i, c in enumerate(candidates)
            if not approved_only or c.get("status") == "approved"
        ]
        if top is not None:
            chosen = chosen[:top]

    if not include_clipped:
        chosen = [i for i in chosen if not candidates[i].get("twitch_clip")]
    return chosen


def plan_clip(
    index: int,
    candidate: dict[str, Any],
    *,
    recording_start_utc: datetime,
    vod_start_utc: datetime,
    vod_duration: float,
    max_seconds: float = TWITCH_MAX_CLIP,
) -> ClipPlan:
    """Reshape one candidate into something Twitch will accept.

    Returns a plan even when the candidate cannot be clipped, with
    ``skip_reason`` set -- a skipped moment should be reported, not silently
    dropped from the run.
    """
    max_seconds = max(TWITCH_MIN_CLIP, min(TWITCH_MAX_CLIP, float(max_seconds)))
    start = float(candidate["start"])
    end = float(candidate["end"])
    title = candidate.get("title") or f"candidate {index}"
    notes: list[str] = []

    # -- length rules, in recording time ---------------------------------
    if end - start > max_seconds:
        notes.append(
            f"trimmed {end - start:.0f}s -> {max_seconds:.0f}s "
            "(Twitch caps native clips at 60s; the local render keeps full length)"
        )
        end = start + max_seconds
    elif end - start < TWITCH_MIN_CLIP:
        middle = (start + end) / 2.0
        notes.append(f"padded {end - start:.1f}s -> {TWITCH_MIN_CLIP:.0f}s (Twitch minimum)")
        start = max(0.0, middle - TWITCH_MIN_CLIP / 2.0)
        end = start + TWITCH_MIN_CLIP

    # -- translate onto the VOD's clock ----------------------------------
    offset = recording_offset_to_vod_offset(start, vod_start_utc, recording_start_utc)
    end_offset = offset + (end - start)

    if end_offset <= 0:
        return ClipPlan(
            index, title, start, end, offset, notes,
            skip_reason=(
                f"falls {abs(end_offset):.0f}s before the VOD starts -- the "
                "recording was running before the stream went live"
            ),
        )
    if offset < 0:
        notes.append(
            f"start clamped to the VOD's first frame (was {offset:.0f}s before it)"
        )
        start += -offset
        offset = 0.0
        if end - start < TWITCH_MIN_CLIP:
            end = start + TWITCH_MIN_CLIP

    if vod_duration > 0:
        if offset >= vod_duration:
            return ClipPlan(
                index, title, start, end, offset, notes,
                skip_reason=(
                    f"starts {offset - vod_duration:.0f}s past the end of the VOD -- "
                    "the recording outlasted the stream"
                ),
            )
        if offset + (end - start) > vod_duration:
            overshoot = offset + (end - start) - vod_duration
            if (end - start) - overshoot < TWITCH_MIN_CLIP:
                return ClipPlan(
                    index, title, start, end, offset, notes,
                    skip_reason="too close to the end of the VOD to fit a 5s clip",
                )
            notes.append(f"trimmed {overshoot:.0f}s to stay inside the VOD")
            end -= overshoot

    return ClipPlan(index, title, start, end, offset, notes)


def plan_clips(
    session: dict[str, Any],
    twitch: TwitchConfig,
    indices: Iterable[int],
    *,
    vod_start_utc: datetime,
    vod_duration: float,
) -> list[ClipPlan]:
    recording_start = datetime.fromisoformat(session["recording_start_utc"])
    candidates = session["candidates"]
    return [
        plan_clip(
            i, candidates[i],
            recording_start_utc=recording_start,
            vod_start_utc=vod_start_utc,
            vod_duration=vod_duration,
            max_seconds=twitch.max_native_clip_seconds,
        )
        for i in indices
    ]


def create_clips(
    client: TwitchClient,
    plans: list[ClipPlan],
    *,
    broadcaster_id: str,
    video_id: str,
    vod_offset_is_start: bool = True,
) -> Iterator[ClipResult]:
    """Create each planned clip, yielding results as they come back.

    Yields rather than returning a list so a long run prints progress, and
    so a failure partway through still reports everything created before it
    -- those clips exist on the channel whether or not the run finished.
    """
    for plan in plans:
        if not plan.usable:
            yield ClipResult(plan=plan, error=plan.skip_reason)
            continue
        try:
            created = client.create_clip_from_vod(
                broadcaster_id=broadcaster_id,
                video_id=video_id,
                start_offset=plan.vod_offset,
                duration=plan.duration,
                vod_offset_is_start=vod_offset_is_start,
            )
        except TwitchError as exc:
            yield ClipResult(plan=plan, error=str(exc))
            continue
        clip_id = created.get("id", "")
        yield ClipResult(
            plan=plan,
            clip_id=clip_id,
            edit_url=created.get("edit_url", ""),
            error="" if clip_id else f"Twitch returned no clip id: {created}",
        )


def record_result(candidate: dict[str, Any], result: ClipResult) -> None:
    """Write a created clip back onto its candidate in the session file."""
    if not result.ok:
        return
    candidate["twitch_clip"] = {
        "id": result.clip_id,
        "url": result.url,
        "edit_url": result.edit_url,
        "vod_offset": round(result.plan.vod_offset, 2),
        "duration": round(result.plan.duration, 2),
        "notes": list(result.plan.notes),
    }


def format_plan(plans: list[ClipPlan]) -> str:
    """Human-readable preview of what a run would create."""
    lines: list[str] = []
    for plan in plans:
        stamp = f"{int(plan.start // 3600):d}:{int(plan.start % 3600 // 60):02d}:{int(plan.start % 60):02d}"
        if plan.usable:
            lines.append(
                f"  #{plan.index:<3d} {stamp}  {plan.duration:4.0f}s  "
                f"vod+{plan.vod_offset:7.0f}s  {plan.title[:48]}"
            )
        else:
            lines.append(f"  #{plan.index:<3d} {stamp}  SKIP  {plan.skip_reason}")
        for note in plan.notes:
            lines.append(f"        note: {note}")
    return "\n".join(lines)
