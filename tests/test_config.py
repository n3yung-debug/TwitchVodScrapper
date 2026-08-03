"""Config loading.

These exist because a config that loads as nested plain dicts fails silently:
every default still looks right until something reads an attribute off a
section, which may be an hour into a render.
"""

from __future__ import annotations

import pytest
import yaml

from vodscrapper.config import (
    CategoryPadding,
    Config,
    RenderConfig,
    VerticalLayout,
    dump_default_config,
    load_config,
)
from vodscrapper.models import Category


def test_missing_file_yields_defaults(tmp_path):
    config = load_config(tmp_path / "nope.yaml")
    assert isinstance(config, Config)
    assert config.render.vertical.height == 1920


def test_default_config_round_trips(tmp_path):
    path = tmp_path / "config.yaml"
    dump_default_config(path)
    config = load_config(path)

    # Every nested section must come back as its dataclass, not a dict.
    assert isinstance(config.render, RenderConfig)
    assert isinstance(config.render.vertical, VerticalLayout)
    assert config.render.vertical.facecam.width == 480
    assert config.detect.chat.z_threshold == 2.5
    assert config.tracks.mic == 2


def test_partial_config_keeps_defaults_for_everything_else(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({
            "paths": {"recordings_dir": "E:/Rec"},
            "render": {"vertical": {"gameplay_height_fraction": 0.7}},
        }),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.paths.recordings_dir == "E:/Rec"
    assert config.render.vertical.gameplay_height_fraction == pytest.approx(0.7)
    # Untouched values keep their defaults rather than vanishing.
    assert config.render.vertical.width == 1080
    assert config.paths.ffmpeg == "ffmpeg"


def test_category_padding_dict_is_rebuilt_as_dataclasses(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({
            "markers": {"padding": {"funny": {"pre": 12.0, "post": 4.0}}}
        }),
        encoding="utf-8",
    )
    config = load_config(path)
    padding = config.markers.padding_for(Category.FUNNY)
    assert isinstance(padding, CategoryPadding)
    assert padding.pre == 12.0


def test_unknown_category_falls_back_to_neutral_padding():
    padding = Config().markers.padding_for(Category.GENERAL)
    assert padding.pre > 0


def test_stream_copy_is_off_by_default():
    # Keyframe-snapped cuts would discard the exact in/out points chosen in
    # the review UI, so the accurate path has to be the default.
    assert Config().render.horizontal_stream_copy is False


def test_default_llm_model_is_pinned():
    assert Config().detect.llm.model == "claude-opus-5"
