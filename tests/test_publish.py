"""Native clip planning.

The reshaping rules are the whole risk here: a clip that silently comes out
with different bounds than the ones reviewed looks fine in the terminal and
wrong on the channel. Every rule that moves a boundary is pinned below.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from vodscrapper.config import TwitchConfig
from vodscrapper.publish import (
    ClipPlan,
    ClipResult,
    format_plan,
    plan_clip,
    plan_clips,
    record_result,
    select_candidates,
)

REC_START = datetime(2026, 8, 3, 20, 0, 0, tzinfo=timezone.utc)
# Nick hit record five minutes before going live -- the common case, and the
# reason recording offsets and VOD offsets are not interchangeable.
VOD_START = REC_START + timedelta(seconds=300)
VOD_DURATION = 7200.0


def make(start: float, end: float, **extra):
    candidate = {"start": start, "end": end, "title": "t"}
    candidate.update(extra)
    return candidate


def plan(start: float, end: float, *, vod_duration: float = VOD_DURATION, max_seconds: float = 60.0):
    return plan_clip(
        0, make(start, end),
        recording_start_utc=REC_START,
        vod_start_utc=VOD_START,
        vod_duration=vod_duration,
        max_seconds=max_seconds,
    )


def test_offset_accounts_for_the_gap_between_record_and_going_live():
    p = plan(600.0, 630.0)
    assert p.usable
    # 600s into the recording is only 300s into the VOD.
    assert p.vod_offset == pytest.approx(300.0)
    assert p.duration == pytest.approx(30.0)


def test_long_candidate_is_truncated_to_the_twitch_cap():
    p = plan(600.0, 690.0)
    assert p.duration == pytest.approx(60.0)
    assert p.start == pytest.approx(600.0)
    assert any("trimmed" in n for n in p.notes)


def test_config_can_cap_shorter_than_twitch_does():
    p = plan(600.0, 690.0, max_seconds=30.0)
    assert p.duration == pytest.approx(30.0)


def test_cap_cannot_be_raised_above_twitchs_own_limit():
    p = plan(600.0, 900.0, max_seconds=600.0)
    assert p.duration == pytest.approx(60.0)


def test_short_candidate_is_padded_around_its_midpoint():
    p = plan(600.0, 602.0)
    assert p.duration == pytest.approx(5.0)
    # Padding is symmetric, so the moment stays centred.
    assert p.start == pytest.approx(598.5)


def test_candidate_entirely_before_the_stream_went_live_is_skipped():
    p = plan(100.0, 130.0)
    assert not p.usable
    assert "before the VOD starts" in p.skip_reason


def test_candidate_straddling_the_stream_start_is_clamped_not_dropped():
    p = plan(280.0, 320.0)
    assert p.usable
    assert p.vod_offset == pytest.approx(0.0)
    assert p.start == pytest.approx(300.0)
    assert p.duration == pytest.approx(20.0)
    assert any("clamped" in n for n in p.notes)


def test_clamped_candidate_is_still_given_the_minimum_length():
    # Only two seconds of this fall inside the VOD.
    p = plan(298.0, 302.0)
    assert p.usable
    assert p.duration >= 5.0


def test_candidate_past_the_end_of_the_vod_is_skipped():
    p = plan(VOD_DURATION + 400.0, VOD_DURATION + 430.0)
    assert not p.usable
    assert "past the end" in p.skip_reason


def test_candidate_overshooting_the_vod_end_is_trimmed():
    # VOD ends at recording offset 7500; this asks for 7480..7540.
    p = plan(7480.0, 7540.0)
    assert p.usable
    assert p.vod_offset + p.duration <= VOD_DURATION + 0.01
    assert any("stay inside the VOD" in n for n in p.notes)


def test_candidate_too_close_to_the_vod_end_to_fit_is_skipped():
    p = plan(7498.0, 7530.0)
    assert not p.usable
    assert "5s clip" in p.skip_reason


def test_unknown_vod_duration_does_not_reject_anything():
    p = plan(9999.0, 10029.0, vod_duration=0.0)
    assert p.usable


class TestSelection:
    candidates = [
        make(0, 10, status="approved"),
        make(20, 30, status="rejected"),
        make(40, 50, status="approved", twitch_clip={"id": "abc"}),
        make(60, 70),
        make(80, 90, status="approved"),
    ]

    def test_defaults_to_approved_and_not_already_clipped(self):
        assert select_candidates(self.candidates) == [0, 4]

    def test_all_includes_unreviewed_ones(self):
        assert select_candidates(self.candidates, approved_only=False) == [0, 1, 3, 4]

    def test_force_includes_ones_that_already_have_a_clip(self):
        assert select_candidates(self.candidates, include_clipped=True) == [0, 2, 4]

    def test_top_limits_after_filtering(self):
        assert select_candidates(self.candidates, top=1) == [0]

    def test_explicit_indices_bypass_the_approval_filter(self):
        assert select_candidates(self.candidates, indices=[1, 3]) == [1, 3]

    def test_out_of_range_indices_are_dropped_rather_than_crashing(self):
        assert select_candidates(self.candidates, indices=[3, 99, -1]) == [3]


def test_plan_clips_reads_the_recording_start_off_the_session():
    session = {
        "recording_start_utc": REC_START.isoformat(),
        "candidates": [make(600, 630), make(700, 730)],
    }
    plans = plan_clips(
        session, TwitchConfig(), [0, 1],
        vod_start_utc=VOD_START, vod_duration=VOD_DURATION,
    )
    assert [round(p.vod_offset) for p in plans] == [300, 400]


def test_results_are_only_recorded_when_a_clip_actually_exists():
    candidate = make(600, 630)
    p = ClipPlan(0, "t", 600, 630, 300)
    record_result(candidate, ClipResult(plan=p, error="boom"))
    assert "twitch_clip" not in candidate

    record_result(candidate, ClipResult(plan=p, clip_id="AbcDef", edit_url="e"))
    assert candidate["twitch_clip"]["url"] == "https://clips.twitch.tv/AbcDef"
    assert candidate["twitch_clip"]["duration"] == 30.0


def test_format_plan_explains_skips_instead_of_hiding_them():
    text = format_plan([plan(600, 630), plan(100, 130)])
    assert "SKIP" in text
    assert "before the VOD starts" in text
