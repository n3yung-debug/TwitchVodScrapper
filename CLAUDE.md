# CLAUDE.md — TwitchVodScrapper

Project-scoped conventions. Anything here applies inside this repo only;
cross-project facts belong in the Book of Truth instead.

Last updated: 2026-08-03.

---

## What this project is

A post-stream clip pipeline for Nick's Twitch channel. Hotkey markers during
the stream, a detection cascade afterwards, a local review UI, then rendered
files and native Twitch clips. **Nothing is ever posted automatically** — the
final publish step is always Nick's. Do not add auto-upload.

Target machine: 9800X3D / RTX 5070 Ti, Windows, Streamlabs Desktop.

**There is no house game.** Nick plays several and they coexist — see the
per-game sections below. Never assume one; if a task is game-specific and the
game isn't stated, ask which one.

---

## Invariants — do not break these without a conversation

1. **One canonical timeline: seconds from the start of the local recording.**
   Markers, chat, audio frames and OCR hits are all converted into it before
   anything is merged. `timing.py` owns the conversions and is deliberately
   over-tested — an error of a few seconds here silently mis-cuts every clip
   in a session and is invisible until you watch the output.

2. **Cuts come from the local recording, not the VOD.** Same machine, same
   clock, so marker → frame is exact subtraction with no drift to calibrate.
   It is also 30–40 Mbps against the VOD's ~6, which is what makes a vertical
   crop hold up. The VOD is used for exactly two things: chat, and native
   clip creation.

3. **The hotkey writes a timestamp, not a video.** No GPU, no encoder, no
   video work of any kind in the daemon. It must not be able to cause a
   stutter mid-stream.

4. **Nothing heavy runs while live.** `analyze` and every render want the GPU
   and will fight the stream for it. They are post-stream steps by design.

5. **Every stage degrades independently.** No VOD chat, no calibrated OCR
   regions, no GPU — the run still produces a ranked candidate list and puts
   what it could not do into `warnings`. Never fail a whole session because
   one optional signal was unavailable.

6. **Retention never deletes an unreviewed recording.** The VOD may already
   have expired, which makes the local file the only copy.

7. **Calibrated geometry lives in YAML, never in Python.** OCR boxes
   (`config/regions.yaml`) and the facecam box are read off screenshots. These
   games patch their UIs; a patch must mean redrawing boxes, not editing code.

8. **No game is ever inferred.** `config.game` defaults to blank, and every
   game-dependent path either takes an explicit `--game` or degrades and says
   so. Guessing reads one game's boxes against another's frame and prompts the
   reranker with the wrong genre — both produce confident nonsense rather than
   an error, which is the worst possible failure mode here.

---

## Conventions

- **Command construction is separate from execution.** Anything ending in
  `_command` returns a plain argument list, so filtergraphs can be unit-tested
  with no ffmpeg installed. Keep it that way.
- **Config is dataclasses + YAML.** `config.py` resolves types with
  `get_type_hints`, not `field.type` — this module uses
  `from __future__ import annotations`, so `field.type` is the *string*
  `"RenderConfig"`. Using it directly makes nested sections silently load as
  plain dicts and the failure only surfaces on attribute access.
- **Dry run by default for anything destructive or outward-facing.** `prune`
  needs `--delete`; `clip` needs `--create`. Both print exactly what they
  would do first.
- **Warnings are prose, not codes.** They are read by one person at 2am and
  should say what was skipped and what to do about it.
- **Tests cover the places where a silent error is expensive** — timing
  arithmetic, marker interpretation, the chat z-score, merge scoring, vertical
  layout geometry, caption timing, ffmpeg command construction, OCR number
  parsing, the retention guard, native clip reshaping, and calibration
  coordinate maths. Not coverage for its own sake.
- Comments explain *why*, especially where the non-obvious choice was
  deliberate. Don't narrate what the code already says.

---

## Games

Shared across all games: markers, chat velocity, mic energy, merge/ranking,
review UI, render, clips, retention. Per game: OCR regions, the reranker's
genre description, and what a "match result" means at all.

Adding a game is config-only — a `games:` entry in `config.yaml` and a section
under `games:` in `regions.yaml`. No code.

Each game names a **tally** (`stats/tally.py`) — the thing that turns its
ending screens into counted results. A game with no tally has its OCR stage
sit out entirely; it is never scored using another game's tally. Only the
`extraction` tally exists today.

### Rust
Survival sandbox, wipe cycles, base building and raiding. No per-match summary
screen the way an extraction game has one, so tier 4 has little to read —
what's worth OCR'ing is situational and currently uncalibrated. **No tally.**

### Mistfall Hunter
PvPvE extraction ARPG, launched 2026-07-29. The game the stats schema was
originally shaped around: `post_match` (kills, survived) and `stash` (stash
value, liquid) screens. Gold is read as first/last endpoints plus a delta,
never per match. Uncalibrated. **Tally: `extraction`** — the only one
implemented, and the only game whose results are counted today.

### EA Sports FC 26
Football. Its full-time screen shares nothing with the extraction games —
goals, not kills. Fields the summary schema doesn't know about land in
`MatchResult.extra`, so it can be tracked without the schema growing first.
Uncalibrated. **No tally** — calibrating its screens is not enough to make it
count; a tally has to be written for it.

---

## Open items

- **`twitch.vod_offset_is_start` is unvalidated (PENDING VALIDATION).**
  Twitch's API reference describes `vod_offset` as the clip start; several
  client libraries and forum posts describe it as the end. The difference
  offsets every native clip by its own length and cannot be resolved from
  documentation. `vodscrap verify-clip-offset` creates one throwaway clip to
  settle it. Until Nick has run it, `vodscrap clip` prints a reminder on every
  run and `doctor` reports it as a warning. **Do not quietly pick a side.**
- **No game's OCR regions are calibrated.** `config/regions.yaml` ships with
  every section uncalibrated, so the stats stage skips itself per game and
  says so. `vodscrap calibrate --game <game>` reads coordinates off a real
  frame; `vodscrap games` shows the state of each.
- **Only Mistfall Hunter is tallied** (decided 2026-08-03). Rust and FC 26
  will get their own ending screens and tallies later; until then their OCR
  stage sits out and says so. Do not generalise the summary schema
  speculatively — write each game's tally when that game's screens are
  actually being counted.
- **The facecam box is a placeholder** (`480x270` at `24,1146`, assuming
  2560x1440). Vertical renders crop the wrong area until it is measured.

---

## Things deliberately not done

- **No auto-posting.** See above.
- **Twitch Dual Format is not used.** It works, but costs horizontal stream
  bitrate and locks vertical framing at stream time, in exchange for a 6 Mbps
  source when the local recording is 30–40.
- **No live-combat HUD OCR.** Only the two static summary screens. Live HUD
  templates would break on the first balance patch.
- **Stream copy is off by default.** It only cuts on keyframes, throwing away
  the exact in/out points chosen in the review UI.
- **Whisper does not run over the full recording.** Tier 3 only looks at ±60s
  around candidates the cheaper tiers already surfaced; transcribing 8 hours
  would cost hours for no gain.
