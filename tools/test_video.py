"""Decode an H.264 clip that moves like a handheld recording.

Builds a short clip from a frame rendered by the real shader: every frame is
warped by a slowly drifting, slightly perspective similarity so the window
wanders and tilts the way a phone held at a monitor does, then the clip is
encoded with libx264 at a streaming-quality CRF. The decoder has to track the
grid frame by frame and accumulate the cells; averaging the raw frames instead
would smear the mark away.

Needs ffmpeg on the PATH.

    python test_video.py [frame.ppm]
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time

import cv2
import numpy as np

from decode_watermark import analyse_frames, corners_from_crop, load_frames
from read_ppm import read_ppm
from test_gpu_frames import DEFAULT_EXE
from test_sync import perspective, similarity, to_u8, warp

PAYLOAD = 1234567


def build_clip(frame, path, base, frames=90, crf=23, seed=3):
    """Write a jittered clip; return the true corners of the first and last frames."""
    height, width = frame.shape[:2]
    rng = np.random.default_rng(seed)
    temp_dir = tempfile.mkdtemp()
    drift = np.zeros(4)
    corners = []
    for index in range(frames):
        # Random walk in shift, rotation and scale, low-pass filtered.
        drift = 0.9 * drift + rng.normal(0, 1, 4) * np.array([2.0, 2.0, 0.15, 0.003])
        jitter = similarity(width, height, scale=1.0 + drift[3], degrees=drift[2], dx=drift[0], dy=drift[1])
        matrix = jitter @ base
        cv2.imwrite(os.path.join(temp_dir, f"f{index:04d}.png"), cv2.cvtColor(to_u8(warp(frame, matrix)), cv2.COLOR_RGB2BGR))
        pts = np.hstack([corners_from_crop(0, 0, width, height), np.ones((4, 1))]) @ matrix.T
        corners.append(pts[:, :2] / pts[:, 2:3])
    even_w, even_h = width - width % 2, height - height % 2
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", "30",
                    "-i", os.path.join(temp_dir, "f%04d.png"), "-vf", f"crop={even_w}:{even_h}:0:0",
                    "-c:v", "libx264", "-crf", str(crf), "-pix_fmt", "yuv420p", path], check=True)
    shutil.rmtree(temp_dir, ignore_errors=True)
    return corners[0], corners[-1]


def main():
    if shutil.which("ffmpeg") is None:
        print("ffmpeg not found; skipping", file=sys.stderr)
        return 0
    temp_dir = tempfile.TemporaryDirectory()
    if len(sys.argv) > 1:
        frame_path = sys.argv[1]
    else:
        if not os.path.exists(DEFAULT_EXE):
            print(f"executable not found: {DEFAULT_EXE}", file=sys.stderr)
            return 2
        frame_path = os.path.join(temp_dir.name, "marked.ppm")
        subprocess.run([DEFAULT_EXE, "--capture", frame_path, str(PAYLOAD)], check=True, timeout=120)

    frame = read_ppm(frame_path)
    height, width = frame.shape[:2]
    rng = np.random.default_rng(4)
    failures = 0

    # Two recordings. A handheld phone clip: zoomed out, tilted, seen from
    # below, drifting; the decoder is given the first frame's corners jittered
    # by up to 15 px, as a person would click them. And a screen recording:
    # the window slightly smaller than the frame and drifting a little, with
    # no corners given at all.
    clips = (
        ("handheld phone, rough corners", 23,
         perspective(width, height, 70) @ similarity(width, height, scale=0.85, degrees=2, dx=20, dy=10), True),
        ("screen recording, no corners", 23, similarity(width, height, scale=0.92, dx=-15, dy=8), False),
    )
    for name, crf, base, give_corners in clips:
        clip = os.path.join(temp_dir.name, "clip.mp4")
        first_corners, last_corners = build_clip(frame, clip, base, crf=crf)
        guess = first_corners + rng.uniform(-15, 15, size=(4, 2)) if give_corners else None
        print(f"{name}: 90 frames, crf {crf}, payload {PAYLOAD}")
        for max_frames, label in ((1, "single frame"), (10, "10 frames"), (30, "30 frames")):
            started = time.time()
            result = analyse_frames(load_frames(clip, max_frames), corners=guess, sync="auto")
            seconds = time.time() - started
            ok = result["present"] and result["crc_ok"] and result["payload"] == PAYLOAD
            failures += not ok
            print(f"  {label:<13} used {result['frames']:2d}  -> {result['payload']:>11d}  "
                  f"crc {'ok ' if result['crc_ok'] else 'BAD'}  presence {result['presence']['cross_validated']:.3f}  "
                  f"margin {np.abs(result['correlation']).min():.3f}  {seconds:5.1f}s  {'ok' if ok else 'FAIL'}")
        error = np.linalg.norm(result["corners"] - last_corners, axis=1).max()
        print(f"  tracked corners vs true last frame: {error:.1f}px\n")

    temp_dir.cleanup()
    if failures:
        print(f"\n{failures} case(s) failed")
        return 1
    print("\nhandheld clip decoded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
