"""Transcription, used as a reranker rather than a scanner.

The important design choice: Whisper never runs over the whole recording.
An 8-hour session would cost hours of GPU time to transcribe in full, and
almost all of it is footage no detector flagged. Instead only the
neighbourhood of an existing candidate is transcribed, which turns the cost
into minutes and simultaneously improves precision -- the model is asked to
judge moments something already pointed at.

Word-level timestamps are requested because the caption renderer needs them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import TranscriptDetect
from ..models import Candidate


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)


@dataclass
class Transcription:
    """A transcript for one candidate window, in recording-timeline seconds."""

    window_start: float
    window_end: float
    segments: list[Segment] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments).strip()

    def words(self) -> list[Word]:
        out: list[Word] = []
        for segment in self.segments:
            out.extend(segment.words)
        return out

    def words_between(self, start: float, end: float) -> list[Word]:
        return [w for w in self.words() if w.end > start and w.start < end]


class Transcriber:
    """Lazy wrapper around faster-whisper.

    The model is loaded on first use so that importing this module (for the
    dataclasses, or in tests) never pulls CUDA into the process.
    """

    def __init__(self, cfg: TranscriptDetect):
        self.cfg = cfg
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "transcription needs faster-whisper: "
                "pip install 'vodscrapper[analyze]'"
            ) from exc
        try:
            self._model = WhisperModel(
                self.cfg.model,
                device=self.cfg.device,
                compute_type=self.cfg.compute_type,
            )
        except Exception as exc:  # pragma: no cover - runtime/env dependent
            if self.cfg.device != "cpu":
                print(f"[transcript] GPU load failed ({exc}); falling back to CPU")
                from faster_whisper import WhisperModel as _WM
                self._model = _WM(self.cfg.model, device="cpu", compute_type="int8")
            else:
                raise
        return self._model

    def transcribe_window(
        self,
        wav_path: str | Path,
        start: float,
        end: float,
    ) -> Transcription:
        """Transcribe one window. Timestamps come back on the recording timeline."""
        model = self._load()
        segments_iter, _info = model.transcribe(
            str(wav_path),
            beam_size=self.cfg.beam_size,
            word_timestamps=True,
            clip_timestamps=[float(start), float(end)],
        )

        segments: list[Segment] = []
        for seg in segments_iter:
            words = [
                Word(start=float(w.start), end=float(w.end), text=w.word.strip())
                for w in (getattr(seg, "words", None) or [])
                if w.start is not None and w.end is not None
            ]
            segments.append(
                Segment(
                    start=float(seg.start),
                    end=float(seg.end),
                    text=seg.text.strip(),
                    words=words,
                )
            )
        return Transcription(window_start=start, window_end=end, segments=segments)


def transcribe_candidates(
    candidates: list[Candidate],
    wav_path: str | Path,
    duration: float,
    cfg: TranscriptDetect,
) -> dict[int, Transcription]:
    """Transcribe the window around each candidate.

    Returns a mapping of candidate index -> transcription, and writes the
    plain text back onto each candidate so the review UI can show it even if
    the LLM pass is skipped.
    """
    if not cfg.enabled or not candidates:
        return {}

    transcriber = Transcriber(cfg)
    out: dict[int, Transcription] = {}
    for idx, candidate in enumerate(candidates):
        start = max(0.0, candidate.start - cfg.window_pre)
        end = min(duration, candidate.end + cfg.window_post)
        if end - start < 1.0:
            continue
        try:
            transcription = transcriber.transcribe_window(wav_path, start, end)
        except Exception as exc:  # pragma: no cover - runtime dependent
            print(f"[transcript] window {idx} failed: {exc}")
            continue
        out[idx] = transcription
        candidate.transcript = transcription.text
    return out
