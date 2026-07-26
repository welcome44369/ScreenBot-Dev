"""Confidence-aware OCR observations used by text trigger runtimes."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import re

from PIL import ImageFilter, ImageOps, ImageStat


OBSERVATION_DEFAULTS = {
    "present_threshold": 0.75,
    "absent_threshold": 0.30,
    "uncertain_text_similarity": 0.65,
    "invalid_readability_threshold": 0.25,
    "text_weight": 0.70,
    "readability_weight": 0.30,
}


@dataclass(frozen=True)
class ReadabilityResult:
    score: float
    contrast: float
    edge_density: float
    near_black: bool
    near_white: bool
    near_uniform: bool
    reason: str = ""

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ObservationResult:
    state: str
    exact_match: bool
    text_similarity: float
    readability_score: float
    visual_similarity: float | None
    presence_score: float
    observation_valid: bool
    reason: str
    recognized_text: str
    target_text: str
    readability: ReadabilityResult | None = None

    def as_dict(self):
        result = asdict(self)
        if self.readability is not None:
            result["readability"] = self.readability.as_dict()
        return result


def _normalize(value):
    return re.sub(r"\s+", "", value or "").casefold()


def _levenshtein_ratio(first, second):
    if first == second:
        return 1.0
    if not first or not second:
        return 0.0
    previous = list(range(len(second) + 1))
    for row, left in enumerate(first, start=1):
        current = [row]
        for column, right in enumerate(second, start=1):
            current.append(min(
                previous[column] + 1,
                current[column - 1] + 1,
                previous[column - 1] + (left != right),
            ))
        previous = current
    return 1.0 - previous[-1] / max(len(first), len(second))


def calculate_text_similarity(target, recognized):
    """Return the strongest similarity of target against OCR lines/text."""
    target = _normalize(target)
    candidates = [_normalize(value) for value in re.split(r"[\r\n]+", recognized or "")]
    candidates.append(_normalize(recognized))
    scores = []
    for value in candidates:
        if not value:
            continue
        if target in value:
            scores.append(1.0)
            continue
        sequence = SequenceMatcher(None, target, value).ratio()
        matched = sum(block.size for block in SequenceMatcher(None, target, value).get_matching_blocks())
        subsequence = matched / max(1, len(target))
        levenshtein = _levenshtein_ratio(target, value)
        overlap = sum((Counter(target) & Counter(value)).values()) / max(len(target), 1)
        prefix = 0
        for left, right in zip(target, value):
            if left != right:
                break
            prefix += 1
        prefix_ratio = prefix / max(len(target), 1)
        scores.append(max(sequence, subsequence, levenshtein, overlap, prefix_ratio))
    return max(scores, default=0.0)


def analyze_region_readability(image):
    if image is None or image.width < 2 or image.height < 2:
        return ReadabilityResult(0.0, 0.0, 0.0, False, False, True, "invalid_crop")
    gray = ImageOps.grayscale(image)
    histogram = gray.histogram()
    pixels = max(1, gray.width * gray.height)
    near_black = sum(histogram[:12]) / pixels >= 0.985
    near_white = sum(histogram[244:]) / pixels >= 0.985
    contrast = min(1.0, ImageStat.Stat(gray).stddev[0] / 64.0)
    edges = gray.filter(ImageFilter.FIND_EDGES)
    edge_density = sum(edges.histogram()[24:]) / pixels
    near_uniform = contrast < 0.06 or edge_density < 0.004
    if near_black or near_white or near_uniform:
        reason = "near_black" if near_black else ("near_white" if near_white else "near_uniform")
        return ReadabilityResult(0.0, contrast, edge_density, near_black, near_white, near_uniform, reason)
    score = max(0.0, min(1.0, contrast * 0.60 + min(1.0, edge_density * 2.5) * 0.40))
    return ReadabilityResult(score, contrast, edge_density, near_black, near_white, near_uniform)


class ObservationEngine:
    def __init__(self, config=None):
        self.config = dict(OBSERVATION_DEFAULTS)
        if config:
            self.config.update({key: value for key, value in config.items() if key in self.config})

    def observe(self, target_text, recognized_text, image=None, *, valid=True, reason=""):
        targets = [target_text] if isinstance(target_text, str) else list(target_text or [])
        targets = [value for value in targets if value]
        target = max(targets, key=lambda value: calculate_text_similarity(value, recognized_text), default="")
        if not valid:
            return ObservationResult("INVALID", False, 0.0, 0.0, None, 0.0, False, reason or "invalid_observation", recognized_text or "", target)
        readability = analyze_region_readability(image) if image is not None else ReadabilityResult(1.0, 1.0, 1.0, False, False, False)
        if readability.score < self.config["invalid_readability_threshold"]:
            return ObservationResult("INVALID", False, 0.0, readability.score, None, 0.0, False, readability.reason or "unreadable_region", recognized_text or "", target, readability)
        normalized = _normalize(recognized_text)
        exact = any(_normalize(value) in normalized for value in targets)
        similarity = 1.0 if exact else calculate_text_similarity(target, recognized_text)
        presence = similarity * self.config["text_weight"] + readability.score * self.config["readability_weight"]
        if exact or (similarity >= 0.90 and presence >= self.config["present_threshold"]):
            state, observation_reason = "PRESENT", "exact_match" if exact else "high_presence"
        # A readable crop does not prove that the target is present, but it
        # deliberately creates a gray zone around low-confidence OCR.  ABSENT
        # is reserved for observations whose combined presence score is low.
        elif (
            similarity >= self.config["uncertain_text_similarity"]
            or presence > self.config["absent_threshold"]
        ):
            state, observation_reason = "UNCERTAIN", "near_match"
        else:
            state, observation_reason = "ABSENT", "low_presence"
        return ObservationResult(state, exact, similarity, readability.score, None, presence, True, observation_reason, recognized_text or "", target, readability)
