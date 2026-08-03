"""Chat spike detection.

The core decision here is that the threshold is **relative, not absolute**.
Chat speed varies between a quiet weeknight and a busy raid, so a fixed
"12 messages per 3 seconds is a spike" rule would fire constantly on one and
never on the other. Instead each bin is scored against a rolling baseline of
the preceding ten minutes of the same stream, so the detector recalibrates
itself continuously and "busy relative to how chat has actually been
behaving" is what triggers.

Messages are weighted rather than counted: a burst of KEKW means more than a
burst of "hi", and someone typing "clip that" is the strongest single signal
chat produces.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from ..config import ChatDetect
from ..models import Candidate, ChatMessage, Source
from ..timing import clamp


@dataclass
class ChatBin:
    start: float
    weight: float
    messages: int
    laughs: int
    hypes: int
    clip_calls: int


def _weight_message(msg: ChatMessage, cfg: ChatDetect) -> tuple[float, int, int, int]:
    """Weight one message. Returns (weight, laughs, hypes, clip_calls)."""
    weight = 1.0
    laughs = hypes = clip_calls = 0

    lowered = msg.text.lower()
    laugh_set = {e.lower() for e in cfg.laugh_emotes}
    hype_set = {e.lower() for e in cfg.hype_emotes}

    # Emote fragments are authoritative when present; fall back to scanning
    # the text so exports without emote metadata still work.
    tokens = [e.lower() for e in msg.emotes] or lowered.split()
    for token in tokens:
        if token in laugh_set:
            laughs += 1
            weight += cfg.laugh_weight
        elif token in hype_set:
            hypes += 1
            weight += cfg.hype_weight

    if any(phrase in lowered for phrase in cfg.clip_phrases):
        clip_calls += 1
        weight += cfg.clip_weight

    return weight, laughs, hypes, clip_calls


def build_bins(
    messages: list[ChatMessage],
    duration: float,
    cfg: ChatDetect,
) -> list[ChatBin]:
    if duration <= 0:
        return []
    count = max(1, int(math.ceil(duration / cfg.bin_seconds)))
    bins = [
        ChatBin(start=i * cfg.bin_seconds, weight=0.0, messages=0,
                laughs=0, hypes=0, clip_calls=0)
        for i in range(count)
    ]
    for msg in messages:
        if msg.offset < 0 or msg.offset >= duration:
            continue
        idx = int(msg.offset // cfg.bin_seconds)
        if idx >= count:
            continue
        weight, laughs, hypes, clips = _weight_message(msg, cfg)
        b = bins[idx]
        b.weight += weight
        b.messages += 1
        b.laughs += laughs
        b.hypes += hypes
        b.clip_calls += clips
    return bins


def rolling_z_scores(bins: list[ChatBin], cfg: ChatDetect) -> list[float]:
    """Z-score each bin against the *preceding* baseline window.

    The current bin is excluded from its own baseline on purpose: a large
    spike would otherwise inflate the mean and standard deviation it is being
    measured against, blunting exactly the events we want to catch.
    """
    window = max(1, int(cfg.baseline_seconds / cfg.bin_seconds))
    scores: list[float] = []
    history: deque[float] = deque(maxlen=window)
    total = 0.0
    total_sq = 0.0

    for b in bins:
        n = len(history)
        if n >= 8:
            mean = total / n
            variance = max(0.0, (total_sq / n) - (mean * mean))
            std = math.sqrt(variance)
            # A dead-quiet baseline has std ~0; without a floor every stray
            # message would read as an infinite spike.
            denom = max(std, 0.75)
            scores.append((b.weight - mean) / denom)
        else:
            scores.append(0.0)

        if len(history) == history.maxlen and history:
            evicted = history[0]
            total -= evicted
            total_sq -= evicted * evicted
        history.append(b.weight)
        total += b.weight
        total_sq += b.weight * b.weight

    return scores


def detect_chat_spikes(
    messages: list[ChatMessage],
    duration: float,
    cfg: ChatDetect,
) -> list[Candidate]:
    if not cfg.enabled or not messages:
        return []

    bins = build_bins(messages, duration, cfg)
    scores = rolling_z_scores(bins, cfg)

    # Collect contiguous runs of bins over threshold.
    runs: list[list[int]] = []
    current: list[int] = []
    for idx, (b, z) in enumerate(zip(bins, scores)):
        hot = z >= cfg.z_threshold and b.messages >= cfg.min_messages
        if hot:
            current.append(idx)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)

    candidates: list[Candidate] = []
    for run in runs:
        peak_idx = max(run, key=lambda i: scores[i])
        peak_z = scores[peak_idx]
        spike_start = bins[run[0]].start
        spike_end = bins[run[-1]].start + cfg.bin_seconds

        # Roll the window back: chat is reacting to something that already
        # happened, so the interesting footage precedes the spike.
        start = spike_start - cfg.reaction_lag - cfg.pre_roll
        end = spike_end + cfg.post_roll

        # Score saturates rather than growing without bound, so one enormous
        # spike cannot dominate the entire session's ranking.
        normalised = 1.0 - math.exp(-(peak_z - cfg.z_threshold + 0.5) / 3.0)
        score = clamp(normalised * cfg.max_score, 0.0, cfg.max_score)

        candidates.append(
            Candidate(
                start=clamp(start, 0.0, duration),
                end=clamp(end, 0.0, duration),
                score=score,
                sources=[Source.CHAT],
                evidence={
                    "chat_z": round(peak_z, 2),
                    "messages": sum(bins[i].messages for i in run),
                    "laugh_emotes": sum(bins[i].laughs for i in run),
                    "hype_emotes": sum(bins[i].hypes for i in run),
                    "clip_calls": sum(bins[i].clip_calls for i in run),
                    "spike_at": round(bins[peak_idx].start, 1),
                },
            )
        )
    return candidates
