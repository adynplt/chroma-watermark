# Project handoff

Everything needed to pick this work up in a fresh session. Written for an agent
with no prior context.

Companion document: `WATERMARK.md` covers the design, the bugs found along the
way, and all measurements. This file covers state, environment, and what to
do next.

---

## 1. What this project is

A Dear ImGui + DirectX 11 desktop window that embeds a **32-bit match ID** into
its own rendered frames as a hidden chroma pattern, plus a Python decoder that
recovers the ID from a capture of that window. The window draws a photograph
behind the ImGui panels so the frames resemble a real application rather than
flat grey.

**Goal, in the user's words:** identify which match a screenshot or video came
from, including when the video was made by pointing a **phone camera at the
monitor**, and without the mark being visible over a live game. The ID is set
by the user's own application — this watermarks the user's own window, with
their own data. There is no other process involved.

**Status:** complete against synthetic tests on photo content. The mark is
close to invisible at the default strength, survives JPEG q30, 3x
downscaling, 8 px blur, H.264 at crf 42 and 70% of the frame covered; the
decoder finds the grid in shifted, rotated, zoomed and perspective-distorted
captures, tracks drifting handheld-style clips, checks a CRC, and rejects
unmarked images. **It has not been tested against a real phone camera** —
section 7.

---

## 2. Environment

| | |
|---|---|
| Working directory | The repository root; `setup.sh` puts Dear ImGui in `imgui/` beneath it |
| Platform | Windows 11 Pro, PowerShell + Git Bash both available |
| Visual Studio | **VS 2026 Community** at `C:\Program Files\Microsoft Visual Studio\18\Community` |
| Toolset | v145, Windows SDK 10.0.26100.0, x64 |
| Python | 3.14 with numpy 2.4, scipy 1.17, opencv 4.13, Pillow 12 — all installed |
| ffmpeg | on PATH (WinGet); only `test_video.py` needs it |

### Build

```bash
./build.sh            # Debug
./build.sh Release    # Release
```

`build.sh` exists because the upstream ImGui `.vcxproj` pins **Windows SDK 8.1
and toolset v141**, neither of which is installed here — building it unmodified
fails with `MSB8036`. The script overrides both on the MSBuild command line
rather than editing the vcxproj, so the ImGui clone stays pristine.

If you build from the Visual Studio IDE instead, you will hit that same SDK
error and VS will offer "Retarget solution". Accepting is fine here (8.1/v141
is ImGui's legacy default, not a deliberate target) but it dirties the file.

**Do not fall back to VS 2022** — it is also installed, but its v143 toolset
does not match. Use the VS 2026 paths above. The user's global instructions
require this. The build emits one pre-existing `fopen` deprecation warning
from the test hook in `main.cpp`; harmless.

---

## 3. Layout

```
<repository root>/
├── setup.sh                    # clones Dear ImGui at a pinned commit, grafts src/ in
├── build.sh                    # build wrapper with SDK/toolset overrides
├── README.md                   # overview, modes, what survives
├── src/                        # the files this project owns, copied in by setup.sh
│   ├── main.cpp                # MODIFIED example — integration, backdrop, capture hook
│   ├── watermark.h             # layout constants
│   ├── watermark.cpp           # embedder + HLSL shaders
│   └── background.h/.cpp       # WIC image loader -> D3D11 texture
├── assets/
│   └── background.jpg          # default backdrop (Windows ThemeA wallpaper, 3840x2400)
├── docs/
│   ├── INTEGRATION.md          # renderer-side changes and porting caveats
│   ├── DESIGN.md               # design, rationale, measurements
│   └── HANDOFF.md              # this file
├── examples/
│   └── roundtrip_demo.py       # self-contained numpy demo, no GPU or build needed
├── tools/
│   ├── decode_watermark.py     # THE DECODER + CLI; all shared constants live here
│   ├── read_ppm.py             # PPM reader for test captures
│   ├── test_roundtrip.py       # decoder vs a numpy model of the shader
│   ├── test_gpu_frames.py      # decoder vs real GPU frames
│   ├── test_occlusion.py       # decoding with part of the frame covered
│   ├── test_sync.py            # grid search on warped captures; negatives
│   └── test_video.py           # tracking drifting H.264 clips (needs ffmpeg)
└── imgui/                      # NOT in version control; created by setup.sh
    └── examples/example_win32_directx11/
        ├── <the five src/ files, copied here>
        ├── example_win32_directx11.vcxproj   # MODIFIED — added the two new .cpp files
        └── Release/example_win32_directx11.exe
```

`src/` is the authority: edit there, re-run `setup.sh` to copy into the ImGui
tree, then build. Editing the copies under `imgui/` works for a quick
experiment but is lost the next time setup runs, and is not version-controlled.

Only `main.cpp` and the `.vcxproj` are modified upstream files; everything else
in `imgui/` is untouched.

### The backdrop

`main.cpp` resolves the image in this order: `--background <file>`, then
`assets/background.jpg` found four directory levels above the executable,
then `C:\Windows\Web\Wallpaper\ThemeA\img20.jpg`, else none. It is decoded
with Windows Imaging Component (no new dependency), drawn with
`ImGui::GetBackgroundDrawList()->AddImage` scaled to cover the viewport, and
toggled by a checkbox in the Watermark panel. The `--capture` test hook
renders it too, so every test suite runs on photo content.

---

## 4. How it works

Short version; `WATERMARK.md` has the full pipeline.

The frame is drawn into an **offscreen texture**, then a fullscreen pixel
shader copies it to the backbuffer while adding a per-cell **blue-yellow
chroma offset** that leaves luma unchanged (+a on blue, -0.1287a on red and
green), shaped as a raised-cosine bump across each cell, multiplied by a
**perceptual weight** — `saturate(luma/0.3) * (0.3 + 0.7 * texture)`,
where texture is an eroded local-activity measure — and clamped so no
channel leaves its range. Dark and flat pixels therefore carry a fraction of
the strength; bright busy pixels all of it. The frame is a **32x32 grid** =
1024 cells: **880 data cells** (40 coded bits — 32-bit ID + CRC-8 — x 22
copies) and **144 pilot cells** with fixed keyed signs, used to locate the
grid in a distorted capture, to test presence, and to resolve each frame's
sign when the optional polarity alternation is on.

The decoder reads **blue minus luma**, recomputes the perceptual weight from
the capture as each cell's expected gain, locates the grid by a fine similarity
grid search (quarter-cell, 1°, 2.5% steps; split-half objective; two dozen
candidates) plus Nelder-Mead refinement of the four corners ranked by a
cross-validated score, reads each cell with a zero-mean matched kernel (bump profile minus
its mean, 49 samples covering the cell — no neighbour background, no
self-interference), votes each bit with a trimmed mean weighted by gain over
variance, checks the CRC, and decides presence with a gain-weighted
cross-validated score on a third group of copies the search never reads.
Video: grid found on the first frame, each later
frame refined from both the previous solution and the first-frame anchor,
a narrow re-search when the score halves, frames accumulated in proportion
to their own presence score; `--alternating` aligns each frame's sign from
the pilots.

### Shared constants — MUST match on both sides

| Constant | Value | C++ | Python (`decode_watermark.py`) |
|---|---|---|---|
| Key | `0x5A17C0DE` | `kWatermarkKey` (`watermark.cpp`) | `WATERMARK_KEY` |
| Payload bits | 32 | `PayloadBits` (`watermark.h`) | `PAYLOAD_BITS` |
| CRC bits | 8 | `CrcBits` | `CRC_BITS` |
| Copies per bit | 22 | `RepeatPerBit` | `REPEAT_PER_BIT` |
| Pilot cells | 144 | `PilotCells` | `PILOT_CELLS` |
| Grid | 32 x 32 | `GridCols`, `GridRows` | `GRID_COLS`, `GRID_ROWS` |
| Chroma axis | (-0.12867, -0.12867, 1) | `kChromaAxis` in the HLSL | `mark_plane()` reads B - Y |
| Cell profile | sin²(πu)·sin²(πv) | HLSL `profile` | `SAMPLE_WEIGHTS`, `SAMPLE_KERNEL` |
| Perceptual weight | knee 0.30, floor 0.30, texture 0.010–0.040, tap radius 0.06 cell | `kMask*` in `watermark.cpp` | `MASK_*`, `perceptual_weight()` |
| Polarity half period | 25 ms | `kPolarityHalfPeriodMs` | — (resolved from pilots) |
| Default strength | 0.08 | `m_strength` in `watermark.h` | — |
| Presence threshold | 0.12 | — | `PRESENCE_THRESHOLD` |

**If you change any of these, change both sides and re-run every suite.** There
is no runtime check that they agree. Four functions are hand-ported and must
stay bit-identical: `HashU32`/`hash_u32`, `Crc8`/`crc8`,
`DescribeCell`/`describe_cell`, `CellPositionTable`/`_build_position_table`.
The numpy `embed()` in `test_roundtrip.py` mirrors the shader (profile,
chroma axis, perceptual weight via the decoder's own `perceptual_weight()`,
polarity, clamping) and must be kept in step with it. The shader skips the
activity reads on pixels whose luma weight is 0 or whose centre activity is
already at or below the lower texture threshold; both are exact shortcuts,
not approximations, so the numpy model does not mirror them. The shader was checked
against that model on real GPU output: correlation 0.98 over stable pixels,
mean difference well under one 8-bit step. The
HLSL constant buffer hardcodes `float4 cellAmplitude[1024]`.

To re-verify layout parity after a change: compile a small C++ program that
prints `(cell, bit, sign, position)` for every cell plus `Crc8` of a few
values, and compare against the Python functions. Last check: 1032/1032.

### main.cpp integration points

| Where | What |
|---|---|
| top | `#include "watermark.h"`, `"background.h"` |
| globals | `g_watermark`; test-capture globals; backdrop globals |
| `ResolveBackgroundPath()`, `DrawBackground()` | backdrop selection and drawing |
| `SaveTexturePPM()`, `SaveBackbufferPPM()`, `PipelineProbe` | test hook: format-aware PPM writer, pipeline-state check |
| `main()` top | `--background`, `--capture`, `--format`, `--size`, `--novsync` parsing |
| after `ImGui_ImplDX11_Init` | watermark init, payload, strength, backdrop load |
| after `ImGui::NewFrame()` | `DrawBackground(show_background)` |
| Watermark panel | ID, strength slider (0–0.20), "Alternate polarity (experimental)", "Show background" |
| before Present | render into `g_watermark.SceneTarget()`, then `Apply()` |

---

## 5. Testing

```bash
cd tools
python test_roundtrip.py      # ~30 s
python test_gpu_frames.py     # ~1 min; renders frames via the exe
python test_occlusion.py      # ~1 min
python test_sync.py           # ~3 min
python test_video.py          # ~3 min; needs ffmpeg
```

**Four of five pass on the photo backdrop; `test_video.py` has one known
failure (handheld clip, single frame).** Timings are roughly: roundtrip
1 min, gpu 1 min, occlusion 1 min, sync 3 min, video 3 min. The suites that render
through the real executable (`--capture` hook) matter most; several real bugs
were invisible to the synthetic suite. `test_sync.py`, `test_occlusion.py`
and `test_video.py` accept a captured frame path (and `test_sync.py` an
unmarked one second) to skip re-rendering.

### The capture hook

```
example_win32_directx11.exe --capture <out.ppm> <match_id> [strength] [--background <file>]
                            [--format rgba8|bgra8|rgb10|srgb] [--size <w> <h>] [--novsync]
```

Renders 8 frames, writes the backbuffer as binary PPM and the unmarked scene
as `<out.ppm>.scene.ppm`, prints `pipeline state restored: ok|FAILED (...)`
and `watermark pass: <ms> ms`, exits. Strength 0 gives
an unmarked frame. Added purely for testing; harmless in normal use.

### Results in brief

Full tables in `WATERMARK.md`. At the default 0.08 on the photo backdrop (a
dark, mostly smooth scene — a hard case for a mark that hides in brightness
and texture): presence 0.52, margin 0.39; every test payload decodes with
CRC; JPEG down to q30, 33% downscale, 8 px blur and H.264 to crf 35 decode
(JPEG q30 and crf 35 only just); the grid is found from a full-frame guess
for shifts, rotations to 5°, zoom to 80% and moderate perspective, and from
rough corners for anything harsher (all 13 geometry cases, presence
0.30–0.52); a drifting screen recording decodes from a single frame
(presence 0.31–0.43) and a handheld-style clip from ten frames (from one it
is a bit short); thirteen kinds of unmarked image are rejected (worst 0.05
vs threshold 0.12); 70% of the frame can be covered in every shape except a
band over the bright bottom (40%). Visibility: mean ΔE 0.35, 95th
percentile 1.6, nothing found at 1:1 on panels, gradients or header bars.
**Grayscale conversion destroys the mark.**

**Suite status: four of five pass outright; `test_video.py` reports one
failure, the handheld clip from a single frame.** Left failing rather than
loosened: it marks where this design stands.

---

## 6. Known gaps and rough edges

**Grayscale.** The mark lives in chroma. Any capture pipeline that discards
colour discards it. Accepted knowingly, for invisibility.

**Robustness was spent on invisibility.** The previous fixed-amplitude
design kept a margin of 0.62+ through every degradation; the perceptual
design keeps 0.1–0.4 on the dark test scene, decodes JPEG q30 and crf 35 by
a hair, and needs ten frames of a handheld-style clip. The knobs, in order
of preference: raise the default strength (0.12 gives margin ≥ 0.08
everywhere and was still clean at 1:1 apart from a faint tint on the
brightest gradient); raise `kMaskFloor` / `MASK_FLOOR` (more on flat areas,
which is where it shows); lower `kMaskKnee` (more in the dark). Judge on
the user's monitor at 1:1 — a downscaled screenshot hid the previous
design's visible lattice completely.

**Dark, smooth content carries little.** By design. A single screenshot of a
dark menu may not decode; video accumulates — but only if the scene moves.
A static scene's interference is identical in every frame and does not
average out; the video test's clips are static scenes with camera jitter
and show exactly that (30 frames no better than 10).

**Polarity alternation is untested on a camera.** The 25 ms half period was
chosen so a 1/60 s exposure sees one sign, but nothing has been filmed.
It is off by default, and the decoder assumes a static mark unless given
`--alternating` — letting the search consider both signs on a static mark
cost a geometry case, because a half-cell-shifted grid reads with flipped
signs at half strength.

**The auto search needs the window to roughly fill the frame.** A desktop
screenshot with the window somewhere on it needs `--box` (a generous
hand-drawn rectangle; the window must be at least 60% of it), which
searches every size and position inside the box in a few seconds. A window
that is both zoomed out and tilted (a phone video) needs `--corners`; rough
corners are enough. Finding the window with no hint at all was looked into
and is not practical at this mark strength: a sign-blind statistic (energy
of the matched-filtered mark plane, folded at each candidate cell pitch)
does not see the lattice under the scene's own chroma even on a lossless
full-window capture, and the pilots, which a fast FFT correlation could
use, read at about four standard deviations on a screenshot -- below the
maximum of the noise over the millions of sizes and positions a whole
desktop offers. The sign-aware statistic works but costs ~50 us per
candidate, so an unhinted desktop search would take tens of minutes.

**Covered-cell detection only works on lossless captures.** It keys on a
kernel response that is exactly zero; after compression nothing is flagged
and the trimmed vote alone carries the occlusion tolerance.

**Search cost:** ~7 s for a first frame (full-frame or from rough
corners), ~1 s per tracked frame, after vectorising the inner loop (it was
40 s). The grid is 34k geometries on a 3x3 kernel, scored 289 shifts at a
time; two dozen candidates are refined by Nelder-Mead, which is now most
of the time (14k objective evaluations at 0.25 ms). Cheaper still would
be an FFT correlation over translation per (scale, rotation), which also
lands the translation exactly instead of to a quarter cell.

**Corner precision is 15–30 px** (up to 80 px on strong perspective) — the
search settles where the mark reads, not on the window edge. Decoding is
unaffected; anything that needs the true edge is not.

**Presence threshold 0.12** sits below marked scores (0.23–0.5) and above
every negative (13 images, worst 0.05; spread about 0.05). Less headroom
than before.

---

## 7. What to do next

**7a. Film the monitor with a phone.** The thing the project was built for and
the only thing not yet measured. Chroma raises two camera-specific questions
luma did not: white-balance drift (cancelled by the 3x3 background, in
theory) and the display's viewing-angle colour shift. Set a known ID, record,
then:

```
python tools/decode_watermark.py phone.mp4 --corners X,Y X,Y X,Y X,Y --expect <id> --verbose
```

If it fails, raise strength and repeat; once it decodes, lower until it
breaks. That measurement is the real deliverable.

**7b. Judge visibility on the real display at 1:1**, over the actual game
content if available, and settle the default strength and the mask floor.
Then try the Alternate polarity switch live: if it is invisible at a higher
strength and the phone test in 7a still decodes with `--alternating`, it
is the better default.

**7c. Seed corners automatically** from the monitor's bezel (edge detection +
quadrilateral fit in OpenCV), so a phone video needs no manual corners; the
same idea applied to window frames on a desktop screenshot would turn the
`--box` requirement into an automatic one.

**7d. Consider a luma fallback** only if grayscale captures turn out to
matter: a low-amplitude luma component alongside the chroma one, read by a
second decoder pass.

### Expectations worth carrying forward

- 32 bits is an identifier, not text.
- This is not adversarially robust: two captures with different IDs can be
  diffed to estimate the pattern. Commercial forensic-marking systems share
  the weakness.
- For pure deterrence, a low-contrast **visible** overlay is what the
  enterprise DLP industry ships for this threat model. The user chose the
  hidden approach knowing this; recorded as context, not a recommendation to
  re-litigate.
