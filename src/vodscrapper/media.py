"""Thin ffmpeg/ffprobe wrapper.

Command *construction* is kept separate from execution so the render logic
can be unit-tested without ffmpeg installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class MediaError(RuntimeError):
    pass


@dataclass
class StreamInfo:
    index: int
    codec_type: str
    codec_name: str
    channels: int | None = None
    width: int | None = None
    height: int | None = None
    title: str = ""


@dataclass
class MediaInfo:
    path: str
    duration: float
    streams: list[StreamInfo]

    @property
    def video(self) -> StreamInfo | None:
        return next((s for s in self.streams if s.codec_type == "video"), None)

    @property
    def audio_tracks(self) -> list[StreamInfo]:
        return [s for s in self.streams if s.codec_type == "audio"]

    @property
    def resolution(self) -> tuple[int, int] | None:
        v = self.video
        if v and v.width and v.height:
            return v.width, v.height
        return None


def run(cmd: Sequence[str], check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise MediaError(
            f"command failed ({proc.returncode}): {' '.join(cmd[:6])}...\n"
            f"{proc.stderr[-2000:]}"
        )
    return proc


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def probe(path: str | Path, ffprobe: str = "ffprobe") -> MediaInfo:
    cmd = [
        ffprobe, "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]
    proc = run(cmd)
    raw = json.loads(proc.stdout)
    streams = []
    for s in raw.get("streams", []):
        streams.append(
            StreamInfo(
                index=s.get("index", 0),
                codec_type=s.get("codec_type", ""),
                codec_name=s.get("codec_name", ""),
                channels=s.get("channels"),
                width=s.get("width"),
                height=s.get("height"),
                title=(s.get("tags") or {}).get("title", ""),
            )
        )
    duration = float(raw.get("format", {}).get("duration", 0.0) or 0.0)
    return MediaInfo(path=str(path), duration=duration, streams=streams)


def nvenc_available(ffmpeg: str = "ffmpeg") -> bool:
    """Check whether this ffmpeg build exposes NVENC.

    Used to fall back to libx264 when the pipeline runs somewhere without
    the GPU, rather than failing the whole render.
    """
    try:
        proc = run([ffmpeg, "-hide_banner", "-encoders"], check=False)
    except FileNotFoundError:
        return False
    return "nvenc" in proc.stdout


def remux(src: str | Path, dest: str | Path, ffmpeg: str = "ffmpeg") -> Path:
    """Losslessly remux MKV -> MP4.

    Streamlabs records to MKV because an MP4 that loses power mid-write is
    unrecoverable; MKV survives. This converts the finished file without
    re-encoding, so it costs I/O and nothing else.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-map", "0", "-c", "copy",
        str(dest),
    ])
    return dest
