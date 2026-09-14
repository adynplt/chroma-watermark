"""Minimal binary PPM reader, for the frames the example writes with --capture.

Kept separate from the decoder so the decoder's only hard dependency is numpy;
imageio is needed for real captures but not for reading these test frames.
"""

import numpy as np


def read_ppm(path):
    """Read a binary PPM into a float array in 0..1."""
    with open(path, "rb") as handle:
        magic = handle.readline().strip()
        if magic != b"P6":
            raise ValueError(f"{path}: not a binary PPM")
        line = handle.readline()
        while line.startswith(b"#"):
            line = handle.readline()
        width, height = (int(value) for value in line.split())
        maxval = int(handle.readline().strip())
        if maxval != 255:
            raise ValueError(f"{path}: unsupported maximum value {maxval}")
        data = np.frombuffer(handle.read(width * height * 3), dtype=np.uint8)
    return data.reshape(height, width, 3).astype(np.float64) / 255.0
