"""Command line entry points."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config, dump_default_config, load_config
from .ingest.recording import find_recordings, latest_recording


def _resolve_recording(config: Config, given: str | None):
    if given:
        from .ingest.recording import load_recording

        return load_recording(given, ffprobe=config.paths.ffprobe)
    recording = latest_recording(config.paths.recordings_dir, ffprobe=config.paths.ffprobe)
    if recording is None:
        raise SystemExit(
            f"no recordings found in {config.paths.recordings_dir}. "
            "Check paths.recordings_dir in your config."
        )
    return recording


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.output)
    if target.exists() and not args.force:
        print(f"{target} already exists; pass --force to overwrite")
        return 1
    dump_default_config(target)
    print(f"wrote {target}")
    print("Next: set paths.recordings_dir, twitch.client_id, and twitch.login.")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    from .markers.daemon import MarkerDaemon, run_tray

    config = load_config(args.config)
    if args.no_tray:
        MarkerDaemon(config).run_forever()
    else:
        run_tray(config)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    recordings = find_recordings(config.paths.recordings_dir, ffprobe=config.paths.ffprobe)
    if not recordings:
        print(f"no recordings in {config.paths.recordings_dir}")
        return 1
    for recording in recordings:
        res = recording.resolution
        estimated = " (start estimated)" if recording.start_is_estimated else ""
        print(
            f"{recording.name}  {recording.duration / 3600:5.2f}h  "
            f"{res[0]}x{res[1] if res else '?'}{estimated}"
        )
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    from .pipeline import analyze, format_report, session_file

    config = load_config(args.config)
    recording = _resolve_recording(config, args.recording)
    print(f"analysing {recording.name} ...")
    result = analyze(config, recording.path)
    print()
    print(format_report(result))
    print()
    print(f"session written to {session_file(config, recording)}")
    print("Review it with:  vodscrap review")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    from .pipeline import session_file
    from .ui import serve

    config = load_config(args.config)
    if args.session:
        path = Path(args.session)
    else:
        recording = _resolve_recording(config, args.recording)
        path = session_file(config, recording)
    if not path.exists():
        raise SystemExit(f"no session at {path}; run 'vodscrap analyze' first")
    serve(config, path)
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    from .pipeline import load_session, session_file
    from .models import MatchResult, SessionSummary, WealthReading
    from .stats.summary import format_summary

    config = load_config(args.config)
    if args.session:
        path = Path(args.session)
    else:
        recording = _resolve_recording(config, args.recording)
        path = session_file(config, recording)
    if not path.exists():
        raise SystemExit(f"no session at {path}; run 'vodscrap analyze' first")

    data = load_session(path)
    raw = data.get("summary")
    if not raw:
        print("no stats recorded for this session.")
        print(
            "Post-match stats need calibrated regions -- see "
            f"{config.stats.regions_file} and docs/calibration.md."
        )
        return 1

    summary = SessionSummary(
        matches=raw.get("matches", 0),
        extracted=raw.get("extracted", 0),
        died=raw.get("died", 0),
        total_kills=raw.get("total_kills", 0),
        best_match_index=raw.get("best_match_index"),
        best_match_kills=raw.get("best_match_kills"),
        notes=raw.get("notes", []),
    )
    if raw.get("wealth_start"):
        summary.wealth_start = WealthReading(**{
            k: v for k, v in raw["wealth_start"].items() if k != "total"
        })
    if raw.get("wealth_end"):
        summary.wealth_end = WealthReading(**{
            k: v for k, v in raw["wealth_end"].items() if k != "total"
        })
    summary.results = [MatchResult(**r) for r in raw.get("results", [])]
    print(format_summary(summary))
    return 0


def cmd_remux(args: argparse.Namespace) -> int:
    from .media import remux

    config = load_config(args.config)
    src = Path(args.input)
    dest = Path(args.output) if args.output else src.with_suffix(".mp4")
    print(f"remuxing {src.name} -> {dest.name} (no re-encode)")
    remux(src, dest, ffmpeg=config.paths.ffmpeg)
    print("done")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    from .retention import find_prunable, prune

    config = load_config(args.config)
    if args.all:
        for candidate in find_prunable(config):
            mark = "DELETE" if candidate.age_days >= config.retention.raw_recording_days and (
                candidate.reviewed or not config.retention.require_reviewed
            ) else "keep  "
            print(f"{mark}  {candidate.path.name:50s} {candidate.size_gb:7.1f} GB  {candidate.reason}")
        return 0

    targets, freed = prune(config, dry_run=not args.delete)
    if not targets:
        print("nothing eligible for deletion")
        return 0
    for candidate in targets:
        print(f"{candidate.path.name}  {candidate.size_gb:.1f} GB  {candidate.reason}")
    verb = "deleted" if args.delete else "would delete"
    print(f"\n{verb} {len(targets)} recording(s), {freed:.1f} GB")
    if not args.delete:
        print("re-run with --delete to actually remove them")
    return 0


def cmd_verify_clip_offset(args: argparse.Namespace) -> int:
    """Settle what Twitch's vod_offset parameter actually means.

    Twitch's own API reference describes vod_offset as the clip START; several
    client libraries and forum posts describe it as the END. The difference
    shifts every native clip by its own length, and it cannot be resolved from
    documentation -- so this creates one throwaway clip at a known offset and
    reports what Twitch says it did.
    """
    from .ingest.twitch import TwitchClient

    config = load_config(args.config)
    if not config.twitch.client_id or not config.oauth_token:
        raise SystemExit(
            "needs twitch.client_id and the OAuth token env var "
            f"({config.twitch.oauth_token_env}), with the clips:edit scope"
        )

    client = TwitchClient(config.twitch.client_id, config.oauth_token)
    user_id = config.twitch.broadcaster_id or client.user_id(config.twitch.login)
    vods = client.videos(user_id, first=1)
    if not vods:
        raise SystemExit("no VODs on this channel to test against")

    vod = vods[0]
    requested_start = float(args.at)
    duration = float(args.duration)
    print(f"VOD {vod.id} ({vod.title}), {vod.duration_seconds:.0f}s long")
    print(f"asking for a {duration:.0f}s clip starting at {requested_start:.0f}s")
    print(f"assuming vod_offset_is_start = {config.twitch.vod_offset_is_start}")

    created = client.create_clip_from_vod(
        broadcaster_id=user_id,
        video_id=vod.id,
        start_offset=requested_start,
        duration=duration,
        vod_offset_is_start=config.twitch.vod_offset_is_start,
    )
    clip_id = created.get("id")
    if not clip_id:
        raise SystemExit(f"Twitch did not return a clip id: {created}")

    print(f"\ncreated clip {clip_id}")
    print(f"edit URL: {created.get('edit_url', '(none returned)')}")
    print(
        "\nWatch it, then check where it actually starts:\n"
        f"  - if it begins at ~{requested_start:.0f}s, vod_offset_is_start = true (current setting is correct)\n"
        f"  - if it begins at ~{requested_start - duration:.0f}s, set twitch.vod_offset_is_start = false\n"
        "Clip creation is asynchronous, so give it 15-30 seconds before checking."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vodscrap",
        description="Turn a Twitch stream into clips worth posting.",
    )
    parser.add_argument("--config", default=None, help="path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="write a default config file")
    p.add_argument("-o", "--output", default="config/config.yaml")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("daemon", help="run the hotkey marker daemon while streaming")
    p.add_argument("--no-tray", action="store_true", help="console only, no tray icon")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("list", help="list recordings found on disk")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("analyze", help="find clip candidates in a recording")
    p.add_argument("recording", nargs="?", help="defaults to the newest recording")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("review", help="open the review UI")
    p.add_argument("recording", nargs="?")
    p.add_argument("--session", help="path to a session.json")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("summary", help="print the session stats summary")
    p.add_argument("recording", nargs="?")
    p.add_argument("--session")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("remux", help="losslessly convert a crash-safe MKV to MP4")
    p.add_argument("input")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_remux)

    p = sub.add_parser("prune", help="delete old, already-reviewed recordings")
    p.add_argument("--delete", action="store_true", help="actually delete (default is a dry run)")
    p.add_argument("--all", action="store_true", help="show every recording and why it is kept")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser(
        "verify-clip-offset",
        help="one-off test that settles what Twitch's vod_offset means",
    )
    p.add_argument("--at", type=float, default=600.0, help="requested clip start, seconds into the VOD")
    p.add_argument("--duration", type=float, default=30.0)
    p.set_defaults(func=cmd_verify_clip_offset)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
