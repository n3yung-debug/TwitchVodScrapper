"""The hotkey daemon that runs while Nick is live.

Design constraint that drives everything here: **this must not be capable of
causing a stutter.** So it never touches the GPU, never touches the encoder,
and never handles video. A press captures a timestamp and hands it to a
worker thread; the hotkey callback itself does no I/O at all.

The disk write is a single short line, and even that happens off the
callback thread so that fsync latency can never sit in the input path.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config, load_config
from ..models import Category, Marker, MarkerKind
from .store import MarkerStore

try:  # pragma: no cover - platform dependent
    import keyboard  # type: ignore
except Exception:  # pragma: no cover
    keyboard = None  # type: ignore


@dataclass
class _Press:
    at_utc: datetime
    monotonic: float
    category: Category | None  # None == undo


class MarkerDaemon:
    """Global-hotkey marker recorder.

    Usage: ``vodscrap daemon``. Leave it running for the whole stream.
    """

    def __init__(self, config: Config | None = None, on_event=None):
        self.config = config or load_config()
        self.store = MarkerStore(self.config.paths.markers_file)
        self._queue: "queue.Queue[_Press | None]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._count = 0
        # Injected by the tray so the worker can surface feedback without
        # importing UI code.
        self._on_event = on_event or (lambda msg: print(f"[markers] {msg}"))

    # -- hotkey path -------------------------------------------------------
    # Everything below runs on the keyboard library's thread. Keep it to a
    # timestamp read and a queue put; no locks, no I/O, no formatting.

    def _press(self, category: Category | None) -> None:
        self._queue.put(
            _Press(
                at_utc=datetime.now(timezone.utc),
                monotonic=time.monotonic(),
                category=category,
            )
        )

    # -- worker ------------------------------------------------------------

    def _drain(self) -> None:
        while not self._stop.is_set():
            item = self._queue.get()
            if item is None:
                break
            try:
                self._handle(item)
            except Exception as exc:  # pragma: no cover - defensive
                self._on_event(f"error recording press: {exc}")

    def _handle(self, press: _Press) -> None:
        if press.category is None:
            removed = self.store.undo()
            if removed is None:
                self._on_event("undo: nothing to remove")
            else:
                self._count = max(0, self._count - 1)
                local = removed.at_utc.astimezone()
                self._on_event(
                    f"undo: removed {removed.category.value} marker "
                    f"at {local:%H:%M:%S} ({self._count} left)"
                )
            return

        marker = Marker(
            at_utc=press.at_utc,
            kind=MarkerKind.POINT,
            category=press.category,
            monotonic=press.monotonic,
        )
        self.store.append(marker)
        self._count += 1
        local = press.at_utc.astimezone()
        self._on_event(
            f"marked {press.category.value} at {local:%H:%M:%S} (#{self._count})"
        )

    # -- lifecycle ---------------------------------------------------------

    def bindings(self) -> list[tuple[str, Category | None, str]]:
        m = self.config.markers
        return [
            (m.hotkey_point, Category.GENERAL, "mark moment"),
            (m.hotkey_funny, Category.FUNNY, "mark funny"),
            (m.hotkey_big_play, Category.BIG_PLAY, "mark big play"),
            (m.hotkey_story, Category.STORY, "mark story"),
            (m.hotkey_undo, None, "undo last marker"),
        ]

    def start(self) -> None:
        if keyboard is None:
            raise RuntimeError(
                "the 'keyboard' package is required for the daemon: "
                "pip install 'vodscrapper[daemon]' (Windows: run as administrator "
                "so global hotkeys register while a game has focus)"
            )
        self._worker = threading.Thread(target=self._drain, name="marker-writer", daemon=True)
        self._worker.start()

        for key, category, label in self.bindings():
            keyboard.add_hotkey(key, self._press, args=(category,), suppress=False)
            self._on_event(f"bound {key.upper()} -> {label}")

        existing = len(self.store.read_all())
        self._count = existing
        if existing:
            self._on_event(f"resuming with {existing} marker(s) already logged")
        self._on_event(f"writing to {self.store.path}")

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)
        if keyboard is not None:  # pragma: no cover
            try:
                keyboard.unhook_all_hotkeys()
            except Exception:
                pass
        if self._worker is not None:
            self._worker.join(timeout=2.0)

    def run_forever(self) -> None:  # pragma: no cover - blocking
        self.start()
        self._on_event("daemon running; Ctrl+C to stop")
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()


def run_tray(config: Config | None = None) -> None:  # pragma: no cover - UI
    """Run the daemon with a system-tray icon and press notifications."""
    cfg = config or load_config()
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception:
        MarkerDaemon(cfg).run_forever()
        return

    def make_icon(count: int) -> "Image.Image":
        img = Image.new("RGB", (64, 64), (18, 18, 22))
        draw = ImageDraw.Draw(img)
        draw.ellipse((10, 10, 54, 54), fill=(145, 70, 255))
        label = str(count) if count < 100 else "99+"
        draw.text((32, 32), label, fill=(255, 255, 255), anchor="mm")
        return img

    icon = pystray.Icon("vodscrap", make_icon(0), "VOD Scrapper — markers")
    daemon: MarkerDaemon

    def on_event(msg: str) -> None:
        print(f"[markers] {msg}")
        try:
            icon.icon = make_icon(daemon._count)
            icon.title = f"VOD Scrapper — {daemon._count} marker(s)"
            icon.notify(msg, "VOD Scrapper")
        except Exception:
            pass

    daemon = MarkerDaemon(cfg, on_event=on_event)

    def setup(ic) -> None:
        ic.visible = True
        daemon.start()

    icon.menu = pystray.Menu(
        pystray.MenuItem("Undo last marker", lambda: daemon._press(None)),
        pystray.MenuItem("Quit", lambda: (daemon.stop(), icon.stop())),
    )
    icon.run(setup)
