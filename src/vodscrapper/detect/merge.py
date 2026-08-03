"""Combining signals into a single ranked list.

Two rules shape this:

1. **Score by source, not by count.** A merged candidate takes the *best*
   score from each source and sums across sources. Summing every overlapping
   detection instead would let one chatty stretch out-rank a genuinely good
   moment simply by producing more detections.

2. **Agreement is itself evidence.** When chat and audio both fire on the
   same moment, that is worth more than either alone, so multi-source
   candidates get a bonus.

Markers are never dropped, merged away, or trimmed out of existence. Nick
pressed the key on purpose; the pipeline's job is to show him that moment,
not to second-guess it.
"""

from __future__ import annotations

from ..config import DetectConfig
from ..models import Candidate, Category, Source
from ..timing import clamp


def _combine(group: list[Candidate], cfg: DetectConfig) -> Candidate:
    start = min(c.start for c in group)
    end = max(c.end for c in group)

    best_by_source: dict[Source, float] = {}
    for c in group:
        for source in c.sources:
            best_by_source[source] = max(best_by_source.get(source, 0.0), c.score)

    score = sum(best_by_source.values())
    if len(best_by_source) > 1:
        score *= 1.0 + 0.15 * (len(best_by_source) - 1)

    evidence: dict = {}
    for c in group:
        for key, value in c.evidence.items():
            evidence.setdefault(key, value)

    # A marker's category is the explicit statement of intent, so it wins
    # over anything inferred.
    category = Category.GENERAL
    for c in group:
        if Source.MARKER in c.sources:
            category = c.category
            break
    else:
        for c in group:
            if c.category is not Category.GENERAL:
                category = c.category
                break

    match_index = next((c.match_index for c in group if c.match_index is not None), None)

    merged = Candidate(
        start=start,
        end=end,
        score=score,
        sources=sorted(best_by_source, key=lambda s: s.value),
        category=category,
        evidence=evidence,
        match_index=match_index,
    )
    return merged


def _enforce_length(candidate: Candidate, cfg: DetectConfig, duration: float) -> Candidate:
    """Clamp a candidate to sane bounds without losing its centre of interest."""
    if candidate.duration > cfg.max_clip_seconds:
        # Keep the window around whatever the detectors pointed at, falling
        # back to the middle when there is no specific anchor.
        anchor = candidate.evidence.get("spike_at")
        if anchor is None:
            anchor = candidate.evidence.get("pressed_at")
        if anchor is None:
            anchor = (candidate.start + candidate.end) / 2.0
        half = cfg.max_clip_seconds / 2.0
        start = clamp(float(anchor) - half, 0.0, duration)
        candidate.start = start
        candidate.end = clamp(start + cfg.max_clip_seconds, 0.0, duration)

    if candidate.duration < cfg.min_clip_seconds:
        centre = (candidate.start + candidate.end) / 2.0
        half = cfg.min_clip_seconds / 2.0
        candidate.start = clamp(centre - half, 0.0, duration)
        candidate.end = clamp(candidate.start + cfg.min_clip_seconds, 0.0, duration)

    return candidate


def merge_candidates(
    candidates: list[Candidate],
    cfg: DetectConfig,
    duration: float,
) -> list[Candidate]:
    if not candidates:
        return []

    ordered = sorted(candidates, key=lambda c: (c.start, c.end))
    groups: list[list[Candidate]] = [[ordered[0]]]

    for candidate in ordered[1:]:
        current = groups[-1]
        group_end = max(c.end for c in current)
        if candidate.start - group_end <= cfg.merge_gap_seconds:
            current.append(candidate)
        else:
            groups.append([candidate])

    merged = [_enforce_length(_combine(g, cfg), cfg, duration) for g in groups]
    return merged


def rank_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Highest score first; markers always float above unmarked moments."""
    return sorted(
        candidates,
        key=lambda c: (Source.MARKER in c.sources, c.score, -c.start),
        reverse=True,
    )
