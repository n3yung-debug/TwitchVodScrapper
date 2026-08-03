"""Pre-flight checks.

The point of these tests is the OK/WARN/FAIL split. A report that marks a
missing optional dependency as a failure is a report Nick learns to ignore,
and then it stops catching the ffmpeg that really is missing.
"""

from __future__ import annotations

import textwrap

from vodscrapper.config import Config
from vodscrapper.doctor import (
    FAIL,
    OK,
    WARN,
    Report,
    check_calibration,
    check_llm,
    check_open_questions,
    check_paths,
    check_twitch,
    format_report,
)


def status_for(report: Report, name: str) -> str:
    return next(c.status for c in report.checks if c.name == name)


def names(report: Report) -> set[str]:
    return {c.name for c in report.checks}


def base_config(tmp_path) -> Config:
    config = Config()
    config.paths.recordings_dir = str(tmp_path / "recordings")
    config.paths.work_dir = str(tmp_path / "work")
    config.paths.output_dir = str(tmp_path / "out")
    config.paths.markers_file = str(tmp_path / "markers.jsonl")
    config.stats.regions_file = str(tmp_path / "regions.yaml")
    return config


class TestPaths:
    def test_missing_recordings_dir_is_a_failure(self, tmp_path):
        report = Report()
        check_paths(base_config(tmp_path), report)
        assert status_for(report, "recordings_dir") == FAIL

    def test_empty_recordings_dir_only_warns(self, tmp_path):
        config = base_config(tmp_path)
        (tmp_path / "recordings").mkdir()
        report = Report()
        check_paths(config, report)
        assert status_for(report, "recordings_dir") == WARN

    def test_a_recording_present_passes(self, tmp_path):
        config = base_config(tmp_path)
        (tmp_path / "recordings").mkdir()
        (tmp_path / "recordings" / "2026-08-03 20-00-00.mkv").write_bytes(b"x")
        report = Report()
        check_paths(config, report)
        assert status_for(report, "recordings_dir") == OK

    def test_work_and_output_dirs_are_created_rather_than_failed(self, tmp_path):
        config = base_config(tmp_path)
        report = Report()
        check_paths(config, report)
        assert (tmp_path / "work").is_dir()
        assert (tmp_path / "out").is_dir()
        assert status_for(report, "markers_file") == OK


class TestTwitch:
    def test_missing_client_id_warns_and_stops_there(self, tmp_path):
        report = Report()
        check_twitch(base_config(tmp_path), report)
        assert status_for(report, "twitch.client_id") == WARN
        assert "twitch token" not in names(report)

    def test_token_env_unset_is_a_warning_not_a_failure(self, tmp_path, monkeypatch):
        config = base_config(tmp_path)
        config.twitch.client_id = "abcdef123456"
        monkeypatch.delenv(config.twitch.oauth_token_env, raising=False)
        report = Report()
        check_twitch(config, report)
        assert status_for(report, "twitch.client_id") == OK
        assert status_for(report, "twitch token") == WARN

    def test_offline_run_does_not_call_twitch(self, tmp_path, monkeypatch):
        config = base_config(tmp_path)
        config.twitch.client_id = "abcdef123456"
        monkeypatch.setenv(config.twitch.oauth_token_env, "token")
        report = Report()
        check_twitch(config, report, online=False)
        assert status_for(report, "twitch token") == OK
        assert "twitch scopes" not in names(report)

    def test_online_run_fails_when_clips_edit_is_missing(self, tmp_path, monkeypatch):
        config = base_config(tmp_path)
        config.twitch.client_id = "abcdef123456"
        monkeypatch.setenv(config.twitch.oauth_token_env, "token")

        import vodscrapper.ingest.twitch as twitch_module

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def validate_token(self):
                return {"login": "nick", "scopes": ["user:read:email"], "expires_in": 7200}

        monkeypatch.setattr(twitch_module, "TwitchClient", FakeClient)
        report = Report()
        check_twitch(config, report, online=True)
        assert status_for(report, "twitch scopes") == FAIL

    def test_online_run_passes_with_the_right_scope(self, tmp_path, monkeypatch):
        config = base_config(tmp_path)
        config.twitch.client_id = "abcdef123456"
        monkeypatch.setenv(config.twitch.oauth_token_env, "token")

        import vodscrapper.ingest.twitch as twitch_module

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def validate_token(self):
                return {"login": "nick", "scopes": ["clips:edit"], "expires_in": 7200}

        monkeypatch.setattr(twitch_module, "TwitchClient", FakeClient)
        report = Report()
        check_twitch(config, report, online=True)
        assert status_for(report, "twitch scopes") == OK


class TestCalibration:
    def test_uncalibrated_regions_warn_with_the_command_to_fix_it(self, tmp_path):
        report = Report()
        check_calibration(base_config(tmp_path), report)
        assert status_for(report, "regions") == WARN
        fix = next(c.fix for c in report.checks if c.name == "regions")
        assert "vodscrap calibrate" in fix

    def test_placeholder_facecam_warns_while_vertical_output_is_on(self, tmp_path):
        report = Report()
        check_calibration(base_config(tmp_path), report)
        assert status_for(report, "facecam") == WARN

    def test_calibrated_facecam_passes(self, tmp_path):
        config = base_config(tmp_path)
        config.render.vertical.facecam.calibrated = True
        report = Report()
        check_calibration(config, report)
        assert status_for(report, "facecam") == OK

    def test_missing_anchor_image_is_a_failure(self, tmp_path):
        config = base_config(tmp_path)
        (tmp_path / "regions.yaml").write_text(
            textwrap.dedent("""
                source_resolution: [2560, 1440]
                calibrated: true
                screens:
                  post_match:
                    anchor:
                      image: anchors/post_match.png
                    fields:
                      kills:
                        box: [1200, 620, 140, 70]
                  stash:
                    anchor:
                      image: anchors/stash.png
                    fields:
                      stash_value:
                        box: [1900, 140, 260, 60]
            """),
            encoding="utf-8",
        )
        report = Report()
        check_calibration(config, report)
        assert status_for(report, "regions") == OK
        assert status_for(report, "anchor:post_match") == FAIL

    def test_stats_disabled_skips_region_checks_entirely(self, tmp_path):
        config = base_config(tmp_path)
        config.stats.enabled = False
        report = Report()
        check_calibration(config, report)
        assert "regions" not in names(report)


def test_llm_key_is_a_warning_because_the_cascade_degrades(tmp_path, monkeypatch):
    config = base_config(tmp_path)
    monkeypatch.delenv(config.detect.llm.api_key_env, raising=False)
    report = Report()
    check_llm(config, report)
    assert status_for(report, "anthropic key") == WARN


def test_unverified_vod_offset_is_surfaced_every_run(tmp_path):
    report = Report()
    check_open_questions(base_config(tmp_path), report)
    assert status_for(report, "vod_offset semantics") == WARN


def test_format_report_prints_fixes_only_for_problems(tmp_path):
    report = Report()
    report.add("good", OK, "fine", fix="should not appear")
    report.add("bad", FAIL, "broken", fix="do the thing")
    text = format_report(report)
    assert "do the thing" in text
    assert "should not appear" not in text
    assert "1 failure(s)" in text
