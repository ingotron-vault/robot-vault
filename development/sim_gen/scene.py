"""Synthetic camera frames for the simulated AMR.

A moving checkerboard + vertical gradient + a landmark blob that pans with the
robot's heading, plus per-frame noise so consecutive frames are never
byte-identical -- which is what makes the deliberately-bad "frozen frames"
degradation detectable at all.

Deterministic: the pixel content is a pure function of the pose and the
`numpy.random.Generator` passed in, and JPEG encoding is done by a pinned
Pillow (see requirements-dev.txt), so the same seed yields the same bytes.
"""

from __future__ import annotations

import io
from functools import lru_cache
from typing import Optional, Tuple

import numpy as np
from PIL import Image, ImageFilter

DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480

#: How far the scene pans per radian of heading / per metre travelled.
PIXELS_PER_RADIAN = 300.0
PIXELS_PER_METRE = 120.0
CHECKER_PERIOD_PX = 48.0


@lru_cache(maxsize=8)
def _grids(width: int, height: int) -> Tuple[np.ndarray, np.ndarray]:
    yy, xx = np.mgrid[0:height, 0:width]
    return xx.astype(np.float32), yy.astype(np.float32)


def render_rgb(
    width: int,
    height: int,
    theta_rad: float,
    distance_m: float,
    rng: np.random.Generator,
) -> np.ndarray:
    """Render one RGB frame as an (H, W, 3) uint8 array."""
    xx, yy = _grids(width, height)
    grad = yy / max(height - 1, 1)

    img = np.empty((height, width, 3), dtype=np.float32)
    img[..., 0] = 38.0 + 118.0 * grad
    img[..., 1] = 62.0 + 96.0 * (1.0 - grad)
    img[..., 2] = 88.0 + 58.0 * grad

    pan = theta_rad * PIXELS_PER_RADIAN
    creep = distance_m * PIXELS_PER_METRE
    board = (
        np.floor((xx + pan) / CHECKER_PERIOD_PX)
        + np.floor((yy + creep) / CHECKER_PERIOD_PX)
    ) % 2.0
    img += (board * 68.0 - 34.0)[..., None]

    # Landmark: a bright blob that pans out of frame as the robot turns.
    span = width + 160.0
    cx = ((width * 0.5 - pan) % span) - 80.0
    cy = height * 0.62
    radius = max(6.0, min(width, height) * 0.09)
    blob = np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2.0 * radius * radius)))
    img[..., 0] += 150.0 * blob
    img[..., 1] += 120.0 * blob

    img += rng.integers(-7, 8, size=(height, width, 3)).astype(np.float32)
    return np.clip(img, 0.0, 255.0).astype(np.uint8)


def encode_jpeg(
    rgb: np.ndarray, quality: int, blur_radius: Optional[float] = None
) -> bytes:
    """JPEG-encode an RGB array, optionally Gaussian-blurring it first."""
    # No `mode=` argument: it is deprecated in Pillow 11 and an (H, W, 3)
    # uint8 array already infers RGB.
    image = Image.fromarray(rgb)
    if blur_radius:
        image = image.filter(ImageFilter.GaussianBlur(radius=float(blur_radius)))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=int(quality), optimize=False)
    return buf.getvalue()
