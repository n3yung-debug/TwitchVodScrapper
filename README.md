# TwitchVodScrapper

Turns a Twitch stream into clips worth posting: hotkey markers while you play,
a detection pass afterwards, a local review UI, and rendered files in a folder.

Built for a 9800X3D / RTX 5070 Ti on Windows with Streamlabs Desktop.
Multi-game: Rust, Mistfall Hunter and FC 26 coexist, each with its own screen
regions, and the active one is always chosen explicitly. Nothing is posted
automatically — the last step is always yours.

---

## How it works

```
  while streaming            after the stream                 you decide
  ───────────────            ────────────────                 ──────────
  F9  mark moment      →     detect  →  rank  →  transcribe   →  review UI
  F10 funny                  ├ your markers      (candidates      trim, retitle,
  F11 big play               ├ chat spikes        only, never      mute a track,
  F12 story                  ├ mic energy         8 hours)         approve
  F8  undo                   └ match results                          ↓
                                                                  ffmpeg
  Streamlabs records                                          16:9 + 9:16 files
  locally in the background                                    in a folder
```

**The hotkey writes a timestamp, not a video.** No GPU, no encoder, no video
of any kind — it cannot cause a stutter. Clip length is decided later in the
UI, where you can see the footage, which is also how you get a different
length for every clip without deciding at press time.

**Cuts come from the local recording, not the VOD.** The recording and the
marker daemon run on the same machine off the same clock, so marker → frame is
exact subtraction with no drift to calibrate. It's also 30–40 Mbps instead of
the VOD's ~6, which is what makes a vertical crop hold up. The VOD is used for
one thing: chat.

---

## Install

```
pip install -e ".[daemon,analyze,ocr,ui]"
vodscrap init
vodscrap doctor
```

Then work through **[docs/streamlabs-setup.md](docs/streamlabs-setup.md)** —
recording settings, multi-track audio, hotkeys, credentials.

`vodscrap doctor` checks everything that is knowable in advance: ffmpeg and
NVENC, the GPU, paths and free space, which optional extras are installed,
the Twitch token and its scopes (`--online`), and whether the OCR regions and
facecam box have actually been calibrated. It separates **FAIL** (nothing
works without this) from **WARN** (degrades gracefully and says so), because
a report that flags everything is a report you learn to ignore.

| Extra | Pulls in | Needed for |
|---|---|---|
| `daemon` | `keyboard`, `pystray` | The hotkey daemon (streaming PC) |
| `analyze` | `numpy`, `faster-whisper`, `anthropic` | Audio detection, transcription, reranking |
| `ocr` | `opencv-python`, `paddleocr` | Post-match stats |
| `ui` | `fastapi`, `uvicorn` | The review UI |

Split so the marker daemon doesn't have to drag in CUDA.

---

## Daily use

```
vodscrap daemon      # leave running for the whole stream (as administrator)

# after the stream — never while live, this wants the GPU
vodscrap analyze
vodscrap review
```

| Command | What it does |
|---|---|
| `vodscrap init` | Write a default config |
| `vodscrap games` | Configured games and how far each is calibrated |
| `vodscrap doctor` | Check install, paths, credentials, calibration |
| `vodscrap daemon` | Hotkey marker daemon with a tray icon |
| `vodscrap list` | Recordings found on disk |
| `vodscrap analyze [file]` | Detection cascade; defaults to the newest recording |
| `vodscrap review` | Opens the review UI at `127.0.0.1:8723` |
| `vodscrap clip` | Native Twitch clips from approved candidates (dry run by default) |
| `vodscrap summary` | Session stats — kills, extract rate, gold |
| `vodscrap calibrate` | Pull a frame with a coordinate grid, to fill in `regions.yaml` |
| `vodscrap remux <file>` | Lossless MKV → MP4 |
| `vodscrap prune` | Delete old, already-reviewed recordings (dry run by default) |
| `vodscrap verify-clip-offset` | Settles Twitch's `vod_offset` ambiguity — run once |

---

## Games

Several games coexist. What's shared and what isn't falls out cleanly:

| Shared across every game | Per game |
|---|---|
| Hotkey markers, chat velocity, mic energy | OCR screen regions and anchors |
| Merge/ranking, review UI, render, clips | The genre description sent to the reranker |
| Retention, timing, calibration tooling | What a "match result" even means |

Tiers 0–2 don't care what you're playing. Tier 4 cares entirely.

```
vodscrap games                        # what's configured, and its state
vodscrap analyze --game rust
vodscrap calibrate grid --game fc26 --at 1800
```

Set `game:` in the config for whatever you play most, and pass `--game` when
it's something else. **There is no default and nothing is inferred** — a wrong
guess reads another game's boxes and prompts the reranker with the wrong
genre, and both fail quietly rather than erroring. With no game selected,
detection still runs on markers, chat and mic; only the OCR stage sits out,
and it says so.

Adding a game is two edits, no code: a `games:` entry in `config.yaml` (label
and a one-line genre description) and a section in `regions.yaml`.

---

## Detection, cheapest first

The ordering matters as much as the signals. Each tier only looks at what the
previous tiers surfaced.

| Tier | Signal | Cost | Notes |
|---|---|---|---|
| 0 | **Your hotkeys** | free | Highest precision by definition. Never dropped, never re-cut, always surfaced. |
| 1 | **Chat velocity** | free | Rolling **z-score**, not an absolute count — self-calibrates between a quiet night and a raid. Weighted for laugh emotes and "clip that". |
| 2 | **Mic energy** | seconds | Sustained jumps above your own rolling baseline; scores higher out of silence. The tier that works when chat is dead. |
| 3 | **Whisper + LLM** | minutes | Runs on **±60s around existing candidates only**. Transcribing 8 hours would cost hours for no gain. |
| 4 | **Match results** | minutes | OCR of the post-match screen. A 9-kill extraction says the previous 90s is worth watching. |

Overlapping detections merge by taking the **best score per source** and
summing across sources — so six small chat hits can't out-rank one deliberate
marker — with a bonus when two independent signals agree on the same moment.

---

## Review UI

Ranked list, with *why* each moment scored. Video player over a proxy that
includes 20s of context on each side, so bounds can be widened as well as
trimmed.

| Key | Action |
|---|---|
| `J` / `L` | Scrub back / forward |
| `K` or `Space` | Play / pause |
| `I` / `O` | Set in / out |
| `↑` / `↓` | Previous / next candidate |
| `A` / `X` | Approve / reject |

Per-clip audio track toggles (mic / game / party), a 16:9 ↔ 9:16 preview
switch, and a title field. Approving queues renders in the background.

---

## Output

- **16:9** — straight cut, re-encoded for frame-accurate bounds.
- **9:16 stacked** — gameplay center-cropped on top, facecam cropped from the
  bottom-left corner and rebuilt as its own panel beneath it.
- Burned-in captions from Whisper word timestamps.
- Loudness-normalised to −14 LUFS (two-pass), the YouTube/TikTok target.
- Per-clip audio muting — drop Discord, drop game music for copyright.

Files land in `output_dir/horizontal/` and `output_dir/vertical/`. Uploading
is yours.

---

## Native Twitch clips

The same reviewed bounds also make clips on the channel itself — the thing
that shows up in Twitch's own discovery and pastes into Discord as a link.

```
vodscrap clip                # dry run: shows exactly what it would create
vodscrap clip --create       # actually creates them
```

Defaults to the candidates you **approved** in the review UI, skips ones that
already have a clip, and never re-cuts silently. `--all` clips every
candidate, `--index N` picks specific ones, `--force` re-clips.

Two things get handled before anything is sent, and both are printed:

- **Timeline translation.** Everything upstream is in recording seconds; the
  clip API addresses the VOD. Those clocks start at different moments — you
  may hit record before going live — so each candidate is converted through
  wall clock. Moments that fall outside the VOD entirely are reported as
  skips, not dropped.
- **Twitch's 5–60s rule.** A 90-second story gets trimmed and a 3-second
  reaction gets padded, with the adjustment named on the line. The local
  render keeps the full length either way.

Created clips are written back into `session.json`, so the run is resumable
and a second run won't duplicate them.

---

## Session stats

```
Matches: 23   ·   Extracted: 14 (61%)   ·   Died: 9
Kills: 87 (3.8 avg, best 9 (match #12))
Gold start: 250,000 (stash 200,000 + liquid 50,000) at 4m
Gold end:   337,000 (stash 310,000 + liquid 27,000) at 421m
Net change: +87,000
```

Gold is read from the **first and last** stash screen in the recording, not
per match — two OCR reads instead of twenty-three, and it captures deaths,
repairs, and purchases rather than only what a match screen showed.

Endpoints are always printed alongside the delta, because stash "value" is an
estimated market price that drifts on its own, and buying gear moves liquid
into equipped items. A lone delta can be confidently wrong; two endpoints and
their timestamps make an odd number visible.

Needs calibration — see **[docs/calibration.md](docs/calibration.md)**.

```
vodscrap calibrate grid --at 1800            # labelled coordinate grid
vodscrap calibrate crop --at 1800 --box 1200,620,140,70
vodscrap calibrate check --at 1800           # draw the configured boxes back
```

The grid comes out of the recording itself, at the resolution it was actually
recorded at — which is what makes the numbers valid. Read `x,y,w,h` off the
picture, put them in `regions.yaml`, then `check` to confirm the boxes landed
where you meant. The same frame gives you the facecam box for vertical
renders.

---

## Design decisions worth knowing

- **Record MKV, remux to MP4.** An MP4 killed mid-write at hour seven is
  unrecoverable. MKV survives it.
- **Stream copy is off by default.** It only cuts on keyframes, which throws
  away the exact in/out points you chose. Opt in for rough cuts.
- **Retention never deletes an unreviewed recording.** The VOD may already
  have expired, making the local file the only copy.
- **Game-HUD OCR is deliberately limited to summary screens.** Live combat
  HUD templates would break on the first balance patch. Summary screens are
  static, high-contrast, and persist for seconds — and their geometry lives in
  YAML, so a patch means redrawing boxes, not editing code.
- **There is no default game.** Guessing reads one game's boxes against
  another's frame, which produces confident nonsense rather than an error.
- **Twitch Dual Format is not used.** It works, but it would cost horizontal
  stream bitrate and lock vertical framing at stream time, in exchange for a
  6 Mbps source when the local recording is 30–40. Reasoning in
  [docs/streamlabs-setup.md](docs/streamlabs-setup.md).

---

## Known open item

`twitch.vod_offset_is_start` is **unvalidated**. Twitch's API reference
describes `vod_offset` as the clip start; several client libraries and forum
posts describe it as the end. The difference offsets every native Twitch clip
by its own length and cannot be resolved from documentation.

```
vodscrap verify-clip-offset --at 600 --duration 30
```

Creates one throwaway clip and tells you what to check. Set the flag from what
you actually see.

---

## Tests

```
pip install -e ".[dev]" && pytest
```

Cover the parts where a silent error is expensive: timing arithmetic, marker
interpretation, the chat z-score's self-calibration, merge scoring, vertical
layout geometry, caption timing, ffmpeg command construction, OCR number
parsing, the retention guard, native clip reshaping (the 5–60s rules and the
recording→VOD conversion), and calibration coordinate maths.
