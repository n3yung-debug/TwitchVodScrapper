# Stage 0 — Streamlabs setup

One-time configuration on the streaming PC. Everything downstream assumes it.

Menu paths below are for Streamlabs Desktop as of August 2026. If a path has
moved, the setting name is the thing to search for — Streamlabs renames menus
more often than it renames settings.

---

## 1. Local recording

**Settings cog (lower-left) → Output → change Mode from `Simple` to `Advanced`
→ Recording tab.**

| Setting | Value | Why |
|---|---|---|
| Recording Path | a folder on the **8 TB drive** | Never the NVMe the game loads from. |
| Recording Format | **mkv** | See the crash note below. `flv` cannot hold multiple audio tracks at all. |
| Encoder | **NVIDIA NVENC HEVC** | Your 5070 Ti has 2× 9th-gen NVENC; recording alongside the stream costs it nothing. |
| Rate Control | CQP or VBR | |
| CQ / CQP Level | **20–23** | ~30–40 Mbps at 1440p60. |
| Preset | P5 / Quality | |
| Resolution | **native 1440p** | See below. |

### Record MKV, not MP4

An MP4 that loses power, crashes, or is killed mid-write is **unrecoverable** —
the index is written at the end. MKV survives it. On a 4–8 hour session this is
the single most likely way to lose a stream's worth of clips.

Convert afterwards, losslessly and in seconds:

```
vodscrap remux "D:/Recordings/2026-08-03 19-45-12.mkv"
```

The pipeline reads MKV directly, so remuxing is only for editors that won't.

### Record at 1440p, not 1080p

You game at 1440p, so recording at native resolution skips a downscale
entirely. It also buys crop headroom: a 9:16 slice out of 2560×1440 needs a
1.33× upscale to reach 1080×1920, where the same slice from 1080p needs 1.78×.
The difference is most visible on the facecam, which is the weakest element in
a stacked vertical layout.

Cost is roughly 50% more disk — about 13 GB/hour, so 55–110 GB per session.
The 8 TB drive holds around 70 of those before retention needs to do anything.

---

## 2. Multi-track audio

**Audio mixer → the settings cog on the mixer → Advanced Audio Settings.**
Assign each source to tracks. Then back in **Output → Recording**, tick the
tracks to record.

| Track | Source | What it buys |
|---|---|---|
| 1 | Everything (mixed) | The reference feed; matches what viewers heard. |
| 2 | **Mic only** | Clean Whisper transcription and clean voice detection — no game audio contamination. |
| 3 | Game only | Can be muted per clip when game music would trip a platform's copyright filter. |
| 4 | Discord / party | Can be muted per clip when a friend says something unpostable. |

Tick tracks **1, 2, 3, 4** under Recording. Keep streaming on track 1 only.

This only works on **local recordings** — the Twitch VOD is always a single
mixed stereo track. It is the main reason the pipeline cuts from the local
file rather than the VOD.

If you skip this, everything still runs: detection falls back to the mixed
feed and says so in the session warnings. You just lose per-clip audio muting
and get a noisier transcript.

---

## 3. The marker daemon

Leave this running for the whole stream:

```
vodscrap daemon
```

On Windows, run the terminal **as administrator** — global hotkeys will not
register over a fullscreen game otherwise.

| Key | Action |
|---|---|
| **F9** | Mark this moment |
| **F9 F9** (within 1.5s) | Explicit range — first press is the start, second the end |
| **F10** | Mark as funny |
| **F11** | Mark as a big play (grabs more lead-up) |
| **F12** | Mark as a story moment |
| **F8** | Undo the last marker |

A press writes one line to a text file. It never touches the GPU, the encoder,
or video of any kind, so it is not capable of causing a stutter. The clip
length is not decided at press time — it is decided later in the review UI,
where you can see the footage.

---

## 4. What runs after the stream

**Never while you are live.** Whisper and NVENC rendering will fight the
stream for the GPU.

```
vodscrap analyze     # find candidates in the newest recording
vodscrap review      # opens the review UI in your browser
vodscrap summary     # print the session stats
```

---

## 5. Twitch credentials (optional, but chat is the best free signal)

1. Register an app at <https://dev.twitch.tv/console/apps> — any redirect URI.
2. Put the Client ID in `config.yaml` under `twitch.client_id`, and your login
   under `twitch.login`.
3. Generate a user token with the **`clips:edit`** scope and export it:

```
setx TWITCH_OAUTH_TOKEN "your-token-here"
```

Without this the pipeline still runs on markers and audio alone, and lists
chat as skipped in the session warnings.

### One thing to settle on first use

Twitch's API reference describes `vod_offset` as the clip **start**; several
client libraries describe it as the **end**. The difference shifts every
native clip by its own length, and it cannot be resolved from documentation.

```
vodscrap verify-clip-offset --at 600 --duration 30
```

It creates one throwaway clip and tells you what to look for. Set
`twitch.vod_offset_is_start` from what you actually see.

---

## Not recommended: Twitch Dual Format

Dual Format is available to Partners and does produce native vertical VODs.
It is still the wrong tool for this pipeline:

- Partner ingest caps around **8,500 Kbps combined**. A usable vertical feed
  wants ~6,000, which would drop your horizontal stream to ~2,500.
- Vertical framing gets locked in Streamlabs at stream time — no per-clip
  layout decisions.
- Your local recording is **30–40 Mbps**. The Dual Format vertical VOD is
  **6 Mbps**. Using it would trade a 5× quality advantage for convenience the
  pipeline does not need.

Worth enabling if you want live vertical *viewers*. Not for clip production.
