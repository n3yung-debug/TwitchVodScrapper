# Calibration — screenshots to config

Two things get calibrated from screenshots: the **facecam box** (for vertical
renders) and the **OCR regions** (for post-match stats). Both are one-time,
and both are config-only — a game patch means redrawing boxes, never editing
code.

---

## What to capture

Take these at your normal recording resolution, uncropped, PNG:

| Screenshot | Used for |
|---|---|
| Any gameplay frame with the facecam visible | The facecam box |
| Post-match summary after a run you **survived** | Kills, outcome, extra fields |
| Post-match summary after a run you **died** on | Confirms the outcome wording differs |
| The stash / inventory screen showing stash value and liquid gold | Gold endpoints |

The two post-match variants matter: if the survived and died screens use
different layouts, the boxes and the anchor have to work for both.

---

## Reading coordinates off a screenshot

The easiest path is to let the tool pull the frame for you, because then it
is guaranteed to be the exact source the pipeline reads, at the resolution it
was actually recorded at:

```
vodscrap calibrate grid --at 1800
```

That writes a PNG of the frame 30 minutes into the newest recording with a
labelled coordinate grid stamped over it — 100 px cells by default
(`--step`), numbered every 500 px. Read `x,y,w,h` straight off the picture.

Then confirm a box before trusting it:

```
vodscrap calibrate crop --at 1800 --box 1200,620,140,70
```

If that crop shows exactly the value you want read and nothing else, the box
is right. This is also how anchor images get made — crop something on the
screen that never changes, and save it next to `regions.yaml`.

Once the YAML is filled in, draw everything back onto the frame at once:

```
vodscrap calibrate check --at 1800
```

Anything landing in the wrong place is a number to fix, not a bug in the OCR.

A screenshot and an image editor work just as well if you prefer. In
Paint.NET, GIMP, or Photoshop, drag a rectangular selection over the value and
read off the position and size. Just make sure the screenshot is at your
recording resolution — coordinates read off a 1080p grab are wrong for a
1440p recording by exactly that factor, and it looks like OCR simply failing.

Boxes are `[x, y, width, height]` in **source pixels**, with `(0, 0)` at the
top-left of the frame.

Draw them a little generously — 10–15 px of padding around the text — but not
so wide that a neighbouring value creeps in. OCR handles surrounding
whitespace far better than it handles two numbers in one box.

---

## The facecam box

In `config.yaml`:

```yaml
render:
  vertical:
    facecam:
      x: 24
      y: 1146
      width: 480
      height: 270
      calibrated: true
```

That's the facecam's rectangle in the **source** frame — bottom-left, in your
layout. The renderer crops it out and rebuilds it as its own panel under the
gameplay, because no single 9:16 slice of a 16:9 frame can hold both the
action and a corner camera at readable size.

`facecam_fit` controls what happens when the camera's aspect doesn't match the
panel:

- `cover` (default) — crops the sides, fills the panel completely.
- `contain` — fits the whole camera and letterboxes the remainder. Use this if
  your framing matters more than filling the space.

Boxes measured at 1440p are scaled automatically if a session was recorded at
1080p, so one calibration covers both.

---

## OCR anchors

An **anchor** is how the scanner recognises a screen. Crop a piece of the
screenshot that is identical on every run — a header, a panel corner, a fixed
icon — and save it as a PNG under `config/anchors/`.

**Do not use a number as an anchor.** It changes every run, which is the one
thing an anchor must not do.

```
config/
  regions.yaml
  anchors/
    post_match.png
    stash.png
```

`search_box` narrows where the scanner looks for the anchor. It is optional
but worth setting: it speeds up the scan and stops a similar-looking element
elsewhere on screen from matching.

`threshold` is the match confidence, 0–1. Start at `0.82`. If screens are
being missed, lower it toward `0.75`. If the wrong frames are matching, raise
it toward `0.90`.

---

## OCR fields

```yaml
fields:
  kills:
    box: [1200, 620, 140, 70]
    type: int
  survived:
    box: [1080, 300, 400, 90]
    type: bool_text
    true_when: [EXTRACTED, ESCAPED, SURVIVED]
    false_when: [DIED, ELIMINATED, KILLED]
```

| `type` | Behaviour |
|---|---|
| `int` | Strips thousands separators, handles `12.4k` / `2M` |
| `float` | Same, keeps the decimal |
| `bool_text` | Matches `true_when` / `false_when` as case-insensitive substrings |
| `text` | Raw recognised string |

Fields other than `kills` and `survived` on `post_match`, and `stash_value` /
`liquid` on `stash`, are still captured — they land in the match's `extra`
map and appear in the session JSON.

---

## Turning it on

Set `calibrated: true` at the top of `regions.yaml`. Until then the stats
stage is skipped, and says so in the session warnings rather than reporting
numbers read from placeholder boxes.

---

## Checking it worked

```
vodscrap analyze
vodscrap summary
```

The summary reports OCR confidence and flags anything it read poorly:

```
Matches: 23   ·   Extracted: 14 (61%)   ·   Died: 9
Kills: 87 (3.8 avg, best 9 (match #12))
Gold start: 250,000 (stash 200,000 + liquid 50,000) at 4m
Gold end:   337,000 (stash 310,000 + liquid 27,000) at 421m
Net change: +87,000
note: low OCR confidence (0.51) on post_match at 3820s -- values may be wrong
```

Frames it read are kept under `work/<session>/frames/`, so a suspicious number
can be checked against the actual pixels rather than argued about.

If a number looks wrong, widen the box slightly and re-run — `analyze` reuses
cached chat but re-reads screens.

---

## Two things the gold number won't tell you

The delta is a **net worth change**, not earnings, and two things can distort
it. Both are why the summary always prints the endpoints and their timestamps
rather than a single figure:

- Stash "value" in an extraction game is usually an estimated market price. It
  moves on its own, without you doing anything.
- Buying gear converts liquid into equipped items. If equipped gear isn't
  counted in stash value, that reads as a loss.

Seeing both reads makes an odd number visible instead of authoritative.

---

## When a patch breaks it

Symptom: `Matches: 0`, or a note about low confidence on every screen.

1. Take a fresh screenshot of the changed screen.
2. Re-crop the anchor PNG if the landmark moved or was restyled.
3. Redraw the affected boxes in `regions.yaml`.
4. Re-run `vodscrap analyze`.

No code changes. That is the entire reason the geometry lives in YAML.
