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


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check everything that can be checked before it costs a stream."""
    from .doctor import format_report, run_checks
    from .config import DEFAULT_CONFIG_PATH

    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    config = load_config(args.config)
    report = run_checks(config, config_path, online=args.online)
    print(format_report(report))
    return 1 if report.failed else 0


def _session_path(config: Config, args: argparse.Namespace) -> Path:
    from .pipeline import session_file

    if getattr(args, "session", None):
        return Path(args.session)
    recording = _resolve_recording(config, getattr(args, "recording", None))
    return session_file(config, recording)


def cmd_clip(args: argparse.Namespace) -> int:
    """Create native Twitch clips from reviewed candidates.

    Dry run by default, like ``prune``: this writes to Nick's public channel,
    and the bounds have already been reshaped to fit Twitch's 5-60s rule, so
    the plan is worth reading before it runs.
    """
    import json

    from .ingest.twitch import TwitchClient, TwitchError
    from .publish import (
        create_clips, format_plan, plan_clips, record_result, select_candidates,
    )
    from .pipeline import load_session

    config = load_config(args.config)
    path = _session_path(config, args)
    if not path.exists():
        raise SystemExit(f"no session at {path}; run 'vodscrap analyze' first")
    session = load_session(path)
    candidates = session.get("candidates", [])
    if not candidates:
        print("this session has no candidates")
        return 1

    vod_id = session.get("vod_id") or args.vod
    if not vod_id:
        raise SystemExit(
            "this session has no Twitch VOD attached, so there is nothing to clip "
            "from. That happens when the recording did not overlap a stream, when "
            "the VOD expired before 'analyze' ran, or when Twitch credentials were "
            "not configured at the time. Pass --vod <id> to name one explicitly."
        )

    if not config.twitch.client_id or not config.oauth_token:
        raise SystemExit(
            "needs twitch.client_id and the OAuth token env var "
            f"({config.twitch.oauth_token_env}), with the clips:edit scope"
        )

    indices = select_candidates(
        candidates,
        approved_only=not args.all,
        top=args.top,
        indices=args.index or None,
        include_clipped=args.force,
    )
    if not indices:
        print(
            "nothing to clip. Approve candidates in 'vodscrap review' first, "
            "or pass --all to clip every candidate, or --force to re-clip ones "
            "that already have a Twitch clip."
        )
        return 1

    client = TwitchClient(config.twitch.client_id, config.oauth_token)
    try:
        user_id = config.twitch.broadcaster_id or client.user_id(config.twitch.login)
    except TwitchError as exc:
        raise SystemExit(str(exc))

    # Prefer what the session recorded; fall back to asking Twitch, which is
    # what happens for sessions analysed before the VOD start was stored.
    vod_start = session.get("vod_start_utc")
    vod_duration = float(session.get("vod_duration") or 0.0)
    if vod_start:
        from datetime import datetime as _dt

        vod_start_utc = _dt.fromisoformat(vod_start)
    else:
        vod = client.video(vod_id)
        if vod is None:
            raise SystemExit(
                f"Twitch has no VOD {vod_id} -- it has probably expired. Native "
                "clips can only be cut from a VOD that still exists; the local "
                "renders are unaffected."
            )
        vod_start_utc, vod_duration = vod.created_at, vod.duration_seconds

    plans = plan_clips(
        session, config.twitch, indices,
        vod_start_utc=vod_start_utc, vod_duration=vod_duration,
    )
    usable = [p for p in plans if p.usable]

    print(f"VOD {vod_id}, {len(plans)} candidate(s) selected:\n")
    print(format_plan(plans))
    print()

    if not config.twitch.vod_offset_is_start:
        print("note: twitch.vod_offset_is_start is false, so offsets are sent as clip ENDs")
    print(
        "reminder: twitch.vod_offset_is_start has not been verified on this "
        "channel -- 'vodscrap verify-clip-offset' settles it with one throwaway clip"
    )

    if not args.create:
        print(f"\nwould create {len(usable)} clip(s). Re-run with --create to do it.")
        return 0
    if not usable:
        print("nothing usable to create")
        return 1

    print(f"\ncreating {len(usable)} clip(s) ...")
    created = 0
    for result in create_clips(
        client, plans,
        broadcaster_id=user_id,
        video_id=vod_id,
        vod_offset_is_start=config.twitch.vod_offset_is_start,
    ):
        if result.ok:
            created += 1
            record_result(candidates[result.plan.index], result)
            print(f"  #{result.plan.index:<3d} {result.url}")
        elif result.plan.usable:
            print(f"  #{result.plan.index:<3d} FAILED: {result.error}")
        # Skipped plans were already explained in the printed plan above.

        with open(path, "w", encoding="utf-8") as fh:
            json.dump(session, fh, indent=2)

    print(f"\ncreated {created} of {len(usable)}. Clips take 15-30s to become playable.")
    return 0 if created == len(usable) else 1


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Pull a frame and stamp coordinates on it, for filling in the YAML."""
    from .calibrate import (
        Box, collect_configured_boxes, crop_box, draw_boxes, draw_grid,
        draw_grid_ffmpeg, extract_frame,
    )
    from .media import MediaError

    config = load_config(args.config)
    recording = _resolve_recording(config, args.recording)
    out_dir = Path(args.out_dir) if args.out_dir else Path(config.paths.work_dir) / "calibration"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{int(args.at):06d}"

    frame = extract_frame(
        recording.path, args.at, out_dir / f"frame-{stamp}.png", ffmpeg=config.paths.ffmpeg
    )
    resolution = recording.resolution or (1920, 1080)
    print(f"frame at {args.at:.0f}s from {recording.name}  ({resolution[0]}x{resolution[1]})")
    print(f"  {frame}")

    if args.mode == "grid":
        dest = out_dir / f"grid-{stamp}.png"
        if args.no_labels:
            draw_grid_ffmpeg(frame, dest, step=args.step, ffmpeg=config.paths.ffmpeg)
        else:
            try:
                draw_grid(frame, dest, step=args.step)
            except MediaError as exc:
                print(f"  ({exc})")
                draw_grid_ffmpeg(frame, dest, step=args.step, ffmpeg=config.paths.ffmpeg)
        print(f"  {dest}")
        print(
            f"\nRead x,y,w,h off the grid ({args.step}px cells, labelled every "
            f"{args.step * 5}px), then fill in {config.stats.regions_file} "
            f"with source_resolution: [{resolution[0]}, {resolution[1]}].\n"
            "Confirm a box with:  vodscrap calibrate crop --at "
            f"{int(args.at)} --box x,y,w,h"
        )
        return 0

    if args.mode == "crop":
        if not args.box:
            raise SystemExit("crop needs --box x,y,w,h")
        box = Box.parse(args.box)
        dest = out_dir / f"crop-{stamp}-{box.x}_{box.y}_{box.width}_{box.height}.png"
        crop_box(frame, box, dest, ffmpeg=config.paths.ffmpeg)
        print(f"  {dest}")
        print(
            "\nIf that shows exactly the value you want read and nothing else, "
            "the box is right. This is also how anchor images are made -- crop "
            "something that never changes, and save it next to regions.yaml."
        )
        return 0

    # mode == "check"
    boxes = collect_configured_boxes(config, resolution)
    if not boxes:
        print("\nnothing configured to draw yet -- start with 'calibrate grid'")
        return 1
    if args.box:
        boxes.append(("--box", Box.parse(args.box)))
    dest = out_dir / f"check-{stamp}.png"
    draw_boxes(frame, boxes, dest)
    print(f"  {dest}")
    print(f"\ndrew {len(boxes)} configured box(es). Anything landing in the wrong "
          "place is a number to fix, not a bug in the OCR.")
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

    p = sub.add_parser("doctor", help="check the install, paths, credentials and calibration")
    p.add_argument(
        "--online", action="store_true",
        help="also validate the Twitch token and its scopes against Twitch",
    )
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("clip", help="create native Twitch clips from approved candidates")
    p.add_argument("recording", nargs="?")
    p.add_argument("--session", help="path to a session.json")
    p.add_argument(
        "--create", action="store_true",
        help="actually create the clips (default is a dry run showing the plan)",
    )
    p.add_argument(
        "--all", action="store_true",
        help="clip every candidate, not just the ones approved in the review UI",
    )
    p.add_argument("--top", type=int, help="limit to the N highest-ranked selected candidates")
    p.add_argument(
        "--index", type=int, action="append",
        help="clip a specific candidate by index; repeatable",
    )
    p.add_argument(
        "--force", action="store_true",
        help="include candidates that already have a Twitch clip",
    )
    p.add_argument("--vod", help="VOD id, when the session did not record one")
    p.set_defaults(func=cmd_clip)

    p = sub.add_parser(
        "calibrate",
        help="pull a frame with a coordinate grid, to fill in regions.yaml",
    )
    p.add_argument(
        "mode", nargs="?", default="grid", choices=["grid", "crop", "check"],
        help="grid: labelled coordinates; crop: cut one box out; check: draw configured boxes",
    )
    # A flag rather than a positional: two optional positionals after a mode
    # choice makes "calibrate foo.mkv" parse as an invalid mode.
    p.add_argument("--recording", help="defaults to the newest recording")
    p.add_argument("--at", type=float, default=600.0, help="seconds into the recording")
    p.add_argument("--step", type=int, default=100, help="grid spacing in pixels")
    p.add_argument("--box", help="x,y,w,h for crop mode")
    p.add_argument("--out-dir", help="defaults to <work_dir>/calibration")
    p.add_argument("--no-labels", action="store_true", help="ffmpeg-only grid, no OpenCV")
    p.set_defaults(func=cmd_calibrate)

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
