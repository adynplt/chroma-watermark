"""End-to-end demonstration: embed a mark, degrade the frame, decode it back.

Runs entirely in numpy against a model of the shader, so it needs no GPU and no
build -- the point is to show the whole pipeline in one readable file. For the
real thing, build the application and decode an actual screenshot; see the
README's Quick start.

    python examples/roundtrip_demo.py

Requires numpy, scipy and opencv-python.
"""

import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

from decode_watermark import analyse_frames  # noqa: E402
from test_roundtrip import embed  # noqa: E402  (the numpy model of the shader)

MATCH_ID = 0xC0FFEE
STRENGTH = 0.08


def demo_frame(width=1582, height=953):
    """A frame with the qualities that matter to the mark: a bright textured
    region, large smooth gradients, and a dark flat panel.

    Real application output is mostly smooth and often dark, which is the hard
    case -- the perceptual weight deliberately gives those areas almost no
    mark. A uniformly bright, uniformly noisy test image would flatter both the
    robustness and the visibility figures.
    """
    y, x = np.mgrid[0:height, 0:width].astype(np.float32)
    u, v = x / width, y / height

    # A dark scene with a bright gradient sweeping through the lower right,
    # roughly the luma distribution of the application's own backdrop.
    glow = np.exp(-(((u - 0.75) ** 2) * 3.0 + ((v - 0.95) ** 2) * 6.0))
    frame = np.stack([0.08 + 0.75 * glow, 0.05 + 0.45 * glow, 0.12 + 0.55 * glow], axis=-1)

    # Texture only where the content is busy, as in a real frame.
    rng = np.random.default_rng(7)
    texture = 0.06 * rng.standard_normal((height, width, 1)).astype(np.float32)
    frame += texture * glow[..., None]

    # A dark flat UI panel, which carries almost nothing: the weight sees to
    # that, and the decoder expects it to.
    frame[80:400, 60:520] = 0.06
    return np.clip(frame, 0.0, 1.0).astype(np.float32)


def jpeg(frame, quality):
    """Round-trip through JPEG at the given quality."""
    bgr = cv2.cvtColor((np.clip(frame, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8), cv2.COLOR_RGB2BGR)
    ok, buffer = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    assert ok, "JPEG encoding failed"
    decoded = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    return cv2.cvtColor(decoded, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0


def rescale(frame, factor):
    """Downscale and back up, as a resized screenshot would be."""
    height, width = frame.shape[:2]
    frame = np.asarray(frame, dtype=np.float32)
    small = cv2.resize(frame, (int(width * factor), int(height * factor)), interpolation=cv2.INTER_AREA)
    return cv2.resize(small, (width, height), interpolation=cv2.INTER_LINEAR)


def cover(frame, from_row, to_column):
    """Paint the bottom-left block black, as a window overlapping the capture
    would. The decoder detects covered cells and drops them from the vote."""
    covered = np.array(frame, dtype=np.float32, copy=True)
    height, width = covered.shape[:2]
    covered[int(height * from_row):, :int(width * to_column)] = 0.0
    return covered


def visibility(clean, marked):
    """Mean and 95th-percentile CIELAB distance per pixel."""
    # OpenCV's colour conversion takes float32, not the float64 embed returns.
    lab_clean = cv2.cvtColor(np.asarray(clean, dtype=np.float32), cv2.COLOR_RGB2LAB)
    lab_marked = cv2.cvtColor(np.asarray(marked, dtype=np.float32), cv2.COLOR_RGB2LAB)
    delta = np.sqrt(((lab_clean - lab_marked) ** 2).sum(axis=-1))
    return delta.mean(), np.percentile(delta, 95)


def decode(frame, label):
    """Decode one frame and report what came back."""
    result = analyse_frames([frame])
    if result is None:
        print(f"  {label:<28} no frames decoded")
        return False

    presence = result["presence"]["cross_validated"]
    margin = float(np.abs(result["correlation"]).min())
    if not result["present"]:
        print(f"  {label:<28} no mark detected (presence {presence:+.3f})")
        return False

    status = "CRC ok" if result["crc_ok"] else "CRC FAILED"
    correct = "correct" if result["payload"] == MATCH_ID else f"WRONG (wanted {MATCH_ID})"
    print(f"  {label:<28} {result['payload']:>10}  {status}, {correct}  "
          f"(presence {presence:.3f}, weakest bit {margin:.3f})")
    return result["payload"] == MATCH_ID and result["crc_ok"]


def main():
    clean = demo_frame()
    marked = embed(clean, MATCH_ID, STRENGTH)

    mean_delta, p95_delta = visibility(clean, marked)
    print(f"Embedded match ID {MATCH_ID} at strength {STRENGTH}")
    print(f"Visibility on this synthetic frame: mean dE {mean_delta:.2f}, "
          f"95th pct dE {p95_delta:.2f}")
    print("  (Real shader output over the application's backdrop measures 0.35 and 1.6.")
    print("   This frame reads higher because its texture is Gaussian noise, which")
    print("   masks far less than real image detail. Judge visibility at 1:1 on a")
    print("   display, never from a synthetic frame or a downscaled screenshot.)\n")

    print("Decoding the marked frame through a series of degradations:")
    cases = [
        ("lossless", marked),
        ("JPEG quality 75", jpeg(marked, 75)),
        ("JPEG quality 50", jpeg(marked, 50)),
        ("downscaled to 50%", rescale(marked, 0.5)),
        ("Gaussian blur sigma 4", cv2.GaussianBlur(np.asarray(marked, dtype=np.float32), (0, 0), 4.0)),
        ("bottom-left corner covered", cover(marked, 0.7, 0.4)),
    ]
    recovered = sum(decode(frame, label) for label, frame in cases)

    print(f"\nAnd the control, which must find nothing:")
    unmarked_rejected = not decode(clean, "unmarked frame")

    print(f"\n{recovered} of {len(cases)} degradations decoded correctly; "
          f"unmarked frame {'rejected' if unmarked_rejected else 'FALSELY ACCEPTED'}.")
    return 0 if recovered == len(cases) and unmarked_rejected else 1


if __name__ == "__main__":
    sys.exit(main())
