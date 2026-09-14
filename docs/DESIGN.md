# Frame watermark

Embeds a 32-bit match ID into the ImGui window's rendered frames as a keyed
chroma pattern, and recovers it from a capture — a screenshot, a screen
recording, or a video of the monitor taken with a phone.

## Running it

Build, then launch normally:

```
cd desktop
./build.sh Release
imgui/examples/example_win32_directx11/Release/example_win32_directx11.exe [--background <image>]
```

The window draws a photograph behind the ImGui panels (`desktop/assets/background.jpg`
by default, a stock Windows wallpaper; any JPEG/PNG/BMP via `--background`).
This is deliberate: flat clear-colour panels are the easiest possible content
for the mark and hid real weaknesses in the decoder. A photo gives the
texture, edges and gradients a real application has.

The **Watermark** panel sets the match ID and the embedding strength, and
toggles the backdrop. Strength is the whole tradeoff: too low and the mark
cannot be recovered, too high and it becomes visible. The default is 0.08,
which is the peak offset on bright textured pixels; flat and dark pixels get
a fraction of it (see *What a cell looks like*). The panel also has an
experimental **Alternate polarity** switch, described below.

To decode a capture:

```
python tools/decode_watermark.py screenshot.png
python tools/decode_watermark.py desktop.jpg --box 1000 150 1900 1200
python tools/decode_watermark.py recording.mp4 --crop 320 180 1600 1080
python tools/decode_watermark.py phone.mp4 --corners 212,88 1710,131 1688,1002 190,957
```

With nothing given the decoder assumes the window fills the frame and
searches from there, which copes with a screen recording that is shifted,
slightly rotated, or zoomed out to 75%. It does not cope with a screenshot
of a whole desktop on which the window is a third of the picture — there is
nothing near the full-frame guess to lock onto and it reports no mark.
For that, give `--box`: a rectangle the window lies somewhere inside. It
can be drawn by hand and generously — title bar, frame and a margin of
desktop included — as long as the window is at least 60% of it in each
dimension; the decoder tries every size and position inside the box (a few
seconds more than a plain decode). `--crop` (axis-aligned rectangle) is
the precise form, to within about a cell, and `--corners` (TL TR BR BL) is
for a window seen in perspective; both are refined. A phone video where
the screen is a small tilted quadrilateral needs `--corners`.

Output reports the window corners found, a presence score, and the match ID
with its CRC status. Exit codes: 0 found, 1 mismatch with `--expect`,
3 no mark detected, 4 mark present but CRC failed. `--verbose` prints the
search score and presence of every frame.

Needs numpy, scipy and opencv-python; video decoding uses opencv's ffmpeg
backend.

## Tests

```
python tools/test_roundtrip.py     # decoder vs a numpy model of the shader
python tools/test_gpu_frames.py    # decoder vs real frames off the GPU
python tools/test_occlusion.py     # decoding with part of the frame covered
python tools/test_sync.py          # finding the grid in warped captures; rejecting unmarked images
python tools/test_video.py         # tracking drifting H.264 clips (needs ffmpeg)
```

The suites that render through the real shader (all but the first) matter
most: they catch the embedder and decoder drifting apart. Four pass on the
photo backdrop; `test_video.py` reports one failure, the handheld clip from
a single frame (it decodes from ten), left as a marker of where the design
stands.

## How it works

### Format

The frame is divided into a **32x32 grid** of cells. The 1024 cells hold:

- **880 data cells**: 40 coded bits x 22 copies. The coded word is the 32-bit
  match ID plus a CRC-8 of it, so a wrong decode is detectable.
- **144 pilot cells**: fixed, key-derived signs independent of the payload.
  They let the decoder find the grid in a distorted capture and tell a
  marked image from an unmarked one.

Each bit's 22 copies are scattered across the frame by a keyed permutation,
half positive and half negative, so covering a region costs every bit a few
copies rather than costing one bit all of them, and an overall colour shift
cancels out. The decoder deals the copies into three groups (8, 7 and 7):
two steer the geometric search, the third is reserved for deciding whether
a mark is present at all. Cells are deliberately large (about 49x30 px at the example's
window size): optical blur and camera downsampling destroy fine detail,
while a coarse pattern survives.

### What a cell looks like

Each cell is a **raised-cosine bump of blue-yellow chroma**, scaled by a
**perceptual weight** computed from the pixels under it. Every part of that
sentence exists because an earlier version was visible: flat-topped luma
tiles read as a grid over any gradient, and smooth chroma bumps at a fixed
amplitude still read as a lattice of blue and yellow patches over dark
panels and dark wallpaper — which is what the second version looked like at
1:1, whatever a downscaled screenshot suggested.

- **Chroma, not luma.** The offset is +a on blue with red and green each
  reduced by 0.1287a, so Rec.601 luma is unchanged. The eye is several times
  less sensitive to low-frequency blue-yellow than to luminance; the decoder
  reads blue minus luma, where the mark appears at full strength and most
  luminance structure is absent. At 49-px cells, 4:2:0 chroma subsampling in
  JPEG and video is harmless.
- **A smooth profile.** sin²(πu)·sin²(πv) across the cell: full strength at
  the centre, zero at the edges, no step anywhere for the eye to pick out.
- **Perceptual weight.** The amplitude at a pixel is
  `strength * profile * weight`, with

  ```
  weight = saturate(luma / 0.30) * (0.30 + 0.70 * texture)
  ```

  `texture` is a smoothstep between 0.010 and 0.040 of the local luma
  activity (mean absolute difference to eight taps on a ring 6% of a cell
  away) after an erosion over a 3x3 of positions at twice that spacing, so a
  lone edge with flat colour beside it — text on a panel — scores as flat.
  Dark pixels carry less because a fixed offset on a near-black panel reads
  as a blue patch on black (and a camera records shadows with the most noise
  anyway); flat pixels carry 30% of the full amount; bright busy pixels all
  of it. The offset is also scaled down wherever it would push a channel past
  0 or 1.

  The decoder recomputes the same weight from the capture and uses it as
  each cell's expected amplitude, so the vote is a matched filter: a cell
  the embedder barely touched barely counts. Without that the decoder's old
  inverse-variance weighting trusted flat cells most — exactly where the
  mark now is weakest — and the masked mark did not decode at all.

The mark is identical on every frame by default, which is what lets a video
encoder preserve it: a static pattern is predicted from the previous frame
and costs the encoder little to keep.

**Alternate polarity** (off by default) flips the sign of the whole pattern
every 25 ms of wall-clock time. A low-contrast chroma flicker at that rate
averages to nothing for the eye, so the mark can be stronger for the same
visibility on a live display, and a screen recording keeps every frame
intact; the decoder aligns each frame's sign from the pilots before
accumulating (a static scene's own structure then cancels between frames
while the mark adds up). The cost is the camera: an exposure spanning both
polarities cancels the mark. Filming a bright screen normally lands at
1/60 s or shorter, which catches one polarity; 1/30 s and longer loses most
of it. That is why it is off by default and marked experimental; it has been
tested only in the numpy model (`test_roundtrip.py`), not filmed.

Measured on the real shader output over the wallpaper and the ImGui panels,
comparing 1:1 crops against the unmarked frame:

| Design | Mean ΔE | 95th pct ΔE | Look at 1:1 |
|---|---|---|---|
| chroma bump, fixed 0.06 + edge boost (previous) | 2.9 | 10.1 | lattice of blue/yellow patches on dark and smooth areas, yellow patches on header bars |
| perceptual weight, 0.04 | 0.15 | 0.7 | nothing found |
| **perceptual weight, 0.08 (default)** | 0.35 | 1.6 | nothing found on panels, gradients or header bars |
| perceptual weight, 0.12 | 0.55 | 2.7 | faint tint on the bright gradient if looking for it |

ΔE is CIELAB distance per pixel, unmarked vs marked; around 1 is the usual
side-by-side threshold, and these figures do not credit texture masking, so
they overstate what is seen on busy content.

### Decoding

1. **Locate the grid.** A homography maps grid coordinates to the capture.
   Starting from the caller's corners (or the full frame), a similarity
   grid search — quarter-cell shift steps, 1° rotation steps, 2.5% scale
   steps, scales from 75% for a full-frame guess or 90% for given corners —
   scores some thirty thousand geometries on a cheap 3x3-sample kernel and
   keeps the best two dozen mutually distinct ones. Each is refined briefly
   by Nelder-Mead over the eight corner coordinates, the best three by the
   cross-validated score are polished, and the best of those wins. The
   search objective is the pilots' gain-weighted correlation plus a
   *split product*: each bit's trimmed vote over group A of its copies
   times its vote over group B, averaged over the bits — large only when
   both groups agree, which they do only on the right grid. Group V of the
   copies is never read during the search.
2. **Sample the cells** through the homography: 49 points covering each cell
   on a blurred copy of the blue-minus-luma plane, and the same points on
   the recomputed perceptual-weight plane for the cell's expected gain.
3. **Matched kernel.** Each cell's response is its samples weighted by the
   bump profile *minus the profile's mean*: a zero-mean kernel that is blind
   to anything flat or linear across the cell. Because every bump is zero on
   its cell's edges, a cell's samples contain none of its neighbours' bumps,
   so there is no self-interference to cancel. (The previous estimator —
   profile mean minus a 3x3 mean of neighbouring cells — leaked a third of
   the neighbours' amplitude into every cell and needed two rounds of
   iterative cancellation to claw part of it back. Replacing it raised the
   presence of the *old* fixed-amplitude mark from 0.61 to 0.81 on the same
   frame.) Cells whose response is exactly zero are covered, and uniform
   cells next to them are treated as covered too.
4. **Vote.** Weight = expected gain / (1 + variance / typical variance).
   Each bit's copies are sorted and the extreme quarter at each end dropped
   before a weighted average.
5. **Check.** The CRC must match. Presence is a cross-validated score: every
   bit is decided from groups A and B of its copies (with the same trimmed
   vote) and scored against group V, gain-weighted, which took no part in
   the decision or the search. Unbiased around zero on an unmarked image
   (spread about 0.05 over the test negatives, worst 0.05); 0.4–0.5 on a
   marked one at the default strength over the dark test scene. The
   threshold is 0.12, and at least 20% of cells must be usable.

For video, the grid is found on the first frame with the full search. Each
later frame is refined from two starts — the previous frame's solution and
the first frame's — keeping the better by the cross-validated score; every
ten frames, if the score has fallen below half the first frame's, a narrow
search is run again from the current estimate. With `--alternating` each
frame's sign is aligned from its pilots. Each frame is decoded alone and
its cells accumulated in proportion to its own presence score, so a badly
tracked frame contributes little.

### Details that turned out to matter

Each found by a failing test or by looking at the output; each would have
produced a plausible-looking but broken or unshippable system:

- **Look at 1:1, not at a downscaled screenshot.** The original flat-topped
  luma cells were an obvious grid on smooth gradients. Chroma with a smooth
  profile at a fixed amplitude was judged "close to invisible" from a
  reduced image and was plainly a lattice of coloured patches at 1:1 on the
  actual display. The perceptual weight fixed that; the matched kernel paid
  for it.
- **Masking and inverse-variance weighting fight each other.** Concentrating
  the mark where texture hides it puts it where the decoder trusted cells
  least. The decoder has to know the embedder's weight; recomputing it from
  the capture works because it depends only on the scene.
- **Cell positions are a permutation, not a hash.** Hashing collided 86 times
  out of 256 and silently destroyed copies the decoder expected.
- **Bits are assigned by division, not by hash**, so every bit gets the same
  number of copies with balanced signs.
- **No neighbour-based background at all.** A median cancelled the mark
  inside uniform regions, a 3x3 mean leaked neighbours' bumps into every
  cell; the zero-mean kernel inside the cell needs neither.
- **Covered cells are detected by a zero residual, not by variance**, and the
  mask is extended to uniform neighbours; cells at a covered region's edge
  otherwise vote at maximum weight with a scene-driven value.
- **Votes are trimmed**, in the decoder, the search objective, and the
  presence decision. On a photograph the raw sum over eleven cells is
  dominated by whichever bright edge one lands on.
- **The grid search steps a quarter cell, one degree and 2.5%.** The
  matched kernel's response is down to half at a quarter-cell offset and
  negative at half a cell, and on a 32-cell-wide window one degree or 2.5%
  of scale moves the far corners by half a cell. The earlier half-cell,
  2°, 5% grid, built for a wider-basin estimator and a stronger mark,
  missed a zoomed-out screen recording outright.
- **The data term of the search is a split product over two groups of
  copies.** The earlier mean magnitude of one group's votes was biased
  upwards on any structured residual; once the perceptual gains
  concentrated the weight on a few bright cells, its noise floor over the
  grid came within 0.1 of the true peak, and Nelder-Mead grew skewed
  quadrilaterals over scene structure that outscored the true grid (0.69
  against 0.61). The split product gave the truth 1.36 against a best wrong
  candidate of 0.54 on the same frame.
- **Presence is scored on copies the search never read.** With the search
  using two groups, ranking its candidates by the cross-validated presence
  score was tried: it locked on well and turned every unmarked test image
  into a confident positive (0.19–0.25), because presence became the
  maximum of a noisy score over two dozen candidates plus a polish pushing
  it up. Hence the third group, reserved for presence.
- **Keep many candidates.** The truth is reliably among the best couple of
  dozen grid candidates on a dark scene, not reliably first.
- **Video tracking anchors to the first frame and weights frames.** Refining
  only from the previous frame compounded settling errors to 50 px.
- **Tried and rejected:** a blur pyramid for the search and vertical keystone
  terms in the coarse grid — neither found a zoomed-and-tilted window from a
  full-frame guess, and both cost time. Such a capture needs rough corners.

## Measured behaviour

All figures from frames rendered by the real shader over the photo backdrop
with the ImGui panels, payload 1234567, default strength 0.08 unless stated.
This scene is dark (mean luma 0.13) and mostly smooth, so the perceptual
weight leaves it a small budget; it is a hard case for the new design and
the figures below are lower than the previous fixed-amplitude design's on
purpose. Brighter, busier content carries more.

### Strength (lossless capture)

| Strength | Presence | Margin |
|----------|----------|--------|
| 0.02 | 0.12 | fails (3 bits) |
| 0.03 | 0.22 | 0.05 |
| 0.04 | 0.29 | 0.24 |
| 0.06 | 0.44 | 0.35 |
| **0.08** | 0.52 | 0.39 |
| 0.12 | 0.61 | 0.43 |

All seven test payloads (0, 1, 42, 1234567, 65535, 2^31, 2^32-1) decode with
CRC at 0.08. An unmarked frame scores -0.01 and is rejected.

### Compression, scaling, blur, video

Margin is the weakest bit's correlation; a decode is correct while it is
positive, and comfortable above about 0.1.

| Degradation | Margin at 0.08 | Margin at 0.12 | Margin at 0.04 |
|---|---|---|---|
| lossless | 0.42 | 0.48 | 0.30 |
| JPEG q90 / q75 / q50 / q30 | 0.28 / 0.12 / 0.05 / 0.01 | 0.40 / 0.25 / 0.18 / 0.08 | 0.05 / fails / fails / fails |
| downscale to 75% / 50% / 33% and back | 0.42 / 0.44 / 0.41 | ≥ 0.46 | ≥ 0.23 |
| Gaussian blur sigma 2 / 4 / 8 px | 0.44 / 0.46 / 0.23 | ≥ 0.39 | 0.23 / 0.19 / fails |
| H.264 crf 18 / 23 / 28 / 35, 30 frames | 0.31 / 0.24 / 0.12 / 0.11 | 0.45 / 0.35 / 0.28 / 0.29 | 0.17 / 0.12 / 0.04 / fails |
| **converted to grayscale** | **lost** | — | — |

Everything but grayscale decodes at 0.08; JPEG q30 does so by a hair. OBS
records around crf 18–23. For comparison the previous fixed-amplitude design
at 0.06 kept a margin of 0.62 or better through all of these — that is what
was spent on invisibility, on this scene. A black-and-white copy of the
capture has no chroma and therefore no mark.

### Geometry (`test_sync.py`)

Decoded from the full-frame guess unless marked *rough* (true corners
jittered by up to 15 px, as a person would click them). Corner error is the
worst of the four corners; the matched kernel reads through a good deal of
it, and the search settles for a geometry that reads the mark rather than
the exact window edge.

| Capture | Start | Corner error | Presence |
|---|---|---|---|
| identity | frame | 19 px | 0.43 |
| shift 25 px / 60 px | frame | 27 / 27 px | 0.51 / 0.38 |
| rotate 2° / 5° | frame | 32 / 23 px | 0.50 / 0.50 |
| zoom out to 90% / 80% | frame | 16 / 15 px | 0.51 / 0.49 |
| perspective 60 px | frame | 61 px | 0.30 |
| rotate 3° + zoom 90% + shift | frame | 20 px | 0.52 |
| perspective 80 px + rotate 2° | frame | 78 px | 0.30 |
| perspective 120 px | rough | 38 px | 0.40 |
| zoom to 60%, offset 200 px | rough | 12 px | 0.49 |
| zoom to 50% + rotate 8° + perspective | rough | 19 px | 0.47 |

All decode with CRC. About 7 s for a first frame, full-frame or from
rough corners (12–16 s with other work on the machine). The search was
40 s before its inner loop was vectorised: the grid stage now scores all
289 shifts of a (scale, rotation) pair through one sampling call, each
bit's trimmed vote is one sort over a padded table instead of a Python
loop, and sampling goes through cv2.remap rather than scipy. None of it
changes the arithmetic beyond remap's 1/32 px coordinate rounding, which
on planes blurred by several pixels moves the statistics by under 0.1%.
Negatives — an unmarked photo frame, the same blurred, three random blobs,
a gradient, a checkerboard — score at most -0.02 after the search has done
its best on them; six further unmarked wallpapers put through the full
search score at most 0.05. All rejected at 0.12 (the gradient and
checkerboard have no chroma at all and are rejected for having no usable
cells).

### Video (`test_video.py`)

90-frame H.264 clips at crf 23, the window drifting frame to frame.

| Clip | Start | 1 frame | 10 frames | 30 frames | Corners tracked to |
|---|---|---|---|---|---|
| handheld: zoom 85%, 2°, seen from below | rough | **1 bit wrong** (presence 0.23, margin 0.00) | presence 0.47, margin 0.12 | presence 0.32, margin 0.01 | 33 px |
| screen recording: zoom 92%, drifting | frame | presence 0.31, margin 0.13 | presence 0.43, margin 0.18 | presence 0.37, margin 0.09 | 18 px |

The screen recording decodes from a single frame. The handheld clip needs
about ten frames: from one frame the weakest bit sits at zero. Note that
accumulating more frames does not keep improving it — the scene is static
so its interference is the same in every frame, and only the tracking
jitter averages. Real gameplay changes the scene under a fixed mark, which
is the case accumulation is built for. About 1 s per tracked frame.

### Covering part of the frame (`test_occlusion.py`)

| Covering | Survives up to |
|---|---|
| top band, left band, centre block, corner block (black or white) | 70%, the largest tried |
| bottom band, black / white | 40% / 30% |

The bottom band is the exception because of the perceptual weight: the
brightest part of the wallpaper is along the bottom, and 55% of the mark's
energy sits in the bottom 40% of this frame. Energy follows the content now,
so what can be covered depends on where the content is.

## What has not been tested

**A real phone camera.** Everything above is synthetic distortion of real
shader output, and the margins are now thin enough on dark content that the
camera test matters more than it did. A camera adds moiré against the monitor's pixel grid, rolling
shutter banding, auto-exposure and white-balance drift, lens distortion, and
the display's own gamma and viewing-angle colour shift — the last two matter
more for a chroma mark than they did for luma. To find the real operating point: set
a known ID, film the monitor, run the decoder with rough corners and
`--verbose`, and raise strength until it decodes, then lower it until it
fails. Over real game content (brighter and busier than the test scene)
expect more margin than the tables above.

**Polarity alternation on a camera.** Only modelled, never filmed. Decode
such a recording with `--alternating`.

## Limits worth knowing

- 32 bits is an identifier, not text. Longer payloads trade directly against
  robustness.
- **Grayscale kills it.** Any pipeline that discards colour discards the mark.
- **Dark, smooth scenes carry little.** The perceptual weight gives a black
  screen nothing and a flat mid-grey 30% of the strength. A menu on a dark
  background may not decode from one screenshot; a video of it, or of
  gameplay, accumulates. This is the invisibility trade, made deliberately.
- **Energy follows the content**, so covering the bright part of the frame
  costs more than covering the dark part.
- The auto search finds the window when it roughly fills the frame; a phone
  video needs `--corners`. Detecting the monitor's bezel automatically would
  remove that step.
- The mark is not adversarially robust. Two captures with different IDs can
  be diffed to estimate the pattern. Commercial forensic-marking systems share
  the weakness.
- The key (`kWatermarkKey` in `watermark.cpp`, `WATERMARK_KEY` in
  `decode_watermark.py`) is a constant in the source. Anyone with the binary
  can recover it.
