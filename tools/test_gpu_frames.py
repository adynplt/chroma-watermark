"""Decode frames rendered by the actual D3D11 shader.

test_roundtrip.py checks the decoder against a numpy model of the embedding
shader, which would not catch the two drifting apart. This renders real frames
through the GPU instead, using the example's --capture hook, and decodes those.

It also checks three things about the pass itself that only real hardware can
show: that it hands the pipeline state back untouched, that it is the identity
at strength 0 in every supported swap-chain format, and how long it takes.

    python test_gpu_frames.py [path-to-exe]
"""

import os
import re
import subprocess
import sys
import tempfile

import numpy as np

from decode_watermark import analyse_frames
from read_ppm import read_ppm

DEFAULT_EXE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "desktop", "imgui", "examples", "example_win32_directx11", "Release",
    "example_win32_directx11.exe")

PAYLOADS = [0, 1, 42, 1234567, 65535, 2147483648, 4294967295]

# Strengths to sweep around the 0.06 default.
STRENGTHS = [0.02, 0.03, 0.04, 0.06, 0.08, 0.12]

# Swap-chain formats the embedder supports, as the example's --format names.
FORMATS = ["rgba8", "bgra8", "rgb10", "srgb"]

# Client sizes for the timing table.
SIZES = [(1280, 800), (2560, 1440), (3840, 2160)]


class Capture:
    """One run of the example's --capture hook and what it reported."""

    def __init__(self, path, stdout):
        self.path = path
        self.scene_path = path + ".scene.ppm"
        self.state_restored = "pipeline state restored: ok" in stdout
        self.state_message = next((line for line in stdout.splitlines()
                                   if line.startswith("pipeline state restored")), "no state report")
        match = re.search(r"watermark pass: ([0-9.]+) ms", stdout)
        self.pass_ms = float(match.group(1)) if match else None


def capture(exe, out_dir, payload, strength=None, fmt=None, size=None, vsync=True):
    name = f"frame_{payload}_{strength}_{fmt}_{size}.ppm"
    path = os.path.join(out_dir, name)
    command = [exe, "--capture", path, str(payload)]
    if strength is not None:
        command.append(str(strength))
    if fmt is not None:
        command += ["--format", fmt]
    if size is not None:
        command += ["--size", str(size[0]), str(size[1])]
    if not vsync:
        command.append("--novsync")
    completed = subprocess.run(command, check=True, timeout=120, capture_output=True, text=True)
    return Capture(path, completed.stdout)


def decode(path, payload):
    result = analyse_frames([read_ppm(path)], sync="off")
    wrong = bin(result["payload"] ^ payload).count("1")
    ok = wrong == 0 and result["crc_ok"] and result["present"]
    return result, wrong, ok


def describe(result):
    return (f"presence {result['presence']['cross_validated']:.3f}  "
            f"margin {np.abs(result['correlation']).min():.4f}")


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EXE
    if not os.path.exists(exe):
        print(f"executable not found: {exe}", file=sys.stderr)
        return 2

    failures = 0
    state_failures = []
    with tempfile.TemporaryDirectory() as out_dir:
        def run(payload, **kwargs):
            frame = capture(exe, out_dir, payload, **kwargs)
            if not frame.state_restored:
                state_failures.append(frame.state_message)
            return frame

        print("payloads at default strength")
        for payload in PAYLOADS:
            result, wrong, ok = decode(run(payload).path, payload)
            failures += not ok
            print(f"  {payload:10d} -> {result['payload']:10d}  crc {'ok ' if result['crc_ok'] else 'BAD'}  "
                  f"{describe(result)}  {'ok' if ok else f'FAIL ({wrong} bits)'}")

        print("\nstrength sweep, payload 1234567")
        for strength in STRENGTHS:
            result, wrong, _ = decode(run(1234567, strength=strength).path, 1234567)
            status = "ok" if (wrong == 0 and result["crc_ok"]) else ("crc bad" if wrong == 0 else f"{wrong} bits wrong")
            print(f"  {strength:.3f} (~{strength * 255:4.1f}/255) -> {result['payload']:10d}  "
                  f"{describe(result)}  {status}")

        print("\nunmarked frame (strength 0)")
        result, _, _ = decode(run(1234567, strength=0.0).path, 1234567)
        ok = not result["present"]
        failures += not ok
        print(f"  decoded {result['payload']}  presence {result['presence']['cross_validated']:.3f}  "
              f"crc {'ok' if result['crc_ok'] else 'bad'}  -> {'rejected, ok' if ok else 'FAIL: accepted as a mark'}")

        # Each format must decode, and at strength 0 the pass must be the
        # identity: the backbuffer and the unmarked scene texture, both
        # written as 8-bit RGB, must match byte for byte. That is the
        # hardware check of the typeless views and the sRGB round trip.
        print("\nswap-chain formats, payload 1234567")
        for fmt in FORMATS:
            result, wrong, ok = decode(run(1234567, fmt=fmt).path, 1234567)
            unmarked = run(1234567, strength=0.0, fmt=fmt)
            difference = np.abs(read_ppm(unmarked.path) - read_ppm(unmarked.scene_path)) * 255.0
            differing = int((difference.max(axis=2) > 0).sum())
            identity = differing == 0
            failures += (not ok) + (not identity)
            print(f"  {fmt:6s} -> {result['payload']:10d}  {describe(result)}  "
                  f"{'ok' if ok else f'FAIL ({wrong} bits)'};  strength 0 identity: "
                  f"{'exact' if identity else f'FAIL ({differing} pixels differ, max {difference.max():.0f}/255)'}")

        # GPU time of the pass alone, from the embedder's own timestamp
        # queries. Informational: it depends on the machine, and a vsync-idle
        # GPU reports pessimistic numbers, so vsync is off here.
        print("\nwatermark pass time (GPU, vsync off)")
        for size in SIZES:
            frame = run(1234567, size=size, vsync=False)
            reading = f"{frame.pass_ms:.3f} ms" if frame.pass_ms is not None else "not measured"
            print(f"  {size[0]}x{size[1]:<5d} {reading}")

    if state_failures:
        print(f"\npipeline state not restored in {len(state_failures)} run(s): {state_failures[0]}")
        failures += 1
    else:
        print("\npipeline state restored after every run")

    if failures:
        print(f"\n{failures} check(s) failed")
        return 1
    print("all checks passed on real GPU output")
    return 0


if __name__ == "__main__":
    sys.exit(main())
