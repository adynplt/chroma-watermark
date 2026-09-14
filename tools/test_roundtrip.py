"""Round-trip test for the watermark, independent of the D3D11 application.

Reimplements the embedding shader in numpy, applies it to synthetic frames,
and checks the decoder recovers the payload. This exercises the pattern
layout and the correlation maths; it says nothing about whether the mark
survives a real camera, which only a recorded capture can answer.

    python test_roundtrip.py
"""

import sys

import numpy as np

from decode_watermark import (GRID_COLS, GRID_ROWS, PAYLOAD_BITS, analyse_frames,
                              pattern_for_payload, perceptual_weight)


def build_amplitude_grid(payload):
    """Mirror of UpdatePatternBuffer in watermark.cpp: data bits, CRC and pilots."""
    return pattern_for_payload(payload).reshape(GRID_ROWS, GRID_COLS)


def embed(frame, payload, strength, polarity=1.0):
    """Mirror of the pixel shader: raised-cosine cells, luma-preserving
    blue-yellow offset, perceptual weight, and range clamping."""
    height, width, _ = frame.shape
    grid = build_amplitude_grid(payload)

    ys = (np.arange(height) + 0.5) / height
    xs = (np.arange(width) + 0.5) / width
    cell_y = np.clip((ys * GRID_ROWS).astype(int), 0, GRID_ROWS - 1)
    cell_x = np.clip((xs * GRID_COLS).astype(int), 0, GRID_COLS - 1)
    amplitude = grid[cell_y][:, cell_x]

    frac_y = (ys * GRID_ROWS) % 1.0
    frac_x = (xs * GRID_COLS) % 1.0
    profile = (np.sin(np.pi * frac_y) ** 2)[:, None] * (np.sin(np.pi * frac_x) ** 2)[None, :]

    weight = perceptual_weight(frame, min(width / GRID_COLS, height / GRID_ROWS))

    axis = np.array([-0.12867, -0.12867, 1.0])
    offset = (amplitude * strength * profile * weight * polarity)[..., None] * axis
    room = np.where(offset > 0, (1.0 - frame) / np.maximum(offset, 1e-6),
                    np.where(offset < 0, frame / np.maximum(-offset, 1e-6), 1e6))
    scale = np.clip(room.min(axis=2), 0.0, 1.0)
    return np.clip(frame + offset * scale[..., None], 0.0, 1.0)


def make_scene(width, height, kind):
    """Synthetic frames standing in for typical ImGui content."""
    rng = np.random.default_rng(7)
    if kind == "flat":
        return np.full((height, width, 3), 0.18)
    if kind == "gradient":
        ramp = np.linspace(0.1, 0.8, width)
        return np.repeat(np.repeat(ramp[None, :, None], height, 0), 3, 2)
    if kind == "panels":
        frame = np.full((height, width, 3), 0.10)
        frame[height // 6:height // 2, width // 8:width // 2] = 0.30
        frame[height // 3:, width // 2:] = 0.45
        return frame
    if kind == "noisy":
        return np.clip(0.35 + rng.normal(0.0, 0.05, (height, width, 3)), 0.0, 1.0)
    raise ValueError(kind)


def run_case(kind, payload, strength, quantise=True, noise=0.0, frames=1, alternate=False):
    width, height = 1280, 800
    scene = make_scene(width, height, kind)

    captured = []
    rng = np.random.default_rng(11)
    for index in range(frames):
        polarity = -1.0 if (alternate and index % 2) else 1.0
        marked = embed(scene, payload, strength, polarity)
        if quantise:
            marked = np.round(marked * 255.0) / 255.0
        if noise > 0.0:
            marked = np.clip(marked + rng.normal(0.0, noise, marked.shape), 0.0, 1.0)
        captured.append(marked)

    result = analyse_frames(captured, sync="off", alternating=alternate)
    recovered = result["payload"]
    correlation = result["correlation"][:PAYLOAD_BITS]
    wrong = bin(recovered ^ payload).count("1")
    return recovered, wrong, np.abs(correlation).min()


def main():
    payload = 1234567
    failures = 0

    print("clean embed, 8-bit quantised")
    for kind in ("flat", "gradient", "panels", "noisy"):
        recovered, wrong, margin = run_case(kind, payload, strength=0.06)
        status = "ok" if wrong == 0 else f"FAIL ({wrong} bits)"
        print(f"  {kind:9s} -> {recovered:10d}  margin {margin:.3f}  {status}")
        failures += wrong != 0

    print("\nadditive noise, single frame, strength 0.10")
    for noise in (0.01, 0.03, 0.06):
        recovered, wrong, margin = run_case("panels", payload, 0.10, noise=noise)
        status = "ok" if wrong == 0 else f"{wrong} bits wrong"
        print(f"  sigma {noise:.2f} -> {recovered:10d}  margin {margin:.3f}  {status}")

    print("\nheavy noise, temporal accumulation, strength 0.10, sigma 0.10")
    for frames in (1, 10, 100):
        recovered, wrong, margin = run_case("panels", payload, 0.10,
                                            noise=0.10, frames=frames)
        status = "ok" if wrong == 0 else f"{wrong} bits wrong"
        print(f"  {frames:4d} frames -> {recovered:10d}  margin {margin:.3f}  {status}")

    print("\nalternating polarity, strength 0.08, sigma 0.06, frames aligned by the pilots")
    for frames in (1, 2, 10):
        recovered, wrong, margin = run_case("panels", payload, 0.08, noise=0.06,
                                            frames=frames, alternate=True)
        status = "ok" if wrong == 0 else f"{wrong} bits wrong"
        print(f"  {frames:4d} frames -> {recovered:10d}  margin {margin:.3f}  {status}")
        failures += wrong != 0

    print("\ndistinct payloads, strength 0.06, panels")
    for value in (0, 1, 42, 65535, 2**31, 4294967295):
        recovered, wrong, _ = run_case("panels", value, strength=0.06)
        status = "ok" if wrong == 0 else f"FAIL ({wrong} bits)"
        print(f"  {value:10d} -> {recovered:10d}  {status}")
        failures += wrong != 0

    if failures:
        print(f"\n{failures} case(s) failed")
        return 1
    print("\nall baseline cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
