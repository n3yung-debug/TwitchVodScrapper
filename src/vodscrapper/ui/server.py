"""Local review UI.

Nothing leaves the machine and nothing is published: the server renders
approved clips to a folder and stops there. That is the deliberate design --
an auto-post is public and permanent, and a review step costs seconds.

Previews are small transcoded proxies rather than the source file, for two
reasons: browsers will not play an HEVC MKV, and a proxy of one clip is
instant to seek where an eight-hour source is not. Each preview covers extra
context on both sides of the candidate so bounds can be widened, not just
narrowed.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..media import run
from ..render.ffmpeg import AudioSelection, RenderJob, Renderer
from ..render.layouts import plan_vertical

CONTEXT_PRE = 20.0
CONTEXT_POST = 15.0
PREVIEW_HEIGHT = 720

STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class RenderTask:
    index: int
    orientation: str
    status: str = "queued"
    output: str = ""
    error: str = ""


@dataclass
class ReviewState:
    session_path: Path
    data: dict[str, Any]
    config: Config
    tasks: list[RenderTask] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def source(self) -> str:
        return self.data["recording_path"]

    @property
    def resolution(self) -> tuple[int, int]:
        res = self.data.get("resolution") or [1920, 1080]
        return int(res[0]) or 1920, int(res[1]) or 1080

    @property
    def work_dir(self) -> Path:
        return self.session_path.parent

    def save(self) -> None:
        with self.lock:
            with open(self.session_path, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2)


def _preview_bounds(candidate: dict[str, Any], duration: float) -> tuple[float, float]:
    start = max(0.0, float(candidate["start"]) - CONTEXT_PRE)
    end = min(duration, float(candidate["end"]) + CONTEXT_POST)
    return start, min(end, start + 300.0)


def build_preview(state: ReviewState, index: int, orientation: str) -> Path:
    """Transcode a small proxy for one candidate, cached on disk."""
    candidate = state.data["candidates"][index]
    duration = float(state.data["duration"])
    start, end = _preview_bounds(candidate, duration)

    dest = state.work_dir / "previews" / f"{index:03d}-{orientation}.mp4"
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)

    width, height = state.resolution
    if orientation == "vertical":
        plan = plan_vertical(width, height, state.config.render.vertical)
        chains = [
            f"[0:v]{plan.gameplay.crop.as_filter()},"
            f"scale=-2:{int(PREVIEW_HEIGHT * plan.gameplay.out_height / plan.height)},setsar=1[g]"
        ]
        if plan.facecam is not None:
            cam_h = int(PREVIEW_HEIGHT * plan.facecam.out_height / plan.height)
            chains.append(
                f"[0:v]{plan.facecam.crop.as_filter()},scale=-2:{cam_h},setsar=1[c]"
            )
            chains.append("[g][c]vstack=inputs=2[v]")
        else:
            chains.append("[g]null[v]")
        video_filter = ";".join(chains)
        maps = ["-map", "[v]"]
    else:
        video_filter = f"[0:v]scale=-2:{PREVIEW_HEIGHT}[v]"
        maps = ["-map", "[v]"]

    # Previews always carry the mixed feed: this is for judging the moment,
    # not for choosing the final audio, which happens per clip at render time.
    track = max(0, state.config.tracks.mixed - 1)
    run([
        state.config.paths.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
        "-i", state.source,
        "-filter_complex", video_filter,
        *maps,
        "-map", f"0:a:{track}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(dest),
    ])
    return dest


def render_candidate(state: ReviewState, task: RenderTask) -> None:
    candidate = state.data["candidates"][task.index]
    renderer = Renderer(state.config)

    tracks = candidate.get("audio_tracks")
    selection = (
        AudioSelection(tracks=[int(t) for t in tracks])
        if tracks
        else AudioSelection.default(state.config.tracks, 4)
    )

    name = candidate.get("title") or f"clip-{task.index:03d}"
    safe = "".join(c for c in name if c.isalnum() or c in " -_").strip()[:60] or f"clip-{task.index:03d}"
    out_dir = Path(state.config.paths.output_dir) / task.orientation
    output = out_dir / f"{safe}.mp4"

    subtitles = None
    if state.config.render.captions.enabled and candidate.get("subtitles_path"):
        subtitles = candidate["subtitles_path"]

    job = RenderJob(
        source=state.source,
        start=float(candidate["start"]),
        end=float(candidate["end"]),
        output=str(output),
        orientation=task.orientation,
        audio=selection,
        subtitles=subtitles,
    )
    try:
        task.status = "rendering"
        renderer.render(job, state.resolution)
        task.status = "done"
        task.output = str(output)
    except Exception as exc:
        task.status = "failed"
        task.error = str(exc)


def serve(config: Config, session_path: str | Path) -> None:  # pragma: no cover - server
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            "the review UI needs fastapi and uvicorn: pip install 'vodscrapper[ui]'"
        ) from exc

    session_path = Path(session_path)
    with open(session_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    state = ReviewState(session_path=session_path, data=data, config=config)

    app = FastAPI(title="VOD Scrapper review")
    workers = threading.Semaphore(2)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/api/session")
    def session() -> JSONResponse:
        return JSONResponse({
            **state.data,
            "context_pre": CONTEXT_PRE,
            "context_post": CONTEXT_POST,
        })

    @app.get("/api/candidates/{index}/preview")
    def preview(index: int, orientation: str = "horizontal") -> FileResponse:
        if index < 0 or index >= len(state.data["candidates"]):
            raise HTTPException(status_code=404, detail="no such candidate")
        try:
            path = build_preview(state, index, orientation)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return FileResponse(path, media_type="video/mp4")

    @app.post("/api/candidates/{index}")
    async def update(index: int, payload: dict[str, Any]) -> JSONResponse:
        if index < 0 or index >= len(state.data["candidates"]):
            raise HTTPException(status_code=404, detail="no such candidate")
        candidate = state.data["candidates"][index]
        for key in ("start", "end", "title", "description", "status", "audio_tracks", "hashtags"):
            if key in payload:
                candidate[key] = payload[key]
        candidate["duration"] = max(0.0, float(candidate["end"]) - float(candidate["start"]))
        state.save()
        return JSONResponse(candidate)

    @app.post("/api/candidates/{index}/approve")
    async def approve(index: int, payload: dict[str, Any]) -> JSONResponse:
        if index < 0 or index >= len(state.data["candidates"]):
            raise HTTPException(status_code=404, detail="no such candidate")
        orientations = payload.get("orientations") or config.render.outputs
        state.data["candidates"][index]["status"] = "approved"
        state.save()

        created: list[dict[str, Any]] = []
        for orientation in orientations:
            task = RenderTask(index=index, orientation=orientation)
            state.tasks.append(task)
            created.append({"index": index, "orientation": orientation})

            def worker(t: RenderTask = task) -> None:
                with workers:
                    render_candidate(state, t)

            threading.Thread(target=worker, daemon=True).start()
        return JSONResponse({"queued": created})

    @app.get("/api/queue")
    def queue() -> JSONResponse:
        return JSONResponse([
            {
                "index": t.index,
                "orientation": t.orientation,
                "status": t.status,
                "output": t.output,
                "error": t.error,
            }
            for t in state.tasks
        ])

    url = f"http://{config.ui.host}:{config.ui.port}/"
    print(f"[ui] review at {url}")
    if config.ui.open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=config.ui.host, port=config.ui.port, log_level="warning")
