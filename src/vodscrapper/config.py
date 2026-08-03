"""Configuration loading.

Everything tunable lives in YAML so that behaviour can change without code
edits -- this matters most for the OCR regions and the facecam box, which
are calibrated from screenshots and will need redrawing whenever the game
patches its UI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

import yaml

from .models import Category

DEFAULT_CONFIG_PATH = Path("config/config.yaml")


@dataclass
class Paths:
    recordings_dir: str = "D:/Recordings"
    work_dir: str = "D:/VodScrapper/work"
    output_dir: str = "D:/VodScrapper/clips"
    markers_file: str = "D:/VodScrapper/markers.jsonl"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"


@dataclass
class TwitchConfig:
    client_id: str = ""
    # Read from env when blank so tokens never land in the repo.
    oauth_token_env: str = "TWITCH_OAUTH_TOKEN"
    broadcaster_id: str = ""
    login: str = ""
    create_native_clips: bool = True
    # PENDING VALIDATION. Twitch's API reference describes vod_offset as the
    # clip START; several client libraries and forum posts describe it as the
    # clip END (start = vod_offset - duration). One test clip settles it.
    # Until then this flag decides, and `vodscrap verify-clip-offset` exists
    # to run that test. Guessing wrong offsets every clip by its own length.
    vod_offset_is_start: bool = True
    max_native_clip_seconds: int = 60


@dataclass
class AudioTracks:
    """Streamlabs multi-track mapping.

    Track 1 is the mixed stream feed; 2-4 are isolated so a clip can drop
    Discord or game music without losing the rest.
    """

    mixed: int = 1
    mic: int = 2
    game: int = 3
    party: int = 4


@dataclass
class CategoryPadding:
    """Default seconds of lead-in/lead-out for a single-tap marker."""

    pre: float = 30.0
    post: float = 10.0


@dataclass
class MarkerConfig:
    hotkey_point: str = "f9"
    hotkey_funny: str = "f10"
    hotkey_big_play: str = "f11"
    hotkey_story: str = "f12"
    hotkey_undo: str = "f8"
    # Two taps of the same key inside this window mark an explicit range
    # instead of two separate points.
    double_tap_seconds: float = 1.5
    padding: dict[str, CategoryPadding] = field(
        default_factory=lambda: {
            Category.GENERAL.value: CategoryPadding(30.0, 10.0),
            Category.FUNNY.value: CategoryPadding(20.0, 8.0),
            # A big play needs the build-up, not just the payoff.
            Category.BIG_PLAY.value: CategoryPadding(45.0, 15.0),
            Category.STORY.value: CategoryPadding(60.0, 20.0),
        }
    )

    def padding_for(self, category: Category) -> CategoryPadding:
        return self.padding.get(category.value, CategoryPadding())


@dataclass
class ChatDetect:
    enabled: bool = True
    bin_seconds: float = 3.0
    # Rolling baseline window. Long enough to ride out a lull, short enough
    # to track a chat that wakes up mid-stream -- this is what makes the
    # detector self-calibrating when chat speed varies night to night.
    baseline_seconds: float = 600.0
    z_threshold: float = 2.5
    min_messages: int = 4
    max_score: float = 40.0
    laugh_emotes: list[str] = field(
        default_factory=lambda: [
            "KEKW", "LULW", "OMEGALUL", "LUL", "ICANT", "KEKWlaugh", "PepeLaugh",
        ]
    )
    hype_emotes: list[str] = field(
        default_factory=lambda: [
            "PogChamp", "POGGERS", "Pog", "PogU", "EZClap", "Clap", "GIGACHAD", "W",
        ]
    )
    clip_phrases: list[str] = field(
        default_factory=lambda: ["clip that", "clipped", "clip it", "!clip"]
    )
    laugh_weight: float = 3.0
    hype_weight: float = 2.5
    clip_weight: float = 6.0
    # Chat reacts *after* the thing happens. Without this the clip opens on
    # the reaction and misses whatever caused it.
    reaction_lag: float = 4.0
    pre_roll: float = 22.0
    post_roll: float = 6.0


@dataclass
class AudioDetect:
    enabled: bool = True
    frame_seconds: float = 0.1
    baseline_seconds: float = 300.0
    # dB above the rolling baseline that counts as a spike.
    spike_db: float = 8.0
    min_spike_seconds: float = 0.6
    # A quiet stretch followed by a burst is a strong reaction signal.
    silence_db: float = -45.0
    silence_lookback_seconds: float = 3.0
    max_score: float = 25.0
    pre_roll: float = 15.0
    post_roll: float = 6.0


@dataclass
class TranscriptDetect:
    enabled: bool = True
    model: str = "large-v3-turbo"
    device: str = "cuda"
    compute_type: str = "int8_float16"
    # Only the neighbourhood of an existing candidate is transcribed. Running
    # Whisper over a full 8-hour recording would cost hours for no gain.
    window_pre: float = 60.0
    window_post: float = 30.0
    beam_size: int = 5


@dataclass
class LLMDetect:
    enabled: bool = True
    model: str = "claude-opus-5"
    max_score: float = 30.0
    api_key_env: str = "ANTHROPIC_API_KEY"
    generate_titles: bool = True
    tighten_bounds: bool = True


@dataclass
class DetectConfig:
    chat: ChatDetect = field(default_factory=ChatDetect)
    audio: AudioDetect = field(default_factory=AudioDetect)
    transcript: TranscriptDetect = field(default_factory=TranscriptDetect)
    llm: LLMDetect = field(default_factory=LLMDetect)
    marker_score: float = 100.0
    match_result_max_score: float = 30.0
    # Candidates closer than this get merged into one clip.
    merge_gap_seconds: float = 8.0
    min_clip_seconds: float = 5.0
    max_clip_seconds: float = 180.0
    # Trim silence/dead air off the ends once bounds are known.
    tighten_dead_air: bool = True
    dead_air_db: float = -40.0


@dataclass
class FacecamBox:
    """Facecam position in the SOURCE frame, in pixels.

    Calibrated once from a screenshot. Nick's facecam sits bottom-left; the
    defaults below assume 2560x1440 and a 480x270 cam, and are placeholders
    until the real screenshot lands.
    """

    x: int = 24
    y: int = 1146
    width: int = 480
    height: int = 270
    calibrated: bool = False


@dataclass
class VerticalLayout:
    width: int = 1080
    height: int = 1920
    # Gameplay occupies the top; facecam sits beneath it.
    gameplay_height_fraction: float = 0.625
    # Horizontal centre of the gameplay crop, 0..1. Nudge if the action
    # consistently sits off-centre.
    gameplay_center_x: float = 0.5
    facecam: FacecamBox = field(default_factory=FacecamBox)
    facecam_fit: str = "cover"  # cover | contain
    background: str = "black"


@dataclass
class CaptionStyle:
    enabled: bool = True
    font: str = "Arial"
    font_size: int = 72
    primary_color: str = "&H00FFFFFF"
    outline_color: str = "&H00000000"
    outline: int = 4
    shadow: int = 2
    # Distance from the bottom of the vertical frame.
    margin_v: int = 260
    max_chars_per_line: int = 24
    uppercase: bool = True


@dataclass
class RenderConfig:
    encoder: str = "hevc_nvenc"
    # Falls back automatically when NVENC is unavailable (e.g. running the
    # analysis pass on a machine without the GPU).
    fallback_encoder: str = "libx264"
    preset: str = "p5"
    cq: int = 21
    audio_bitrate: str = "192k"
    # Platform loudness target. -14 LUFS integrated is the YouTube/TikTok
    # normalisation point; going louder just gets turned back down.
    loudness_i: float = -14.0
    loudness_tp: float = -1.5
    loudness_lra: float = 11.0
    # Stream copy is much faster but can only cut on keyframes, so the clip
    # starts up to a GOP early -- which throws away the exact in/out points
    # chosen in the review UI. Off by default; turn it on only for rough cuts.
    horizontal_stream_copy: bool = False
    two_pass_loudness: bool = True
    captions: CaptionStyle = field(default_factory=CaptionStyle)
    vertical: VerticalLayout = field(default_factory=VerticalLayout)
    outputs: list[str] = field(default_factory=lambda: ["horizontal", "vertical"])


@dataclass
class GameProfile:
    """One game's own settings.

    Games are kept apart rather than merged because almost nothing carries
    between them: the summary screens differ, the vocabulary differs, and a
    prompt describing the wrong game actively degrades reranking. Detection
    tiers 1 and 2 (chat and mic energy) are genuinely game-agnostic and stay
    shared.
    """

    label: str = ""
    # Fed to the LLM reranker so it knows what it is looking at. Left blank,
    # the prompt stays generic rather than naming the wrong game.
    description: str = ""
    # Which section of regions.yaml holds this game's screens. Defaults to
    # the profile's own key.
    regions_key: str = ""


@dataclass
class StatsConfig:
    enabled: bool = True
    regions_file: str = "config/regions.yaml"
    # Summary screens persist for 10-30s, so half-fps sampling cannot miss
    # one while keeping an 8-hour scan to minutes.
    sample_fps: float = 0.5
    anchor_threshold: float = 0.82
    ocr_lang: str = "en"
    use_gpu: bool = True
    # A strong match result retroactively marks the preceding window as
    # likely clip-worthy even with no hotkey press.
    result_signal_enabled: bool = True
    result_signal_pre: float = 90.0
    result_kill_threshold: int = 4


@dataclass
class RetentionConfig:
    enabled: bool = True
    raw_recording_days: int = 21
    work_files_days: int = 7
    # Never delete anything that has not been through the review UI.
    require_reviewed: bool = True


@dataclass
class UIConfig:
    host: str = "127.0.0.1"
    port: int = 8723
    open_browser: bool = True


@dataclass
class Config:
    # The active game. Deliberately blank by default: there is no house game,
    # and guessing wrong silently mis-reads summary screens and mis-prompts
    # the reranker. Commands that need it ask, or take --game.
    game: str = ""
    games: dict[str, GameProfile] = field(
        default_factory=lambda: {
            "rust": GameProfile(
                label="Rust",
                description=(
                    "A hardcore multiplayer survival sandbox: players gather "
                    "resources, build and raid bases, and fight over loot on a "
                    "persistent wipe-cycle server. Losing a base or a kit is as "
                    "postable as taking one."
                ),
            ),
            "mistfall": GameProfile(
                label="Mistfall Hunter",
                description=(
                    "A dark-fantasy PvPvE extraction ARPG: players drop in, "
                    "fight monsters and rival squads for loot, and only keep "
                    "what they carry out through an extraction point. Losing a "
                    "full kit is as postable as winning one."
                ),
            ),
            "fc26": GameProfile(
                label="EA Sports FC 26",
                description=(
                    "A football simulation played in matches against other "
                    "players. Goals, near-misses, comebacks and refereeing "
                    "decisions are the postable moments."
                ),
            ),
        }
    )
    paths: Paths = field(default_factory=Paths)
    twitch: TwitchConfig = field(default_factory=TwitchConfig)
    tracks: AudioTracks = field(default_factory=AudioTracks)
    markers: MarkerConfig = field(default_factory=MarkerConfig)
    detect: DetectConfig = field(default_factory=DetectConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    stats: StatsConfig = field(default_factory=StatsConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    ui: UIConfig = field(default_factory=UIConfig)

    @property
    def profile(self) -> GameProfile | None:
        """The active game's profile, or None when no game is selected."""
        return self.games.get(self.game) if self.game else None

    @property
    def regions_key(self) -> str:
        """Which section of regions.yaml the active game reads."""
        profile = self.profile
        if profile and profile.regions_key:
            return profile.regions_key
        return self.game

    def known_games(self) -> list[str]:
        return sorted(self.games)

    @property
    def oauth_token(self) -> str:
        return os.environ.get(self.twitch.oauth_token_env, "")

    @property
    def anthropic_key(self) -> str:
        return os.environ.get(self.detect.llm.api_key_env, "")


def _build(cls: type, data: Any) -> Any:
    """Recursively construct nested dataclasses from plain dicts.

    Type hints are resolved with ``get_type_hints`` rather than read off
    ``field.type``: this module uses ``from __future__ import annotations``,
    so ``field.type`` is the *string* ``"RenderConfig"``, not the class. Using
    it directly makes every nested section load as a plain dict, and the
    failure is silent until something reads an attribute off it.
    """
    if not is_dataclass(cls) or not isinstance(data, dict):
        return data

    try:
        hints = get_type_hints(cls)
    except Exception:  # pragma: no cover - defensive
        hints = {}

    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        ftype = hints.get(f.name, f.type)
        if is_dataclass(ftype) and isinstance(value, dict):
            kwargs[f.name] = _build(ftype, value)  # type: ignore[arg-type]
        elif get_origin(ftype) is dict and isinstance(value, dict):
            args = get_args(ftype)
            if len(args) == 2 and is_dataclass(args[1]):
                kwargs[f.name] = {k: _build(args[1], v) for k, v in value.items()}
            else:
                kwargs[f.name] = value
        else:
            kwargs[f.name] = value
    return cls(**kwargs)


def load_config(path: str | Path | None = None) -> Config:
    """Load YAML config, falling back to defaults for anything unset."""
    target = Path(path) if path else DEFAULT_CONFIG_PATH
    if not target.exists():
        return Config()
    with open(target, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return _build(Config, raw)


def dump_default_config(path: str | Path) -> None:
    """Write a fully-populated config file for a fresh install."""
    from dataclasses import asdict

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(asdict(Config()), fh, sort_keys=False, default_flow_style=False)
