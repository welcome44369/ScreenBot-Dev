"""Stable target-client ROI identity and lightweight OpenCV comparison."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math

import cv2
import numpy as np


VISUAL_PRESENT_STRONG_THRESHOLD = 0.85
VISUAL_PRESENT_LIKELY_THRESHOLD = 0.60
VISUAL_ABSENT_THRESHOLD = 0.30
VISUAL_FRAME_SIZE = (128, 64)
MIN_ROI_PIXELS = 4


@dataclass(frozen=True)
class StableRoi:
    normalized: tuple[float, float, float, float]
    pixel_rect: tuple[int, int, int, int]
    client_size: tuple[int, int]
    revision: str
    valid: bool
    invalid_reason: str | None = None


def normalize_roi(region):
    keys = ("x_ratio", "y_ratio", "width_ratio", "height_ratio")
    try:
        values = tuple(float(region[key]) for key in keys)
    except (KeyError, TypeError, ValueError):
        return None
    x, y, width, height = values
    if not all(math.isfinite(value) for value in values):
        return None
    if (
        x < 0.0
        or y < 0.0
        or width <= 0.0
        or height <= 0.0
        or x + width > 1.0
        or y + height > 1.0
    ):
        return None
    return values


def roi_revision(normalized):
    encoded = json.dumps(
        [round(value, 8) for value in normalized],
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()[:16]


def resolve_stable_roi(region, client_size):
    normalized = normalize_roi(region)
    width, height = (int(client_size[0]), int(client_size[1]))
    if normalized is None or width <= 1 or height <= 1:
        return StableRoi(
            normalized or (0.0, 0.0, 0.0, 0.0),
            (0, 0, 0, 0),
            (width, height),
            "invalid",
            False,
            "invalid_normalized_roi",
        )
    x, y, roi_width, roi_height = normalized
    left = int(round(x * width))
    top = int(round(y * height))
    right = int(round((x + roi_width) * width))
    bottom = int(round((y + roi_height) * height))
    valid = (
        0 <= left < right <= width
        and 0 <= top < bottom <= height
        and right - left >= MIN_ROI_PIXELS
        and bottom - top >= MIN_ROI_PIXELS
    )
    return StableRoi(
        normalized,
        (left, top, right, bottom),
        (width, height),
        roi_revision(normalized),
        valid,
        None if valid else "roi_out_of_bounds_or_too_small",
    )


def prepare_visual_frame(image):
    """Return a fixed-size, contrast-normalized grayscale ROI."""
    rgb = np.asarray(image.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    resized = cv2.resize(
        gray, VISUAL_FRAME_SIZE, interpolation=cv2.INTER_AREA
    )
    return cv2.equalizeHist(resized)


def compare_visual_frame(template, current):
    """Return a normalized structural similarity in the inclusive 0..1 range."""
    if (
        template is None
        or current is None
        or template.shape != current.shape
        or template.size == 0
    ):
        return None
    correlation = float(
        cv2.matchTemplate(
            current, template, cv2.TM_CCOEFF_NORMED
        )[0, 0]
    )
    if not math.isfinite(correlation):
        correlation = 0.0
    correlation = max(0.0, min(1.0, correlation))
    difference_score = 1.0 - float(
        np.mean(cv2.absdiff(current, template))
    ) / 255.0
    template_edges = cv2.Canny(template, 50, 150)
    current_edges = cv2.Canny(current, 50, 150)
    edge_score = 1.0 - float(
        np.mean(cv2.absdiff(current_edges, template_edges))
    ) / 255.0
    return max(
        0.0,
        min(
            1.0,
            correlation * 0.65
            + difference_score * 0.30
            + edge_score * 0.05,
        ),
    )


def classify_visual(score):
    if score is None:
        return "VISUAL_UNAVAILABLE"
    if score >= VISUAL_PRESENT_STRONG_THRESHOLD:
        return "VISUAL_PRESENT_STRONG"
    if score >= VISUAL_PRESENT_LIKELY_THRESHOLD:
        return "VISUAL_PRESENT_LIKELY"
    if score <= VISUAL_ABSENT_THRESHOLD:
        return "VISUAL_ABSENT_EVIDENCE"
    return "VISUAL_UNKNOWN"
