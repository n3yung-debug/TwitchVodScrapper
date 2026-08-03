"""How a game's ending screens become a session tally.

Every game ends a round on some kind of screen, but what that screen *means*
is entirely game-specific. An extraction run ends with kills and whether you
got out; a football match ends with a scoreline; a Rust session does not
really end at all. There is no shared vocabulary to generalise over, so
rather than inventing one, each game names a tally and a tally knows exactly
which screens it reads and what it does with them.

Only the extraction tally exists today, and only Mistfall Hunter uses it.
That is deliberate. The alternative -- running the extraction tally against
whatever screens happen to be calibrated -- would report a football session
as "0 kills, 0 extracted, 0 died" with total confidence. A game with no
tally yet gets told so plainly and its OCR stage sits out; the rest of the
cascade is unaffected, because markers, chat and mic energy never cared what
was being played.

Adding a game's tally later means writing its three functions and
registering it here. Nothing else in the pipeline changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .summary import build_summary, collect, result_candidates


@dataclass(frozen=True)
class Tally:
    """One game's ending-screen bookkeeping.

    ``screens`` is the set of screen names this tally knows how to read. It
    is checked against the calibrated regions so a half-calibrated game says
    which screen is missing rather than quietly tallying nothing.
    """

    name: str
    label: str
    screens: tuple[str, ...]
    collect: Callable
    summarise: Callable
    candidates: Callable

    def missing_screens(self, calibrated: dict) -> list[str]:
        return [name for name in self.screens if name not in calibrated]


EXTRACTION = Tally(
    name="extraction",
    label="extraction runs -- kills, extracted/died, stash gold",
    screens=("post_match", "stash"),
    collect=collect,
    summarise=build_summary,
    candidates=result_candidates,
)


# Keyed by the name a game profile refers to. A game whose profile names no
# tally, or names one that is not here, simply has no tally yet.
TALLIES: dict[str, Tally] = {
    EXTRACTION.name: EXTRACTION,
}


def tally_for(name: str) -> Tally | None:
    return TALLIES.get(name) if name else None


def known_tallies() -> list[str]:
    return sorted(TALLIES)
