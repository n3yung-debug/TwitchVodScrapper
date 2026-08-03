"""Housekeeping for the recording drive.

Raw recordings are the biggest thing this pipeline creates -- roughly 13 GB
per hour at 1440p, so 55-110 GB for a 4-8 hour session. An 8 TB drive holds
around 70 of those, which is plenty but not unlimited.

The guard that matters: a recording is never deleted unless its session has
actually been reviewed. Deleting the only copy of a stream because a cron
job fired is not a recoverable mistake, and the VOD may already have expired.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .ingest.recording import VIDEO_SUFFIXES


@dataclass
class PruneCandidate:
    path: Path
    age_days: float
    size_bytes: int
    reviewed: bool
    reason: str

    @property
    def size_gb(self) -> float:
        return self.size_bytes / (1024 ** 3)


def _session_reviewed(work_dir: Path, name: str) -> bool:
    """A session counts as reviewed once any candidate has been acted on."""
    session = work_dir / name / "session.json"
    if not session.exists():
        return False
    try:
        with open(session, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return False
    candidates = data.get("candidates") or []
    if not candidates:
        # Nothing was found, so there is nothing left to review.
        return True
    return any(c.get("status") in {"approved", "rejected"} for c in candidates)


def find_prunable(config: Config) -> list[PruneCandidate]:
    recordings = Path(config.paths.recordings_dir)
    work_dir = Path(config.paths.work_dir)
    if not recordings.exists():
        return []

    cutoff_days = config.retention.raw_recording_days
    now = time.time()
    out: list[PruneCandidate] = []

    for path in sorted(recordings.iterdir()):
        if path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        stat = path.stat()
        age_days = (now - stat.st_mtime) / 86400.0
        reviewed = _session_reviewed(work_dir, path.stem)

        if age_days < cutoff_days:
            reason = f"only {age_days:.1f} days old (keeping {cutoff_days})"
        elif config.retention.require_reviewed and not reviewed:
            reason = "old enough, but its session has not been reviewed yet"
        else:
            reason = f"{age_days:.1f} days old and reviewed"

        out.append(
            PruneCandidate(
                path=path,
                age_days=age_days,
                size_bytes=stat.st_size,
                reviewed=reviewed,
                reason=reason,
            )
        )
    return out


def prunable_only(config: Config) -> list[PruneCandidate]:
    cutoff = config.retention.raw_recording_days
    return [
        c for c in find_prunable(config)
        if c.age_days >= cutoff
        and (c.reviewed or not config.retention.require_reviewed)
    ]


def prune(config: Config, dry_run: bool = True) -> tuple[list[PruneCandidate], float]:
    """Delete eligible recordings. Returns (deleted, gigabytes_freed)."""
    targets = prunable_only(config)
    freed = sum(c.size_gb for c in targets)
    if dry_run:
        return targets, freed
    for candidate in targets:
        candidate.path.unlink()
    return targets, freed
