# Chroma watermark

An invisible 32-bit identifier embedded into a Direct3D 11 application's own
rendered frames, recoverable from a screenshot, a screen recording, or a video
of the monitor filmed with a phone.

The identifier survives JPEG compression, rescaling, blur, H.264 encoding and
partial occlusion of the window, while staying below the visible threshold at
1:1 on a real display (mean ΔE 0.35 at the default strength). It is carried in
**chroma only**, along a blue-yellow axis that leaves luminance untouched,
because the eye is several times less sensitive to low-frequency blue-yellow
detail than to brightness.

This repository holds two implementations of the same mark and one decoder
for both:

- **`desktop/`**: a Direct3D 11 post-process pass in HLSL, demonstrated on the
  Dear ImGui Win32/DirectX11 example. For marking a native application's
  window.
- **`web/`**: the same pass as WebGL, for marking an image, video or canvas in
  a web page. One self-contained `index.html` demonstrates it on an
  ordinary-looking website and in a full-screen view.
- **`tools/`**: the Python decoder and the test suites, shared by both. A
  screenshot of either decodes with the same command.

**Scope.** The intended use is marking your *own* application's window with
your *own* identifier, so a leaked screenshot or recording of it can be traced
back to a session. It is not a general-purpose steganography tool and it is not
adversarially robust; see [Limits](#limits).

---

## Contents

| Path | What it is |
|---|---|
| `desktop/src/watermark.{cpp,h}` | The D3D11 embedder: pattern layout, HLSL shader, D3D11 plumbing |
| `desktop/src/background.{cpp,h}` | Photo backdrop loader, so the demo runs over realistic content |
| `desktop/src/main.cpp` | Dear ImGui example with the watermark panel and a capture hook |
| `desktop/setup.sh`, `desktop/build.sh` | Fetch Dear ImGui at a pinned commit, graft the sources in, build |
| `web/watermark.js` | The WebGL embedder, no dependencies; same key, layout and weight |
| `web/index.html` | Self-contained demo: an ordinary website with one marked element, plus a full-screen view |
| `web/README.md` | How to use the WebGL embedder on a page, and what it cannot do |
| `tools/decode_watermark.py` | The decoder and its command line, shared by both |
| `tools/test_*.py` | Five test suites, described under [Testing](#testing) |
| `docs/INTEGRATION.md` | **Renderer-side changes**: what a D3D11 host application has to do |
| `docs/DESIGN.md` | Full design: format, perceptual weighting, decoding, measurements |
| `docs/HANDOFF.md` | Implementation notes, known gaps, and what to do next |

---

## Quick start

### Desktop

```bash
git clone <this repo> chroma-watermark
cd chroma-watermark/desktop
./setup.sh                 # clones Dear ImGui at a pinned commit, grafts the example in
./build.sh Release         # MSBuild, toolset and SDK overridden on the command line
imgui/examples/example_win32_directx11/Release/example_win32_directx11.exe
```

The window draws a photograph behind the ImGui panels. This is deliberate: flat
clear-colour panels are the easiest possible content for the mark and hid real
weaknesses in the decoder during development. Tick **Watermark** in the
"Hello, world!" window to open the control panel.

Take a screenshot of the window, then, from the repository root:

```bash
pip install numpy scipy opencv-python
python tools/decode_watermark.py screenshot.png
```

```
window corners   : 1,-7 1575,-2 1569,954 0,954
search score     : 1.677   pilots 0.537
presence score   : 0.499  (threshold 0.12, full-pattern 0.552)
weakest bit      : 0.2588   mean 0.6347
match ID         : 1264723528   (CRC ok)
```

The application prints its match ID to stdout at startup, so a capture can be
checked against the truth.

### Web

Open `web/index.html` in a browser; it needs no server and no other files.
It lands on an ordinary-looking page whose hero image carries the mark, with
the ID in the nav (blurred until hovered; type a number and press Enter to
change it). Take a screenshot and decode the image's rectangle:

```bash
python tools/decode_watermark.py screenshot.png --box 540 90 680 450
```

`web/README.md` covers the page's other view, the JavaScript API for marking
an element of your own page, and the limits of doing this client-side.

### Requirements

Building needs Visual Studio 2022 or newer with the Desktop C++ workload. The
build script defaults to Visual Studio 18 with toolset v145 and Windows SDK
10.0.26100.0; override with environment variables if yours differ:

```bash
MSBUILD="/c/Program Files/Microsoft Visual Studio/2022/Community/MSBuild/Current/Bin/MSBuild.exe" \
TOOLSET=v143 SDK=10.0.22621.0 ./build.sh Release      # from desktop/
```

The web version needs only a browser with WebGL; its verifier additionally
needs Chrome.

Decoding needs Python 3.9+ with `numpy`, `scipy` and `opencv-python`. Video
decoding uses OpenCV's ffmpeg backend.

---

## How it works

The full treatment is in [`docs/DESIGN.md`](docs/DESIGN.md), including every
design decision that turned out to matter and why. The short version:

### 1. The frame becomes a 32×32 grid

1024 cells, each about 49×30 px at the example's window size. Cells are
deliberately coarse because optical blur, camera downsampling and video
encoding destroy fine detail while a coarse pattern survives.

```
  32 cells wide
┌───┬───┬───┬───┬───┐
│ D │ P │ D │ D │ D │   D  880 data cells
├───┼───┼───┼───┼───┤      40 coded bits × 22 copies
│ D │ D │ D │ P │ D │
├───┼───┼───┼───┼───┤   P  144 pilot cells
│ P │ D │ D │ D │ D │      fixed key-derived signs
└───┴───┴───┴───┴───┘
```

The **coded word** is the 32-bit match ID plus a CRC-8 of it, so a wrong decode
is detectable rather than silently plausible. Each bit's 22 copies are
scattered across the frame by a keyed permutation, half positive and half
negative, so covering a region costs every bit a few copies rather than costing
one bit all of them, and an overall colour shift cancels out.

The **pilot cells** carry fixed signs independent of the payload. They let the
decoder find the grid in a distorted capture and distinguish a marked image
from an unmarked one.

### 2. Each cell is a perceptually weighted chroma bump

The offset is `+a` on blue with red and green each reduced by `0.1287a`, which
leaves Rec.601 luma unchanged. Across the cell it follows `sin²(πu)·sin²(πv)`:
full strength at the centre, zero at every edge, no step anywhere for the eye
to catch. Amplitude is then scaled by a perceptual weight computed from the
pixels underneath:

```
weight = saturate(luma / 0.30) * (0.30 + 0.70 * texture)
```

Dark pixels carry less, because a fixed offset on a near-black panel reads as a
blue patch on black. Flat pixels carry 30% of the full amount; bright busy
pixels all of it. `texture` is a smoothstep of local luma activity, eroded over
a 3×3 of positions so that a lone edge with flat colour beside it — text on a
panel — correctly scores as flat.

The decoder recomputes the identical weight from the capture and uses it as
each cell's expected amplitude, making the vote a matched filter: a cell the
embedder barely touched barely counts.

### 3. Decoding locates the grid, then votes

1. **Locate.** A homography maps grid coordinates to the capture. A similarity
   grid search (quarter-cell shifts, 1° rotations, 2.5% scale steps) scores
   ~30,000 geometries on a cheap kernel, keeps the best two dozen distinct
   ones, and refines them by Nelder-Mead over the eight corner coordinates.
2. **Sample** each cell through the homography on a blurred blue-minus-luma
   plane, and on the recomputed weight plane for its expected gain.
3. **Matched kernel.** Each cell's response is its samples weighted by the bump
   profile *minus that profile's mean* — a zero-mean kernel blind to anything
   flat or linear across the cell.
4. **Vote.** Each bit's copies are sorted, the extreme quarter at each end
   dropped, and the rest averaged with weight `gain / (1 + variance/typical)`.
5. **Check.** The CRC must match, and a cross-validated presence score must
   clear 0.12. The copies are dealt into three groups: two steer the geometric
   search, and the third is never read until presence is scored, so the score
   cannot be inflated by the search having maximised it.

---

## Renderer integration

The embedder is a full-screen post-process pass that runs after everything else
is drawn and before Present. It takes a Direct3D 11 device and a render target
view, so it has no dependency on ImGui and drops into any D3D11 renderer.

Because the shader must *read* the finished frame to decide how much mark each
pixel can hide, the frame is drawn into an offscreen target and the pass writes
the marked result to the swap chain. That is the only change to the host's own
rendering:

```cpp
FrameWatermark watermark;                       // once, alongside the device
watermark.Initialise(device, context);
watermark.ResizeBuffers(width, height);         // and on every swap-chain resize
watermark.SetPayload(match_id);

// Each frame: draw into the offscreen target instead of the swap chain...
ID3D11RenderTargetView* target = watermark.SceneTarget();
context->OMSetRenderTargets(1, &target, nullptr);
context->ClearRenderTargetView(target, clear_color);

DrawEverything();                               // unchanged

watermark.Apply(swapchain_rtv);                 // ...then resolve to the swap chain
swapchain->Present(1, 0);
```

Against the stock ImGui example that is about twenty lines in `main.cpp`, plus
four in the project file. No upstream ImGui source is modified.

Full details, including how the pass saves and restores pipeline state, which
swap-chain formats it takes and how sRGB is handled, what it costs (measured:
0.02 ms at 1280×800, 0.05 ms at 1440p on an RTX 5090), why it must be last
and must run at native resolution, and how to verify a port — see
**[docs/INTEGRATION.md](docs/INTEGRATION.md)**.

---

## Modes

### Embedding modes (the Watermark panel)

| Control | Default | What it does |
|---|---|---|
| **Enabled** | on | Turns embedding off entirely, for A/B comparison at 1:1 |
| **Match ID** | random per launch | The 32-bit payload; takes effect on the next frame |
| **Strength** | 0.08 | Peak chroma offset on bright textured pixels; the whole visibility/robustness tradeoff |
| **Alternate polarity** | off | Experimental; flips the whole pattern's sign every 25 ms |
| **Show background** | on | Toggles the photo backdrop, to see the mark over flat panels |

**Strength** is the one number that matters. Too low and the mark cannot be
recovered; too high and it becomes visible. Measured on real shader output over
the test scene:

| Strength | Mean ΔE | 95th pct ΔE | Look at 1:1 | Presence |
|---|---|---|---|---|
| 0.04 | 0.15 | 0.7 | nothing found | 0.29 |
| **0.08** (default) | 0.35 | 1.6 | nothing found | 0.52 |
| 0.12 | 0.55 | 2.7 | faint tint on the bright gradient if looking for it | 0.61 |

ΔE is CIELAB distance per pixel against the unmarked frame; around 1 is the
usual side-by-side threshold.

**Static mode** (the default) uses an identical mark on every frame, which is
what lets a video encoder preserve it: a static pattern is predicted from the
previous frame and costs the encoder almost nothing to keep.

**Alternate polarity** flips the sign of the whole pattern every 25 ms. A
low-contrast chroma flicker at that rate averages to nothing for the eye, so
the mark can be stronger for the same visibility, and a screen recording keeps
every frame intact. The cost is the camera: an exposure spanning both
polarities cancels the mark, so 1/30 s and longer loses most of it. Decode such
a recording with `--alternating`. It is experimental and has been tested only
in the numpy model, never filmed.

### Decoding modes (geometry hints)

The decoder must find the window's four corners in the capture. How much help
it needs depends on what the capture contains:

| Capture | Flag | Cost |
|---|---|---|
| The window fills the frame (screenshot of the window, screen recording shifted, rotated a little, or zoomed out to 75%) | *nothing* | ~7 s |
| A whole desktop, window somewhere on it | `--box X Y W H` | ~15 s |
| Window rectangle known to within a cell | `--crop X Y W H` | ~7 s |
| Window seen in perspective (phone video) | `--corners TL TR BR BL` | ~7 s |

```bash
python tools/decode_watermark.py screenshot.png
python tools/decode_watermark.py desktop.jpg --box 1000 150 1900 1200
python tools/decode_watermark.py recording.mp4 --crop 320 180 1600 1080
python tools/decode_watermark.py phone.mp4 --corners 212,88 1710,131 1688,1002 190,957
```

`--box` is a rectangle the window lies somewhere *inside*. It can be drawn by
hand and generously — title bar, frame and a margin of desktop included — as
long as the window is at least 60% of it in each dimension. The decoder tries
every size and position within the box.

`--crop` and `--corners` only need to be rough, within about 15 px; both are
refined. A phone video where the screen is a small tilted quadrilateral needs
`--corners`, since there is nothing near the full-frame guess to lock onto.

### Search modes (`--sync`)

| Mode | Behaviour |
|---|---|
| `auto` (default) | Coarse search, then refinement. Use for any single capture |
| `refine` | Local refinement only, from the given corners. For video where tracking is already close |
| `off` | Trust the corners exactly. For synthetic tests |

Further tuning: `--search-cells`, `--search-degrees` and `--search-scale` widen
or narrow the coarse search; `--max-frames` limits how much of a video is
sampled; `--expect` checks the result against a known ID; `--verbose` prints
the score of every frame.

### Video mode

Any video path triggers it. The grid is found on the first frame with the full
search, then each later frame is refined from two starts — the previous frame's
solution and the first frame's — keeping the better one. Every ten frames, if
the score has fallen below half the first frame's, a narrow search runs again.
Each frame is decoded alone and its cells accumulated in proportion to its own
presence score, so a badly tracked frame contributes little. About 1 s per
tracked frame after the first.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Mark found, CRC valid |
| 1 | Found, but did not match `--expect` |
| 3 | No mark detected |
| 4 | Mark present but CRC failed |

---

## What survives

Margin is the weakest bit's correlation; a decode is correct while it is
positive and comfortable above about 0.1. All figures from real shader output
over the test scene, which is dark (mean luma 0.13) and mostly smooth — a hard
case. Brighter, busier content carries more.

| Degradation | Margin at 0.08 | at 0.12 | at 0.04 |
|---|---|---|---|
| lossless | 0.42 | 0.48 | 0.30 |
| JPEG q90 / q75 / q50 / q30 | 0.28 / 0.12 / 0.05 / 0.01 | 0.40 / 0.25 / 0.18 / 0.08 | 0.05 / fails / fails / fails |
| downscale to 75% / 50% / 33% | 0.42 / 0.44 / 0.41 | ≥ 0.46 | ≥ 0.23 |
| Gaussian blur σ 2 / 4 / 8 px | 0.44 / 0.46 / 0.23 | ≥ 0.39 | 0.23 / 0.19 / fails |
| H.264 crf 18 / 23 / 28 / 35 | 0.31 / 0.24 / 0.12 / 0.11 | 0.45 / 0.35 / 0.28 / 0.29 | 0.17 / 0.12 / 0.04 / fails |
| **converted to grayscale** | **lost** | lost | lost |

OBS records around crf 18–23. Covering part of the window: top, left, centre
and corner blocks survive up to 70% coverage (the largest tried); a bottom band
survives 40%. The bottom is the exception because the brightest part of this
particular wallpaper is along the bottom, and the perceptual weight puts 55% of
the mark's energy there. Energy follows the content.

Geometric distortion, all decoding with a valid CRC: shifts up to 60 px,
rotations to 5°, zoom out to 60%, and perspective warps up to 120 px from
rough corners. Full table in [`docs/DESIGN.md`](docs/DESIGN.md).

---

## Testing

```bash
python tools/test_roundtrip.py     # decoder vs a numpy model of the shader
python tools/test_gpu_frames.py    # decoder vs real frames off the GPU
python tools/test_occlusion.py     # decoding with part of the frame covered
python tools/test_sync.py          # finding the grid in warped captures; rejecting unmarked images
python tools/test_video.py         # tracking drifting H.264 clips (needs ffmpeg)
python web/tools/verify.py         # WebGL embedder: headless Chrome renders, decoded; needs Chrome
```

The suites that render through the real shader (all but the first) matter most,
because they catch the embedder and decoder drifting apart. Four pass. The
video suite reports one known failure: the handheld clip decoded from a single
frame, where the weakest bit sits at exactly zero. It decodes from ten frames,
and the failure is left in as an honest marker of where the design stands.

The application has a capture hook the GPU suites drive:

```bash
example_win32_directx11.exe --capture out.ppm 1234567 0.08
```

It renders eight frames, writes the backbuffer as a binary PPM and exits, along
with the unmarked frame as `out.ppm.scene.ppm`, a line saying whether the pass
handed the pipeline state back untouched, and the pass's GPU time. Pass
`random` in place of the ID to have it draw one and print it. Three switches
exist for testing and measuring, with or without `--capture`:
`--format rgba8|bgra8|rgb10|srgb` picks the swap-chain format, `--size <w> <h>`
the client area, and `--novsync` presents without waiting for the display.

Unmarked images are rejected reliably: an unmarked photo, the same blurred,
random blobs, a gradient, a checkerboard and six further wallpapers all score
at most 0.05 after the search has done its best on them, against a threshold
of 0.12.

---

## Limits

- **32 bits is an identifier, not text.** Longer payloads trade directly
  against robustness.
- **Grayscale kills it.** Any pipeline that discards colour discards the mark.
- **Dark, smooth scenes carry little.** The perceptual weight gives a black
  screen nothing and a flat mid-grey 30% of the strength. A menu on a dark
  background may not decode from one screenshot; a video of it accumulates.
  This is the invisibility trade, made deliberately.
- **Not adversarially robust.** Two captures with different IDs can be diffed
  to estimate the pattern. Commercial forensic-marking systems share this
  weakness.
- **The key is a constant in the source** (`kWatermarkKey` in `watermark.cpp`,
  `WATERMARK_KEY` in `decode_watermark.py`). Anyone with the binary can recover
  it. Both sides must agree, so changing it means changing both.
- **No real phone camera test yet.** Every figure above is synthetic distortion
  of real shader output. A camera adds moiré, rolling shutter banding,
  auto-exposure and white-balance drift, lens distortion, and the display's own
  gamma and viewing-angle colour shift — the last two matter more for a chroma
  mark than for a luma one.
- **Automatic window finding needs a hint** on a full-desktop capture. Finding
  it with no box at all was investigated and is impractical at this mark
  strength; the reasoning is recorded in `box_search`'s docstring.

---

## Licence

The watermark sources, decoder and tests in this repository are MIT licensed
(see `LICENSE`). Dear ImGui is fetched by `desktop/setup.sh` and carries its
own MIT licence. `desktop/assets/background.jpg` and the images under
`web/assets/` are stock Windows wallpapers, included only as demo content;
substitute your own (`--background <file>` for the desktop demo).
