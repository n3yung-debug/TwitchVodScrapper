# TwitchVodScrapper

Turns a Twitch stream into clips worth posting: hotkey markers while you play,
a detection pass afterwards, a local review UI, and rendered files in a folder.

Built for Mistfall Hunter on a 9800X3D / RTX 5070 Ti, Windows, Streamlabs
Desktop. Nothing is posted automatically — the last step is always yours.

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
```

Then work through **[docs/streamlabs-setup.md](docs/streamlabs-setup.md)** —
recording settings, multi-track audio, hotkeys, credentials.

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
| `vodscrap daemon` | Hotkey marker daemon with a tray icon |
| `vodscrap list` | Recordings found on disk |
| `vodscrap analyze [file]` | Detection cascade; defaults to the newest recording |
| `vodscrap review` | Opens the review UI at `127.0.0.1:8723` |
| `vodscrap summary` | Session stats — kills, extract rate, gold |
| `vodscrap remux <file>` | Lossless MKV → MP4 |
| `vodscrap prune` | Delete old, already-reviewed recordings (dry run by default) |
| `vodscrap verify-clip-offset` | Settles Twitch's `vod_offset` ambiguity — run once |

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

---

## Design decisions worth knowing

- **Record MKV, remux to MP4.** An MP4 killed mid-write at hour seven is
  unrecoverable. MKV survives it.
- **Stream copy is off by default.** It only cuts on keyframes, which throws
  away the exact in/out points you chose. Opt in for rough cuts.
- **Retention never deletes an unreviewed recording.** The VOD may already
  have expired, making the local file the only copy.
- **Game-HUD OCR is deliberately limited to the two summary screens.** Mistfall
  Hunter launched 2026-07-29; live combat HUD templates would break on the
  first balance patch. Summary screens are static, high-contrast, and persist
  for seconds — and their geometry lives in YAML, so a patch means redrawing
  boxes, not editing code.
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
parsing, and the retention guard.
