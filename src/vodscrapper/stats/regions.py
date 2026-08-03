"""Screen and field geometry, calibrated from screenshots.

Every coordinate in this file comes from a screenshot rather than from code.
That is the whole point: Mistfall Hunter launched at the end of July 2026 and
its UI will move. When a patch redesigns the summary screen the fix is to
redraw the boxes in ``config/regions.yaml`` -- not to edit Python.

Boxes are stored against a declared ``source_resolution`` and scaled to
whatever the recording actually is, so a 1440p calibration keeps working if a
session gets recorded at 1080p.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class RegionError(ValueError):
    pass


@dataclass
class Box:
    x: int
    y: int
    width: int
    height: int

    def scaled(self, factor_x: float, factor_y: float) -> "Box":
        return Box(
            x=int(round(self.x * factor_x)),
            y=int(round(self.y * factor_y)),
            width=max(1, int(round(self.width * factor_x))),
            height=max(1, int(round(self.height * factor_y))),
        )

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.width, self.height

    @classmethod
    def parse(cls, raw: Any) -> "Box":
        if isinstance(raw, dict):
            return cls(int(raw["x"]), int(raw["y"]), int(raw["width"]), int(raw["height"]))
        if isinstance(raw, (list, tuple)) and len(raw) == 4:
            return cls(int(raw[0]), int(raw[1]), int(raw[2]), int(raw[3]))
        raise RegionError(f"cannot read box from {raw!r}; expected [x, y, w, h]")


@dataclass
class Field:
    """One OCR target inside a screen."""

    name: str
    box: Box
    kind: str = "int"  # int | float | text | bool_text | duration
    true_when: list[str] = field(default_factory=list)
    false_when: list[str] = field(default_factory=list)
    # Optional label shown in the session summary; defaults to the field name.
    label: str = ""
    # Fields not in this list are still captured, but land in MatchResult.extra
    # rather than becoming first-class columns.
    required: bool = False

    @classmethod
    def parse(cls, name: str, raw: dict[str, Any]) -> "Field":
        return cls(
            name=name,
            box=Box.parse(raw.get("box")),
            kind=raw.get("type", raw.get("kind", "int")),
            true_when=[str(v) for v in raw.get("true_when", [])],
            false_when=[str(v) for v in raw.get("false_when", [])],
            label=raw.get("label", ""),
            required=bool(raw.get("required", False)),
        )


@dataclass
class Anchor:
    """A stable visual landmark identifying a screen type.

    ``image`` is a cropped PNG taken from the calibration screenshot -- a piece
    of the screen that does not change between runs (a header, a frame corner,
    a fixed icon). ``search_box`` narrows where to look for it, which both
    speeds up matching and avoids false hits elsewhere on screen.
    """

    image: str
    search_box: Box | None = None
    threshold: float = 0.82

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "Anchor":
        if "image" not in raw:
            raise RegionError("anchor needs an 'image' path")
        return cls(
            image=str(raw["image"]),
            search_box=Box.parse(raw["search_box"]) if raw.get("search_box") else None,
            threshold=float(raw.get("threshold", 0.82)),
        )


@dataclass
class Screen:
    name: str
    anchor: Anchor
    fields: list[Field] = field(default_factory=list)
    # Ignore re-detections within this many seconds -- a summary screen sits on
    # screen for 10-30s and would otherwise be read a dozen times.
    debounce_seconds: float = 45.0

    @classmethod
    def parse(cls, name: str, raw: dict[str, Any]) -> "Screen":
        return cls(
            name=name,
            anchor=Anchor.parse(raw.get("anchor") or {}),
            fields=[Field.parse(k, v) for k, v in (raw.get("fields") or {}).items()],
            debounce_seconds=float(raw.get("debounce_seconds", 45.0)),
        )


@dataclass
class RegionConfig:
    source_resolution: tuple[int, int] = (2560, 1440)
    screens: dict[str, Screen] = field(default_factory=dict)
    calibrated: bool = False
    base_dir: Path = field(default_factory=lambda: Path("."))

    def scale_for(self, resolution: tuple[int, int]) -> tuple[float, float]:
        src_w, src_h = self.source_resolution
        return resolution[0] / max(1, src_w), resolution[1] / max(1, src_h)

    def resolve(self, path: str) -> Path:
        candidate = Path(path)
        return candidate if candidate.is_absolute() else self.base_dir / candidate

    @property
    def post_match(self) -> Screen | None:
        return self.screens.get("post_match")

    @property
    def stash(self) -> Screen | None:
        return self.screens.get("stash")


def load_regions(path: str | Path) -> RegionConfig:
    """Load the calibrated region file.

    A missing or uncalibrated file is not an error -- the rest of the pipeline
    runs fine without stats, and saying so plainly beats failing the whole
    session because one screenshot hasn't been taken yet.
    """
    target = Path(path)
    if not target.exists():
        return RegionConfig(calibrated=False, base_dir=target.parent)

    with open(target, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    resolution = raw.get("source_resolution") or [2560, 1440]
    screens = {
        name: Screen.parse(name, cfg)
        for name, cfg in (raw.get("screens") or {}).items()
    }
    return RegionConfig(
        source_resolution=(int(resolution[0]), int(resolution[1])),
        screens=screens,
        calibrated=bool(screens) and bool(raw.get("calibrated", True)),
        base_dir=target.parent,
    )
