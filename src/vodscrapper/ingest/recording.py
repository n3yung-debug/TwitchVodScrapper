"""Finding the local recording and establishing its start time.

The start time is the anchor for the entire pipeline, so it is derived from
the filename (which OBS/Streamlabs stamps at record-start) rather than from
file mtime (which is write-*end* and therefore off by the whole stream
length). When the filename cannot be parsed we fall back to mtime minus
duration and say so loudly, because that fallback is only as accurate as the
probe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..media import MediaInfo, probe
from ..timing import TimingError, parse_recording_start

VIDEO_SUFFIXES = {".mkv", ".mp4", ".mov", ".flv", ".ts"}


@dataclass
class Recording:
    path: Path
    start_utc: datetime
    duration: float
    info: MediaInfo
    start_is_estimated: bool = False

    @property
    def end_utc(self) -> datetime:
        return self.start_utc + timedelta(seconds=self.duration)

    @property
    def name(self) -> str:
        return self.path.stem

    @property
    def resolution(self) -> tuple[int, int] | None:
        return self.info.resolution


def load_recording(path: str | Path, ffprobe: str = "ffprobe") -> Recording:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"recording not found: {path}")

    info = probe(path, ffprobe=ffprobe)
    estimated = False
    try:
        start = parse_recording_start(path.name)
    except TimingError:
        # mtime is when writing *finished*, so back the duration out of it.
        # Accurate to within a second or two in practice, but flagged so the
        # review UI can warn that marker alignment is approximate.
        estimated = True
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        start = mtime - timedelta(seconds=info.duration)

    return Recording(
        path=path,
        start_utc=start,
        duration=info.duration,
        info=info,
        start_is_estimated=estimated,
    )


def find_recordings(directory: str | Path, ffprobe: str = "ffprobe") -> list[Recording]:
    """All parseable recordings in a folder, oldest first."""
    directory = Path(directory)
    if not directory.exists():
        return []
    out: list[Recording] = []
    for candidate in sorted(directory.iterdir()):
        if candidate.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        try:
            out.append(load_recording(candidate, ffprobe=ffprobe))
        except Exception as exc:
            print(f"[ingest] skipping {candidate.name}: {exc}")
    out.sort(key=lambda r: r.start_utc)
    return out


def latest_recording(directory: str | Path, ffprobe: str = "ffprobe") -> Recording | None:
    recordings = find_recordings(directory, ffprobe=ffprobe)
    return recordings[-1] if recordings else None
