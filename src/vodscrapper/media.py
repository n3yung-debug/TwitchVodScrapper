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


_NVENC_CACHE: dict[str, bool] = {}


def nvenc_available(ffmpeg: str = "ffmpeg", encoder: str = "hevc_nvenc") -> bool:
    """Check whether NVENC can actually encode, not just whether it is listed.

    Listing is not the same as working. ``ffmpeg -encoders`` reports every
    encoder compiled into the build, and NVENC is compiled into most of
    them -- but it only loads at runtime if the NVIDIA driver is present and
    usable. On a machine without the GPU (or with a driver mismatch) the
    listing check passes and the render then dies with "Cannot load
    libcuda.so.1", which is exactly the fallback this function exists to
    trigger.

    So: cheap listing check first, then a real one-frame encode to a null
    output. That costs a few hundred milliseconds once per process and is
    cached, against a render that otherwise fails after the loudness pass.
    """
    key = f"{ffmpeg}\x00{encoder}"
    if key in _NVENC_CACHE:
        return _NVENC_CACHE[key]

    result = False
    try:
        listed = run([ffmpeg, "-hide_banner", "-encoders"], check=False)
        if encoder in listed.stdout:
            probe = run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "nullsrc=s=256x144:d=0.1",
                    "-c:v", encoder, "-f", "null", "-",
                ],
                check=False,
            )
            result = probe.returncode == 0
    except (FileNotFoundError, OSError):
        result = False

    _NVENC_CACHE[key] = result
    return result


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
