"""Mapping every input onto the recording's timeline.

This module is small and heavily tested on purpose: an error of a few
seconds here silently mis-cuts every clip in a session, and the mistake is
invisible until you watch the output. Everything resolves to *seconds from
the start of the local recording file*.

Why the local recording and not the VOD: the recording and the marker
daemon run on the same machine off the same clock, so the mapping is exact
subtraction. Twitch VOD offsets drift relative to wall clock across an
8-hour stream (ingest lag, reconnects, dropped frames), so they are only
used for chat, and only after being anchored through wall clock.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from .models import Marker, MarkerKind

# OBS / Streamlabs default recording filename format is
# "%CCYY-%MM-%DD %hh-%mm-%ss", e.g. "2026-08-03 19-45-12.mkv".
# Critically this is LOCAL time, not UTC -- treating it as UTC is an
# off-by-hours bug that looks like the markers are wildly wrong.
_FILENAME_RE = re.compile(
    r"(?P<Y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})[ _](?P<H>\d{2})-(?P<M>\d{2})-(?P<S>\d{2})"
)


class TimingError(ValueError):
    """Raised when a timestamp cannot be resolved onto the recording timeline."""


def parse_recording_start(name: str, tz: timezone | None = None) -> datetime:
    """Extract the recording start time from an OBS/Streamlabs filename.

    ``tz`` defaults to the system local timezone, matching how OBS names
    files. Returns an aware UTC datetime.
    """
    match = _FILENAME_RE.search(name)
    if not match:
        raise TimingError(
            f"no OBS-style timestamp in filename {name!r}; "
            "expected something like '2026-08-03 19-45-12.mkv'"
        )
    parts = {k: int(v) for k, v in match.groupdict().items()}
    naive = datetime(parts["Y"], parts["m"], parts["d"], parts["H"], parts["M"], parts["S"])
    if tz is None:
        # astimezone() on a naive datetime attaches the system local zone.
        return naive.astimezone().astimezone(timezone.utc)
    return naive.replace(tzinfo=tz).astimezone(timezone.utc)


def offset_from_utc(at_utc: datetime, recording_start_utc: datetime) -> float:
    """Seconds from the start of the recording for an absolute timestamp."""
    return (at_utc.astimezone(timezone.utc) - recording_start_utc.astimezone(timezone.utc)).total_seconds()


def vod_offset_to_recording_offset(
    vod_offset: float,
    vod_start_utc: datetime,
    recording_start_utc: datetime,
) -> float:
    """Convert a Twitch VOD offset (used by chat) onto the recording timeline."""
    absolute = vod_start_utc.astimezone(timezone.utc) + timedelta(seconds=vod_offset)
    return offset_from_utc(absolute, recording_start_utc)


def recording_offset_to_vod_offset(
    offset: float,
    vod_start_utc: datetime,
    recording_start_utc: datetime,
) -> float:
    """Inverse of :func:`vod_offset_to_recording_offset`.

    Needed when creating native Twitch clips: the clip API addresses the VOD,
    but everything upstream of it speaks in recording offsets.
    """
    absolute = recording_start_utc.astimezone(timezone.utc) + timedelta(seconds=offset)
    return (absolute - vod_start_utc.astimezone(timezone.utc)).total_seconds()


def detect_clock_jump(markers: Sequence[Marker], tolerance: float = 2.0) -> list[tuple[int, float]]:
    """Find markers where wall clock and monotonic clock disagree.

    A system clock adjustment mid-stream (NTP correction, DST rollover)
    shifts wall time without touching the monotonic counter. Every marker
    after the jump would then map to the wrong frame. This compares the two
    deltas between consecutive markers and reports the discrepancies rather
    than silently trusting wall clock.

    Returns ``(index, discrepancy_seconds)`` pairs for markers that drifted.
    """
    jumps: list[tuple[int, float]] = []
    usable = [(i, m) for i, m in enumerate(markers) if m.monotonic is not None]
    for (_, prev), (idx, cur) in zip(usable, usable[1:]):
        wall_delta = (cur.at_utc - prev.at_utc).total_seconds()
        mono_delta = cur.monotonic - prev.monotonic  # type: ignore[operator]
        discrepancy = wall_delta - mono_delta
        if abs(discrepancy) > tolerance:
            jumps.append((idx, discrepancy))
    return jumps


def correct_clock_jumps(markers: Sequence[Marker], tolerance: float = 2.0) -> list[Marker]:
    """Rebuild wall-clock times from the monotonic clock after a jump.

    The first marker's wall time is trusted as the anchor; subsequent times
    are recomputed from monotonic deltas whenever a jump is detected. Markers
    without a monotonic reading are passed through untouched.
    """
    out = list(markers)
    jumps = dict(detect_clock_jump(markers, tolerance=tolerance))
    if not jumps:
        return out

    cumulative = 0.0
    for i in range(1, len(out)):
        if i in jumps:
            cumulative += jumps[i]
        if cumulative:
            out[i] = Marker(
                at_utc=out[i].at_utc - timedelta(seconds=cumulative),
                kind=out[i].kind,
                category=out[i].category,
                monotonic=out[i].monotonic,
                note=(out[i].note + " [clock-jump corrected]").strip(),
            )
    return out


def pair_range_markers(markers: Iterable[Marker]) -> list[tuple[Marker, Marker | None]]:
    """Pair RANGE_START markers with their RANGE_END.

    An unmatched start (Nick marked the beginning of something and never
    closed it, or the stream ended) yields ``(start, None)`` so the caller
    can fall back to default padding rather than dropping the moment.
    """
    pairs: list[tuple[Marker, Marker | None]] = []
    pending: Marker | None = None
    for marker in markers:
        if marker.kind is MarkerKind.RANGE_START:
            if pending is not None:
                pairs.append((pending, None))
            pending = marker
        elif marker.kind is MarkerKind.RANGE_END:
            if pending is not None:
                pairs.append((pending, marker))
                pending = None
            # A stray end with no start is ignored: it is almost always a
            # double-tap that got split across a daemon restart.
    if pending is not None:
        pairs.append((pending, None))
    return pairs


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
