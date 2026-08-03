"""Audio track extraction.

Multi-track recording is what lets a clip drop Discord or game music without
losing the rest, and it is what gives Whisper a clean mic feed instead of one
contaminated by game audio. Track indices are configured, not guessed --
Streamlabs writes them in the order the user set up, and getting the mapping
wrong means transcribing the game instead of Nick.
"""

from __future__ import annotations

from pathlib import Path

from ..config import AudioTracks
from ..media import MediaInfo, probe, run


def extract_track(
    src: str | Path,
    dest: str | Path,
    track: int,
    sample_rate: int = 16000,
    mono: bool = True,
    ffmpeg: str = "ffmpeg",
) -> Path:
    """Extract one audio track to WAV.

    ``track`` is 1-based to match how Streamlabs labels tracks in its UI.
    16 kHz mono is what Whisper wants and is plenty for energy analysis.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-map", f"0:a:{max(0, track - 1)}",
        "-ar", str(sample_rate),
        "-ac", "1" if mono else "2",
        "-c:a", "pcm_s16le",
        str(dest),
    ]
    run(cmd)
    return dest


def available_tracks(src: str | Path, ffprobe: str = "ffprobe") -> int:
    info: MediaInfo = probe(src, ffprobe=ffprobe)
    return len(info.audio_tracks)


def resolve_analysis_track(src: str | Path, tracks: AudioTracks, ffprobe: str = "ffprobe") -> int:
    """Pick the best track for voice analysis.

    Prefers the isolated mic track. Falls back to the mixed feed when the
    recording turns out to be single-track -- the detectors still work, just
    less precisely, and that is far better than failing the run.
    """
    count = available_tracks(src, ffprobe=ffprobe)
    if count >= tracks.mic:
        return tracks.mic
    return 1


def extract_for_analysis(
    src: str | Path,
    work_dir: str | Path,
    tracks: AudioTracks,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> tuple[Path, bool]:
    """Extract the analysis track. Returns (path, used_isolated_mic)."""
    track = resolve_analysis_track(src, tracks, ffprobe=ffprobe)
    dest = Path(work_dir) / "analysis-mic.wav"
    extract_track(src, dest, track, ffmpeg=ffmpeg)
    return dest, track == tracks.mic
