"""Durable marker storage.

Append-only JSONL, flushed and fsynced on every press. A stream can run
eight hours and the machine can crash at hour seven; the markers written up
to that point must survive. Undo rewrites via a temp file and atomic
replace so a crash mid-undo cannot truncate the log.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..models import Marker


class MarkerStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, marker: Marker) -> None:
        """Write one marker, durably.

        fsync on every press is deliberate. Presses are rare (tens per
        stream) so the cost is irrelevant, and the alternative is losing the
        buffer on a hard crash -- which is exactly when you most want the
        markers.
        """
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(marker.to_json() + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def read_all(self) -> list[Marker]:
        if not self.path.exists():
            return []
        out: list[Marker] = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(Marker.from_json(line))
                except Exception as exc:  # pragma: no cover - defensive
                    # A single corrupt line must not cost the whole session.
                    print(f"[markers] skipping unreadable line {lineno}: {exc}")
        return out

    def undo(self) -> Marker | None:
        """Remove and return the most recent marker.

        Used by the undo hotkey when a press was a fumble or the wrong
        category. Returns ``None`` when there is nothing left to undo.
        """
        markers = self.read_all()
        if not markers:
            return None
        removed = markers.pop()
        self._rewrite(markers)
        return removed

    def _rewrite(self, markers: list[Marker]) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".jsonl.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for marker in markers:
                    fh.write(marker.to_json() + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def between(self, start_utc: datetime, end_utc: datetime) -> list[Marker]:
        """Markers falling inside a recording's wall-clock window."""
        start_utc = start_utc.astimezone(timezone.utc)
        end_utc = end_utc.astimezone(timezone.utc)
        return [
            m for m in self.read_all()
            if start_utc <= m.at_utc.astimezone(timezone.utc) <= end_utc
        ]

    def archive(self, session_name: str) -> Path | None:
        """Move consumed markers aside after a session is processed.

        Keeps the live file short and preserves a per-session record.
        """
        if not self.path.exists():
            return None
        dest = self.path.parent / f"{self.path.stem}.{session_name}{self.path.suffix}"
        os.replace(self.path, dest)
        return dest
