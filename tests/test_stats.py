"""OCR parsing, session summary, and the retention guard."""

from __future__ import annotations

import textwrap

import json

import pytest

from vodscrapper.config import Config, StatsConfig
from vodscrapper.models import MatchResult, WealthReading
from vodscrapper.retention import find_prunable, prune
from vodscrapper.stats.ocr import parse_bool, parse_number
from vodscrapper.stats.regions import available_games, Box, Field, load_regions
from vodscrapper.stats.summary import build_summary, format_summary, result_candidates


def test_parse_number_strips_thousands_separators():
    # "287,500" must not come back as 287.
    assert parse_number("287,500") == 287500
    assert parse_number("1 234") == 1234


def test_parse_number_handles_abbreviations():
    assert parse_number("12.4k") == 12400
    assert parse_number("2M") == 2000000


def test_parse_number_ignores_surrounding_text():
    assert parse_number("Gold: 4,200 g") == 4200


def test_parse_number_returns_none_on_garbage():
    assert parse_number("") is None
    assert parse_number("~~~") is None


def test_parse_bool_matches_case_insensitively_and_partially():
    field = Field(
        name="survived",
        box=Box(0, 0, 10, 10),
        kind="bool_text",
        true_when=["EXTRACTED"],
        false_when=["DIED"],
    )
    assert parse_bool("- extracted -", field) is True
    assert parse_bool("YOU DIED", field) is False
    assert parse_bool("???", field) is None


def test_box_scales_proportionally():
    scaled = Box(100, 200, 50, 40).scaled(0.75, 0.75)
    assert (scaled.x, scaled.y, scaled.width, scaled.height) == (75, 150, 38, 30)


def test_missing_region_file_is_not_an_error(tmp_path):
    config = load_regions(tmp_path / "nope.yaml")
    assert config.calibrated is False
    assert config.screens == {}


def test_region_file_round_trips(tmp_path):
    path = tmp_path / "regions.yaml"
    path.write_text(
        """
source_resolution: [2560, 1440]
screens:
  post_match:
    anchor:
      image: anchors/post_match.png
      search_box: [800, 100, 960, 200]
      threshold: 0.85
    fields:
      kills: {box: [1200, 620, 120, 60], type: int}
      survived:
        box: [1100, 300, 360, 90]
        type: bool_text
        true_when: [EXTRACTED]
        false_when: [DIED]
""",
        encoding="utf-8",
    )
    config = load_regions(path)
    assert config.calibrated is True
    screen = config.post_match
    assert screen is not None
    assert screen.anchor.threshold == 0.85
    names = {f.name for f in screen.fields}
    assert names == {"kills", "survived"}


def test_summary_counts_outcomes():
    matches = [
        MatchResult(at_offset=100.0, kills=3, survived=True),
        MatchResult(at_offset=500.0, kills=7, survived=False),
        MatchResult(at_offset=900.0, kills=1, survived=True),
    ]
    summary = build_summary(matches, [])
    assert summary.matches == 3
    assert summary.extracted == 2
    assert summary.died == 1
    assert summary.total_kills == 11
    assert summary.best_match_kills == 7
    assert summary.best_match_index == 2
    assert summary.extract_rate == pytest.approx(2 / 3)


def test_unreadable_outcome_is_reported_not_guessed():
    matches = [
        MatchResult(at_offset=100.0, kills=3, survived=True),
        MatchResult(at_offset=500.0, kills=2, survived=None),
    ]
    summary = build_summary(matches, [])
    assert summary.extracted + summary.died == 1
    assert any("unreadable outcome" in n for n in summary.notes)


def test_gold_reports_endpoints_and_delta():
    wealth = [
        WealthReading(at_offset=60.0, stash_value=200000, liquid=50000),
        WealthReading(at_offset=9000.0, stash_value=310000, liquid=27000),
    ]
    summary = build_summary([], wealth)
    assert summary.wealth_start.total == 250000
    assert summary.wealth_end.total == 337000
    assert summary.wealth_delta == 87000


def test_single_stash_reading_yields_no_delta_and_says_so():
    summary = build_summary([], [WealthReading(at_offset=60.0, stash_value=1, liquid=1)])
    assert summary.wealth_delta is None
    assert any("only one stash reading" in n for n in summary.notes)


def test_no_stash_reading_is_reported():
    summary = build_summary([], [])
    assert any("no readable stash screen" in n for n in summary.notes)


def test_format_summary_shows_both_endpoints():
    summary = build_summary(
        [MatchResult(at_offset=1.0, kills=2, survived=True)],
        [
            WealthReading(at_offset=60.0, stash_value=100, liquid=10),
            WealthReading(at_offset=600.0, stash_value=200, liquid=20),
        ],
    )
    text = format_summary(summary)
    assert "Gold start" in text and "Gold end" in text
    assert "Net change: +110" in text


def test_strong_match_becomes_a_candidate():
    cfg = StatsConfig(result_kill_threshold=4, result_signal_pre=90.0)
    matches = [MatchResult(at_offset=600.0, kills=9, survived=True)]
    found = result_candidates(matches, 3600.0, cfg, max_score=30.0)
    assert len(found) == 1
    assert found[0].end == pytest.approx(600.0)
    assert found[0].start == pytest.approx(510.0)


def test_quiet_match_produces_no_candidate():
    cfg = StatsConfig(result_kill_threshold=4)
    matches = [MatchResult(at_offset=600.0, kills=1, survived=True)]
    assert result_candidates(matches, 3600.0, cfg, 30.0) == []


def test_costly_death_still_counts_as_a_candidate():
    # In an extraction game, losing a loaded kit is postable.
    cfg = StatsConfig(result_kill_threshold=4)
    matches = [MatchResult(at_offset=600.0, kills=2, survived=False)]
    assert len(result_candidates(matches, 3600.0, cfg, 30.0)) == 1


def test_result_signal_can_be_switched_off():
    cfg = StatsConfig(result_signal_enabled=False)
    matches = [MatchResult(at_offset=600.0, kills=9, survived=True)]
    assert result_candidates(matches, 3600.0, cfg, 30.0) == []


def _make_recording(tmp_path, name: str, age_days: float, size: int = 1024):
    import os
    import time

    recordings = tmp_path / "recordings"
    recordings.mkdir(exist_ok=True)
    path = recordings / name
    path.write_bytes(b"\0" * size)
    when = time.time() - age_days * 86400
    os.utime(path, (when, when))
    return path


def _make_session(tmp_path, name: str, statuses: list[str]):
    work = tmp_path / "work" / name
    work.mkdir(parents=True, exist_ok=True)
    (work / "session.json").write_text(
        json.dumps({"candidates": [{"status": s} for s in statuses]}),
        encoding="utf-8",
    )


def _config(tmp_path) -> Config:
    config = Config()
    config.paths.recordings_dir = str(tmp_path / "recordings")
    config.paths.work_dir = str(tmp_path / "work")
    config.retention.raw_recording_days = 21
    return config


def test_unreviewed_recording_is_never_deleted(tmp_path):
    """The guard that matters: the VOD may already be gone."""
    _make_recording(tmp_path, "2026-07-01 19-00-00.mkv", age_days=60)
    _make_session(tmp_path, "2026-07-01 19-00-00", ["pending", "pending"])

    deleted, freed = prune(_config(tmp_path), dry_run=True)
    assert deleted == []
    assert freed == 0.0


def test_old_reviewed_recording_is_eligible(tmp_path):
    _make_recording(tmp_path, "2026-07-01 19-00-00.mkv", age_days=60)
    _make_session(tmp_path, "2026-07-01 19-00-00", ["approved", "rejected"])

    deleted, _ = prune(_config(tmp_path), dry_run=True)
    assert len(deleted) == 1


def test_recent_recording_is_kept_even_when_reviewed(tmp_path):
    _make_recording(tmp_path, "2026-08-01 19-00-00.mkv", age_days=2)
    _make_session(tmp_path, "2026-08-01 19-00-00", ["approved"])

    deleted, _ = prune(_config(tmp_path), dry_run=True)
    assert deleted == []


def test_dry_run_leaves_the_file_on_disk(tmp_path):
    path = _make_recording(tmp_path, "2026-07-01 19-00-00.mkv", age_days=60)
    _make_session(tmp_path, "2026-07-01 19-00-00", ["approved"])

    prune(_config(tmp_path), dry_run=True)
    assert path.exists()

    prune(_config(tmp_path), dry_run=False)
    assert not path.exists()


def test_find_prunable_explains_why_each_file_is_kept(tmp_path):
    _make_recording(tmp_path, "2026-08-01 19-00-00.mkv", age_days=2)
    reasons = [c.reason for c in find_prunable(_config(tmp_path))]
    assert any("days old" in r for r in reasons)


class TestPerGameRegions:
    """Regions are stored per game because nothing about them transfers.

    Reading one game's boxes against another game's frame produces confident
    nonsense rather than an error, so selecting the wrong section -- or no
    section -- has to be visible.
    """

    FILE = textwrap.dedent("""
        games:
          rust:
            source_resolution: [2560, 1440]
            calibrated: true
            screens:
              post_match:
                anchor:
                  image: anchors/rust.png
                fields:
                  kills:
                    box: [100, 100, 80, 40]
          fc26:
            source_resolution: [1920, 1080]
            calibrated: true
            screens:
              post_match:
                anchor:
                  image: anchors/fc26.png
                fields:
                  goals:
                    box: [900, 300, 60, 40]
    """)

    def write(self, tmp_path):
        path = tmp_path / "regions.yaml"
        path.write_text(self.FILE, encoding="utf-8")
        return path

    def test_each_game_loads_its_own_section(self, tmp_path):
        path = self.write(tmp_path)
        rust = load_regions(path, game="rust")
        fc26 = load_regions(path, game="fc26")

        assert rust.calibrated and fc26.calibrated
        assert rust.source_resolution == (2560, 1440)
        assert fc26.source_resolution == (1920, 1080)
        assert [f.name for f in rust.post_match.fields] == ["kills"]
        assert [f.name for f in fc26.post_match.fields] == ["goals"]

    def test_no_game_selected_loads_nothing_and_reports_the_options(self, tmp_path):
        regions = load_regions(self.write(tmp_path), game="")
        assert not regions.calibrated
        assert regions.screens == {}
        assert regions.offered == ["fc26", "rust"]

    def test_unknown_game_reports_the_options_rather_than_falling_back(self, tmp_path):
        regions = load_regions(self.write(tmp_path), game="mistfall")
        assert not regions.calibrated
        assert regions.screens == {}
        assert regions.offered == ["fc26", "rust"]

    def test_available_games_lists_the_sections(self, tmp_path):
        assert available_games(self.write(tmp_path)) == ["fc26", "rust"]

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        regions = load_regions(tmp_path / "nope.yaml", game="rust")
        assert not regions.calibrated
        assert available_games(tmp_path / "nope.yaml") == []

    def test_older_flat_file_still_loads(self, tmp_path):
        # Files written before regions were split per game have screens at
        # the top level; they should keep working rather than silently
        # reporting nothing calibrated.
        path = tmp_path / "flat.yaml"
        path.write_text(
            textwrap.dedent("""
                source_resolution: [2560, 1440]
                calibrated: true
                screens:
                  post_match:
                    anchor:
                      image: anchors/x.png
                    fields:
                      kills:
                        box: [100, 100, 80, 40]
            """),
            encoding="utf-8",
        )
        regions = load_regions(path, game="anything")
        assert regions.calibrated
        assert [f.name for f in regions.post_match.fields] == ["kills"]
