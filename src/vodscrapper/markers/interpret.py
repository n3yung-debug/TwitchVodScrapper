"""Turning raw key presses into intent.

The daemon deliberately does nothing clever: every press is written
immediately as a POINT so that a crash can never lose one. All
interpretation -- double-tap ranges, category padding, span construction --
happens here, in pure functions that can be tested without a keyboard.
"""

from __future__ import annotations

from datetime import datetime

from ..config import MarkerConfig
from ..models import Category, Candidate, Marker, MarkerKind, Source
from ..timing import clamp, offset_from_utc, pair_range_markers


def resolve_double_taps(
    markers: list[Marker],
    window_seconds: float,
) -> list[Marker]:
    """Convert two quick presses of the same category into an explicit range.

    A tap marks a moment; two taps inside ``window_seconds`` mark a start and
    an end. Doing this after the fact (rather than making the daemon wait to
    see if a second press arrives) keeps every press durable the instant it
    happens.
    """
    out: list[Marker] = []
    i = 0
    while i < len(markers):
        current = markers[i]
        nxt = markers[i + 1] if i + 1 < len(markers) else None
        is_pair = (
            nxt is not None
            and current.kind is MarkerKind.POINT
            and nxt.kind is MarkerKind.POINT
            and current.category == nxt.category
            and 0 <= (nxt.at_utc - current.at_utc).total_seconds() <= window_seconds
        )
        if is_pair and nxt is not None:
            out.append(
                Marker(current.at_utc, MarkerKind.RANGE_START, current.category,
                       current.monotonic, current.note)
            )
            out.append(
                Marker(nxt.at_utc, MarkerKind.RANGE_END, nxt.category,
                       nxt.monotonic, nxt.note)
            )
            i += 2
        else:
            out.append(current)
            i += 1
    return out


def markers_to_candidates(
    markers: list[Marker],
    recording_start_utc: datetime,
    duration: float,
    marker_cfg: MarkerConfig,
    score: float = 100.0,
) -> list[Candidate]:
    """Build candidate spans from markers.

    Point markers get their category's default padding; explicit ranges use
    the marked bounds with a small courtesy pad. Nothing is dropped for being
    too short or too long here -- the review UI is where bounds get finalised,
    and silently discarding a moment Nick deliberately marked would be worse
    than showing him a rough one.
    """
    resolved = resolve_double_taps(markers, marker_cfg.double_tap_seconds)
    candidates: list[Candidate] = []

    ranges = pair_range_markers(
        [m for m in resolved if m.kind is not MarkerKind.POINT]
    )
    for start_marker, end_marker in ranges:
        start = offset_from_utc(start_marker.at_utc, recording_start_utc)
        if end_marker is not None:
            end = offset_from_utc(end_marker.at_utc, recording_start_utc)
            note = "explicit range"
        else:
            # Unclosed range: fall back to the category's post-padding rather
            # than dropping a moment that was clearly intentional.
            pad = marker_cfg.padding_for(start_marker.category)
            end = start + pad.pre + pad.post
            note = "range start with no end; padded"
        candidates.append(
            Candidate(
                start=clamp(min(start, end), 0.0, duration),
                end=clamp(max(start, end), 0.0, duration),
                score=score,
                sources=[Source.MARKER],
                category=start_marker.category,
                evidence={"marker": note, "category": start_marker.category.value},
            )
        )

    for marker in resolved:
        if marker.kind is not MarkerKind.POINT:
            continue
        at = offset_from_utc(marker.at_utc, recording_start_utc)
        pad = marker_cfg.padding_for(marker.category)
        candidates.append(
            Candidate(
                start=clamp(at - pad.pre, 0.0, duration),
                end=clamp(at + pad.post, 0.0, duration),
                score=score,
                sources=[Source.MARKER],
                category=marker.category,
                evidence={
                    "marker": "point",
                    "category": marker.category.value,
                    "pressed_at": round(at, 2),
                },
            )
        )

    candidates.sort(key=lambda c: c.start)
    return candidates
