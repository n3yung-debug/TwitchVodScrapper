"""Reading pixel coordinates off a real frame.

Two things in this project are calibrated from screenshots rather than
computed: the OCR boxes in ``config/regions.yaml`` and the facecam box in
``render.vertical.facecam``. Both are just numbers in a YAML file, and both
are miserable to get right by opening a screenshot in an image viewer and
guessing.

So this pulls a frame straight out of the recording -- the exact source the
pipeline will read, at the exact resolution it was recorded at, which is
what makes the numbers valid -- and stamps a labelled coordinate grid over
it. Read the numbers off the picture, type them into the YAML, then use
``check`` to draw the boxes back onto the frame and confirm they landed
where you meant.

Grid drawing prefers OpenCV (already required for the OCR stage, and its
built-in Hershey fonts need no font files, which matters on Windows). When
OpenCV is missing it falls back to ffmpeg's ``drawgrid``, which produces an
unlabelled grid -- still useful, just count the cells.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .media import MediaError, run

# BGR, because OpenCV. Chosen to stay readable over both a bright inventory
# screen and a dark cave.
MINOR_COLOR = (90, 90, 90)
MAJOR_COLOR = (0, 220, 255)
BOX_COLOR = (0, 255, 0)
LABEL_COLOR = (0, 220, 255)
LABEL_BG = (0, 0, 0)


@dataclass
class Box:
    x: int
    y: int
    width: int
    height: int

    @classmethod
    def parse(cls, text: str) -> "Box":
        """Parse an ``x,y,w,h`` string as typed on the command line."""
        parts = [p.strip() for p in text.replace(" ", ",").split(",") if p.strip()]
        if len(parts) != 4:
            raise ValueError(f"expected x,y,w,h -- got {text!r}")
        try:
            values = [int(round(float(p))) for p in parts]
        except ValueError as exc:
            raise ValueError(f"box coordinates must be numbers: {text!r}") from exc
        if values[2] <= 0 or values[3] <= 0:
            raise ValueError(f"box width and height must be positive: {text!r}")
        return cls(*values)

    def as_list(self) -> list[int]:
        return [self.x, self.y, self.width, self.height]


def extract_frame(
    source: str | Path,
    at: float,
    dest: str | Path,
    ffmpeg: str = "ffmpeg",
) -> Path:
    """Pull a single frame out of the recording as a PNG.

    ``-ss`` goes before ``-i`` so ffmpeg seeks rather than decoding from the
    start; on an eight-hour file that is the difference between instant and
    several minutes.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, float(at)):.3f}",
        "-i", str(source),
        "-frames:v", "1",
        str(dest),
    ])
    if not dest.exists():
        raise MediaError(
            f"no frame written at {at:.1f}s -- is that past the end of the recording?"
        )
    return dest


def _cv2():
    try:
        import cv2  # type: ignore
    except ImportError:
        return None
    return cv2


def _label(cv2, image, text: str, origin: tuple[int, int], scale: float = 0.5) -> None:
    """Draw text on a filled background so it stays readable over any frame."""
    font = cv2.FONT_HERSHEY_SIMPLEX
    (w, h), baseline = cv2.getTextSize(text, font, scale, 1)
    x, y = origin
    cv2.rectangle(image, (x - 2, y - h - 2), (x + w + 2, y + baseline), LABEL_BG, -1)
    cv2.putText(image, text, (x, y), font, scale, LABEL_COLOR, 1, cv2.LINE_AA)


def draw_grid(
    frame_path: str | Path,
    dest: str | Path,
    step: int = 100,
    major_every: int = 5,
) -> Path:
    """Overlay a labelled coordinate grid on an extracted frame."""
    cv2 = _cv2()
    if cv2 is None:
        raise MediaError(
            "OpenCV is needed for the labelled grid: pip install -e '.[ocr]' "
            "(or use --no-labels for the ffmpeg fallback)"
        )
    image = cv2.imread(str(frame_path))
    if image is None:
        raise MediaError(f"could not read {frame_path}")

    height, width = image.shape[:2]
    for x in range(0, width, step):
        major = (x // step) % major_every == 0
        cv2.line(image, (x, 0), (x, height), MAJOR_COLOR if major else MINOR_COLOR, 1)
        if major and x:
            _label(cv2, image, str(x), (x + 4, 18))
    for y in range(0, height, step):
        major = (y // step) % major_every == 0
        cv2.line(image, (0, y), (width, y), MAJOR_COLOR if major else MINOR_COLOR, 1)
        if major and y:
            _label(cv2, image, str(y), (4, y - 4))

    _label(cv2, image, f"{width}x{height}  grid {step}px", (4, height - 8), scale=0.6)
    dest = Path(dest)
    cv2.imwrite(str(dest), image)
    return dest


def draw_grid_ffmpeg(
    frame_path: str | Path,
    dest: str | Path,
    step: int = 100,
    ffmpeg: str = "ffmpeg",
) -> Path:
    """Unlabelled grid, for when OpenCV is not installed."""
    dest = Path(dest)
    run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(frame_path),
        "-vf", f"drawgrid=w={step}:h={step}:t=1:c=cyan@0.5",
        str(dest),
    ])
    return dest


def draw_boxes(
    frame_path: str | Path,
    boxes: list[tuple[str, Box]],
    dest: str | Path,
) -> Path:
    """Draw named boxes onto a frame, to confirm a calibration is right."""
    cv2 = _cv2()
    if cv2 is None:
        raise MediaError("OpenCV is needed to draw boxes: pip install -e '.[ocr]'")
    image = cv2.imread(str(frame_path))
    if image is None:
        raise MediaError(f"could not read {frame_path}")

    for name, box in boxes:
        cv2.rectangle(
            image, (box.x, box.y), (box.x + box.width, box.y + box.height), BOX_COLOR, 2
        )
        _label(cv2, image, name, (box.x + 4, max(14, box.y - 6)))
    dest = Path(dest)
    cv2.imwrite(str(dest), image)
    return dest


def crop_box(
    frame_path: str | Path,
    box: Box,
    dest: str | Path,
    ffmpeg: str = "ffmpeg",
) -> Path:
    """Cut one box out of a frame.

    This is how a box gets confirmed before it is trusted: if the crop shows
    exactly the number you want OCR to read and nothing else, the box is
    right. It is also how anchor images are produced.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(frame_path),
        "-vf", f"crop={box.width}:{box.height}:{box.x}:{box.y}",
        str(dest),
    ])
    return dest


def scale_box(box: Box, from_res: tuple[int, int], to_res: tuple[int, int]) -> Box:
    """Rescale a box between resolutions.

    Coordinates read off a 1080p frame are wrong for a 1440p recording by
    exactly this factor, and that mistake looks like OCR simply failing.
    """
    fx = to_res[0] / max(1, from_res[0])
    fy = to_res[1] / max(1, from_res[1])
    return Box(
        x=int(round(box.x * fx)),
        y=int(round(box.y * fy)),
        width=max(1, int(round(box.width * fx))),
        height=max(1, int(round(box.height * fy))),
    )


def collect_configured_boxes(config, resolution: tuple[int, int]) -> list[tuple[str, Box]]:
    """Every box currently configured, scaled to the frame's resolution.

    Pulls from both calibration surfaces -- the OCR regions and the facecam
    -- because they are read off the same screenshot and are easiest to
    verify in one picture.
    """
    from .stats.regions import load_regions

    out: list[tuple[str, Box]] = []
    regions = load_regions(config.stats.regions_file, game=config.game)
    fx, fy = regions.scale_for(resolution)
    for screen in regions.screens.values():
        if screen.anchor.search_box:
            b = screen.anchor.search_box.scaled(fx, fy)
            out.append((f"{screen.name}.anchor", Box(b.x, b.y, b.width, b.height)))
        for field_ in screen.fields:
            b = field_.box.scaled(fx, fy)
            out.append((f"{screen.name}.{field_.name}", Box(b.x, b.y, b.width, b.height)))

    # The facecam box has no declared source resolution of its own: it is
    # measured directly against the recording, so it is drawn unscaled.
    cam = config.render.vertical.facecam
    if cam.width > 0 and cam.height > 0:
        out.append(("facecam", Box(cam.x, cam.y, cam.width, cam.height)))
    return out
