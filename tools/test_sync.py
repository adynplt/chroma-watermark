"""Check that the decoder can find the grid in a distorted capture.

Takes a frame rendered by the real shader, warps it the way a capture would
(shift, rotation, zoom, perspective, and combinations), and decodes with the
geometric search started from the unhelpful guess that the window fills the
frame. Because the warp is applied here, the true window corners are known,
so the search result is checked for accuracy as well as the recovered ID.

Also feeds unmarked and synthetic images through the same path: the search
will happily find its best geometry on any image, and the presence test has
to reject the result.

    python test_sync.py [frame.ppm] [unmarked.ppm]
"""

import os
import subprocess
import sys
import tempfile
import time

import cv2
import numpy as np

from decode_watermark import (PRESENCE_THRESHOLD, analyse_frames, cell_size_px,
                              corners_from_crop)
from read_ppm import read_ppm
from test_gpu_frames import DEFAULT_EXE

PAYLOAD = 1234567


def to_u8(frame):
    return np.clip(np.round(frame * 255.0), 0, 255).astype(np.uint8)


def warp(frame, matrix):
    """Apply a 3x3 homography to an RGB float frame, replicating the border."""
    height, width = frame.shape[:2]
    warped = cv2.warpPerspective(to_u8(frame), matrix, (width, height),
                                 flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return warped.astype(np.float64) / 255.0


def transform_corners(corners, matrix):
    pts = np.hstack([corners, np.ones((4, 1))]) @ matrix.T
    return pts[:, :2] / pts[:, 2:3]


def similarity(width, height, scale=1.0, degrees=0.0, dx=0.0, dy=0.0):
    rotation = cv2.getRotationMatrix2D((width / 2.0, height / 2.0), degrees, scale)
    matrix = np.vstack([rotation, [0, 0, 1]])
    matrix[0, 2] += dx
    matrix[1, 2] += dy
    return matrix


def perspective(width, height, pull):
    """Tilt: pull the top corners inwards, as a camera looking up at a screen."""
    src = np.float32([[0, 0], [width, 0], [width, height], [0, height]])
    dst = np.float32([[pull, pull * 0.6], [width - pull, pull * 0.6], [width, height], [0, height]])
    return cv2.getPerspectiveTransform(src, dst).astype(np.float64)


def cases(width, height):
    """(name, homography, rough corners given). Cases without corners start from
    the full-frame guess; the rest get the true corners jittered by up to 15 px,
    standing in for a person clicking the window's corners in the capture."""
    yield "identity", np.eye(3), False
    yield "shift 25px (half cell)", similarity(width, height, dx=25), False
    yield "shift 60px", similarity(width, height, dx=60, dy=-40), False
    yield "rotate 2 deg", similarity(width, height, degrees=2), False
    yield "rotate 5 deg", similarity(width, height, degrees=5), False
    yield "zoom out 0.9", similarity(width, height, scale=0.9), False
    yield "zoom out 0.8", similarity(width, height, scale=0.8), False
    yield "perspective 60px", perspective(width, height, 60), False
    yield "rot 3 + zoom 0.9 + shift", similarity(width, height, scale=0.9, degrees=3, dx=30, dy=20), False
    yield "persp 80 + rot 2", perspective(width, height, 80) @ similarity(width, height, degrees=2), False
    yield "perspective 120px", perspective(width, height, 120), True
    yield "zoom 0.6, offset", similarity(width, height, scale=0.6, dx=-200, dy=90), True
    yield "zoom 0.5 + rot 8 + persp", perspective(width, height, 100) @ similarity(width, height, scale=0.5, degrees=8, dx=120, dy=-60), True


def negatives(unmarked, width, height):
    yield "unmarked frame", unmarked
    yield "unmarked, jpeg-like blur", cv2.GaussianBlur(unmarked, (0, 0), 1.5)
    rng = np.random.default_rng(5)
    for k in range(3):
        blob = cv2.GaussianBlur(rng.random((height, width, 3)), (0, 0), 8)
        yield f"random blob {k}", blob
    gradient = np.linspace(0.1, 0.9, width)[None, :, None] * np.ones((height, 1, 3))
    yield "gradient", gradient
    checker = ((np.arange(height)[:, None] // 40 + np.arange(width)[None, :] // 40) % 2).astype(np.float64)
    yield "checkerboard", np.repeat(checker[..., None] * 0.6 + 0.2, 3, axis=2)


def main():
    temp_dir = None
    if len(sys.argv) > 2:
        frame_path, unmarked_path = sys.argv[1], sys.argv[2]
    else:
        if not os.path.exists(DEFAULT_EXE):
            print(f"executable not found: {DEFAULT_EXE}", file=sys.stderr)
            return 2
        temp_dir = tempfile.TemporaryDirectory()
        frame_path = os.path.join(temp_dir.name, "marked.ppm")
        unmarked_path = os.path.join(temp_dir.name, "unmarked.ppm")
        subprocess.run([DEFAULT_EXE, "--capture", frame_path, str(PAYLOAD)], check=True, timeout=120)
        subprocess.run([DEFAULT_EXE, "--capture", unmarked_path, str(PAYLOAD), "0"], check=True, timeout=120)

    frame = read_ppm(frame_path)
    unmarked = read_ppm(unmarked_path)
    height, width = frame.shape[:2]
    true_corners = corners_from_crop(0, 0, width, height)
    cell_w, cell_h = cell_size_px(true_corners)
    print(f"frame {width}x{height}, cell ~{cell_w:.0f}x{cell_h:.0f}px, payload {PAYLOAD}\n")

    failures = 0
    rng = np.random.default_rng(11)
    print("distorted captures")
    print(f"  {'case':<28} start     {'decoded':>11}  crc  present  cv     corner err  secs")
    for name, matrix, rough in cases(width, height):
        expected = transform_corners(true_corners, matrix)
        guess = expected + rng.uniform(-15, 15, size=(4, 2)) if rough else None
        started = time.time()
        result = analyse_frames([warp(frame, matrix)], corners=guess, sync="auto")
        seconds = time.time() - started
        error = np.linalg.norm(result["corners"] - expected, axis=1).max()
        ok = result["present"] and result["crc_ok"] and result["payload"] == PAYLOAD
        failures += not ok
        print(f"  {name:<28} {'rough' if rough else 'frame':<8}  {result['payload']:>11d}  "
              f"{'ok ' if result['crc_ok'] else 'BAD'}  {'yes' if result['present'] else 'NO '}      "
              f"{result['presence']['cross_validated']:.3f}  {error:6.1f}px    {seconds:4.1f}  {'ok' if ok else 'FAIL'}")

    print("\nnegatives, must report no mark")
    print(f"  {'case':<28} {'decoded':>11}  crc  present  cv     pilots  usable")
    for name, image in negatives(unmarked, width, height):
        result = analyse_frames([image], sync="auto")
        ok = not result["present"]
        failures += not ok
        print(f"  {name:<28} {result['payload']:>11d}  {'ok ' if result['crc_ok'] else 'BAD'}  "
              f"{'YES' if result['present'] else 'no '}      {result['presence']['cross_validated']:.3f}  "
              f"{result['presence']['pilots']:.3f}   {result['usable_fraction'] * 100:3.0f}%  "
              f"{'ok' if ok else 'FAIL (false positive)'}")

    print(f"\npresence threshold {PRESENCE_THRESHOLD}")
    if temp_dir is not None:
        temp_dir.cleanup()
    if failures:
        print(f"{failures} case(s) failed")
        return 1
    print("all sync cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
