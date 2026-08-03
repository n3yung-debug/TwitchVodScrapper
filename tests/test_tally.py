"""Per-game ending-screen tallies.

The rule these protect: a game with no tally is never scored using another
game's. Running the extraction tally over a football full-time screen returns
"0 kills, 0 extracted, 0 died" -- a confident, wrong answer, which is the
failure mode this whole design exists to prevent.
"""

from __future__ import annotations

import textwrap

from vodscrapper.config import Config
from vodscrapper.pipeline import _gather_stats
from vodscrapper.stats.tally import EXTRACTION, known_tallies, tally_for


class TestRegistry:
    def test_only_the_extraction_tally_exists_today(self):
        assert known_tallies() == ["extraction"]

    def test_lookup_by_name(self):
        assert tally_for("extraction") is EXTRACTION

    def test_blank_and_unknown_names_resolve_to_nothing(self):
        assert tally_for("") is None
        assert tally_for("football") is None

    def test_extraction_declares_the_screens_it_reads(self):
        assert EXTRACTION.screens == ("post_match", "stash")

    def test_missing_screens_are_reported(self):
        assert EXTRACTION.missing_screens({"post_match": object()}) == ["stash"]
        assert EXTRACTION.missing_screens(
            {"post_match": object(), "stash": object()}
        ) == []


class TestProfiles:
    def test_only_mistfall_is_tallied_for_now(self):
        config = Config()
        tallied = {k for k, v in config.games.items() if tally_for(v.tally)}
        assert tallied == {"mistfall"}

    def test_the_other_games_declare_no_tally_rather_than_a_wrong_one(self):
        config = Config()
        assert config.games["rust"].tally == ""
        assert config.games["fc26"].tally == ""


def regions_file(tmp_path, game: str, screens: str) -> str:
    path = tmp_path / "regions.yaml"
    path.write_text(
        textwrap.dedent(f"""
            games:
              {game}:
                source_resolution: [2560, 1440]
                calibrated: true
                screens:
{textwrap.indent(textwrap.dedent(screens), ' ' * 18)}
        """),
        encoding="utf-8",
    )
    return str(path)


FULL_EXTRACTION_SCREENS = """
post_match:
  anchor:
    image: a.png
  fields:
    kills:
      box: [1, 1, 10, 10]
stash:
  anchor:
    image: b.png
  fields:
    stash_value:
      box: [1, 1, 10, 10]
"""


class TestGatherStatsGating:
    """_gather_stats decides whether anything gets counted at all."""

    @staticmethod
    def config_for(tmp_path, game: str, screens: str) -> Config:
        config = Config()
        config.game = game
        config.stats.regions_file = regions_file(tmp_path, game, screens)
        return config

    def test_a_game_without_a_tally_is_skipped_and_says_so(self, tmp_path):
        # fc26's screens are calibrated here, so the ONLY thing stopping a
        # bogus extraction tally is the missing tally itself.
        config = self.config_for(tmp_path, "fc26", FULL_EXTRACTION_SCREENS)
        warnings: list[str] = []
        summary, candidates = _gather_stats(config, None, tmp_path, warnings)

        assert summary is None and candidates == []
        assert any("no tally is implemented for 'fc26'" in w for w in warnings)
        assert any("Detection is unaffected" in w for w in warnings)

    def test_a_tallied_game_missing_a_screen_says_which_one(self, tmp_path):
        config = self.config_for(
            tmp_path, "mistfall",
            """
            post_match:
              anchor:
                image: a.png
              fields:
                kills:
                  box: [1, 1, 10, 10]
            """,
        )
        warnings: list[str] = []
        summary, _ = _gather_stats(config, None, tmp_path, warnings)
        assert summary is None
        assert any("missing the stash screen" in w for w in warnings)

    def test_no_game_selected_is_skipped_before_regions_are_even_read(self, tmp_path):
        config = self.config_for(tmp_path, "mistfall", FULL_EXTRACTION_SCREENS)
        config.game = ""
        warnings: list[str] = []
        summary, _ = _gather_stats(config, None, tmp_path, warnings)
        assert summary is None
        assert any("no game selected" in w for w in warnings)

    def test_stats_disabled_skips_silently(self, tmp_path):
        config = self.config_for(tmp_path, "mistfall", FULL_EXTRACTION_SCREENS)
        config.stats.enabled = False
        warnings: list[str] = []
        assert _gather_stats(config, None, tmp_path, warnings) == (None, [])
        assert warnings == []
