"""Core data types.

Time conventions used everywhere in this package:

* ``*_utc``  -- absolute wall-clock ``datetime`` in UTC.
* ``*_offset`` / ``start`` / ``end`` -- float seconds measured from the start
  of the local recording file. This is the canonical timeline: markers, chat,
  audio and OCR results are all converted into it before anything is merged.

Keeping one canonical timeline is what makes marker-to-frame mapping exact
instead of approximate. See ``vodscrapper.timing``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable


class MarkerKind(str, Enum):
    """How a marker was produced by the daemon."""

    POINT = "point"          # single tap: pad around it, trim later in the UI
    RANGE_START = "range_start"
    RANGE_END = "range_end"


class Category(str, Enum):
    """Live intent tag. Drives default padding, and survives into the UI."""

    GENERAL = "general"
    FUNNY = "funny"
    BIG_PLAY = "big_play"
    STORY = "story"


class Source(str, Enum):
    """What surfaced a candidate. Used for scoring weights and UI evidence."""

    MARKER = "marker"
    CHAT = "chat"
    AUDIO = "audio"
    MATCH_RESULT = "match_result"


@dataclass
class Marker:
    """One hotkey press.

    ``monotonic`` is stored alongside the wall clock purely as a guard: if
    Windows adjusts the system clock mid-stream (NTP correction, DST), the
    monotonic sequence still orders presses correctly and lets us detect the
    jump instead of silently mis-cutting every clip after it.
    """

    at_utc: datetime
    kind: MarkerKind = MarkerKind.POINT
    category: Category = Category.GENERAL
    monotonic: float | None = None
    note: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "at_utc": self.at_utc.astimezone(timezone.utc).isoformat(),
                "kind": self.kind.value,
                "category": self.category.value,
                "monotonic": self.monotonic,
                "note": self.note,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, line: str) -> "Marker":
        raw = json.loads(line)
        return cls(
            at_utc=datetime.fromisoformat(raw["at_utc"]),
            kind=MarkerKind(raw.get("kind", "point")),
            category=Category(raw.get("category", "general")),
            monotonic=raw.get("monotonic"),
            note=raw.get("note", ""),
        )


@dataclass
class Candidate:
    """A span of the recording that might be worth posting.

    ``start``/``end`` are seconds into the recording file. ``evidence`` is
    free-form per-source detail that the review UI renders so Nick can see
    *why* something scored, rather than trusting a bare number.
    """

    start: float
    end: float
    score: float
    sources: list[Source] = field(default_factory=list)
    category: Category = Category.GENERAL
    evidence: dict[str, Any] = field(default_factory=dict)
    transcript: str = ""
    title: str = ""
    description: str = ""
    hashtags: list[str] = field(default_factory=list)
    match_index: int | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def overlaps(self, other: "Candidate", tolerance: float = 0.0) -> bool:
        return self.start - tolerance < other.end and other.start - tolerance < self.end

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["sources"] = [s.value for s in self.sources]
        d["category"] = self.category.value
        d["duration"] = self.duration
        return d


@dataclass
class MatchResult:
    """One post-match summary screen, read by OCR.

    ``extra`` holds any additional fields the summary screen turns out to
    carry once the regions are calibrated from Nick's screenshots -- the
    schema deliberately does not hard-code the full field list, because the
    game is new and its summary screen will change.
    """

    at_offset: float
    kills: int | None = None
    survived: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    frame_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WealthReading:
    """A stash/liquid reading taken from an inventory screen."""

    at_offset: float
    stash_value: int | None = None
    liquid: int | None = None
    confidence: float = 0.0
    frame_path: str = ""

    @property
    def total(self) -> int | None:
        if self.stash_value is None and self.liquid is None:
            return None
        return (self.stash_value or 0) + (self.liquid or 0)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["total"] = self.total
        return d


@dataclass
class SessionSummary:
    """End-of-session stats.

    Gold is reported as endpoints plus a delta rather than a single number.
    Stash "value" in an extraction game is an estimated market price and
    moves on its own, and buying gear shifts liquid into equipped items --
    so a lone delta can mislead. Showing both reads and their timestamps
    makes an odd number visible instead of authoritative.
    """

    matches: int = 0
    extracted: int = 0
    died: int = 0
    total_kills: int = 0
    best_match_index: int | None = None
    best_match_kills: int | None = None
    wealth_start: WealthReading | None = None
    wealth_end: WealthReading | None = None
    results: list[MatchResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def extract_rate(self) -> float | None:
        return (self.extracted / self.matches) if self.matches else None

    @property
    def avg_kills(self) -> float | None:
        return (self.total_kills / self.matches) if self.matches else None

    @property
    def wealth_delta(self) -> int | None:
        if not self.wealth_start or not self.wealth_end:
            return None
        a, b = self.wealth_start.total, self.wealth_end.total
        if a is None or b is None:
            return None
        return b - a

    def to_dict(self) -> dict[str, Any]:
        return {
            "matches": self.matches,
            "extracted": self.extracted,
            "died": self.died,
            "extract_rate": self.extract_rate,
            "total_kills": self.total_kills,
            "avg_kills": self.avg_kills,
            "best_match_index": self.best_match_index,
            "best_match_kills": self.best_match_kills,
            "wealth_start": self.wealth_start.to_dict() if self.wealth_start else None,
            "wealth_end": self.wealth_end.to_dict() if self.wealth_end else None,
            "wealth_delta": self.wealth_delta,
            "results": [r.to_dict() for r in self.results],
            "notes": list(self.notes),
        }


@dataclass
class ChatMessage:
    """A single VOD chat message, already converted to the recording timeline."""

    offset: float
    user: str
    text: str
    emotes: list[str] = field(default_factory=list)


@dataclass
class Session:
    """Everything known about one stream."""

    recording_path: str
    recording_start_utc: datetime
    duration: float
    vod_id: str = ""
    vod_start_utc: datetime | None = None
    markers: list[Marker] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    summary: SessionSummary | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "recording_path": self.recording_path,
            "recording_start_utc": self.recording_start_utc.isoformat(),
            "duration": self.duration,
            "vod_id": self.vod_id,
            "vod_start_utc": self.vod_start_utc.isoformat() if self.vod_start_utc else None,
            "candidates": [c.to_dict() for c in self.candidates],
            "summary": self.summary.to_dict() if self.summary else None,
        }


def dump_jsonl(items: Iterable[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, separators=(",", ":")) + "\n")
