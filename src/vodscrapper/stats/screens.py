"""Finding the frames that contain a summary or stash screen.

Two passes, for cost reasons:

1. **Scan.** ffmpeg streams the whole recording as small greyscale frames at a
   low sample rate, straight into memory. Nothing is written to disk. Eight
   hours at 0.5 fps is ~14,000 tiny frames, and a summary screen persists for
   10-30 seconds, so half-fps cannot miss one.
2. **Extract.** Only for the timestamps that matched does ffmpeg seek back and
   pull a single full-resolution frame for OCR.

Doing it the naive way -- decoding every frame, or writing 14,000 full-res
PNGs -- would cost hours and tens of gigabytes for the same result.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import StatsConfig
from .regions import Box, RegionConfig, Screen

# Width of the downscaled scan frames. Small enough to be cheap, large enough
# that an anchor crop still has usable structure after downscaling.
SCAN_WIDTH = 480


@dataclass
class ScreenHit:
    screen: str
    offset: float
    confidence: float


def _require_cv2():
    try:
        import cv2  # type: ignore
        import numpy as np  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "screen detection needs opencv and numpy: "
            "pip install 'vodscrapper[ocr]'"
        ) from exc
    return cv2


def _scan_dimensions(resolution: tuple[int, int]) -> tuple[int, int]:
    width, height = resolution
    scale = SCAN_WIDTH / max(1, width)
    scan_h = int(round(height * scale))
    # ffmpeg's scale filter needs even dimensions for most pixel formats.
    return SCAN_WIDTH, scan_h + (scan_h % 2)


def iter_scan_frames(
    src: str | Path,
    resolution: tuple[int, int],
    sample_fps: float,
    ffmpeg: str = "ffmpeg",
):
    """Yield ``(offset_seconds, frame)`` greyscale numpy arrays.

    Frames arrive over a pipe as raw bytes; nothing touches disk.
    """
    import numpy as np

    width, height = _scan_dimensions(resolution)
    frame_bytes = width * height
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-vf", f"fps={sample_fps},scale={width}:{height}",
        "-pix_fmt", "gray",
        "-f", "rawvideo", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdout is not None

    index = 0
    try:
        while True:
            buffer = proc.stdout.read(frame_bytes)
            if not buffer or len(buffer) < frame_bytes:
                break
            frame = np.frombuffer(buffer, dtype=np.uint8).reshape((height, width))
            yield index / sample_fps, frame
            index += 1
    finally:
        proc.stdout.close()
        proc.wait()


def _prepare_anchor(cv2, config: RegionConfig, screen: Screen, scan_scale: float):
    """Load and downscale an anchor image to the scan frame's scale."""
    path = config.resolve(screen.anchor.image)
    if not path.exists():
        raise RuntimeError(
            f"anchor image missing for screen '{screen.name}': {path}. "
            "Crop it out of the calibration screenshot and point regions.yaml at it."
        )
    template = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if template is None:
        raise RuntimeError(f"could not read anchor image {path}")
    new_w = max(8, int(round(template.shape[1] * scan_scale)))
    new_h = max(8, int(round(template.shape[0] * scan_scale)))
    return cv2.resize(template, (new_w, new_h), interpolation=cv2.INTER_AREA)


def find_screens(
    src: str | Path,
    resolution: tuple[int, int],
    config: RegionConfig,
    cfg: StatsConfig,
    ffmpeg: str = "ffmpeg",
) -> list[ScreenHit]:
    """Scan the recording for every calibrated screen type."""
    if not config.calibrated or not config.screens:
        return []

    cv2 = _require_cv2()
    scan_w, scan_h = _scan_dimensions(resolution)
    # Anchors were cropped from a screenshot at source_resolution, so they
    # scale by source -> scan, not recording -> scan.
    scan_scale = scan_w / max(1, config.source_resolution[0])

    templates = {}
    search_boxes: dict[str, Box | None] = {}
    for name, screen in config.screens.items():
        templates[name] = _prepare_anchor(cv2, config, screen, scan_scale)
        box = screen.anchor.search_box
        search_boxes[name] = box.scaled(scan_scale, scan_scale) if box else None

    hits: list[ScreenHit] = []
    last_seen: dict[str, float] = {}

    for offset, frame in iter_scan_frames(src, resolution, cfg.sample_fps, ffmpeg=ffmpeg):
        for name, screen in config.screens.items():
            debounce = screen.debounce_seconds
            if name in last_seen and offset - last_seen[name] < debounce:
                continue

            region = frame
            box = search_boxes[name]
            if box is not None:
                y0 = max(0, box.y)
                x0 = max(0, box.x)
                region = frame[y0:y0 + box.height, x0:x0 + box.width]

            template = templates[name]
            if region.shape[0] < template.shape[0] or region.shape[1] < template.shape[1]:
                continue

            result = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
            _, best, _, _ = cv2.minMaxLoc(result)
            threshold = max(screen.anchor.threshold, cfg.anchor_threshold)
            if best >= threshold:
                hits.append(ScreenHit(screen=name, offset=offset, confidence=float(best)))
                last_seen[name] = offset

    hits.sort(key=lambda h: h.offset)
    return hits


def extract_frame(
    src: str | Path,
    offset: float,
    dest: str | Path,
    ffmpeg: str = "ffmpeg",
) -> Path:
    """Pull one full-resolution frame for OCR.

    The seek goes before ``-i`` so ffmpeg jumps rather than decoding forward
    from the start -- the difference between milliseconds and minutes on an
    eight-hour file.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{max(0.0, offset):.3f}",
            "-i", str(src),
            "-frames:v", "1",
            str(dest),
        ],
        capture_output=True,
        check=True,
    )
    return dest
