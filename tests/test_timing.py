"""Timing is the one place a small error silently ruins every clip."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from vodscrapper.markers.interpret import markers_to_candidates, resolve_double_taps
from vodscrapper.config import MarkerConfig
from vodscrapper.models import Category, Marker, MarkerKind
from vodscrapper.timing import (
    TimingError,
    correct_clock_jumps,
    detect_clock_jump,
    offset_from_utc,
    pair_range_markers,
    parse_recording_start,
    recording_offset_to_vod_offset,
    vod_offset_to_recording_offset,
)

UTC = timezone.utc


def test_parse_recording_start_reads_obs_filename():
    start = parse_recording_start("2026-08-03 19-45-12.mkv", tz=UTC)
    assert start == datetime(2026, 8, 3, 19, 45, 12, tzinfo=UTC)


def test_parse_recording_start_accepts_underscore_separator():
    start = parse_recording_start("Replay_2026-08-03_19-45-12.mp4", tz=UTC)
    assert start == datetime(2026, 8, 3, 19, 45, 12, tzinfo=UTC)


def test_parse_recording_start_rejects_unparseable_name():
    with pytest.raises(TimingError):
        parse_recording_start("stream-final-FINAL.mkv")


def test_filename_is_local_time_not_utc():
    # An OBS filename is stamped in local time. Reading it as UTC is an
    # off-by-hours bug that looks like every marker is wrong.
    eastern = timezone(timedelta(hours=-4))
    start = parse_recording_start("2026-08-03 19-45-12.mkv", tz=eastern)
    assert start == datetime(2026, 8, 3, 23, 45, 12, tzinfo=UTC)


def test_offset_from_utc_is_plain_subtraction():
    start = datetime(2026, 8, 3, 19, 0, 0, tzinfo=UTC)
    at = datetime(2026, 8, 3, 21, 30, 15, tzinfo=UTC)
    assert offset_from_utc(at, start) == pytest.approx(9015.0)


def test_vod_offset_round_trips_through_recording_timeline():
    recording_start = datetime(2026, 8, 3, 19, 0, 0, tzinfo=UTC)
    # Stream went live 90 seconds before the local recording started.
    vod_start = recording_start - timedelta(seconds=90)

    recording_offset = vod_offset_to_recording_offset(300.0, vod_start, recording_start)
    assert recording_offset == pytest.approx(210.0)

    back = recording_offset_to_vod_offset(recording_offset, vod_start, recording_start)
    assert back == pytest.approx(300.0)


def test_detect_clock_jump_flags_wall_clock_drift():
    base = datetime(2026, 8, 3, 19, 0, 0, tzinfo=UTC)
    markers = [
        Marker(at_utc=base, monotonic=100.0),
        Marker(at_utc=base + timedelta(seconds=60), monotonic=160.0),
        # NTP pulled the clock back 30s; monotonic kept counting normally.
        Marker(at_utc=base + timedelta(seconds=90), monotonic=280.0),
    ]
    jumps = detect_clock_jump(markers)
    assert len(jumps) == 1
    index, discrepancy = jumps[0]
    assert index == 2
    assert discrepancy == pytest.approx(-90.0)


def test_correct_clock_jumps_realigns_later_markers():
    base = datetime(2026, 8, 3, 19, 0, 0, tzinfo=UTC)
    markers = [
        Marker(at_utc=base, monotonic=0.0),
        Marker(at_utc=base + timedelta(seconds=60), monotonic=60.0),
        Marker(at_utc=base + timedelta(seconds=90), monotonic=180.0),
    ]
    fixed = correct_clock_jumps(markers)
    # The third marker is 180s of real elapsed time from the first.
    assert offset_from_utc(fixed[2].at_utc, base) == pytest.approx(180.0)
    assert "clock-jump corrected" in fixed[2].note


def test_no_correction_when_clocks_agree():
    base = datetime(2026, 8, 3, 19, 0, 0, tzinfo=UTC)
    markers = [
        Marker(at_utc=base + timedelta(seconds=i * 30), monotonic=float(i * 30))
        for i in range(4)
    ]
    assert detect_clock_jump(markers) == []
    assert correct_clock_jumps(markers) == markers


def test_pair_range_markers_handles_unclosed_start():
    base = datetime(2026, 8, 3, 19, 0, 0, tzinfo=UTC)
    markers = [
        Marker(base, MarkerKind.RANGE_START),
        Marker(base + timedelta(seconds=10), MarkerKind.RANGE_END),
        Marker(base + timedelta(seconds=40), MarkerKind.RANGE_START),
    ]
    pairs = pair_range_markers(markers)
    assert len(pairs) == 2
    assert pairs[0][1] is not None
    assert pairs[1][1] is None


def test_double_tap_becomes_a_range():
    base = datetime(2026, 8, 3, 19, 0, 0, tzinfo=UTC)
    markers = [
        Marker(base, MarkerKind.POINT, Category.FUNNY),
        Marker(base + timedelta(seconds=1.0), MarkerKind.POINT, Category.FUNNY),
    ]
    resolved = resolve_double_taps(markers, window_seconds=1.5)
    assert [m.kind for m in resolved] == [MarkerKind.RANGE_START, MarkerKind.RANGE_END]


def test_slow_second_tap_stays_two_points():
    base = datetime(2026, 8, 3, 19, 0, 0, tzinfo=UTC)
    markers = [
        Marker(base, MarkerKind.POINT, Category.FUNNY),
        Marker(base + timedelta(seconds=8), MarkerKind.POINT, Category.FUNNY),
    ]
    resolved = resolve_double_taps(markers, window_seconds=1.5)
    assert [m.kind for m in resolved] == [MarkerKind.POINT, MarkerKind.POINT]


def test_different_categories_never_pair():
    base = datetime(2026, 8, 3, 19, 0, 0, tzinfo=UTC)
    markers = [
        Marker(base, MarkerKind.POINT, Category.FUNNY),
        Marker(base + timedelta(seconds=0.4), MarkerKind.POINT, Category.BIG_PLAY),
    ]
    resolved = resolve_double_taps(markers, window_seconds=1.5)
    assert all(m.kind is MarkerKind.POINT for m in resolved)


def test_marker_candidates_use_category_padding():
    start = datetime(2026, 8, 3, 19, 0, 0, tzinfo=UTC)
    cfg = MarkerConfig()
    markers = [Marker(start + timedelta(seconds=600), MarkerKind.POINT, Category.BIG_PLAY)]

    candidates = markers_to_candidates(markers, start, 3600.0, cfg)
    assert len(candidates) == 1
    # A big play needs the build-up, not just the payoff.
    pad = cfg.padding_for(Category.BIG_PLAY)
    assert candidates[0].start == pytest.approx(600.0 - pad.pre)
    assert candidates[0].end == pytest.approx(600.0 + pad.post)


def test_marker_candidate_is_clamped_to_recording():
    start = datetime(2026, 8, 3, 19, 0, 0, tzinfo=UTC)
    markers = [Marker(start + timedelta(seconds=5), MarkerKind.POINT)]
    candidates = markers_to_candidates(markers, start, 600.0, MarkerConfig())
    assert candidates[0].start == 0.0
