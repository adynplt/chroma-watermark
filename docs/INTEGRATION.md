# Renderer-side integration

What changes in the host application to embed the watermark, and what to watch
for when putting it into a renderer that is not this demo.

The embedder is a **full-screen post-process pass**. It reads the finished
frame, adds a keyed chroma pattern modulated by what is underneath, and writes
the result to the swap chain. It does not care what drew the frame, so nothing
in the application's own rendering has to change — only where that rendering
lands, and one call before Present.

---

## What this repository changes in the stock example

Measured against upstream Dear ImGui at commit `e0a2f6d`:

| File | Change |
|---|---|
| `main.cpp` | about +450 lines, of which **~20 are the integration**; the rest is the demo backdrop, the control panel, the test capture hook and its format, size and vsync switches |
| `example_win32_directx11.vcxproj` | +4 lines, adding the two new translation units |
| `watermark.cpp/h` | New, about 1090 lines — the embedder, of which roughly a third is state save/restore, format handling and timing |
| `background.cpp/h` | New, 125 lines — demo backdrop only, not part of the watermark |

No upstream ImGui file is modified. The embedder does not depend on ImGui at
all; it takes a Direct3D 11 device and a render target view, which is why the
same object drops into any D3D11 renderer.

---

## The whole change, in five edits

Against the stock example, the integration proper is five edits to `main.cpp`,
about twenty lines. Everything else in this repository is the embedder itself,
the demo's backdrop, the control panel, and a test capture hook — none of which
a real integration needs.

### 1. Include and instantiate

```cpp
#include "watermark.h"

static FrameWatermark g_watermark;
```

One object for the lifetime of the device. It owns an offscreen colour target,
two shaders, a constant buffer, a sampler, and three pipeline state objects.

### 2. Initialise after the device exists, size it to the client area

```cpp
RECT client_rect;
::GetClientRect(hwnd, &client_rect);

bool watermark_ready =
    g_watermark.Initialise(g_pd3dDevice, g_pd3dDeviceContext) &&
    g_watermark.ResizeBuffers(client_rect.right  - client_rect.left,
                              client_rect.bottom - client_rect.top,
                              swapchain_format);   // the format of the view you render with

g_watermark.SetPayload(match_id);   // the 32-bit ID this session stamps
```

`Initialise` compiles the shaders and creates the state objects. `ResizeBuffers`
creates the offscreen target and must be called at least once before the first
frame. Both return `false` rather than throwing; if either fails, keep
rendering without the watermark rather than failing the frame. The format
argument defaults to `DXGI_FORMAT_R8G8B8A8_UNORM`; see below for what else it
accepts.

### 3. Keep the offscreen target in step with the swap chain

In the existing resize handler, next to `ResizeBuffers` on the swap chain:

```cpp
CleanupRenderTarget();
g_pSwapChain->ResizeBuffers(0, g_ResizeWidth, g_ResizeHeight, DXGI_FORMAT_UNKNOWN, 0);
g_watermark.ResizeBuffers(g_ResizeWidth, g_ResizeHeight, swapchain_format);   // <-- added
g_ResizeWidth = g_ResizeHeight = 0;
CreateRenderTarget();
```

Miss this and the mark is embedded on the old grid: the pattern is laid out in
cells relative to the target's dimensions, so a stale size puts every cell in
the wrong place and nothing decodes.

### 4. Render into the offscreen target, then apply

This is the only change to the render loop, and the only one that touches the
application's own drawing:

```cpp
// Before:
//   g_pd3dDeviceContext->OMSetRenderTargets(1, &g_mainRenderTargetView, nullptr);
//   g_pd3dDeviceContext->ClearRenderTargetView(g_mainRenderTargetView, clear_color);
//   ImGui_ImplDX11_RenderDrawData(ImGui::GetDrawData());

const bool use_watermark = watermark_ready && g_watermark.Enabled();

ID3D11RenderTargetView* frame_target =
    use_watermark ? g_watermark.SceneTarget() : g_mainRenderTargetView;

g_pd3dDeviceContext->OMSetRenderTargets(1, &frame_target, nullptr);
g_pd3dDeviceContext->ClearRenderTargetView(frame_target, clear_color);

ImGui_ImplDX11_RenderDrawData(ImGui::GetDrawData());   // unchanged

if (use_watermark)
    g_watermark.Apply(g_mainRenderTargetView);         // resolves to the swap chain
```

**Why the frame cannot be marked in place.** The shader reads the finished
pixel to decide how much mark it can hide there — it needs the local luma and
the local texture. A Direct3D resource cannot be bound as a render target and
a shader resource simultaneously, so the frame is drawn to an offscreen target
first and the pass reads from it while writing to the swap chain. This is the
same reason any tone-mapping or colour-grading pass needs an intermediate.

When the watermark is disabled the frame goes straight to the swap chain as it
did before, and the offscreen target is never touched.

### 5. Release before the device

```cpp
g_watermark.Release();
ImGui_ImplDX11_Shutdown();
```

`Release` is idempotent and safe to call on an object that failed to
initialise.

---

## The API surface

| Call | When | Notes |
|---|---|---|
| `Initialise(device, context)` | Once, after the device exists | Returns false on shader compile or state creation failure |
| `ResizeBuffers(w, h, format)` | Startup and every swap-chain resize | Recreates the offscreen target in the given view format; false for an unsupported format |
| `SceneTarget()` | Each frame, to pick the render target | Null until `ResizeBuffers` succeeds |
| `SceneTexture()` | Tests and debugging | The texture behind `SceneTarget()`, the unmarked frame |
| `Apply(destination)` | Each frame, after drawing, before Present | Draws the marked frame to `destination`; saves and restores the pipeline state it touches |
| `SetPayload(uint32_t)` | Whenever the ID changes | Takes effect next frame; re-derives the pattern |
| `SetStrength(float)` | Tuning | Peak chroma offset at a cell centre; default 0.08 |
| `SetEnabled(bool)` | Toggling | When false, skip `SceneTarget`/`Apply` entirely |
| `SetAlternatePolarity(bool)` | Experimental | Flips the pattern sign every 25 ms |
| `SetRestoresState(bool)` | Only if you push/pop state yourself | Default true; see below |
| `LastPassMilliseconds()` | Profiling | GPU time of the most recent resolved pass; negative until the first sample |
| `Release()` | Before destroying the device | Idempotent |

`SetPayload` is the only one with real cost: it rebuilds the 1024-cell sign
table on the CPU and re-uploads it. Calling it every frame with an unchanged
value is wasteful but harmless; calling it with a changing value every frame
would break decoding, which averages a bit's copies across frames.

---

## Integrating into a renderer that is not this demo

The demo is a clean case: one render target, no depth buffer, one swap-chain
format. Four things to know before putting the pass into a real engine.

### Pipeline state is saved and restored

`Apply` sets the render targets, viewport, rasteriser state, depth-stencil
state, blend state, input layout, primitive topology, all five shader stages,
two pixel-shader resource slots, one sampler and one constant buffer. It reads
every one of those slots on entry and puts them back on exit, the same way
Dear ImGui's own DX11 backend does, so an engine with a state cache sees the
context exactly as it left it. The demo checks this on every `--capture` run
and prints `pipeline state restored: ok`.

Two things cannot be put back, because Direct3D itself unbinds them: any
pixel-shader UAVs (binding a render target clears them) and any shader
resource view over the destination's own resource (the render-target hazard
rule). Neither is normally live at the point just before Present.

If your engine already brackets external passes with its own state push and
pop, `SetRestoresState(false)` skips the save and restore. The pass then still
unbinds its own shader resources on exit, as it always has.

### It must be the last thing before Present

Anything drawn after `Apply` lands on the swap chain unmarked, and worse,
covers marked pixels with unmarked ones. Overlays that composite late — a
Steam overlay, an FPS counter, an injected capture-tool overlay — sit outside
the mark by construction. That is usually harmless, since the decoder tolerates
70% occlusion in most regions, but a late overlay covering the bright part of
the frame costs more than its area suggests, because the perceptual weight put
most of the mark's energy there.

### The offscreen target takes the swap chain's format

`ResizeBuffers(w, h, format)` creates the intermediate to match the render
target view you draw with. Pass the **view** format, which on a flip-model
swap chain may be the sRGB twin of the buffer format. Supported:

| Format | Notes |
|---|---|
| `R8G8B8A8_UNORM`, `B8G8R8A8_UNORM` | The common cases; the default is RGBA8 |
| `R8G8B8A8_UNORM_SRGB`, `B8G8R8A8_UNORM_SRGB` | See below |
| `R10G10B10A2_UNORM` | 10-bit SDR |

Anything else returns `false`: typeless and unknown formats because they are
ambiguous, and float HDR targets because the mark is defined in
display-referred 0..1 units. The shader's clamps are wrong above 1.0, and the
perceptual weight assumes luma in 0..1, so HDR needs a redesign of the weight
model rather than a wider format list.

**How sRGB is handled.** The intermediate is created typeless. Its render
target view carries your format, sRGB or not, so your drawing into it behaves
exactly as drawing into the swap chain did, encode-on-write included. Its
shader resource view is always the plain UNORM twin, so the pass reads the
encoded display codes, which is the domain the perceptual weight and the
Python decoder are defined in. When the destination view is sRGB the shader
decodes its result to linear before writing, so the hardware's encode-on-write
reproduces the encoded value; that decode uses the exact sRGB curve, and
encode(decode(x)) returns x for every 8-bit code. The demo verifies this on
hardware: at strength 0 the swap-chain contents equal the unmarked scene byte
for byte in every supported format. Nothing is ever created over the
destination's own resource, so the same path works for typed sRGB textures and
for flip-model chains with a UNORM buffer and an sRGB view.

The demo takes `--format rgba8|bgra8|rgb10|srgb` to exercise all of this. In
`srgb` mode the demo itself looks brighter, because ImGui writes
already-encoded colours that the sRGB view encodes again; that is a test-mode
artefact, not a watermark bug.

### No depth buffer is bound

`Apply` binds the destination with a null depth-stencil view and uses a
depth-stencil state with depth testing off. A full-screen triangle needs
neither. If your engine asserts that a depth buffer is always bound, this is
where it will complain.

---

## Cost

One full-screen triangle reading one texture, with the perceptual weight
computed in the pixel shader. The weight is the expensive part: local luma
activity costs nine samples (a centre plus eight on a ring), and that is
eroded over a 3×3 of positions, so a pixel that needs the full measure does
**82 texture reads** — 9 × 9 for the weight, plus one for the scene colour.

Most pixels do not need it. Two early-outs skip the reads where they cannot
change the result: a pixel too dark to carry anything has weight 0 whatever
its texture, and because the erosion is a minimum, a centre activity already
at or below the lower threshold makes the texture term exactly 0 no matter
what the other eight positions read. Those pixels cost 1 or 10 reads. The
output is identical at every pixel; the early-outs were checked against the
full shader by byte-comparing captures.

`Apply` times itself with GPU timestamp queries, read back without stalling a
few frames later, and `LastPassMilliseconds()` returns the latest resolved
value. The demo shows it in the Watermark panel and prints it after a
`--capture` run. Measured on an NVIDIA GeForce RTX 5090 with vsync off, over
the demo's backdrop with the demo window open:

| Client size | Full shader | With early-outs |
|---|---|---|
| 1280×800 | 0.048 ms | 0.017 ms |
| 2560×1440 | 0.162 ms | 0.048 ms |
| 3840×2160 | not measured | 0.065 ms |

The cost scales with pixel count and is independent of scene complexity, so
one measurement at your resolution on your hardware generalises. Expect an
integrated GPU to be several times slower. With vsync on the GPU idles between
frames and its clocks drop, so the readout runs 20 to 40% higher than the
pass really costs; the demo's `--novsync` switch exists for measuring.

The one-off costs are the shader compilation in `Initialise` and the offscreen
target allocation in `ResizeBuffers` (width × height × 4 bytes in every
supported format).

**The pass must run at native resolution.** The sampler is
`MIN_MAG_MIP_POINT`, and the cell grid is laid out in the target's own pixels.
Running the mark at a lower internal resolution and upscaling afterwards blurs
the cells and costs margin; embed after any upscaling, not before.

---

## What the application should do with the ID

The demo draws a random 32-bit ID at startup, prints it to stdout, and lets the
panel edit it. A real deployment would set it from whatever identifies the
session — a match ID, a user ID, a hash of a session token — and record that
mapping somewhere the decoder's output can be checked against.

```cpp
printf("match ID: %u\n", match_id);   // so a capture can be checked
fflush(stdout);
g_watermark.SetPayload(match_id);
```

Two constraints worth knowing before designing around it:

- **32 bits is the payload.** Not 33. Widening it means changing
  `WatermarkLayout::PayloadBits` and the decoder's `PAYLOAD_BITS` together,
  and it trades directly against robustness: the cells are a fixed budget, so
  more bits means fewer copies of each.
- **The key is a compile-time constant** (`kWatermarkKey` in `watermark.cpp`,
  `WATERMARK_KEY` in `decode_watermark.py`). Both sides must hold the same
  value. Anyone with the binary can recover it, so it protects against
  accidental collision, not against an adversary.

---

## Verifying an integration

The fastest check that a port still embeds correctly:

```bash
# Render a few frames and dump the backbuffer, bypassing screenshot tooling.
example_win32_directx11.exe --capture out.ppm 1234567 0.08

# Decode it. Exit code 0 and "CRC ok" means the pipeline is intact.
python tools/decode_watermark.py out.ppm --expect 1234567
```

The capture hook in `main.cpp` is about 40 lines and worth porting alongside
the watermark: it writes the backbuffer straight to a PPM, so a failure is
unambiguously in the embedder rather than in a screen-capture path.

Then `python tools/test_gpu_frames.py`, which drives that hook across several
payloads and strengths and decodes each result. It is the suite that catches an
embedder and decoder that have drifted apart. It also asserts the pipeline
state check on every run, decodes a capture in each of the four supported
swap-chain formats, checks that the pass at strength 0 is the identity in each
of them, and prints the pass time at three resolutions.
