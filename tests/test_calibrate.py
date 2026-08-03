"""Calibration helpers.

Nothing here touches ffmpeg or OpenCV -- the parts worth testing are the
coordinate arithmetic and the input parsing, which is where a typo turns
into an OCR box that reads the wrong corner of the screen.
"""

from __future__ import annotations

import textwrap

import pytest

from vodscrapper.calibrate import Box, collect_configured_boxes, scale_box
from vodscrapper.config import Config


class TestBoxParsing:
    def test_comma_separated(self):
        assert Box.parse("1200,620,140,70").as_list() == [1200, 620, 140, 70]

    def test_spaces_are_accepted_too(self):
        assert Box.parse("1200 620 140 70").as_list() == [1200, 620, 140, 70]

    def test_floats_are_rounded(self):
        assert Box.parse("1200.4,619.6,140,70").as_list() == [1200, 620, 140, 70]

    @pytest.mark.parametrize("text", ["1200,620,140", "1200,620,140,70,5", ""])
    def test_wrong_arity_is_rejected(self, text):
        with pytest.raises(ValueError, match="x,y,w,h"):
            Box.parse(text)

    def test_non_numbers_are_rejected(self):
        with pytest.raises(ValueError, match="numbers"):
            Box.parse("a,b,c,d")

    @pytest.mark.parametrize("text", ["10,10,0,70", "10,10,140,-5"])
    def test_zero_or_negative_size_is_rejected(self, text):
        # A zero-width box crops nothing and fails much later, in ffmpeg.
        with pytest.raises(ValueError, match="positive"):
            Box.parse(text)


def test_scale_box_between_resolutions():
    box = Box(1200, 620, 140, 70)
    scaled = scale_box(box, (2560, 1440), (1920, 1080))
    assert scaled.as_list() == [900, 465, 105, 52]


def test_scale_box_never_collapses_a_box_to_nothing():
    scaled = scale_box(Box(10, 10, 2, 2), (2560, 1440), (320, 180))
    assert scaled.width >= 1 and scaled.height >= 1


REGIONS = textwrap.dedent("""
    source_resolution: [2560, 1440]
    calibrated: true
    screens:
      post_match:
        anchor:
          image: anchors/post_match.png
          search_box: [880, 120, 800, 240]
        fields:
          kills:
            box: [1200, 620, 140, 70]
            type: int
""")


def test_collect_configured_boxes_scales_regions_to_the_frame(tmp_path):
    regions_file = tmp_path / "regions.yaml"
    regions_file.write_text(REGIONS, encoding="utf-8")

    config = Config()
    config.stats.regions_file = str(regions_file)
    config.render.vertical.facecam.x = 24
    config.render.vertical.facecam.y = 1146

    boxes = dict(collect_configured_boxes(config, (1920, 1080)))
    assert boxes["post_match.kills"].as_list() == [900, 465, 105, 52]
    assert boxes["post_match.anchor"].as_list() == [660, 90, 600, 180]
    # The facecam box is measured against the recording itself, so it is not
    # rescaled the way the region boxes are.
    assert boxes["facecam"].as_list() == [24, 1146, 480, 270]


def test_collect_configured_boxes_is_empty_before_calibration(tmp_path):
    config = Config()
    config.stats.regions_file = str(tmp_path / "missing.yaml")
    config.render.vertical.facecam.width = 0
    assert collect_configured_boxes(config, (1920, 1080)) == []
