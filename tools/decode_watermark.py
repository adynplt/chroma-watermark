"""Recover the embedded match ID from a capture of the watermarked window.

Accepts a still image or a video file. The decoder first locates the cell grid
in the capture by searching for the pilot pattern (a set of cells with fixed,
key-derived signs), which lets it cope with a capture that is shifted, scaled,
rotated or seen in perspective. It then reads the coded bits by correlating the
remaining cells against the key, checks the embedded CRC, and reports how
confident it is that a mark is present at all.

    python decode_watermark.py capture.png
    python decode_watermark.py recording.mp4 --crop 320 180 1600 1080
    python decode_watermark.py phone.mp4 --corners 212,88 1710,131 1688,1002 190,957

Give --crop or --corners when the capture contains anything besides the
window. The search then refines from there; it will not find a window that is
a small unknown quadrilateral somewhere in a larger frame on its own.

Requires numpy, scipy and opencv-python. Video needs opencv's ffmpeg backend.
"""

import argparse
import sys

import cv2
import numpy as np
from scipy.optimize import minimize

# Must match the constants in watermark.h / watermark.cpp.
WATERMARK_KEY = 0x5A17C0DE
PAYLOAD_BITS = 32
CRC_BITS = 8
CODED_BITS = PAYLOAD_BITS + CRC_BITS
REPEAT_PER_BIT = 22
DATA_CELLS = CODED_BITS * REPEAT_PER_BIT
PILOT_CELLS = 144
TOTAL_CELLS = DATA_CELLS + PILOT_CELLS
GRID_COLS = 32
GRID_ROWS = 32
assert GRID_COLS * GRID_ROWS == TOTAL_CELLS

# Presence decision. The cross-validated data score (see presence_scores) is
# near zero on an unmarked image and well above this on a marked one; the
# value was set from measurements on marked frames after JPEG, blur and H.264
# against unmarked frames and synthetic negatives. See WATERMARK.md.
PRESENCE_THRESHOLD = 0.12

# A presence claim also needs enough of the grid to be readable. An image whose
# residual is zero almost everywhere (a smooth gradient, say) leaves a handful
# of structured edge cells, and a statistic over those is meaningless.
MIN_USABLE_FRACTION = 0.20

U32 = 0xFFFFFFFF

# Perceptual weighting of the mark, shared with the pixel shader (see
# kMask* in watermark.cpp; keep both in step). The embedded amplitude at a
# pixel is strength * profile * weight, where weight is
#     saturate(luma / MASK_KNEE) * (MASK_FLOOR + (1 - MASK_FLOOR) * texture)
# and texture is a smoothstep over the local luma activity after an erosion,
# so that a lone edge (text on a flat panel) does not count as texture.
# Dark pixels carry less: a fixed offset on a near-black panel reads as a
# blue patch on black, and a camera records shadows with the most noise
# anyway. Flat pixels carry MASK_FLOOR of the full amount, textured ones all
# of it. The decoder recomputes this weight from the capture and uses it as
# each cell's expected amplitude.
MASK_KNEE = 0.30
MASK_FLOOR = 0.30
MASK_T0 = 0.010
MASK_T1 = 0.040
# Activity tap radius, as a fraction of a cell; the erosion samples at twice
# this spacing. Expressed in cells so a capture at any scale measures the
# same thing.
MASK_RADIUS_CELLS = 0.06


# --------------------------------------------------------------------------
# Key-derived layout. Every function here is a port of its C++ counterpart in
# watermark.cpp and must stay bit-identical.
# --------------------------------------------------------------------------

def hash_u32(x):
    """Port of HashU32."""
    x &= U32
    x ^= x >> 16
    x = (x * 0x7FEB352D) & U32
    x ^= x >> 15
    x = (x * 0x846CA68B) & U32
    x ^= x >> 16
    return x


def crc8(value):
    """Port of Crc8: polynomial 0x07, no reflection, zero init, 4 bytes LSB first."""
    crc = 0
    for byte in range(4):
        crc ^= (value >> (8 * byte)) & 0xFF
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def describe_cell(cell_index):
    """Port of DescribeCell: (bit index or -1 for a pilot, sign)."""
    if cell_index >= DATA_CELLS:
        h = hash_u32(WATERMARK_KEY ^ hash_u32(cell_index + 0x50110000))
        return -1, (-1.0 if h & 1 else 1.0)
    bit_index = cell_index // REPEAT_PER_BIT
    copy_index = cell_index % REPEAT_PER_BIT
    h = hash_u32(WATERMARK_KEY ^ hash_u32(bit_index))
    second_half = copy_index >= REPEAT_PER_BIT // 2
    flip = bool(h & 1)
    return bit_index, (-1.0 if (second_half != flip) else 1.0)


def _build_position_table():
    """Port of CellPositionTable: keyed Fisher-Yates over the grid."""
    table = list(range(TOTAL_CELLS))
    for i in range(TOTAL_CELLS - 1, 0, -1):
        j = hash_u32(WATERMARK_KEY + 0x9E3779B9 + i) % (i + 1)
        table[i], table[j] = table[j], table[i]
    return table


_POSITION_TABLE = _build_position_table()


def scramble_cell_position(cell_index):
    """Port of ScrambleCellPosition."""
    return _POSITION_TABLE[cell_index]


# Per grid position: which coded bit it carries (-1 for pilots), its sign, and
# which group of its bit's copies it belongs to. Everything downstream indexes
# by grid position, so these are built once.
POS_BIT = np.full(TOTAL_CELLS, -1, dtype=np.int64)
POS_SIGN = np.zeros(TOTAL_CELLS, dtype=np.float64)
POS_GROUP = np.zeros(TOTAL_CELLS, dtype=np.int64)
for _cell in range(TOTAL_CELLS):
    _bit, _sign = describe_cell(_cell)
    _pos = scramble_cell_position(_cell)
    POS_BIT[_pos] = _bit
    POS_SIGN[_pos] = _sign
    POS_GROUP[_pos] = (_cell % REPEAT_PER_BIT) % 3 if _bit >= 0 else -1
IS_PILOT = POS_BIT < 0
IS_DATA = ~IS_PILOT
# Each bit's 22 copies are dealt into three groups by copy index modulo 3
# (8, 7 and 7 copies). Groups A and B steer the geometric search, whose
# objective is the product of the two groups' votes, and rank its
# candidates. Group V takes no part in the search at all, so a presence
# score computed on it cannot have been inflated by it. An earlier split
# into two halves had the search using one and presence the other; once
# the search objective needed both halves, presence had to move to a third
# group -- ranking the refined candidates by the presence statistic itself
# turned every unmarked test image into a confident positive.
DATA_GROUP_A = IS_DATA & (POS_GROUP == 0)
DATA_GROUP_B = IS_DATA & (POS_GROUP == 1)
DATA_GROUP_V = IS_DATA & (POS_GROUP == 2)
DATA_SEARCH = DATA_GROUP_A | DATA_GROUP_B


def coded_word(payload):
    """Payload with its CRC-8 above it, as embedded."""
    return payload | (crc8(payload) << PAYLOAD_BITS)


def pattern_for_coded(coded):
    """Signed amplitude per grid position for a coded word, pilots included."""
    pattern = POS_SIGN.copy()
    for pos in np.flatnonzero(IS_DATA):
        if not (coded >> int(POS_BIT[pos])) & 1:
            pattern[pos] = -pattern[pos]
    return pattern


def pattern_for_payload(payload):
    """Mirror of UpdatePatternBuffer: the full embedded pattern for a payload."""
    return pattern_for_coded(coded_word(payload))


# --------------------------------------------------------------------------
# Geometry: reading cell means through a homography.
# --------------------------------------------------------------------------

def luma(frame):
    """Rec.601 luma, matching the shader's weights."""
    return frame[..., 0] * 0.299 + frame[..., 1] * 0.587 + frame[..., 2] * 0.114


def mark_plane(frame):
    """The plane the mark lives in: blue minus luma.

    The shader adds a luma-preserving blue-yellow offset, +a on blue with red
    and green reduced to cancel it in luma, so blue minus luma carries the
    full offset a while ordinary luminance structure is largely removed.
    """
    return frame[..., 2] - luma(frame)


def smoothstep(edge0, edge1, x):
    t = np.clip((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _shift(plane, dy, dx):
    """Shift with edge clamping, like a clamped texture fetch."""
    h, w = plane.shape
    ys = np.clip(np.arange(h) + dy, 0, h - 1)
    xs = np.clip(np.arange(w) + dx, 0, w - 1)
    return plane[ys][:, xs]


def texture_activity(lum, radius):
    """Mean absolute luma difference to eight taps on a ring of the given radius."""
    taps = [(0, radius), (0, -radius), (radius, 0), (-radius, 0),
            (radius, radius), (radius, -radius), (-radius, radius), (-radius, -radius)]
    total = np.zeros_like(lum)
    for dy, dx in taps:
        total += np.abs(_shift(lum, dy, dx) - lum)
    return total / len(taps)


def perceptual_weight(frame, cell_px):
    """Per-pixel relative amplitude of the mark, 0..1, as the shader applies it.

    `cell_px` is the cell size in this image's pixels, so the activity taps
    scale with the capture.
    """
    lum = luma(frame)
    radius = max(1, int(round(MASK_RADIUS_CELLS * cell_px)))
    activity = texture_activity(lum, radius)
    eroded = activity.copy()
    for dy in (-2 * radius, 0, 2 * radius):
        for dx in (-2 * radius, 0, 2 * radius):
            eroded = np.minimum(eroded, _shift(activity, dy, dx))
    texture = smoothstep(MASK_T0, MASK_T1, eroded)
    return np.clip(lum / MASK_KNEE, 0.0, 1.0) * (MASK_FLOOR + (1.0 - MASK_FLOOR) * texture)


def corners_from_crop(x, y, w, h):
    """Corners (TL, TR, BR, BL) of an axis-aligned window rectangle."""
    return np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.float64)


def full_frame_corners(plane):
    height, width = plane.shape
    return corners_from_crop(0, 0, width, height)


# Sample points covering each cell, in grid units. Two weightings are built
# over them:
#
#   SAMPLE_WEIGHTS  the cell's raised-cosine profile, normalised to sum to 1;
#                   used to read the expected gain of a cell.
#   SAMPLE_KERNEL   the profile minus its own mean over the cell, scaled so
#                   the response to a unit bump is 1. This is a matched
#                   filter for one cell's bump that is blind to anything flat
#                   or linear across the cell. Because every bump is zero on
#                   its cell's edges, a cell's samples contain no part of its
#                   neighbours' bumps, so this reads the mark with no
#                   self-interference at all. The earlier estimator (profile
#                   mean minus a 3x3 mean of neighbouring cells) leaked a
#                   third of the neighbours' amplitude into every cell and
#                   needed an iterative cancellation pass to claw it back.
_SAMPLES_PER_SIDE = 7
_offsets = (np.arange(_SAMPLES_PER_SIDE) + 0.5) / _SAMPLES_PER_SIDE
_rows, _cols = np.meshgrid(np.arange(GRID_ROWS), np.arange(GRID_COLS), indexing="ij")
_oy, _ox = np.meshgrid(_offsets, _offsets, indexing="ij")
SAMPLE_U = ((_cols.reshape(-1, 1) + _ox.reshape(1, -1)) / GRID_COLS).reshape(-1)
SAMPLE_V = ((_rows.reshape(-1, 1) + _oy.reshape(1, -1)) / GRID_ROWS).reshape(-1)
SAMPLES_PER_CELL = _SAMPLES_PER_SIDE * _SAMPLES_PER_SIDE
_profile = (np.sin(np.pi * _oy) ** 2 * np.sin(np.pi * _ox) ** 2).reshape(-1)
SAMPLE_WEIGHTS = _profile / _profile.sum()
_kernel = _profile - _profile.mean()
SAMPLE_KERNEL = _kernel / (_kernel * _profile).sum()


def _sample_set(per_side):
    """(u, v, profile weights, matched kernel) for a per_side x per_side grid over every cell."""
    offsets = (np.arange(per_side) + 0.5) / per_side
    oy, ox = np.meshgrid(offsets, offsets, indexing="ij")
    u = ((_cols.reshape(-1, 1) + ox.reshape(1, -1)) / GRID_COLS).reshape(-1)
    v = ((_rows.reshape(-1, 1) + oy.reshape(1, -1)) / GRID_ROWS).reshape(-1)
    profile = (np.sin(np.pi * oy) ** 2 * np.sin(np.pi * ox) ** 2).reshape(-1)
    kernel = profile - profile.mean()
    return u, v, profile / profile.sum(), kernel / (kernel * profile).sum()


# The geometric search evaluates its objective tens of thousands of times.
# The grid stage uses 3 x 3 samples per cell; the refinement and the final
# read use the 7 x 7 set above (a 5 x 5 refinement left corners 20 px off).
FAST_U, FAST_V, _, FAST_KERNEL = _sample_set(3)
# The gain plane is blurred and slowly varying, so during the search one
# sample at each cell's centre stands for the cell.
CENTRE_U = ((_cols.reshape(-1) + 0.5) / GRID_COLS)
CENTRE_V = ((_rows.reshape(-1) + 0.5) / GRID_ROWS)


def _homogeneous(u, v):
    """Sample points as a 3 x N matrix a homography can be applied to. Single
    precision: the sampler takes single-precision coordinates and rounds
    them to 1/32 px anyway, and the projection of fifty thousand points is
    done fourteen thousand times per decode."""
    return np.stack([u, v, np.ones_like(u)], axis=0).astype(np.float32)


def _centre_index(per_side):
    """Index, within each cell's samples, of the one at the cell centre."""
    return np.arange(TOTAL_CELLS) * per_side * per_side + (per_side // 2) * per_side + per_side // 2


SAMPLE_PTS = _homogeneous(SAMPLE_U, SAMPLE_V)
FAST_PTS = _homogeneous(FAST_U, FAST_V)
# The gain plane is read at the cell centres only during the search; the
# centre is one of the samples of every odd-sided set, so no second
# projection is needed.
SAMPLE_CENTRE = _centre_index(_SAMPLES_PER_SIDE)
FAST_CENTRE = _centre_index(3)


def homography_from_corners(corners):
    src = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=np.float32)
    return cv2.getPerspectiveTransform(src, np.asarray(corners, dtype=np.float32))


def cell_size_px(corners):
    """Approximate cell dimensions in pixels for the given window corners."""
    corners = np.asarray(corners)
    width = 0.5 * (np.linalg.norm(corners[1] - corners[0]) + np.linalg.norm(corners[2] - corners[3]))
    height = 0.5 * (np.linalg.norm(corners[3] - corners[0]) + np.linalg.norm(corners[2] - corners[1]))
    return width / GRID_COLS, height / GRID_ROWS


class FramePlanes:
    """The mark plane and the expected-gain plane of one frame, blurred and
    ready to be sampled through any homography."""

    def __init__(self, frame, corners):
        self.raw = np.ascontiguousarray(mark_plane(frame), dtype=np.float64)
        cw, ch = cell_size_px(corners)
        # Blur so a point sample stands for a neighbourhood about a quarter of
        # a cell wide; the sample grid then covers the cell interior evenly.
        sigma = max(0.5, 0.125 * min(cw, ch))
        self.blurred = cv2.GaussianBlur(self.raw, (0, 0), sigma)
        gain = np.ascontiguousarray(perceptual_weight(frame, min(cw, ch)), dtype=np.float64)
        self.gain = cv2.GaussianBlur(gain, (0, 0), sigma)

    # Sampling goes through cv2.remap rather than scipy's map_coordinates:
    # the same bilinear lookup with edge clamping, seven times faster, and
    # it takes a whole batch of geometries in one call. Its coordinates are
    # fixed point at 1/32 px, which on planes blurred by several pixels
    # changes nothing measurable.
    @staticmethod
    def _project(corners, pts):
        """Pixel x and y of the sample points under the corners' homography."""
        mapped = homography_from_corners(corners).astype(np.float32) @ pts
        return mapped[0] / mapped[2], mapped[1] / mapped[2]

    @staticmethod
    def _sample(plane, x, y):
        """Bilinear samples of the plane at (x, y); shape follows the maps.
        remap limits each map dimension to 32767, so the maps are folded
        into (geometries x grid rows) by (grid columns x samples per cell)
        for the call and unfolded after."""
        shape = x.shape
        folded = (-1, shape[-1] // GRID_ROWS)
        samples = cv2.remap(plane, np.ascontiguousarray(x, dtype=np.float32).reshape(folded),
                            np.ascontiguousarray(y, dtype=np.float32).reshape(folded),
                            cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        return samples.reshape(shape)

    def residuals(self, corners, fast=False):
        """Per-cell matched-kernel response and expected gain; what the search
        needs. `fast` selects the 3 x 3 sample set for the grid stage."""
        pts, kernel, centre = (FAST_PTS, FAST_KERNEL, FAST_CENTRE) if fast else (SAMPLE_PTS, SAMPLE_KERNEL, SAMPLE_CENTRE)
        x, y = self._project(corners, pts)
        response = self._sample(self.blurred, x[None], y[None]).reshape(TOTAL_CELLS, -1) @ kernel
        gains = self._sample(self.gain, x[None, centre], y[None, centre]).reshape(-1)
        return response, gains

    def shifted_residuals(self, corners, shifts, fast=True):
        """residuals() for the corners translated by each (dx, dy) row of
        `shifts`, in two sampling calls; returns (shifts x cells) arrays.
        A translation of the window moves every sample by the same pixel
        offset, so no homography is rebuilt per candidate."""
        pts, kernel, centre = (FAST_PTS, FAST_KERNEL, FAST_CENTRE) if fast else (SAMPLE_PTS, SAMPLE_KERNEL, SAMPLE_CENTRE)
        shifts = np.asarray(shifts, dtype=np.float32)
        x, y = self._project(corners, pts)
        samples = self._sample(self.blurred, x[None] + shifts[:, 0:1], y[None] + shifts[:, 1:2])
        response = samples.reshape(len(shifts), TOTAL_CELLS, -1) @ kernel
        gains = self._sample(self.gain, x[None, centre] + shifts[:, 0:1], y[None, centre] + shifts[:, 1:2])
        return response, gains

    def sample(self, corners):
        """Per-cell matched-kernel response (blurred plane), variance (raw plane) and expected gain."""
        x, y = self._project(corners, SAMPLE_PTS)
        x, y = x[None], y[None]
        residual = self._sample(self.blurred, x, y).reshape(TOTAL_CELLS, -1) @ SAMPLE_KERNEL
        gains = self._sample(self.gain, x, y).reshape(TOTAL_CELLS, -1) @ SAMPLE_WEIGHTS
        raw = self._sample(self.raw, x, y).reshape(TOTAL_CELLS, SAMPLES_PER_CELL)
        return residual, raw.var(axis=1), gains


# --------------------------------------------------------------------------
# Cell-domain processing.
# --------------------------------------------------------------------------

def _median_last_axis(values):
    """np.median(values, axis=-1, keepdims=True), by a partial sort. Same
    result (the mean of the two middle elements for the even cell count);
    a third of the cost, and the search computes two per candidate."""
    n = values.shape[-1]
    middle = (n - 1) // 2, n // 2
    parted = np.partition(values, middle, axis=-1)
    return 0.5 * (parted[..., middle[0]:middle[0] + 1] + parted[..., middle[1]:middle[1] + 1])


def condition(residual, usable=None):
    """Clamp outliers and normalise, as fed to the correlators.

    The spread is measured over usable cells only. If most cells are exactly
    zero (covered, or a smooth ramp the background removes entirely) a median
    over everything is zero too, and nothing gets clamped.
    """
    if usable is None:
        # The search calls this on a whole batch of candidate geometries at
        # once, residual shaped (candidates, cells); every statistic is taken
        # along the last axis.
        centre = _median_last_axis(residual)
        spread = _median_last_axis(np.abs(residual - centre))
        limit = np.where(spread > 1e-12, 3.0 * spread, np.inf)
        residual = np.clip(residual, -limit, limit)
        scale = np.abs(residual).mean(axis=-1, keepdims=True)
        return residual / np.where(scale > 1e-12, scale, 1.0)
    live = residual[usable]
    if live.size == 0:
        return residual
    spread = np.median(np.abs(live - np.median(live)))
    if spread > 1e-12:
        residual = np.clip(residual, -3.0 * spread, 3.0 * spread)
    scale = np.abs(residual[usable]).mean()
    if scale > 1e-12:
        residual = residual / scale
    return residual


def pearson(a, b):
    a = a - a.mean()
    b = b - b.mean()
    denominator = np.linalg.norm(a) * np.linalg.norm(b)
    return float((a * b).sum() / denominator) if denominator > 1e-12 else 0.0


def pilot_score(residual, usable=None):
    """Correlation of the residual with the pilot signs."""
    mask = IS_PILOT if usable is None else (IS_PILOT & usable)
    return pearson(residual[mask], POS_SIGN[mask]) if mask.sum() > 2 else 0.0


_UNIT_WEIGHTS = np.ones(TOTAL_CELLS)

# Added to every cell's expected gain when it is used as a weight, so a frame
# whose gain estimate is near zero everywhere still votes rather than
# dividing by nothing.
GAIN_EPSILON = 0.02


def search_statistic(residual, gains, polarity=1.0):
    """How strongly a mark reads at the current geometry, independent of payload.

    The pilots contribute their gain-weighted correlation with their keyed
    signs. The data cells contribute a split product: each bit's trimmed
    vote over group A of its copies times its vote over group B, averaged
    over the bits. At the right geometry both groups vote the same way and
    the product is large; at a wrong one the groups are independent and the
    product averages to zero. Group V is never touched here. That is the property that matters: the
    earlier data term, the mean magnitude of the half-A votes, is biased
    upwards on any structured residual, and once the perceptual gains had
    concentrated the weight on a few bright cells its noise floor rose to
    within 0.1 of the true peak over a coarse grid of 2800 candidates (the
    best wrong candidate 0.52 against the truth 0.61 on a screen recording;
    with this statistic 0.54 against 1.36). Nelder-Mead had also learned to
    grow skewed quadrilaterals over scene structure that outscored the true
    grid under the old term.

    The statistic is unbiased at a wrong geometry, so the same value ranks
    the refined candidates. It is signed: the pilot term and the products
    are multiplied by the polarity being assumed. The matched kernel reads a
    grid displaced by half a cell with every sign flipped at about half
    strength, so taking the absolute value (tried, for the polarity option)
    let those half-cell peaks compete; the polarity is instead carried per
    candidate through the search.
    """
    # Accepts a batch: residual and gains shaped (candidates, cells) give a
    # statistic per candidate. The grid search scores hundreds at a time.
    conditioned = condition(residual)
    weights = gains + GAIN_EPSILON
    signed = conditioned * POS_SIGN * polarity
    pilot_weights = weights[..., IS_PILOT]
    pilot_term = (signed[..., IS_PILOT] * pilot_weights).sum(axis=-1) / pilot_weights.sum(axis=-1)
    votes_a, _ = trimmed_bit_votes(signed, weights, BITS_A)
    votes_b, _ = trimmed_bit_votes(signed, weights, BITS_B)
    # The product is scaled so that a fully aligned mark scores about the
    # same as its pilot term.
    statistic = 0.5 * pilot_term + 2.0 * (votes_a * votes_b).mean(axis=-1)
    return float(statistic) if statistic.ndim == 0 else statistic


class BitTable:
    """The cells of each bit as one padded index matrix, with the trimming
    decided per row in advance. With it the votes of all bits, for a whole
    batch of candidate geometries, come out of one sort and a few array
    operations; the earlier loop over the bits with a sort each was half
    the decoder's running time."""

    def __init__(self, cells_of_bit):
        counts = np.array([len(cells) for cells in cells_of_bit], dtype=np.int64)
        width = max(1, int(counts.max()))
        self.index = np.zeros((CODED_BITS, width), dtype=np.int64)
        self.valid = np.zeros((CODED_BITS, width), dtype=bool)
        for bit, cells in enumerate(cells_of_bit):
            self.index[bit, :len(cells)] = cells
            self.valid[bit, :len(cells)] = True
        self.live = counts
        # Positions in each row's sorted order that survive the trim. The
        # padding sorts last, so the real values occupy the first `count`.
        drop = np.where(counts > 4, np.maximum(1, (counts * 0.25).astype(np.int64)), 0)
        position = np.arange(width)[None, :]
        self.keep = (position >= drop[:, None]) & (position < (counts - drop)[:, None])


def trimmed_bit_votes(signed, weights, cells_of_bit):
    """Trimmed, weighted mean of `signed` over each bit's cells.

    Each bit's copies are sorted and the most extreme quarter at each end
    dropped before a weighted average. This is what makes the spatial
    redundancy pay off: a few copies landing on high-contrast content are
    outvoted rather than dragging the average across zero. Returns the vote
    per bit and the number of live copies it was made from.

    `cells_of_bit` is a list of index arrays or a prebuilt BitTable. Leading
    axes of `signed` and `weights` are batch axes.
    """
    table = cells_of_bit if isinstance(cells_of_bit, BitTable) else BitTable(cells_of_bit)
    values = np.where(table.valid, signed[..., table.index], np.inf)
    order = np.argsort(values, axis=-1)
    values = np.where(table.keep, np.take_along_axis(values, order, axis=-1), 0.0)
    vote_weights = np.take_along_axis(np.broadcast_to(weights[..., table.index], values.shape),
                                      order, axis=-1) * table.keep
    total = vote_weights.sum(axis=-1)
    votes = (values * vote_weights).sum(axis=-1) / np.where(total > 1e-12, total, 1.0)
    return np.where(total > 1e-12, votes, 0.0), table.live


# Cells of each bit, by grid position, for the full set and for each group.
CELLS_OF_BIT = [np.flatnonzero(POS_BIT == bit) for bit in range(CODED_BITS)]
CELLS_OF_BIT_A = [np.flatnonzero(DATA_GROUP_A & (POS_BIT == bit)) for bit in range(CODED_BITS)]
CELLS_OF_BIT_B = [np.flatnonzero(DATA_GROUP_B & (POS_BIT == bit)) for bit in range(CODED_BITS)]
CELLS_OF_BIT_SEARCH = [np.flatnonzero(DATA_SEARCH & (POS_BIT == bit)) for bit in range(CODED_BITS)]
BITS_A = BitTable(CELLS_OF_BIT_A)
BITS_B = BitTable(CELLS_OF_BIT_B)


def vote(residual, weights, occluded):
    """Decode a coded word from a conditioned residual: trimmed votes, then signs."""
    cells = [c[~occluded[c]] for c in CELLS_OF_BIT]
    correlation, live = trimmed_bit_votes(residual * POS_SIGN, weights, cells)
    coded = 0
    for bit in range(CODED_BITS):
        if correlation[bit] > 0.0:
            coded |= 1 << bit
    return coded, correlation, live


def decode_cells(residual, variances, gains):
    """Decode a coded word from per-cell kernel responses, variances and expected gains.

    Returns (coded, per-bit correlation, live copies per bit, occluded mask).
    The gain is the perceptual weight the embedder applied, re-estimated from
    the capture: a cell in a dark or flat area carries a fraction of the
    amplitude that a bright textured cell does, and the vote is weighted
    accordingly (a matched filter, up to the trimming).
    """
    # A painted-over region is uniform, so the zero-mean kernel returns zero
    # to floating-point precision. Variance cannot make this call: most of a
    # flat ImGui window has near-zero cell variance too, and it is the mark
    # that separates the two.
    occluded = np.abs(residual) < 1e-7

    # Covered cells along the edge of a covered region are missed by that
    # test: their samples reach uncovered pixels, so their response is not
    # zero -- it is large, and scene-driven -- while their variance is
    # small, which the weighting below reads as reliable. Extend the mask
    # onto uniform cells adjacent to a covered one.
    uniform = variances < 1e-9
    grid = occluded.reshape(GRID_ROWS, GRID_COLS)
    padded = np.pad(grid, 1, mode="constant")
    near_covered = np.zeros_like(grid)
    for dy in range(3):
        for dx in range(3):
            near_covered |= padded[dy:dy + GRID_ROWS, dx:dx + GRID_COLS]
    occluded = occluded | (uniform & near_covered.reshape(-1))

    # Cells full of detail give an unreliable reading of the mark, and cells
    # the embedder left nearly untouched have little to say either way.
    typical = np.median(variances)
    weights = (gains + GAIN_EPSILON) / (1.0 + variances / (typical + 1e-9))
    weights = np.where(occluded, 0.0, weights)

    usable = ~occluded
    coded, correlation, live = vote(condition(residual, usable), weights, occluded)
    return coded, correlation, live, occluded


def presence_scores(residual, occluded, coded, gains):
    """Statistics for whether a mark is present at all.

    cross_validated: each bit is decided from groups A and B of its copies,
    with the same trimmed vote the decoder uses, and that decision is scored
    against group V. Group V took no part in the decision or in the
    geometric search, so on an unmarked image this is unbiased around zero;
    with 280 cells and gain weighting its spread on the test negatives is
    about 0.05. It is the primary presence signal. With a plain untrimmed
    sum deciding the bits, textured content made the decision noisier than
    the decoder's and a correctly decoded frame could fail it.

    pilots: correlation over all pilots at the final geometry. The search
    maximised this, so it runs high on any image; reported for reference.

    full: correlation of the whole reconstructed pattern with the residual,
    likewise optimistic and reported for reference only.
    """
    usable = ~occluded
    conditioned = condition(residual, usable)
    signed = conditioned * POS_SIGN

    weights = gains + GAIN_EPSILON
    cells_search = [c[usable[c]] for c in CELLS_OF_BIT_SEARCH]
    votes_search, _ = trimmed_bit_votes(signed, weights, cells_search)
    bit_values = np.where(votes_search > 0, 1.0, -1.0)
    score = DATA_GROUP_V & usable
    expected = POS_SIGN[score] * bit_values[POS_BIT[score]] * weights[score]
    observed = conditioned[score]
    energy = np.sqrt((observed ** 2).sum() * (expected ** 2).sum())
    cross_validated = float((observed * expected).sum() / energy) if energy > 1e-12 else 0.0

    pattern = pattern_for_coded(coded) * weights
    full = pearson(conditioned[usable], pattern[usable]) if usable.sum() > 2 else 0.0
    return {"cross_validated": cross_validated, "pilots": pilot_score(conditioned, usable), "full": float(full)}


# --------------------------------------------------------------------------
# Geometric search.
# --------------------------------------------------------------------------

def _search_objective(planes, polarity=1.0):
    def objective(flat):
        residual, gains = planes.residuals(flat.reshape(4, 2))
        return -search_statistic(residual, gains, polarity)
    return objective


STATIC_POLARITY = (1.0,)
EITHER_POLARITY = (1.0, -1.0)


def _similarity(corners, scale, degrees, dx, dy):
    """Scale and rotate the quad about its centroid, then translate."""
    corners = np.asarray(corners, dtype=np.float64)
    centre = corners.mean(axis=0)
    theta = np.deg2rad(degrees)
    rot = np.array([[np.cos(theta), -np.sin(theta)], [np.sin(theta), np.cos(theta)]])
    return (corners - centre) @ (scale * rot).T + centre + np.array([dx, dy])


def refine_corners(planes, corners, fractions=(0.3, 0.1), max_evaluations=600, polarity=1.0):
    """Local 8-parameter refinement of the window corners by Nelder-Mead.

    One pass per entry in `fractions`, each starting a simplex that wide (in
    cells). The default pair reaches the peak with a wide simplex and then
    settles on it with a narrow one; a single pass stopped pixels short.
    The polarity is fixed for the whole refinement.
    """
    objective = _search_objective(planes, polarity)
    x = np.asarray(corners, dtype=np.float64).reshape(-1)
    value = objective(x)
    for fraction in fractions:
        cw, ch = cell_size_px(x.reshape(4, 2))
        step = fraction * min(cw, ch)
        simplex = np.vstack([x] + [x + step * np.eye(8)[i] for i in range(8)])
        result = minimize(objective, x, method="Nelder-Mead",
                          options={"initial_simplex": simplex, "xatol": 0.02, "fatol": 1e-6,
                                   "maxfev": max_evaluations})
        if result.fun < value:
            x, value = result.x, result.fun
    return x.reshape(4, 2), -value


def _grid_search(planes, corners, cw, ch, cells, cell_step, degrees, degree_step, scales,
                 polarities=STATIC_POLARITY):
    """Score a similarity grid about the corners on the fast sample set;
    return every (value, candidate, polarity), unsorted."""
    shifts_x = np.arange(-cells, cells + 1e-9, cell_step) * cw
    shifts_y = np.arange(-cells, cells + 1e-9, cell_step) * ch
    shifts = np.array([(dx, dy) for dx in shifts_x for dy in shifts_y])
    rotations = np.arange(-degrees, degrees + 1e-9, degree_step)
    found = _Candidates()
    for scale in scales:
        for rotation in rotations:
            base = _similarity(corners, scale, rotation, 0.0, 0.0)
            found.score(planes, base, shifts, polarities)
    return found


class _Candidates:
    """Scored candidate geometries of a coarse search, as arrays: the box
    search produces a hundred thousand of them, too many for a list of
    tuples to sort."""

    def __init__(self):
        self.values, self.corners, self.polarities = [], [], []

    def score(self, planes, base, shifts, polarities):
        """Score `base` translated by every row of `shifts`, on the fast
        kernel, under each polarity."""
        residual, gains = planes.shifted_residuals(base, shifts, fast=True)
        translated = base[None, :, :] + shifts[:, None, :]
        for polarity in polarities:
            self.values.append(search_statistic(residual, gains, polarity))
            self.corners.append(translated)
            self.polarities.append(np.full(len(shifts), polarity))

    def add(self, planes, corners, polarities):
        """Score one geometry as it stands."""
        self.score(planes, np.asarray(corners, dtype=np.float64), np.zeros((1, 2)), polarities)

    def best(self):
        """The single best entry, as (corners, value, polarity)."""
        return self.distinct(0.0, 1)[0]

    def distinct(self, min_separation, keep):
        """Best `keep` entries, as (corners, value, polarity), whose corners
        differ from every better one by at least `min_separation` px."""
        values = np.concatenate(self.values)
        corners = np.concatenate(self.corners)
        polarities = np.concatenate(self.polarities)
        chosen = []
        for k in np.argsort(-values):
            if all(np.abs(corners[k] - other[0]).max() >= min_separation for other in chosen):
                chosen.append((corners[k], float(values[k]), float(polarities[k])))
                if len(chosen) == keep:
                    break
        return chosen


def coarse_search(planes, corners, cells=2.0, degrees=4.0, scale_range=0.06, min_scale=0.75, keep=24,
                  polarities=STATIC_POLARITY):
    """Similarity grid search around the initial corners: quarter-cell shift
    steps, one-degree rotation steps, 2.5% scale steps from `min_scale` up.
    Returns the best `keep` mutually distinct candidates for refinement.

    The steps are set by the matched kernel's basin: its response is down
    to half at a quarter-cell offset and goes negative at half a cell, and
    on a 32-cell-wide window one degree of rotation or 2.5% of scale moves
    the far corners by half a cell. The earlier two-stage search (half-cell
    steps, two degrees, 5%) was built for a wider-basin estimator and a
    stronger mark; with the perceptual weighting it missed the truth on a
    zoomed-out screen recording outright.

    Many candidates are kept because the statistic's noise floor over some
    thirty thousand geometries comes within a few tenths of the true peak
    on a dark scene; the truth is reliably among the best couple of dozen
    but not reliably first. Refinement plus the cross-validated ranking in
    locate() sorts them out.

    The family is similarities only. Adding vertical keystone and height
    scale to the grid was tried for a window that is both zoomed out and
    seen from below: it did not help, because a coarse grid in six
    dimensions cannot land within half a cell at all four corners at once,
    and it tripled the search time. Such a capture needs rough corners from
    the caller; from there the refinement locks on reliably.
    """
    cw, ch = cell_size_px(corners)
    corners = np.asarray(corners, dtype=np.float64)
    scales = np.arange(min_scale, 1.0 + scale_range + 1e-9, 0.025)
    found = _grid_search(planes, corners, cw, ch, cells, 0.25, degrees, 1.0, scales, polarities=polarities)
    found.add(planes, corners, polarities)
    return found.distinct(0.5 * min(cw, ch), keep)


# Candidates per sampling call in the box search. The sampler folds the
# maps to (candidates x grid rows) rows, which must stay under 32767.
BOX_BATCH = 512
# Box-search candidates given the axis-aligned local grid before Nelder-Mead.
BOX_LOCAL = 12


def axis_aligned_refine(planes, corners, polarity=1.0, size_range=0.05, size_step=0.0125,
                        shift_cells=0.375, shift_step=0.125):
    """Local grid over the width, height and position of an axis-aligned
    window, about its centre; returns the best (corners, value, polarity).

    The box search's 5% size steps leave the far corners up to 0.4 cell
    off, outside the basin the eight-parameter Nelder-Mead refinement can
    climb out of: from such starts it settled at 1.00-1.07 on a desktop
    screenshot whose true peak read 1.17, and a false lock at 1.12 won.
    This grid brings a candidate to within about an eighth of a cell at
    every corner first, which is what the grid search's own quarter-cell
    steps give Nelder-Mead in the isotropic case.
    """
    corners = np.asarray(corners, dtype=np.float64)
    x0, y0 = corners[0]
    w, h = corners[1, 0] - corners[0, 0], corners[3, 1] - corners[0, 1]
    factors = np.arange(1.0 - size_range, 1.0 + size_range + 1e-9, size_step)
    steps = np.arange(-shift_cells, shift_cells + 1e-9, shift_step)
    found = _Candidates()
    for fw in factors:
        for fh in factors:
            ww, hh = w * fw, h * fh
            cw, ch = ww / GRID_COLS, hh / GRID_ROWS
            base = corners_from_crop(x0 + 0.5 * (w - ww), y0 + 0.5 * (h - hh), ww, hh)
            shifts = np.array([(dx * cw, dy * ch) for dx in steps for dy in steps])
            found.score(planes, base, shifts, (polarity,))
    return found.best()


def box_search(planes, box, keep=24, polarities=STATIC_POLARITY, min_fraction=0.6, size_step=0.05):
    """Find the window somewhere inside an axis-aligned box.

    For screen captures: the window is unrotated and at 1:1 scale, but a
    desktop screenshot leaves its size and place unknown, and the
    full-frame search assumes the window is most of the picture. Given a
    box drawn around the window -- title bar, frame and a margin of desktop
    included is fine -- every window size from `min_fraction` of the box
    to the whole of it is tried, width and height independently in
    `size_step` steps, at every quarter-cell position that keeps the window
    inside the box. About a hundred thousand candidates for a generous box,
    a few seconds; the survivors go through the same refinement as the
    grid search's.

    Finding the window with no box at all was looked into and is not
    practical at this mark strength: with the signs unknown, a sign-blind
    statistic (the energy of the matched-filtered plane, folded at each
    candidate pitch) does not see the lattice under the scene's own
    chroma, even on a lossless capture that the window fills; and the
    pilots alone, which a fast correlation could use, read at about four
    standard deviations on a screenshot, below the maximum of the noise
    over the millions of positions and sizes a whole desktop offers.
    """
    x0, y0, box_w, box_h = box
    fractions = np.arange(min_fraction, 1.0 + 1e-9, size_step)
    found = _Candidates()
    for fw in fractions:
        for fh in fractions:
            w, h = fw * box_w, fh * box_h
            cw, ch = w / GRID_COLS, h / GRID_ROWS
            xs = np.arange(0.0, box_w - w + 1e-9, 0.25 * cw)
            ys = np.arange(0.0, box_h - h + 1e-9, 0.25 * ch)
            shifts = np.array([(dx, dy) for dx in xs for dy in ys])
            base = corners_from_crop(x0, y0, w, h)
            for start in range(0, len(shifts), BOX_BATCH):
                found.score(planes, base, shifts[start:start + BOX_BATCH], polarities)
    smallest_cell = min_fraction * min(box_w / GRID_COLS, box_h / GRID_ROWS)
    return found.distinct(0.5 * smallest_cell, keep)


def _ranked(planes, entries):
    """Re-score (corners, _, polarity) entries with the search statistic on
    the full 7x7 kernel; best first. Ranking by the presence statistic was
    tried and gave every unmarked image a confident positive, because
    presence is then the maximum of a noisy score over two dozen candidates
    and a polish that pushed it up further."""
    scored = []
    for corners, _, polarity in entries:
        residual, gains = planes.residuals(corners)
        scored.append((corners, search_statistic(residual, gains, polarity), polarity))
    scored.sort(key=lambda item: -item[1])
    return scored


def locate(planes, corners, mode="auto", polarities=STATIC_POLARITY, box=None, **search):
    """Find the window grid in the frame, starting from the given corners.

    Returns (corners, search statistic, polarity). "auto" runs the grid
    search (or, with `box`, the box search), refines each candidate briefly
    and keeps the best; "refine" only polishes locally; "off" trusts the
    corners. `polarities` lists the pattern signs to consider: just +1 for
    a static mark, both when the embedder alternates. Allowing both when
    the mark is static is not free: the matched kernel reads a grid
    displaced by half a cell with every sign flipped at about half
    strength, and on a weak capture that candidate can beat the truth once
    the sign is free -- it did, on the zoomed-out geometry case.
    """
    corners = np.asarray(corners, dtype=np.float64)
    if mode == "off":
        return _ranked(planes, [(corners, 0.0, p) for p in polarities])[0]
    if mode == "auto":
        if box is not None:
            candidates = [axis_aligned_refine(planes, candidate, polarity)
                          for candidate, _, polarity in box_search(planes, box, polarities=polarities)[:BOX_LOCAL]]
        else:
            candidates = coarse_search(planes, corners, polarities=polarities, **search)
        refined = [refine_corners(planes, candidate, max_evaluations=250, polarity=polarity) + (polarity,)
                   for candidate, _, polarity in candidates]
        # Polish the best three properly.
        best = _ranked(planes, refined)[:3]
        refined = [refine_corners(planes, candidate, polarity=polarity) + (polarity,)
                   for candidate, _, polarity in best]
    else:
        refined = [refine_corners(planes, corners, polarity=polarity) + (polarity,) for polarity in polarities]
    return _ranked(planes, refined)[0]


# --------------------------------------------------------------------------
# Whole-capture decoding.
# --------------------------------------------------------------------------

def analyse_frames(frames, corners=None, sync="auto", verbose=False, resync_every=10,
                   alternating=False, box=None, **search):
    """Decode across an iterable of RGB float frames in 0..1.

    `box` is an axis-aligned (x, y, w, h) rectangle the window lies
    somewhere inside; see box_search. It replaces the grid search on the
    first frame.

    `alternating` says the embedder had polarity alternation on, so each
    frame may carry the pattern with either sign: the search then considers
    both signs and each frame is aligned from its pilots before it
    accumulates.

    The grid is located on the first frame with a full search. Later frames
    are refined from two starts -- the previous frame's solution and the
    first frame's -- and the better-scoring result is kept; if a frame's
    score falls below half the first frame's, a narrow search is run again
    from the current estimate (checked every `resync_every` frames). Refining only from the
    previous frame let small settling errors compound: over a drifting clip
    the tracked grid wandered 50 px from where a fresh search put it, and the
    mark it read faded by a third. Cell responses are accumulated across frames
    and decoded once at the end; averaging raw frames instead would smear a
    handheld recording.
    """
    sum_residual = np.zeros(TOTAL_CELLS)
    sum_variances = np.zeros(TOTAL_CELLS)
    sum_gains = np.zeros(TOTAL_CELLS)
    total_weight = 0.0
    count = 0
    current = None
    anchor = None
    scores = []
    polarities = EITHER_POLARITY if alternating else STATIC_POLARITY
    # Given corners are trusted to within a cell or so; the full-frame guess
    # is not, and gets the wide grid.
    extent = dict(cells=0.75, degrees=2.0, min_scale=0.90, scale_range=0.06) if corners is not None \
        else dict(cells=2.0, degrees=4.0, min_scale=0.75, scale_range=0.06)
    extent.update(search)
    anchor_score = 0.0
    for frame in frames:
        if current is None:
            if box is not None:
                # A stand-in for the first frame's blur scale only: a window
                # of middling size within the box.
                x0, y0, box_w, box_h = box
                current = corners_from_crop(x0 + 0.1 * box_w, y0 + 0.1 * box_h, 0.8 * box_w, 0.8 * box_h)
            elif corners is None:
                current = full_frame_corners(frame[..., 0])
            else:
                current = np.asarray(corners, dtype=np.float64)
        planes = FramePlanes(frame, current)
        if sync == "off":
            current, score, _ = locate(planes, current, mode="off", polarities=polarities)
        elif count == 0:
            current, score, _ = locate(planes, current, mode="auto" if sync == "auto" else "refine",
                                       polarities=polarities, box=box, **extent)
            anchor, anchor_score = current, score
        else:
            candidates = [locate(planes, current, mode="refine", polarities=polarities),
                          locate(planes, anchor, mode="refine", polarities=polarities)]
            current, score, _ = max(candidates, key=lambda item: item[1])
            # Tracking has slipped if the frame reads much worse than the
            # first one did: search again from the current estimate, with the
            # narrow grid, rather than every N frames regardless.
            if sync == "auto" and resync_every and count % resync_every == 0 and score < 0.5 * anchor_score:
                current, score, _ = locate(planes, current, mode="auto", polarities=polarities,
                                           cells=0.75, degrees=2.0, min_scale=0.90, scale_range=0.06)
                if score > anchor_score:
                    anchor, anchor_score = current, score
        scores.append(score)
        residual, variances, gains = planes.sample(current)

        # Weight the frame by how well it reads on its own. A frame whose grid
        # settled badly contributes anti-signal to a plain average -- on a
        # handheld clip the accumulated presence came out below the best
        # single frame's -- so each frame is decoded alone first and enters
        # the sum in proportion to its own presence score.
        # With polarity alternation on (see SetAlternatePolarity in
        # watermark.h) the pilots, which carry a fixed keyed sign, tell
        # which sign this frame was captured under; frames are aligned
        # before they accumulate.
        if alternating and pilot_score(condition(residual), None) < 0.0:
            residual = -residual
        frame_coded, _, _, frame_occluded = decode_cells(residual, variances, gains)
        frame_presence = presence_scores(residual, frame_occluded, frame_coded, gains)["cross_validated"]
        weight = max(frame_presence, 0.0) + 0.02
        sum_residual += weight * residual
        sum_variances += weight * variances
        sum_gains += weight * gains
        total_weight += weight
        count += 1
        if verbose:
            print(f"  frame {count}: search score {score:.3f}  presence {frame_presence:.3f}", file=sys.stderr)
    if count == 0:
        return None

    residual = sum_residual / total_weight
    variances = sum_variances / total_weight
    gains = sum_gains / total_weight
    coded, correlation, live, occluded = decode_cells(residual, variances, gains)
    payload = coded & U32
    embedded_crc = (coded >> PAYLOAD_BITS) & 0xFF
    scores_present = presence_scores(residual, occluded, coded, gains)
    return {
        "frames": count,
        "corners": current,
        "search_score": float(np.mean(scores)),
        "payload": payload,
        "crc_ok": embedded_crc == crc8(payload),
        "correlation": correlation,
        "live_copies": live,
        "occluded_cells": int(occluded.sum()),
        "presence": scores_present,
        "usable_fraction": float(1.0 - occluded.mean()),
        "present": (scores_present["cross_validated"] >= PRESENCE_THRESHOLD
                    and (1.0 - occluded.mean()) >= MIN_USABLE_FRACTION),
    }


def decode(frame, sync="off", corners=None, alternating=False):
    """Compatibility wrapper for the tests: (payload, per-bit correlation) from one RGB frame.

    Returns the correlation of the 32 payload bits only.
    """
    result = analyse_frames([frame], corners=corners, sync=sync, alternating=alternating)
    return result["payload"], result["correlation"][:PAYLOAD_BITS]


# --------------------------------------------------------------------------
# File loading and command line.
# --------------------------------------------------------------------------

def load_frames(path, max_frames):
    """Yield RGB float frames in 0..1 from an image or a video."""
    lowered = path.lower()
    if lowered.endswith((".mp4", ".mov", ".mkv", ".avi", ".webm")):
        capture = cv2.VideoCapture(path)
        total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        step = max(1, total // max_frames) if total > 0 else 1
        index = 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if index % step == 0:
                yield cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float64) / 255.0
            index += 1
        capture.release()
        return
    if lowered.endswith(".ppm"):
        from read_ppm import read_ppm
        yield read_ppm(path)
        return
    image = cv2.imread(path, cv2.IMREAD_COLOR)
    if image is None:
        raise SystemExit(f"could not read {path}")
    yield cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.float64) / 255.0


def parse_corners(items):
    corners = []
    for item in items:
        x, y = item.split(",")
        corners.append([float(x), float(y)])
    return np.array(corners)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", help="image or video file containing the window")
    parser.add_argument("--crop", nargs=4, type=float, metavar=("X", "Y", "W", "H"),
                        help="axis-aligned window rectangle in the capture, to within a cell")
    parser.add_argument("--box", nargs=4, type=float, metavar=("X", "Y", "W", "H"),
                        help="rectangle the window lies somewhere inside (screenshots of a whole desktop; "
                             "title bar and a margin may be included; the window must be at least 60%% of it)")
    parser.add_argument("--corners", nargs=4, metavar="X,Y",
                        help="window corners TL TR BR BL, for a capture seen in perspective")
    parser.add_argument("--sync", choices=("auto", "refine", "off"), default="auto",
                        help="auto: coarse search then refine (default); refine: local only; off: trust the corners")
    parser.add_argument("--search-cells", type=float, default=2.0, help="shift search range, in cells")
    parser.add_argument("--search-degrees", type=float, default=4.0, help="rotation search range")
    parser.add_argument("--search-scale", type=float, default=0.06, help="scale search range, as a fraction")
    parser.add_argument("--max-frames", type=int, default=60, help="frames to sample from a video")
    parser.add_argument("--expect", type=int, default=None, help="match ID that should be recovered")
    parser.add_argument("--alternating", action="store_true",
                        help="the window had 'Alternate polarity' on: consider both pattern signs")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    corners = None
    if args.crop:
        corners = corners_from_crop(*args.crop)
    if args.corners:
        corners = parse_corners(args.corners)

    result = analyse_frames(load_frames(args.path, args.max_frames), corners=corners, sync=args.sync,
                            box=args.box, verbose=args.verbose, alternating=args.alternating, cells=args.search_cells,
                            degrees=args.search_degrees, scale_range=args.search_scale)
    if result is None:
        print("no frames decoded", file=sys.stderr)
        return 2

    margins = np.abs(result["correlation"])
    scores = result["presence"]
    corner_text = " ".join(f"{x:.0f},{y:.0f}" for x, y in result["corners"])
    print(f"frames used      : {result['frames']}")
    print(f"window corners   : {corner_text}")
    print(f"search score     : {result['search_score']:.3f}   pilots {scores['pilots']:.3f}")
    print(f"presence score   : {scores['cross_validated']:.3f}  "
          f"(threshold {PRESENCE_THRESHOLD:.2f}, full-pattern {scores['full']:.3f})")
    if result["occluded_cells"]:
        print(f"covered cells    : {result['occluded_cells']} of {TOTAL_CELLS}"
              f" ({result['usable_fraction'] * 100:.0f}% usable)")
    print(f"weakest bit      : {margins.min():.4f}   mean {margins.mean():.4f}")

    if not result["present"]:
        print("result           : NO MARK DETECTED")
        return 3
    status = "CRC ok" if result["crc_ok"] else "CRC MISMATCH - ID unreliable"
    print(f"match ID         : {result['payload']}   ({status})")
    if not result["crc_ok"]:
        return 4
    if args.expect is not None and result["payload"] != args.expect:
        wrong = bin(result["payload"] ^ args.expect).count("1")
        print(f"FAIL: expected {args.expect} ({wrong} bits wrong)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
