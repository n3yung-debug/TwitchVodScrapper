"""Vertical layout geometry.

Pure arithmetic, kept away from ffmpeg so it can be tested without rendering
anything. Every value ffmpeg needs for the 9:16 stacked layout is computed
here and handed over as plain numbers.

The stacked layout exists because the facecam sits in a corner of the source
frame. No single 9:16 slice of a 16:9 frame can contain both the action and a
corner camera at a readable size, so the two are cropped separately and
re-composited: gameplay on top, facecam beneath it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import VerticalLayout


@dataclass
class CropRect:
    """A crop in source pixels. ffmpeg's crop filter takes exactly these."""

    x: int
    y: int
    width: int
    height: int

    def as_filter(self) -> str:
        return f"crop={self.width}:{self.height}:{self.x}:{self.y}"


@dataclass
class Panel:
    crop: CropRect
    out_width: int
    out_height: int
    # Set when the crop cannot fill the panel and letterboxing is needed.
    pad_x: int = 0
    pad_y: int = 0
    scale_width: int = 0
    scale_height: int = 0

    def __post_init__(self) -> None:
        if not self.scale_width:
            self.scale_width = self.out_width
        if not self.scale_height:
            self.scale_height = self.out_height

    @property
    def needs_pad(self) -> bool:
        return (
            self.scale_width != self.out_width
            or self.scale_height != self.out_height
        )


@dataclass
class VerticalPlan:
    width: int
    height: int
    gameplay: Panel
    facecam: Panel | None


def _even(value: float) -> int:
    """Round to an even integer.

    Chroma-subsampled encoders reject odd dimensions, and an off-by-one here
    surfaces as an ffmpeg error halfway through a batch render.
    """
    number = int(round(value))
    return number - (number % 2)


def _center_crop(
    source_w: int,
    source_h: int,
    target_aspect: float,
    center_x: float = 0.5,
    center_y: float = 0.5,
) -> CropRect:
    """Largest crop of a given aspect ratio that fits inside the source."""
    crop_h = source_h
    crop_w = crop_h * target_aspect
    if crop_w > source_w:
        crop_w = source_w
        crop_h = crop_w / target_aspect

    crop_w = _even(min(crop_w, source_w))
    crop_h = _even(min(crop_h, source_h))

    x = _even(center_x * source_w - crop_w / 2)
    y = _even(center_y * source_h - crop_h / 2)
    x = max(0, min(x, source_w - crop_w))
    y = max(0, min(y, source_h - crop_h))
    return CropRect(x=x, y=y, width=crop_w, height=crop_h)


def _fit_box(
    box_w: int,
    box_h: int,
    panel_w: int,
    panel_h: int,
    mode: str,
) -> tuple[CropRect | None, int, int]:
    """Fit a source box into a panel.

    ``cover`` crops the box to the panel's aspect and fills it completely.
    ``contain`` scales the whole box down and letterboxes the remainder --
    useful when the facecam framing matters more than filling the panel.
    """
    panel_aspect = panel_w / panel_h
    box_aspect = box_w / box_h

    if mode == "contain":
        if box_aspect > panel_aspect:
            scale_w = panel_w
            scale_h = _even(panel_w / box_aspect)
        else:
            scale_h = panel_h
            scale_w = _even(panel_h * box_aspect)
        return None, max(2, scale_w), max(2, scale_h)

    # cover: trim the long axis so the box matches the panel's aspect.
    if box_aspect > panel_aspect:
        new_w = _even(box_h * panel_aspect)
        offset_x = _even((box_w - new_w) / 2)
        return CropRect(offset_x, 0, max(2, new_w), box_h), panel_w, panel_h

    new_h = _even(box_w / panel_aspect)
    offset_y = _even((box_h - new_h) / 2)
    return CropRect(0, offset_y, box_w, max(2, new_h)), panel_w, panel_h


def plan_vertical(
    source_width: int,
    source_height: int,
    layout: VerticalLayout,
) -> VerticalPlan:
    """Compute crops and scales for the stacked 9:16 output."""
    out_w = _even(layout.width)
    out_h = _even(layout.height)

    fraction = min(0.95, max(0.3, layout.gameplay_height_fraction))
    game_h = _even(out_h * fraction)
    cam_h = out_h - game_h

    gameplay_crop = _center_crop(
        source_width,
        source_height,
        target_aspect=out_w / game_h,
        center_x=min(1.0, max(0.0, layout.gameplay_center_x)),
    )
    gameplay = Panel(crop=gameplay_crop, out_width=out_w, out_height=game_h)

    facecam: Panel | None = None
    box = layout.facecam
    if cam_h > 0 and box.width > 0 and box.height > 0:
        # Clamp the calibrated box to the frame; a box drawn on a 1440p
        # screenshot but applied to a 1080p recording would otherwise run off
        # the edge and make ffmpeg fail.
        bx = max(0, min(box.x, source_width - 2))
        by = max(0, min(box.y, source_height - 2))
        bw = _even(min(box.width, source_width - bx))
        bh = _even(min(box.height, source_height - by))

        inner, scale_w, scale_h = _fit_box(bw, bh, out_w, cam_h, layout.facecam_fit)
        crop = CropRect(
            x=bx + (inner.x if inner else 0),
            y=by + (inner.y if inner else 0),
            width=inner.width if inner else bw,
            height=inner.height if inner else bh,
        )
        facecam = Panel(
            crop=crop,
            out_width=out_w,
            out_height=cam_h,
            scale_width=scale_w,
            scale_height=scale_h,
        )

    return VerticalPlan(width=out_w, height=out_h, gameplay=gameplay, facecam=facecam)


def scale_facecam_box(
    layout: VerticalLayout,
    calibrated_resolution: tuple[int, int],
    actual_resolution: tuple[int, int],
) -> VerticalLayout:
    """Rescale a facecam box calibrated at one resolution to another.

    Lets a single calibration survive a change in recording resolution
    instead of silently cropping the wrong part of the frame.
    """
    if calibrated_resolution == actual_resolution:
        return layout
    fx = actual_resolution[0] / max(1, calibrated_resolution[0])
    fy = actual_resolution[1] / max(1, calibrated_resolution[1])

    import copy

    scaled = copy.deepcopy(layout)
    scaled.facecam.x = int(round(layout.facecam.x * fx))
    scaled.facecam.y = int(round(layout.facecam.y * fy))
    scaled.facecam.width = int(round(layout.facecam.width * fx))
    scaled.facecam.height = int(round(layout.facecam.height * fy))
    return scaled
