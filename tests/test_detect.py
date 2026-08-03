"""Detector behaviour, especially the parts that must self-calibrate."""

from __future__ import annotations

import pytest

from vodscrapper.config import AudioDetect, ChatDetect, DetectConfig
from vodscrapper.detect.audio_signal import detect_from_energies, find_quiet_bounds
from vodscrapper.detect.chat_signal import build_bins, detect_chat_spikes, rolling_z_scores
from vodscrapper.detect.merge import merge_candidates, rank_candidates
from vodscrapper.models import Candidate, ChatMessage, Source


def chat(offset: float, text: str = "hello", emotes=None) -> ChatMessage:
    return ChatMessage(offset=offset, user="viewer", text=text, emotes=emotes or [])


def steady_chat(duration: float, per_bin: int, bin_seconds: float) -> list[ChatMessage]:
    out = []
    t = 0.0
    while t < duration:
        for _ in range(per_bin):
            out.append(chat(t + 0.1))
        t += bin_seconds
    return out


def test_no_spike_in_perfectly_steady_chat():
    cfg = ChatDetect()
    messages = steady_chat(1800.0, per_bin=6, bin_seconds=cfg.bin_seconds)
    assert detect_chat_spikes(messages, 1800.0, cfg) == []


def test_spike_detected_against_quiet_baseline():
    cfg = ChatDetect()
    messages = steady_chat(1200.0, per_bin=2, bin_seconds=cfg.bin_seconds)
    burst_at = 900.0
    messages += [chat(burst_at + 0.1, "KEKW", ["KEKW"]) for _ in range(40)]

    found = detect_chat_spikes(sorted(messages, key=lambda m: m.offset), 1200.0, cfg)
    assert found, "a 20x burst should register"
    assert any(c.start <= burst_at <= c.end for c in found)


def test_threshold_is_relative_so_busy_and_quiet_streams_both_work():
    """The same relative burst must fire on a quiet night and a busy one.

    This is the whole reason the detector uses a rolling z-score instead of
    an absolute message count.
    """
    cfg = ChatDetect()
    results = []
    for baseline in (2, 20):
        messages = steady_chat(1200.0, per_bin=baseline, bin_seconds=cfg.bin_seconds)
        messages += [chat(900.1) for _ in range(baseline * 15)]
        messages.sort(key=lambda m: m.offset)
        results.append(detect_chat_spikes(messages, 1200.0, cfg))

    assert results[0], "quiet stream burst should fire"
    assert results[1], "busy stream burst should fire too"


def test_clip_requests_weigh_more_than_plain_messages():
    cfg = ChatDetect()
    plain = build_bins([chat(1.0) for _ in range(5)], 60.0, cfg)
    asking = build_bins([chat(1.0, "clip that") for _ in range(5)], 60.0, cfg)
    assert asking[0].weight > plain[0].weight
    assert asking[0].clip_calls == 5


def test_laugh_emotes_weigh_more_than_plain_messages():
    cfg = ChatDetect()
    plain = build_bins([chat(1.0) for _ in range(5)], 60.0, cfg)
    laughing = build_bins([chat(1.0, "KEKW", ["KEKW"]) for _ in range(5)], 60.0, cfg)
    assert laughing[0].weight > plain[0].weight
    assert laughing[0].laughs == 5


def test_z_scores_ignore_the_current_bin_in_its_own_baseline():
    cfg = ChatDetect(baseline_seconds=60.0, bin_seconds=1.0)
    messages = [chat(float(i) + 0.1) for i in range(40)]
    messages += [chat(40.1) for _ in range(50)]
    bins = build_bins(sorted(messages, key=lambda m: m.offset), 60.0, cfg)
    scores = rolling_z_scores(bins, cfg)
    assert scores[40] > 5.0


def test_chat_window_opens_before_the_spike():
    """Chat reacts after the fact; the clip has to include the cause."""
    cfg = ChatDetect()
    messages = steady_chat(600.0, per_bin=2, bin_seconds=cfg.bin_seconds)
    messages += [chat(300.1) for _ in range(50)]
    messages.sort(key=lambda m: m.offset)

    found = detect_chat_spikes(messages, 600.0, cfg)
    assert found
    assert found[0].start < 300.0 - cfg.reaction_lag


def test_audio_spike_needs_to_be_sustained():
    cfg = AudioDetect(frame_seconds=0.1, baseline_seconds=30.0, min_spike_seconds=0.6)
    energies = [-30.0] * 600
    energies[300] = -5.0  # a single 100ms click
    assert detect_from_energies(energies, 0.1, 60.0, cfg) == []


def test_audio_spike_fires_when_sustained():
    cfg = AudioDetect(frame_seconds=0.1, baseline_seconds=30.0, min_spike_seconds=0.6)
    energies = [-30.0] * 600
    for i in range(300, 320):  # two seconds of shouting
        energies[i] = -5.0
    found = detect_from_energies(energies, 0.1, 60.0, cfg)
    assert len(found) == 1
    assert found[0].evidence["from_silence"] is False


def test_burst_out_of_silence_scores_higher():
    cfg = AudioDetect(frame_seconds=0.1, baseline_seconds=30.0, min_spike_seconds=0.6)

    talking = [-30.0] * 600
    for i in range(300, 320):
        talking[i] = -5.0

    quiet = [-30.0] * 600
    for i in range(270, 300):
        quiet[i] = -60.0
    for i in range(300, 320):
        quiet[i] = -5.0

    a = detect_from_energies(talking, 0.1, 60.0, cfg)[0]
    b = detect_from_energies(quiet, 0.1, 60.0, cfg)[0]
    assert b.evidence["from_silence"] is True
    assert b.score > a.score


def test_find_quiet_bounds_trims_dead_air():
    energies = [-80.0] * 20 + [-20.0] * 40 + [-80.0] * 20
    start, end = find_quiet_bounds(energies, 0.1, 0.0, 8.0, threshold_db=-40.0)
    assert start == pytest.approx(2.0)
    assert end == pytest.approx(6.0)


def test_find_quiet_bounds_leaves_all_quiet_span_alone():
    # Trimming a silent span to nothing would be worse than leaving it.
    energies = [-80.0] * 80
    assert find_quiet_bounds(energies, 0.1, 0.0, 8.0, -40.0) == (0.0, 8.0)


def test_merge_takes_best_per_source_not_the_sum():
    cfg = DetectConfig()
    many_small = [
        Candidate(start=10.0 + i, end=20.0 + i, score=10.0, sources=[Source.CHAT])
        for i in range(6)
    ]
    merged = merge_candidates(many_small, cfg, 600.0)
    assert len(merged) == 1
    # Six overlapping chat hits must not out-score one strong marker.
    assert merged[0].score == pytest.approx(10.0)


def test_multi_source_agreement_earns_a_bonus():
    cfg = DetectConfig()
    solo = merge_candidates(
        [Candidate(10.0, 40.0, 20.0, [Source.CHAT])], cfg, 600.0
    )[0]
    agreed = merge_candidates(
        [
            Candidate(10.0, 40.0, 20.0, [Source.CHAT]),
            Candidate(12.0, 38.0, 20.0, [Source.AUDIO]),
        ],
        cfg,
        600.0,
    )[0]
    assert agreed.score > solo.score + 20.0


def test_distant_candidates_are_not_merged():
    cfg = DetectConfig(merge_gap_seconds=8.0)
    merged = merge_candidates(
        [
            Candidate(10.0, 30.0, 10.0, [Source.CHAT]),
            Candidate(300.0, 320.0, 10.0, [Source.CHAT]),
        ],
        cfg,
        600.0,
    )
    assert len(merged) == 2


def test_overlong_merge_is_trimmed_around_the_evidence():
    cfg = DetectConfig(max_clip_seconds=60.0)
    merged = merge_candidates(
        [Candidate(0.0, 400.0, 10.0, [Source.CHAT], evidence={"spike_at": 350.0})],
        cfg,
        600.0,
    )[0]
    assert merged.duration == pytest.approx(60.0)
    assert merged.start <= 350.0 <= merged.end


def test_short_candidate_is_widened_to_the_minimum():
    cfg = DetectConfig(min_clip_seconds=5.0)
    merged = merge_candidates(
        [Candidate(100.0, 101.0, 10.0, [Source.AUDIO])], cfg, 600.0
    )[0]
    assert merged.duration >= 5.0


def test_markers_outrank_higher_scoring_detections():
    ranked = rank_candidates([
        Candidate(0.0, 10.0, 95.0, [Source.CHAT]),
        Candidate(20.0, 30.0, 40.0, [Source.MARKER]),
    ])
    assert Source.MARKER in ranked[0].sources


def test_marker_category_survives_a_merge():
    from vodscrapper.models import Category

    cfg = DetectConfig()
    merged = merge_candidates(
        [
            Candidate(10.0, 40.0, 100.0, [Source.MARKER], category=Category.BIG_PLAY),
            Candidate(15.0, 35.0, 20.0, [Source.CHAT], category=Category.GENERAL),
        ],
        cfg,
        600.0,
    )[0]
    assert merged.category is Category.BIG_PLAY
