"""First-run and pre-stream self-check.

Every failure this catches is one that would otherwise surface at the worst
possible moment: a missing ffmpeg after an eight-hour stream, an expired
OAuth token when chat is the strongest signal you have, an NVENC-less build
discovered halfway through a render queue. Checking takes two seconds and
the answers are all knowable in advance.

Checks are ordered cheapest-first and each one degrades on its own -- a
missing optional dependency is a WARN because the pipeline is built to run
without it, while a missing ffmpeg is a FAIL because nothing works without
it. That distinction is the whole point: an all-red report that includes
things which do not matter tonight teaches you to ignore the report.
"""

from __future__ import annotations

import importlib.util
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .media import run

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", fix: str = "") -> None:
        self.checks.append(Check(name, status, detail, fix))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]


# Optional dependencies, grouped by the install extra that provides them and
# what stops working without them.
OPTIONAL_DEPS: list[tuple[str, str, str, str]] = [
    ("keyboard", "daemon", "hotkey markers", "pip install -e '.[daemon]'"),
    ("pystray", "daemon", "tray icon (the daemon still runs headless)", "pip install -e '.[daemon]'"),
    ("numpy", "analyze", "mic-energy detection", "pip install -e '.[analyze]'"),
    ("faster_whisper", "analyze", "transcription and captions", "pip install -e '.[analyze]'"),
    ("anthropic", "analyze", "LLM reranking and auto-titles", "pip install -e '.[analyze]'"),
    ("cv2", "ocr", "post-match stats and calibration overlays", "pip install -e '.[ocr]'"),
    ("paddleocr", "ocr", "reading numbers off summary screens", "pip install -e '.[ocr]'"),
    ("fastapi", "ui", "review UI", "pip install -e '.[ui]'"),
    ("uvicorn", "ui", "review UI", "pip install -e '.[ui]'"),
]


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _binary_version(binary: str) -> str:
    try:
        proc = run([binary, "-version"], check=False)
    except (FileNotFoundError, OSError):
        return ""
    first = (proc.stdout or proc.stderr).splitlines()
    return first[0].strip() if first else ""


def check_binaries(config: Config, report: Report) -> None:
    for label, binary in (("ffmpeg", config.paths.ffmpeg), ("ffprobe", config.paths.ffprobe)):
        resolved = shutil.which(binary)
        if not resolved:
            report.add(
                label, FAIL, f"not found on PATH (configured as {binary!r})",
                "Install ffmpeg and put it on PATH, or set paths.%s to the full "
                "path to the .exe" % label,
            )
            continue
        report.add(label, OK, _binary_version(binary)[:70] or resolved)


def check_encoder(config: Config, report: Report) -> None:
    if not shutil.which(config.paths.ffmpeg):
        return
    proc = run([config.paths.ffmpeg, "-hide_banner", "-encoders"], check=False)
    encoders = proc.stdout or ""
    wanted = config.render.encoder
    if wanted in encoders:
        report.add("encoder", OK, f"{wanted} available")
    elif config.render.fallback_encoder in encoders:
        report.add(
            "encoder", WARN,
            f"{wanted} is not in this ffmpeg build; renders will fall back to "
            f"{config.render.fallback_encoder} (CPU, much slower)",
            "Install a CUDA-enabled ffmpeg build to use the 5070 Ti for encoding",
        )
    else:
        report.add(
            "encoder", FAIL,
            f"neither {wanted} nor {config.render.fallback_encoder} is available",
            "This ffmpeg build cannot encode video; install a full build",
        )


def check_gpu(config: Config, report: Report) -> None:
    if not shutil.which("nvidia-smi"):
        report.add(
            "gpu", WARN, "nvidia-smi not found; cannot confirm the GPU is visible",
            "Only matters for NVENC renders and CUDA Whisper",
        )
        return
    proc = run(
        ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
        check=False,
    )
    name = (proc.stdout or "").strip().splitlines()
    report.add("gpu", OK, name[0] if name else "present")


def check_paths(config: Config, report: Report) -> None:
    recordings = Path(config.paths.recordings_dir)
    if not recordings.exists():
        report.add(
            "recordings_dir", FAIL, f"{recordings} does not exist",
            "Point paths.recordings_dir at the folder Streamlabs records into",
        )
    else:
        from .ingest.recording import VIDEO_SUFFIXES

        count = sum(
            1 for p in recordings.iterdir()
            if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES
        )
        status = OK if count else WARN
        report.add("recordings_dir", status, f"{recordings} ({count} recordings)")

    for label in ("work_dir", "output_dir"):
        target = Path(getattr(config.paths, label))
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".vodscrap-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            report.add(label, FAIL, f"{target} is not writable: {exc}")
            continue
        free_gb = shutil.disk_usage(target).free / (1024 ** 3)
        status = OK if free_gb >= 50 else WARN
        report.add(label, status, f"{target} ({free_gb:.0f} GB free)")

    markers = Path(config.paths.markers_file)
    try:
        markers.parent.mkdir(parents=True, exist_ok=True)
        with open(markers, "a", encoding="utf-8"):
            pass
        report.add("markers_file", OK, str(markers))
    except OSError as exc:
        report.add(
            "markers_file", FAIL, f"{markers} is not writable: {exc}",
            "The daemon appends every hotkey press here; without it markers are lost",
        )


def check_dependencies(report: Report) -> None:
    for module, extra, purpose, fix in OPTIONAL_DEPS:
        if _installed(module):
            continue
        report.add(f"dep:{module}", WARN, f"missing -- no {purpose}", fix)


def check_twitch(config: Config, report: Report, online: bool = False) -> None:
    if not config.twitch.client_id:
        report.add(
            "twitch.client_id", WARN, "not set -- chat detection and native clips are off",
            "Create an app at dev.twitch.tv/console/apps and set twitch.client_id",
        )
        return
    report.add("twitch.client_id", OK, config.twitch.client_id[:8] + "...")

    if not config.oauth_token:
        report.add(
            "twitch token", WARN,
            f"env var {config.twitch.oauth_token_env} is empty",
            "A user token with the clips:edit scope; see docs/streamlabs-setup.md",
        )
        return

    if not online:
        report.add(
            "twitch token", OK,
            f"{config.twitch.oauth_token_env} is set (re-run with --online to validate it)",
        )
        return

    from .ingest.twitch import TwitchClient, TwitchError

    try:
        client = TwitchClient(config.twitch.client_id, config.oauth_token)
        info = client.validate_token()
    except TwitchError as exc:
        report.add(
            "twitch token", FAIL, str(exc),
            "Re-authorise; Twitch user tokens expire",
        )
        return

    scopes = info.get("scopes") or []
    expires = info.get("expires_in")
    detail = f"valid for {info.get('login', '?')}"
    if expires:
        detail += f", expires in {int(expires) // 3600}h"
    report.add("twitch token", OK, detail)

    if config.twitch.create_native_clips and "clips:edit" not in scopes:
        report.add(
            "twitch scopes", FAIL,
            f"token is missing clips:edit (has: {', '.join(scopes) or 'none'})",
            "Native clip creation will 401; re-authorise with the clips:edit scope",
        )
    else:
        report.add("twitch scopes", OK, ", ".join(scopes) or "none")


def check_llm(config: Config, report: Report) -> None:
    if not config.detect.llm.enabled:
        report.add("anthropic key", OK, "LLM rerank disabled in config")
        return
    if config.anthropic_key:
        report.add("anthropic key", OK, f"{config.detect.llm.api_key_env} is set")
    else:
        report.add(
            "anthropic key", WARN,
            f"{config.detect.llm.api_key_env} is empty -- no LLM rerank or auto-titles",
            "Detection still works; the cascade just stops at tier 2",
        )


def check_calibration(config: Config, report: Report) -> None:
    from .stats.regions import load_regions

    if config.stats.enabled:
        regions = load_regions(config.stats.regions_file)
        if regions.calibrated:
            missing = [
                name for name in ("post_match", "stash") if name not in regions.screens
            ]
            if missing:
                report.add(
                    "regions", WARN,
                    f"calibrated, but no {', '.join(missing)} screen defined",
                )
            else:
                report.add(
                    "regions", OK,
                    f"{len(regions.screens)} screens at "
                    f"{regions.source_resolution[0]}x{regions.source_resolution[1]}",
                )
            for screen in regions.screens.values():
                anchor = regions.resolve(screen.anchor.image)
                if not anchor.exists():
                    report.add(
                        f"anchor:{screen.name}", FAIL,
                        f"anchor image {anchor} does not exist",
                        "Crop it out of a screenshot; see docs/calibration.md",
                    )
        else:
            report.add(
                "regions", WARN,
                f"{config.stats.regions_file} is not calibrated -- no kills, "
                "extract rate, or gold tracking",
                "vodscrap calibrate grid --at <seconds>, then docs/calibration.md",
            )

    facecam = config.render.vertical.facecam
    if "vertical" in config.render.outputs and not facecam.calibrated:
        report.add(
            "facecam", WARN,
            f"box is still the placeholder ({facecam.width}x{facecam.height} at "
            f"{facecam.x},{facecam.y}) -- vertical clips will crop the wrong area",
            "vodscrap calibrate grid --at <seconds> to read the real coordinates",
        )
    elif "vertical" in config.render.outputs:
        report.add("facecam", OK, f"{facecam.width}x{facecam.height} at {facecam.x},{facecam.y}")


def check_open_questions(config: Config, report: Report) -> None:
    """Surface things that are configured but not yet proven true."""
    if config.twitch.create_native_clips:
        report.add(
            "vod_offset semantics", WARN,
            f"twitch.vod_offset_is_start is set to {config.twitch.vod_offset_is_start} "
            "but has not been verified -- every native clip is offset by its own "
            "length if this is wrong",
            "vodscrap verify-clip-offset --at 600 --duration 30 (one throwaway clip)",
        )


def run_checks(config: Config, config_path: Path | None, online: bool = False) -> Report:
    report = Report()

    if config_path and config_path.exists():
        report.add("config", OK, str(config_path))
    else:
        report.add(
            "config", WARN,
            f"{config_path or 'config/config.yaml'} not found; using built-in defaults",
            "vodscrap init",
        )

    check_binaries(config, report)
    check_encoder(config, report)
    check_gpu(config, report)
    check_paths(config, report)
    check_dependencies(report)
    check_twitch(config, report, online=online)
    check_llm(config, report)
    check_calibration(config, report)
    check_open_questions(config, report)
    return report


def format_report(report: Report) -> str:
    width = max((len(c.name) for c in report.checks), default=10)
    lines = []
    for check in report.checks:
        lines.append(f"  [{check.status:4s}] {check.name:<{width}}  {check.detail}")
        if check.fix and check.status != OK:
            lines.append(f"           {' ' * width}  -> {check.fix}")

    failed, warned = len(report.failed), len(report.warned)
    lines.append("")
    if failed:
        lines.append(f"{failed} failure(s), {warned} warning(s). Fix the failures first.")
    elif warned:
        lines.append(
            f"No failures, {warned} warning(s). Everything warned about degrades "
            "gracefully -- the pipeline runs without it and says so."
        )
    else:
        lines.append("All checks passed.")
    return "\n".join(lines)
