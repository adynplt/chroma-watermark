"""Decode frames rendered by the actual D3D11 shader.

test_roundtrip.py checks the decoder against a numpy model of the embedding
shader, which would not catch the two drifting apart. This renders real frames
through the GPU instead, using the example's --capture hook, and decodes those.

    python test_gpu_frames.py [path-to-exe]
"""

import os
import subprocess
import sys
import tempfile

import numpy as np

from decode_watermark import analyse_frames
from read_ppm import read_ppm

DEFAULT_EXE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "imgui", "examples", "example_win32_directx11", "Release",
    "example_win32_directx11.exe")

PAYLOADS = [0, 1, 42, 1234567, 65535, 2147483648, 4294967295]

# Strengths to sweep around the 0.06 default.
STRENGTHS = [0.02, 0.03, 0.04, 0.06, 0.08, 0.12]


def capture(exe, out_dir, payload, strength=None):
    path = os.path.join(out_dir, f"frame_{payload}_{strength}.ppm")
    command = [exe, "--capture", path, str(payload)]
    if strength is not None:
        command.append(str(strength))
    subprocess.run(command, check=True, timeout=120)
    return path


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EXE
    if not os.path.exists(exe):
        print(f"executable not found: {exe}", file=sys.stderr)
        return 2

    failures = 0
    with tempfile.TemporaryDirectory() as out_dir:
        print("payloads at default strength")
        for payload in PAYLOADS:
            result = analyse_frames([read_ppm(capture(exe, out_dir, payload))], sync="off")
            ok = result["payload"] == payload and result["crc_ok"] and result["present"]
            failures += not ok
            wrong = bin(result["payload"] ^ payload).count("1")
            print(f"  {payload:10d} -> {result['payload']:10d}  crc {'ok ' if result['crc_ok'] else 'BAD'}  "
                  f"presence {result['presence']['cross_validated']:.3f}  "
                  f"margin {np.abs(result['correlation']).min():.4f}  {'ok' if ok else f'FAIL ({wrong} bits)'}")

        print("\nstrength sweep, payload 1234567")
        for strength in STRENGTHS:
            result = analyse_frames([read_ppm(capture(exe, out_dir, 1234567, strength))], sync="off")
            wrong = bin(result["payload"] ^ 1234567).count("1")
            status = "ok" if (wrong == 0 and result["crc_ok"]) else ("crc bad" if wrong == 0 else f"{wrong} bits wrong")
            print(f"  {strength:.3f} (~{strength * 255:4.1f}/255) -> {result['payload']:10d}  "
                  f"presence {result['presence']['cross_validated']:.3f}  "
                  f"margin {np.abs(result['correlation']).min():.4f}  {status}")

        print("\nunmarked frame (strength 0)")
        result = analyse_frames([read_ppm(capture(exe, out_dir, 1234567, 0.0))], sync="off")
        ok = not result["present"]
        failures += not ok
        print(f"  decoded {result['payload']}  presence {result['presence']['cross_validated']:.3f}  "
              f"crc {'ok' if result['crc_ok'] else 'bad'}  -> {'rejected, ok' if ok else 'FAIL: accepted as a mark'}")

    if failures:
        print(f"\n{failures} payload(s) failed")
        return 1
    print("\nall payloads recovered from real GPU output")
    return 0


if __name__ == "__main__":
    sys.exit(main())
