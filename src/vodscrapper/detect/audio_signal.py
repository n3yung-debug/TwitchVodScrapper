"""Microphone energy detection.

This is the detector that still works when chat is dead. It looks for the
shape of a reaction rather than raw volume: a sustained jump above the
speaker's own recent baseline, and especially a jump that follows a quiet
stretch, which is what "oh my god" after a tense moment looks like on a
waveform.

Baseline is computed only over frames that are actually above the silence
floor. Averaging in the silence would drag the reference down and make
ordinary speech look like shouting.

Note on scope: this is deliberately an *energy and dynamics* heuristic, not
laughter classification. A real laughter model is a worthwhile upgrade, but
labelling a heuristic as laughter detection would overstate what it does.
"""

from __future__ import annotations

import math
import wave
from collections import deque
from pathlib import Path

from ..config import AudioDetect
from ..models import Candidate, Source
from ..timing import clamp

_SILENCE_FLOOR_DB = -90.0


def frame_energies(
    wav_path: str | Path,
    frame_seconds: float = 0.1,
) -> tuple[list[float], float]:
    """RMS energy per frame in dBFS. Returns (energies, actual_frame_seconds).

    Requires numpy: an eight-hour recording is hundreds of millions of
    samples and pure Python cannot chew through that in reasonable time.
    """
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "audio detection needs numpy: pip install 'vodscrapper[analyze]'"
        ) from exc

    with wave.open(str(wav_path), "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        if width != 2:
            raise RuntimeError(f"expected 16-bit PCM, got {width * 8}-bit")
        total = wf.getnframes()
        raw = wf.readframes(total)

    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)

    per_frame = max(1, int(rate * frame_seconds))
    usable = (len(samples) // per_frame) * per_frame
    if usable == 0:
        return [], frame_seconds
    frames = samples[:usable].reshape(-1, per_frame)

    rms = np.sqrt(np.mean(np.square(frames / 32768.0), axis=1))
    with np.errstate(divide="ignore"):
        db = 20.0 * np.log10(np.maximum(rms, 1e-9))
    db = np.maximum(db, _SILENCE_FLOOR_DB)
    return db.tolist(), per_frame / rate


def _rolling_baseline(energies: list[float], window: int, silence_db: float) -> list[float]:
    """Trailing mean of non-silent frames."""
    out: list[float] = []
    history: deque[float] = deque(maxlen=window)
    total = 0.0
    for value in energies:
        if history:
            out.append(total / len(history))
        else:
            out.append(value)
        if value > silence_db:
            if len(history) == history.maxlen and history:
                total -= history[0]
            history.append(value)
            total += value
    return out


def detect_from_energies(
    energies: list[float],
    frame_seconds: float,
    duration: float,
    cfg: AudioDetect,
) -> list[Candidate]:
    """Pure detection logic, separated so it can be tested without audio."""
    if not cfg.enabled or not energies:
        return []

    window = max(8, int(cfg.baseline_seconds / max(frame_seconds, 1e-6)))
    baseline = _rolling_baseline(energies, window, cfg.silence_db)
    min_frames = max(1, int(cfg.min_spike_seconds / max(frame_seconds, 1e-6)))
    lookback = max(1, int(cfg.silence_lookback_seconds / max(frame_seconds, 1e-6)))

    runs: list[tuple[int, int]] = []
    start_idx: int | None = None
    for idx, (value, base) in enumerate(zip(energies, baseline)):
        if value >= base + cfg.spike_db:
            if start_idx is None:
                start_idx = idx
        elif start_idx is not None:
            if idx - start_idx >= min_frames:
                runs.append((start_idx, idx))
            start_idx = None
    if start_idx is not None and len(energies) - start_idx >= min_frames:
        runs.append((start_idx, len(energies)))

    candidates: list[Candidate] = []
    for begin, end in runs:
        peak_idx = max(range(begin, end), key=lambda i: energies[i])
        excess = energies[peak_idx] - baseline[peak_idx]

        # Was it quiet just before? A burst out of silence reads as a
        # reaction; a burst during continuous loud talking usually does not.
        pre_start = max(0, begin - lookback)
        pre = energies[pre_start:begin]
        from_silence = bool(pre) and (sum(pre) / len(pre)) <= cfg.silence_db + 6.0

        normalised = 1.0 - math.exp(-(excess - cfg.spike_db + 1.0) / 8.0)
        score = normalised * cfg.max_score
        if from_silence:
            score *= 1.25
        score = clamp(score, 0.0, cfg.max_score)

        spike_start = begin * frame_seconds
        spike_end = end * frame_seconds
        candidates.append(
            Candidate(
                start=clamp(spike_start - cfg.pre_roll, 0.0, duration),
                end=clamp(spike_end + cfg.post_roll, 0.0, duration),
                score=score,
                sources=[Source.AUDIO],
                evidence={
                    "peak_db": round(energies[peak_idx], 1),
                    "above_baseline_db": round(excess, 1),
                    "from_silence": from_silence,
                    "spike_at": round(peak_idx * frame_seconds, 1),
                    "sustained_seconds": round(spike_end - spike_start, 2),
                },
            )
        )
    return candidates


def detect_audio_spikes(
    wav_path: str | Path,
    duration: float,
    cfg: AudioDetect,
) -> list[Candidate]:
    if not cfg.enabled:
        return []
    energies, frame_seconds = frame_energies(wav_path, cfg.frame_seconds)
    return detect_from_energies(energies, frame_seconds, duration, cfg)


def find_quiet_bounds(
    energies: list[float],
    frame_seconds: float,
    start: float,
    end: float,
    threshold_db: float,
) -> tuple[float, float]:
    """Tighten a span inward past leading/trailing dead air.

    Used for the dead-air trimming feature. Returns the original bounds
    unchanged when the whole span is quiet, since trimming it to nothing
    would be worse than leaving it alone.
    """
    if not energies:
        return start, end
    first = max(0, int(start / frame_seconds))
    last = min(len(energies), int(end / frame_seconds))
    if last <= first:
        return start, end

    lead = first
    while lead < last and energies[lead] <= threshold_db:
        lead += 1
    trail = last - 1
    while trail > lead and energies[trail] <= threshold_db:
        trail -= 1
    if lead >= trail:
        return start, end
    return lead * frame_seconds, (trail + 1) * frame_seconds
