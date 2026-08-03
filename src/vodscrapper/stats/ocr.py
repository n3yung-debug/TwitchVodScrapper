"""Reading calibrated fields out of a frame.

PaddleOCR is the default engine rather than Tesseract because game UIs use
stylised, decorative type that Tesseract handles poorly, and game UIs
is dark fantasy.

Each field is cropped and upscaled before recognition. Small stylised numerals
recognise far more reliably at 3-4x than at native size, and cropping to the
calibrated box removes the surrounding art that would otherwise confuse the
detector.

Nothing here is trusted blindly: every read carries a confidence, and the
session summary reports which fields were low-confidence rather than
presenting a shaky number as fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import StatsConfig
from .regions import Box, Field, RegionConfig, Screen

_UPSCALE = 3
_NUMBER_RE = re.compile(r"-?[\d][\d,.\s]*")


@dataclass
class FieldRead:
    name: str
    raw_text: str
    value: Any
    confidence: float


class OCREngine:
    """Lazy PaddleOCR wrapper."""

    def __init__(self, cfg: StatsConfig):
        self.cfg = cfg
        self._engine = None

    def _load(self):
        if self._engine is not None:
            return self._engine
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "OCR needs paddleocr: pip install 'vodscrapper[ocr]'"
            ) from exc
        self._engine = PaddleOCR(
            use_angle_cls=False,
            lang=self.cfg.ocr_lang,
            use_gpu=self.cfg.use_gpu,
            show_log=False,
        )
        return self._engine

    def read_image(self, image) -> tuple[str, float]:
        """Recognise text in a cropped numpy image. Returns (text, confidence)."""
        engine = self._load()
        result = engine.ocr(image, cls=False)
        if not result or not result[0]:
            return "", 0.0

        parts: list[str] = []
        confidences: list[float] = []
        for line in result[0]:
            # PaddleOCR yields [box, (text, confidence)] per detected line.
            try:
                text, confidence = line[1]
            except (IndexError, TypeError, ValueError):
                continue
            parts.append(str(text))
            confidences.append(float(confidence))

        if not parts:
            return "", 0.0
        return " ".join(parts).strip(), sum(confidences) / len(confidences)


def parse_number(text: str) -> int | float | None:
    """Pull a number out of OCR text.

    Game UIs format currency with separators and sometimes abbreviate
    (``12.4k``). Thousands separators are stripped before parsing so
    ``287,500`` does not come back as ``287``.
    """
    if not text:
        return None
    lowered = text.strip().lower()
    multiplier = 1
    if lowered.endswith("k"):
        multiplier = 1_000
        lowered = lowered[:-1]
    elif lowered.endswith("m"):
        multiplier = 1_000_000
        lowered = lowered[:-1]

    match = _NUMBER_RE.search(lowered)
    if not match:
        return None
    cleaned = match.group(0).replace(",", "").replace(" ", "").rstrip(".")
    if not cleaned or cleaned == "-":
        return None
    try:
        if "." in cleaned:
            value = float(cleaned) * multiplier
            return int(value) if multiplier > 1 and value.is_integer() else value
        return int(cleaned) * multiplier
    except ValueError:
        return None


def parse_bool(text: str, field: Field) -> bool | None:
    """Resolve an outcome word into survived True/False.

    Matching is case-insensitive and substring-based because OCR routinely
    picks up decoration around the word ("- EXTRACTED -").
    """
    if not text:
        return None
    upper = text.upper()
    for token in field.true_when:
        if token.upper() in upper:
            return True
    for token in field.false_when:
        if token.upper() in upper:
            return False
    return None


def _crop(image, box: Box):
    import cv2

    height, width = image.shape[:2]
    x0 = max(0, min(box.x, width - 1))
    y0 = max(0, min(box.y, height - 1))
    x1 = max(x0 + 1, min(box.x + box.width, width))
    y1 = max(y0 + 1, min(box.y + box.height, height))
    crop = image[y0:y1, x0:x1]
    if crop.size == 0:
        return crop
    return cv2.resize(
        crop,
        (crop.shape[1] * _UPSCALE, crop.shape[0] * _UPSCALE),
        interpolation=cv2.INTER_CUBIC,
    )


def read_screen(
    frame_path: str | Path,
    screen: Screen,
    config: RegionConfig,
    engine: OCREngine,
) -> dict[str, FieldRead]:
    """Read every calibrated field on one screen."""
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("OCR needs opencv: pip install 'vodscrapper[ocr]'") from exc

    image = cv2.imread(str(frame_path))
    if image is None:
        raise RuntimeError(f"could not read frame {frame_path}")

    height, width = image.shape[:2]
    factor_x, factor_y = config.scale_for((width, height))

    out: dict[str, FieldRead] = {}
    for field in screen.fields:
        crop = _crop(image, field.box.scaled(factor_x, factor_y))
        if crop.size == 0:
            out[field.name] = FieldRead(field.name, "", None, 0.0)
            continue

        text, confidence = engine.read_image(crop)
        if field.kind in {"int", "float"}:
            value = parse_number(text)
            if field.kind == "int" and isinstance(value, float):
                value = int(round(value))
        elif field.kind == "bool_text":
            value = parse_bool(text, field)
        else:
            value = text or None

        out[field.name] = FieldRead(field.name, text, value, confidence)
    return out
