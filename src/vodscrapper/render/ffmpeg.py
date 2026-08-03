"""Building and running the render commands.

Command *construction* is separated from execution so the filtergraph can be
tested without ffmpeg present. Every function ending in ``_command`` returns a
plain argument list.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..config import AudioTracks, Config, RenderConfig
from ..media import nvenc_available, run
from .layouts import Panel, VerticalPlan, plan_vertical


@dataclass
class AudioSelection:
    """Which recorded tracks make it into a clip.

    This is what per-clip muting buys: drop the party track when a friend
    says something unpostable, drop the game track when its music would trip
    a platform's copyright filter, keep the mic either way.
    """

    tracks: list[int] = field(default_factory=list)

    @classmethod
    def default(cls, tracks: AudioTracks, available: int) -> "AudioSelection":
        # Isolated tracks give per-clip control; the mixed feed is the
        # fallback when a recording turns out to be single-track.
        isolated = [t for t in (tracks.mic, tracks.game, tracks.party) if t <= available]
        return cls(tracks=isolated or [tracks.mixed])

    def as_labels(self) -> list[str]:
        return [f"[0:a:{max(0, t - 1)}]" for t in self.tracks]


@dataclass
class RenderJob:
    source: str
    start: float
    end: float
    output: str
    orientation: str = "horizontal"  # horizontal | vertical
    audio: AudioSelection | None = None
    subtitles: str | None = None
    loudnorm_measured: dict[str, str] | None = None

    @property
    def duration(self) -> float:
        return max(0.05, self.end - self.start)


def escape_filter_path(path: str) -> str:
    """Escape a path for use inside an ffmpeg filter argument.

    Windows paths are the reason this exists: ``C:\\clips\\a.ass`` contains
    both a backslash and a colon, and ffmpeg's filter parser treats each as
    syntax.
    """
    text = str(path).replace("\\", "/")
    text = text.replace(":", r"\:")
    text = text.replace("'", r"\'")
    return text


def _panel_chain(label_in: str, panel: Panel, label_out: str, background: str) -> str:
    chain = f"{label_in}{panel.crop.as_filter()},scale={panel.scale_width}:{panel.scale_height}"
    if panel.needs_pad:
        chain += (
            f",pad={panel.out_width}:{panel.out_height}:"
            f"(ow-iw)/2:(oh-ih)/2:color={background}"
        )
    chain += f",setsar=1{label_out}"
    return chain


def build_video_filters(
    orientation: str,
    plan: VerticalPlan | None,
    subtitles: str | None,
    background: str = "black",
) -> tuple[list[str], str]:
    """Return (filter chains, final video label)."""
    chains: list[str] = []

    if orientation == "vertical" and plan is not None:
        chains.append(_panel_chain("[0:v]", plan.gameplay, "[game]", background))
        if plan.facecam is not None:
            chains.append(_panel_chain("[0:v]", plan.facecam, "[cam]", background))
            chains.append("[game][cam]vstack=inputs=2[stacked]")
            current = "[stacked]"
        else:
            # No facecam calibrated: pad the gameplay panel out to full height
            # rather than emitting a short frame.
            chains.append(
                f"[game]pad={plan.width}:{plan.height}:0:(oh-ih)/2:color={background}[stacked]"
            )
            current = "[stacked]"
    else:
        chains.append("[0:v]null[base]")
        current = "[base]"

    if subtitles:
        chains.append(f"{current}subtitles='{escape_filter_path(subtitles)}'[v]")
        current = "[v]"

    return chains, current


def build_audio_filter(
    selection: AudioSelection,
    render: RenderConfig,
    measured: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Return (filter chain, final audio label)."""
    labels = selection.as_labels()
    if len(labels) > 1:
        # normalize=0 matters: ffmpeg's default divides each input by the
        # number of inputs, which would quietly halve the volume of a clip
        # that mixes mic and game audio.
        chain = "".join(labels) + f"amix=inputs={len(labels)}:normalize=0"
    else:
        chain = labels[0] + "anull"

    loud = (
        f"loudnorm=I={render.loudness_i}:TP={render.loudness_tp}:LRA={render.loudness_lra}"
    )
    if measured:
        loud += (
            f":measured_I={measured['input_i']}"
            f":measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}"
            f":measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}"
            ":linear=true:print_format=summary"
        )
    chain += f",{loud},aresample=48000[a]"
    return chain, "[a]"


def measure_loudness_command(job: RenderJob, ffmpeg: str, render: RenderConfig) -> list[str]:
    """First pass: measure the clip's loudness without writing a file."""
    selection = job.audio or AudioSelection(tracks=[1])
    labels = selection.as_labels()
    if len(labels) > 1:
        chain = "".join(labels) + f"amix=inputs={len(labels)}:normalize=0,"
    else:
        chain = labels[0]
    chain += (
        f"loudnorm=I={render.loudness_i}:TP={render.loudness_tp}"
        f":LRA={render.loudness_lra}:print_format=json"
    )
    return [
        ffmpeg, "-hide_banner", "-nostats",
        "-ss", f"{job.start:.3f}",
        "-t", f"{job.duration:.3f}",
        "-i", job.source,
        "-filter_complex", chain,
        "-f", "null", "-",
    ]


def parse_loudness(stderr: str) -> dict[str, str] | None:
    """Pull loudnorm's JSON block out of ffmpeg's stderr."""
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    required = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
    if not all(key in data for key in required):
        return None
    return {key: str(data[key]) for key in required}


def build_render_command(
    job: RenderJob,
    config: Config,
    source_resolution: tuple[int, int],
    encoder: str | None = None,
) -> list[str]:
    render = config.render
    plan = None
    if job.orientation == "vertical":
        plan = plan_vertical(source_resolution[0], source_resolution[1], render.vertical)

    video_chains, video_label = build_video_filters(
        job.orientation, plan, job.subtitles, render.vertical.background
    )
    selection = job.audio or AudioSelection(tracks=[config.tracks.mixed])
    audio_chain, audio_label = build_audio_filter(selection, render, job.loudnorm_measured)

    filter_complex = ";".join(video_chains + [audio_chain])
    chosen = encoder or render.encoder

    cmd = [
        config.paths.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        # Seeking before -i is the fast path; ffmpeg then decodes accurately
        # from the nearest keyframe because the output is re-encoded.
        "-ss", f"{job.start:.3f}",
        "-t", f"{job.duration:.3f}",
        "-i", job.source,
        "-filter_complex", filter_complex,
        "-map", video_label,
        "-map", audio_label,
        "-c:v", chosen,
    ]
    if "nvenc" in chosen:
        cmd += ["-preset", render.preset, "-rc", "vbr", "-cq", str(render.cq), "-b:v", "0"]
    else:
        cmd += ["-preset", "medium", "-crf", str(render.cq)]
    cmd += [
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", render.audio_bitrate,
        "-movflags", "+faststart",
        job.output,
    ]
    return cmd


def build_stream_copy_command(job: RenderJob, config: Config) -> list[str]:
    """Rough-cut fast path.

    Only correct when exact bounds do not matter: stream copy cuts on
    keyframes, so the clip can start up to one GOP early.
    """
    return [
        config.paths.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{job.start:.3f}",
        "-t", f"{job.duration:.3f}",
        "-i", job.source,
        "-map", "0:v:0", "-map", f"0:a:{max(0, config.tracks.mixed - 1)}",
        "-c", "copy",
        job.output,
    ]


class Renderer:
    def __init__(self, config: Config):
        self.config = config
        self._encoder: str | None = None

    def encoder(self) -> str:
        if self._encoder is None:
            wanted = self.config.render.encoder
            if "nvenc" in wanted and not nvenc_available(self.config.paths.ffmpeg):
                print(
                    f"[render] {wanted} unavailable in this ffmpeg build; "
                    f"falling back to {self.config.render.fallback_encoder}"
                )
                self._encoder = self.config.render.fallback_encoder
            else:
                self._encoder = wanted
        return self._encoder

    def render(self, job: RenderJob, source_resolution: tuple[int, int]) -> Path:
        Path(job.output).parent.mkdir(parents=True, exist_ok=True)

        if (
            job.orientation == "horizontal"
            and self.config.render.horizontal_stream_copy
            and not job.subtitles
        ):
            run(build_stream_copy_command(job, self.config))
            return Path(job.output)

        if self.config.render.two_pass_loudness and job.loudnorm_measured is None:
            proc = subprocess.run(
                measure_loudness_command(job, self.config.paths.ffmpeg, self.config.render),
                capture_output=True,
                text=True,
            )
            job.loudnorm_measured = parse_loudness(proc.stderr)

        run(build_render_command(job, self.config, source_resolution, self.encoder()))
        return Path(job.output)
