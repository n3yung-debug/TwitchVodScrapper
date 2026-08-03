"""Post-stream orchestration.

Each stage degrades independently. A session with no VOD chat, no calibrated
OCR regions, or no GPU still produces a ranked candidate list from whatever
signals are available -- and says in ``warnings`` exactly what it could not
do, rather than quietly returning a thinner result.

Nothing here should ever run while Nick is live: Whisper and NVENC will fight
the stream for the GPU. ``analyze`` is a deliberate post-stream step.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Config
from .detect.audio_signal import detect_from_energies, frame_energies, find_quiet_bounds
from .detect.chat_signal import detect_chat_spikes
from .detect.merge import merge_candidates, rank_candidates
from .ingest.audio import extract_for_analysis
from .ingest.chat import fetch_chat, load_cached_chat, save_chat
from .ingest.recording import Recording, load_recording
from .ingest.twitch import TwitchClient, TwitchError
from .markers.interpret import markers_to_candidates
from .markers.store import MarkerStore
from .models import Candidate, ChatMessage, SessionSummary, Source
from .stats.regions import load_regions
from .timing import clamp


@dataclass
class AnalysisResult:
    recording: Recording
    candidates: list[Candidate] = field(default_factory=list)
    summary: SessionSummary | None = None
    warnings: list[str] = field(default_factory=list)
    vod_id: str = ""
    # Recorded so that native clip creation can convert recording offsets to
    # VOD offsets later without re-querying Twitch -- and so it still works
    # after the VOD's metadata has aged out of easy reach.
    vod_start_utc: datetime | None = None
    vod_duration: float = 0.0
    used_isolated_mic: bool = False
    audio_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "recording_path": str(self.recording.path),
            "recording_name": self.recording.name,
            "recording_start_utc": self.recording.start_utc.isoformat(),
            "duration": self.recording.duration,
            "resolution": list(self.recording.resolution or (0, 0)),
            "vod_id": self.vod_id,
            "vod_start_utc": self.vod_start_utc.isoformat() if self.vod_start_utc else None,
            "vod_duration": self.vod_duration,
            "used_isolated_mic": self.used_isolated_mic,
            "audio_path": self.audio_path,
            "warnings": list(self.warnings),
            "summary": self.summary.to_dict() if self.summary else None,
            "candidates": [c.to_dict() for c in self.candidates],
        }


def session_dir(config: Config, recording: Recording) -> Path:
    path = Path(config.paths.work_dir) / recording.name
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class VodRef:
    """The minimum needed to address a VOD after the fact."""

    id: str = ""
    start_utc: datetime | None = None
    duration: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "start_utc": self.start_utc.isoformat() if self.start_utc else None,
            "duration": self.duration,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "VodRef":
        start = raw.get("start_utc")
        return cls(
            id=raw.get("id", ""),
            start_utc=datetime.fromisoformat(start) if start else None,
            duration=float(raw.get("duration", 0.0) or 0.0),
        )


def _gather_chat(
    config: Config,
    recording: Recording,
    work_dir: Path,
    warnings: list[str],
) -> tuple[list[ChatMessage], VodRef]:
    cache = work_dir / "chat.json"
    vod_cache = work_dir / "vod.json"
    if cache.exists():
        # The VOD reference is cached beside the chat because a re-analyze
        # must not silently lose it -- native clip creation needs the VOD
        # start time, and the VOD itself may be gone by then.
        ref = VodRef()
        if vod_cache.exists():
            try:
                ref = VodRef.from_dict(json.loads(vod_cache.read_text(encoding="utf-8")))
            except Exception:
                pass
        return load_cached_chat(cache), ref

    if not config.twitch.client_id or not config.oauth_token:
        warnings.append(
            "Twitch credentials not configured, so chat was skipped. "
            "Chat is usually the strongest signal -- set twitch.client_id and "
            f"the {config.twitch.oauth_token_env} env var."
        )
        return [], VodRef()

    try:
        client = TwitchClient(config.twitch.client_id, config.oauth_token)
        user_id = config.twitch.broadcaster_id or client.user_id(config.twitch.login)
        vod = client.match_vod(user_id, recording.start_utc)
        if vod is None:
            warnings.append(
                "no Twitch VOD overlapped this recording, so chat was skipped "
                "(the VOD may have expired, or the recording was made offline)"
            )
            return [], VodRef()
        messages = fetch_chat(vod.id, vod.created_at, recording.start_utc)
        save_chat(messages, cache)
        ref = VodRef(id=vod.id, start_utc=vod.created_at, duration=vod.duration_seconds)
        vod_cache.write_text(json.dumps(ref.to_dict(), indent=2), encoding="utf-8")
        return messages, ref
    except (TwitchError, Exception) as exc:
        warnings.append(f"chat retrieval failed, continuing without it: {exc}")
        return [], VodRef()


def _gather_stats(
    config: Config,
    recording: Recording,
    work_dir: Path,
    warnings: list[str],
) -> tuple[SessionSummary | None, list[Candidate]]:
    if not config.stats.enabled:
        return None, []

    regions = load_regions(config.stats.regions_file)
    if not regions.calibrated:
        warnings.append(
            "post-match stats skipped: no calibrated regions. Add screenshots "
            f"and fill in {config.stats.regions_file} to enable kills, "
            "extract rate, and gold tracking."
        )
        return None, []

    try:
        from .stats.screens import find_screens
        from .stats.summary import build_summary, collect, result_candidates

        hits = find_screens(
            recording.path,
            recording.resolution or (1920, 1080),
            regions,
            config.stats,
            ffmpeg=config.paths.ffmpeg,
        )
        matches, wealth, notes = collect(
            recording.path, hits, regions, config.stats, work_dir,
            ffmpeg=config.paths.ffmpeg,
        )
        summary = build_summary(matches, wealth, notes)
        extra = result_candidates(
            matches, recording.duration, config.stats, config.detect.match_result_max_score
        )
        return summary, extra
    except Exception as exc:
        warnings.append(f"post-match stats failed, continuing without them: {exc}")
        return None, []


def analyze(config: Config, recording_path: str | Path) -> AnalysisResult:
    """Run the full detection cascade over one recording."""
    recording = load_recording(recording_path, ffprobe=config.paths.ffprobe)
    work_dir = session_dir(config, recording)
    warnings: list[str] = []

    if recording.start_is_estimated:
        warnings.append(
            "recording start time was estimated from file mtime rather than "
            "read from the filename, so marker alignment may be a second or "
            "two off -- check the trim before rendering"
        )

    result = AnalysisResult(recording=recording, warnings=warnings)
    candidates: list[Candidate] = []

    # -- Tier 0: markers. Highest precision by definition.
    store = MarkerStore(config.paths.markers_file)
    markers = store.between(recording.start_utc, recording.end_utc)
    if markers:
        candidates.extend(
            markers_to_candidates(
                markers,
                recording.start_utc,
                recording.duration,
                config.markers,
                score=config.detect.marker_score,
            )
        )
    else:
        warnings.append("no hotkey markers fell inside this recording")

    # -- Tier 1: chat, scored relative to this stream's own baseline.
    messages, vod = _gather_chat(config, recording, work_dir, warnings)
    result.vod_id = vod.id
    result.vod_start_utc = vod.start_utc
    result.vod_duration = vod.duration
    if messages:
        candidates.extend(
            detect_chat_spikes(messages, recording.duration, config.detect.chat)
        )

    # -- Tier 2: microphone energy. Works when chat is dead.
    energies: list[float] = []
    frame_seconds = config.detect.audio.frame_seconds
    try:
        audio_path, isolated = extract_for_analysis(
            recording.path, work_dir, config.tracks,
            ffmpeg=config.paths.ffmpeg, ffprobe=config.paths.ffprobe,
        )
        result.audio_path = str(audio_path)
        result.used_isolated_mic = isolated
        if not isolated:
            warnings.append(
                "no isolated mic track found, so voice detection ran on the "
                "mixed feed. Enable multi-track audio in Streamlabs for a "
                "cleaner signal and per-clip audio muting."
            )
        energies, frame_seconds = frame_energies(audio_path, config.detect.audio.frame_seconds)
        candidates.extend(
            detect_from_energies(energies, frame_seconds, recording.duration, config.detect.audio)
        )
    except Exception as exc:
        warnings.append(f"audio analysis skipped: {exc}")

    # -- Tier 4: match outcomes, which also segment the session.
    summary, result_cands = _gather_stats(config, recording, work_dir, warnings)
    result.summary = summary
    candidates.extend(result_cands)

    if not candidates:
        warnings.append("no candidates found by any detector")
        result.candidates = []
        return result

    merged = merge_candidates(candidates, config.detect, recording.duration)

    # -- Tier 3: transcription and LLM rerank, on candidates only.
    transcriptions: dict[int, Any] = {}
    if config.detect.transcript.enabled and result.audio_path:
        try:
            from .detect.transcript import transcribe_candidates

            transcriptions = transcribe_candidates(
                merged, result.audio_path, recording.duration, config.detect.transcript
            )
        except Exception as exc:
            warnings.append(f"transcription skipped: {exc}")

    if config.detect.llm.enabled:
        try:
            from .detect.llm import rerank

            rerank(merged, transcriptions, recording.duration, config.detect.llm)
        except Exception as exc:
            warnings.append(f"LLM rerank skipped: {exc}")

    # -- Trim dead air off the ends, without ever emptying a clip.
    if config.detect.tighten_dead_air and energies:
        for candidate in merged:
            start, end = find_quiet_bounds(
                energies, frame_seconds, candidate.start, candidate.end,
                config.detect.dead_air_db,
            )
            if end - start >= config.detect.min_clip_seconds:
                candidate.start = clamp(start, 0.0, recording.duration)
                candidate.end = clamp(end, 0.0, recording.duration)

    result.candidates = rank_candidates(merged)
    save_session(config, result)
    return result


def session_file(config: Config, recording: Recording) -> Path:
    return session_dir(config, recording) / "session.json"


def save_session(config: Config, result: AnalysisResult) -> Path:
    path = session_file(config, result.recording)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result.to_dict(), fh, indent=2)
    return path


def load_session(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def format_report(result: AnalysisResult) -> str:
    """Terminal summary of an analysis run."""
    lines = [
        f"Recording : {result.recording.name}",
        f"Length    : {timedelta(seconds=int(result.recording.duration))}",
        f"Candidates: {len(result.candidates)}",
    ]
    if result.summary:
        from .stats.summary import format_summary

        lines.append("")
        lines.append(format_summary(result.summary))

    lines.append("")
    lines.append("Top candidates:")
    for index, candidate in enumerate(result.candidates[:10], start=1):
        sources = "+".join(s.value for s in candidate.sources)
        stamp = str(timedelta(seconds=int(candidate.start)))
        title = candidate.title or candidate.evidence.get("llm_reason", "")
        lines.append(
            f"  {index:2d}. {stamp}  {candidate.duration:5.1f}s  "
            f"score {candidate.score:6.1f}  [{sources}]  {title}"
        )

    if result.warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"  - {w}" for w in result.warnings)
    return "\n".join(lines)
