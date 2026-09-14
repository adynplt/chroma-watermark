"""Check that the mark survives part of the frame being covered up.

Each payload bit is carried by many cells scattered across the frame, so
blacking out a region should cost every bit some of its copies rather than
costing any bit all of them. This measures how much can be covered before the
decode fails, for several shapes and positions of covering.

Needs a captured frame; renders one with the example's --capture hook if no
path is given.

    python test_occlusion.py [frame.ppm]
"""

import os
import subprocess
import sys
import tempfile

import numpy as np

from decode_watermark import decode
from read_ppm import read_ppm
from test_gpu_frames import DEFAULT_EXE

PAYLOAD = 1234567


def cover(frame, region, value):
    """Return a copy of the frame with a region painted a flat value."""
    covered = frame.copy()
    y0, y1, x0, x1 = region
    covered[y0:y1, x0:x1] = value
    return covered


def regions_for(height, width, fraction):
    """Coverings of the given area fraction, in various shapes."""
    band = int(height * fraction)
    side = int(width * fraction)
    # A square block of the same area, placed centrally.
    block = int(np.sqrt(fraction) * min(height, width))
    top = (height - block) // 2
    left = (width - block) // 2
    return {
        "top band": (0, band, 0, width),
        "bottom band": (height - band, height, 0, width),
        "left band": (0, height, 0, side),
        "centre block": (top, top + block, left, left + block),
        "corner block": (0, block, 0, block),
    }


def main():
    if len(sys.argv) > 1:
        frame_path = sys.argv[1]
        temp_dir = None
    else:
        if not os.path.exists(DEFAULT_EXE):
            print(f"executable not found: {DEFAULT_EXE}", file=sys.stderr)
            return 2
        temp_dir = tempfile.TemporaryDirectory()
        frame_path = os.path.join(temp_dir.name, "frame.ppm")
        subprocess.run([DEFAULT_EXE, "--capture", frame_path, str(PAYLOAD)],
                       check=True, timeout=120)

    frame = read_ppm(frame_path)
    height, width, _ = frame.shape
    print(f"frame {width}x{height}, payload {PAYLOAD}\n")

    recovered, correlation = decode(frame)
    print(f"uncovered: {recovered} "
          f"({'ok' if recovered == PAYLOAD else 'FAIL'}), "
          f"margin {np.abs(correlation).min():.4f}\n")

    worst_survived = {}
    print(f"{'covered':>8}  {'shape':<14} {'fill':<6} {'decoded':>11}  wrong  margin")
    for fraction in (0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70):
        for name, region in regions_for(height, width, fraction).items():
            for fill_name, fill in (("black", 0.0), ("white", 1.0)):
                covered = cover(frame, region, fill)
                recovered, correlation = decode(covered)
                wrong = bin(recovered ^ PAYLOAD).count("1")
                if wrong == 0:
                    key = (name, fill_name)
                    worst_survived[key] = max(worst_survived.get(key, 0), fraction)
                print(f"{fraction * 100:7.0f}%  {name:<14} {fill_name:<6} "
                      f"{recovered:11d}  {wrong:5d}  "
                      f"{np.abs(correlation).min():.4f}")
        print()

    print("largest covering survived, by shape:")
    for (name, fill_name), fraction in sorted(worst_survived.items()):
        print(f"  {name:<14} {fill_name:<6} {fraction * 100:3.0f}%")

    if temp_dir is not None:
        temp_dir.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
