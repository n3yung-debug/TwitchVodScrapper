"""Layout geometry, captions, and ffmpeg command construction."""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from vodscrapper import media

from vodscrapper.config import (
    AudioTracks,
    CaptionStyle,
    Config,
    FacecamBox,
    VerticalLayout,
)
from vodscrapper.detect.transcript import Word
from vodscrapper.render.captions import build_ass, group_words
from vodscrapper.render.ffmpeg import (
    AudioSelection,
    RenderJob,
    Renderer,
    build_audio_filter,
    build_render_command,
    build_video_filters,
    escape_filter_path,
    parse_loudness,
)
from vodscrapper.render.layouts import plan_vertical


def layout(**kwargs) -> VerticalLayout:
    base = VerticalLayout(facecam=FacecamBox(x=24, y=1146, width=480, height=270))
    for key, value in kwargs.items():
        setattr(base, key, value)
    return base


def test_vertical_panels_exactly_fill_the_frame():
    plan = plan_vertical(2560, 1440, layout())
    assert plan.gameplay.out_height + plan.facecam.out_height == plan.height


def test_all_output_dimensions_are_even():
    # Odd dimensions make yuv420p encoders fail mid-render.
    plan = plan_vertical(2560, 1440, layout(gameplay_height_fraction=0.617))
    for value in (
        plan.width, plan.height,
        plan.gameplay.out_width, plan.gameplay.out_height,
        plan.gameplay.crop.width, plan.gameplay.crop.height,
        plan.facecam.out_height, plan.facecam.crop.width, plan.facecam.crop.height,
    ):
        assert value % 2 == 0, value


def test_gameplay_crop_stays_inside_the_source():
    plan = plan_vertical(1920, 1080, layout())
    crop = plan.gameplay.crop
    assert crop.x >= 0 and crop.y >= 0
    assert crop.x + crop.width <= 1920
    assert crop.y + crop.height <= 1080


def test_gameplay_crop_is_centred_by_default():
    plan = plan_vertical(2560, 1440, layout())
    crop = plan.gameplay.crop
    centre = crop.x + crop.width / 2
    assert centre == pytest.approx(1280, abs=2)


def test_gameplay_center_x_shifts_the_crop():
    left = plan_vertical(2560, 1440, layout(gameplay_center_x=0.25)).gameplay.crop
    right = plan_vertical(2560, 1440, layout(gameplay_center_x=0.75)).gameplay.crop
    assert left.x < right.x


def test_facecam_box_calibrated_at_1440p_is_clamped_at_1080p():
    # The box would run off the bottom of a 1080p frame; it must be clamped
    # rather than handed to ffmpeg as an invalid crop.
    plan = plan_vertical(1920, 1080, layout())
    crop = plan.facecam.crop
    assert crop.x + crop.width <= 1920
    assert crop.y + crop.height <= 1080


def test_cover_fills_the_panel_and_contain_letterboxes():
    cover = plan_vertical(2560, 1440, layout(facecam_fit="cover")).facecam
    contain = plan_vertical(2560, 1440, layout(facecam_fit="contain")).facecam
    assert not cover.needs_pad
    assert contain.needs_pad


def test_missing_facecam_still_produces_a_full_height_plan():
    plan = plan_vertical(2560, 1440, layout(facecam=FacecamBox(0, 0, 0, 0)))
    assert plan.facecam is None
    chains, label = build_video_filters("vertical", plan, None)
    assert any("pad=" in c for c in chains)
    assert label == "[stacked]"


def test_vertical_filtergraph_stacks_game_over_cam():
    plan = plan_vertical(2560, 1440, layout())
    chains, label = build_video_filters("vertical", plan, None)
    graph = ";".join(chains)
    assert "[game]" in graph and "[cam]" in graph
    assert "vstack=inputs=2" in graph
    # Gameplay must be the first vstack input, i.e. on top.
    assert "[game][cam]vstack" in graph
    assert label == "[stacked]"


def test_subtitles_are_appended_last():
    plan = plan_vertical(2560, 1440, layout())
    chains, label = build_video_filters("vertical", plan, "/tmp/a.ass")
    assert "subtitles=" in chains[-1]
    assert label == "[v]"


def test_windows_subtitle_path_is_escaped():
    escaped = escape_filter_path(r"D:\VodScrapper\work\a.ass")
    assert escaped == r"D\:/VodScrapper/work/a.ass"


def test_amix_disables_normalisation():
    # ffmpeg's default amix divides by input count, which halves the volume.
    chain, label = build_audio_filter(AudioSelection(tracks=[2, 3]), Config().render)
    assert "amix=inputs=2:normalize=0" in chain
    assert label == "[a]"


def test_single_track_skips_amix():
    chain, _ = build_audio_filter(AudioSelection(tracks=[2]), Config().render)
    assert "amix" not in chain
    assert "[0:a:1]" in chain


def test_audio_track_numbers_are_one_based_in_config():
    # Streamlabs labels tracks 1-4; ffmpeg indexes from 0.
    assert AudioSelection(tracks=[1]).as_labels() == ["[0:a:0]"]
    assert AudioSelection(tracks=[4]).as_labels() == ["[0:a:3]"]


def test_default_selection_prefers_isolated_tracks():
    selection = AudioSelection.default(AudioTracks(), available=4)
    assert selection.tracks == [2, 3, 4]


def test_default_selection_falls_back_to_mixed_on_single_track():
    selection = AudioSelection.default(AudioTracks(), available=1)
    assert selection.tracks == [1]


def test_measured_loudness_is_applied_in_the_second_pass():
    measured = {
        "input_i": "-21.5", "input_tp": "-3.2", "input_lra": "8.1",
        "input_thresh": "-32.0", "target_offset": "0.4",
    }
    chain, _ = build_audio_filter(AudioSelection(tracks=[2]), Config().render, measured)
    assert "measured_I=-21.5" in chain
    assert "linear=true" in chain


def test_parse_loudness_reads_ffmpeg_json():
    stderr = """
    [Parsed_loudnorm_0 @ 0x1]
    {
        "input_i" : "-21.50",
        "input_tp" : "-3.20",
        "input_lra" : "8.10",
        "input_thresh" : "-32.00",
        "output_i" : "-14.00",
        "target_offset" : "0.40"
    }
    """
    parsed = parse_loudness(stderr)
    assert parsed is not None
    assert parsed["input_i"] == "-21.50"


def test_parse_loudness_returns_none_without_a_block():
    assert parse_loudness("no json here") is None


def test_render_command_seeks_before_input():
    config = Config()
    job = RenderJob(source="in.mkv", start=100.0, end=130.0, output="out.mp4")
    cmd = build_render_command(job, config, (2560, 1440), encoder="libx264")
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-t") + 1] == "30.000"


def test_render_command_uses_cq_for_nvenc_and_crf_otherwise():
    config = Config()
    job = RenderJob(source="in.mkv", start=0.0, end=10.0, output="out.mp4")
    nvenc = build_render_command(job, config, (2560, 1440), encoder="hevc_nvenc")
    x264 = build_render_command(job, config, (2560, 1440), encoder="libx264")
    assert "-cq" in nvenc and "-crf" not in nvenc
    assert "-crf" in x264 and "-cq" not in x264


def test_group_words_breaks_on_length():
    words = [Word(i * 0.3, i * 0.3 + 0.25, "word") for i in range(12)]
    lines = group_words(words, max_chars=20)
    assert len(lines) > 1
    assert all(len(line.text) <= 24 for line in lines)


def test_group_words_breaks_on_a_pause():
    words = [
        Word(0.0, 0.4, "one"),
        Word(0.4, 0.8, "two"),
        Word(5.0, 5.4, "three"),  # long gap
    ]
    lines = group_words(words, max_chars=100, max_gap=0.8)
    assert len(lines) == 2


def test_ass_timestamps_are_rebased_to_the_clip():
    words = [Word(120.0, 120.5, "hello"), Word(120.6, 121.0, "there")]
    ass = build_ass(words, clip_start=118.0, clip_end=125.0,
                    width=1080, height=1920, style=CaptionStyle())
    assert "0:00:02.00" in ass
    assert "PlayResX: 1080" in ass


def test_ass_drops_words_outside_the_clip():
    words = [Word(10.0, 10.5, "before"), Word(120.0, 120.5, "inside")]
    ass = build_ass(words, 118.0, 125.0, 1080, 1920, CaptionStyle())
    assert "BEFORE" not in ass
    assert "INSIDE" in ass


def test_ass_escapes_braces():
    words = [Word(0.0, 0.5, "{drop}")]
    ass = build_ass(words, 0.0, 5.0, 1080, 1920, CaptionStyle(uppercase=False))
    assert r"\{drop\}" in ass


class TestNvencDetection:
    """Being listed by ffmpeg is not the same as being able to encode.

    NVENC is compiled into most ffmpeg builds but only loads if the NVIDIA
    driver is usable. The listing-only check passed on a machine with no
    usable driver and the render then died with "Cannot load libcuda.so.1"
    after the loudness pass had already run -- which is precisely the case
    the fallback exists to handle.
    """

    @staticmethod
    def _fake_run(listed: bool, encodes: bool, calls: list[list[str]]):
        def fake(cmd, check=True):
            calls.append(list(cmd))
            if "-encoders" in cmd:
                return SimpleNamespace(
                    stdout="hevc_nvenc h264_nvenc\n" if listed else "libx264\n",
                    stderr="",
                    returncode=0,
                )
            return SimpleNamespace(stdout="", stderr="", returncode=0 if encodes else 1)

        return fake

    def test_listed_but_unusable_is_reported_unavailable(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(media, "_NVENC_CACHE", {})
        monkeypatch.setattr(media, "run", self._fake_run(True, False, calls))
        assert media.nvenc_available("ffmpeg-a") is False
        # It must have actually attempted an encode, not just read the list.
        assert any("-f" in c and "null" in c for c in calls)

    def test_listed_and_usable_is_available(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(media, "_NVENC_CACHE", {})
        monkeypatch.setattr(media, "run", self._fake_run(True, True, calls))
        assert media.nvenc_available("ffmpeg-b") is True

    def test_not_listed_skips_the_probe_encode(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(media, "_NVENC_CACHE", {})
        monkeypatch.setattr(media, "run", self._fake_run(False, True, calls))
        assert media.nvenc_available("ffmpeg-c") is False
        assert len(calls) == 1

    def test_result_is_cached_per_binary(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(media, "_NVENC_CACHE", {})
        monkeypatch.setattr(media, "run", self._fake_run(True, True, calls))
        media.nvenc_available("ffmpeg-d")
        media.nvenc_available("ffmpeg-d")
        assert len(calls) == 2  # one listing + one probe, not four

    def test_missing_binary_is_not_an_error(self, monkeypatch):
        def boom(cmd, check=True):
            raise FileNotFoundError(cmd[0])

        monkeypatch.setattr(media, "_NVENC_CACHE", {})
        monkeypatch.setattr(media, "run", boom)
        assert media.nvenc_available("nope") is False

    def test_renderer_falls_back_when_nvenc_cannot_encode(self, monkeypatch):
        config = Config()
        config.render.encoder = "hevc_nvenc"
        config.render.fallback_encoder = "libx264"
        monkeypatch.setattr(
            "vodscrapper.render.ffmpeg.nvenc_available", lambda *a, **k: False
        )
        assert Renderer(config).encoder() == "libx264"

    def test_renderer_keeps_nvenc_when_it_works(self, monkeypatch):
        config = Config()
        config.render.encoder = "hevc_nvenc"
        monkeypatch.setattr(
            "vodscrapper.render.ffmpeg.nvenc_available", lambda *a, **k: True
        )
        assert Renderer(config).encoder() == "hevc_nvenc"
