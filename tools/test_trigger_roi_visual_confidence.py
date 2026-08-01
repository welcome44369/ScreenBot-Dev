"""Stable target-client ROI and confirmed-present template tests."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.roi_visual_confidence import (
    classify_visual,
    compare_visual_frame,
    prepare_visual_frame,
    resolve_stable_roi,
)
from app.text_detector import TextDetector
from tools.test_disappear_burst_verification import (
    _visual_frame,
    disappear_trigger,
    observation,
)


class TriggerRoiVisualConfidenceTests(unittest.TestCase):
    def test_text_detector_owns_target_client_roi_without_desktop_fallback(self):
        class CaptureService:
            def __init__(self):
                self.calls = []

            def capture_target_client_result(
                self, hwnd, *, allow_desktop_fallback
            ):
                self.calls.append((hwnd, allow_desktop_fallback))
                return SimpleNamespace(
                    image=Image.new("RGB", (800, 600), "gray"),
                    metadata={
                        "capture_id": "frame-1",
                        "captured_monotonic": 1.0,
                    },
                )

        detector = TextDetector.__new__(TextDetector)
        detector.window_tracker = SimpleNamespace(
            target=SimpleNamespace(hwnd=100, root_hwnd=100)
        )
        detector.capture_service = CaptureService()
        image, metadata = detector.capture_region_with_metadata(
            {
                "x_ratio": 0.1,
                "y_ratio": 0.2,
                "width_ratio": 0.3,
                "height_ratio": 0.4,
            }
        )
        self.assertEqual(detector.capture_service.calls, [(100, False)])
        self.assertEqual(image.size, (240, 240))
        self.assertEqual(metadata["client_size"], (800, 600))
        self.assertTrue(metadata["roi_valid"])

    def test_normalized_roi_recomputes_after_proportional_resize(self):
        region = {
            "x_ratio": 0.1,
            "y_ratio": 0.2,
            "width_ratio": 0.3,
            "height_ratio": 0.4,
        }
        original = resolve_stable_roi(region, (800, 600))
        resized = resolve_stable_roi(region, (1600, 1200))
        self.assertTrue(original.valid)
        self.assertTrue(resized.valid)
        self.assertEqual(original.revision, resized.revision)
        self.assertEqual(original.pixel_rect, (80, 120, 320, 360))
        self.assertEqual(resized.pixel_rect, (160, 240, 640, 720))

    def test_out_of_bounds_or_too_small_roi_is_invalid(self):
        out_of_bounds = resolve_stable_roi(
            {
                "x_ratio": 0.9,
                "y_ratio": 0.0,
                "width_ratio": 0.2,
                "height_ratio": 1.0,
            },
            (800, 600),
        )
        too_small = resolve_stable_roi(
            {
                "x_ratio": 0.0,
                "y_ratio": 0.0,
                "width_ratio": 0.001,
                "height_ratio": 0.001,
            },
            (800, 600),
        )
        self.assertFalse(out_of_bounds.valid)
        self.assertFalse(too_small.valid)

    def test_opencv_comparison_is_normalized_and_classified(self):
        present = _visual_frame("present")
        absent = _visual_frame("absent")
        self.assertEqual(
            classify_visual(compare_visual_frame(present, present)),
            "VISUAL_PRESENT_STRONG",
        )
        self.assertEqual(
            classify_visual(compare_visual_frame(present, absent)),
            "VISUAL_ABSENT_EVIDENCE",
        )

    def test_preprocessing_produces_fixed_grayscale_frame(self):
        frame = prepare_visual_frame(
            Image.new("RGB", (320, 120), (80, 120, 160))
        )
        self.assertEqual(frame.shape, (64, 128))
        self.assertEqual(str(frame.dtype), "uint8")

    def test_confirmed_present_creates_run_local_template(self):
        trigger = disappear_trigger()
        result = trigger.update(observation("present", 0.0, 0), now=0.0)
        events = [item["event"] for item in result.condition_events]
        status = trigger.get_status_snapshot()
        self.assertIn("PRESENT_TEMPLATE_CREATED", events)
        self.assertTrue(status["present_template_available"])
        self.assertEqual(
            status["present_template_metadata"]["generation"], 1
        )
        self.assertEqual(
            status["present_template_metadata"]["roi_revision"], "roi-a"
        )

    def test_present_likely_never_updates_fixed_template(self):
        trigger = disappear_trigger()
        trigger.update(observation("present", 0.0, 0), now=0.0)
        before = trigger.get_status_snapshot()["present_template_metadata"]
        result = trigger.update(
            observation("likely", 0.2, 1, similarity=0.75), now=0.2
        )
        after = trigger.get_status_snapshot()["present_template_metadata"]
        self.assertNotIn(
            "PRESENT_TEMPLATE_CREATED",
            [item["event"] for item in result.condition_events],
        )
        self.assertEqual(before["capture_id"], after["capture_id"])

    def test_generation_change_invalidates_old_template(self):
        trigger = disappear_trigger()
        trigger.update(observation("present", 0.0, 0), now=0.0)
        result = trigger.update(
            observation("present", 0.2, 1, generation=2), now=0.2
        )
        self.assertIn(
            "PRESENT_TEMPLATE_INVALIDATED",
            [item["event"] for item in result.condition_events],
        )
        self.assertFalse(
            trigger.get_status_snapshot()["present_template_available"]
        )

    def test_proportional_client_resize_keeps_normalized_template(self):
        trigger = disappear_trigger()
        trigger.update(observation("present", 0.0, 0), now=0.0)
        resized = replace(
            observation("empty", 0.2, 1),
            client_size=(1600, 1200),
            roi_pixel_rect=(160, 240, 640, 720),
        )
        result = trigger.update(resized, now=0.2)
        self.assertEqual(
            result.observation.fused_classification, "PRESENT_STRONG"
        )
        self.assertTrue(
            trigger.get_status_snapshot()["present_template_available"]
        )

    def test_roi_revision_change_invalidates_template(self):
        trigger = disappear_trigger()
        trigger.update(observation("present", 0.0, 0), now=0.0)
        changed = replace(
            observation("empty", 0.2, 1),
            roi_revision="roi-b",
            normalized_roi=(0.2, 0.2, 0.3, 0.4),
        )
        result = trigger.update(changed, now=0.2)
        invalidated = [
            item
            for item in result.condition_events
            if item["event"] == "PRESENT_TEMPLATE_INVALIDATED"
        ]
        self.assertTrue(invalidated)
        self.assertFalse(
            trigger.get_status_snapshot()["present_template_available"]
        )

    def test_material_client_aspect_change_invalidates_template(self):
        trigger = disappear_trigger()
        trigger.update(observation("present", 0.0, 0), now=0.0)
        changed = replace(
            observation("empty", 0.2, 1),
            client_size=(1000, 600),
        )
        result = trigger.update(changed, now=0.2)
        reasons = [
            item["data"].get("reason")
            for item in result.condition_events
            if item["event"] == "PRESENT_TEMPLATE_INVALIDATED"
        ]
        self.assertIn("client_aspect_changed", reasons)

    def test_invalid_roi_is_unknown_and_cannot_start_burst(self):
        trigger = disappear_trigger()
        trigger.update(observation("present", 0.0, 0), now=0.0)
        invalid = replace(
            observation("absent", 0.2, 1),
            roi_valid=False,
            roi_invalid_reason="roi_out_of_bounds",
        )
        result = trigger.update(invalid, now=0.2)
        self.assertEqual(result.observation.fused_classification, "UNKNOWN")
        self.assertFalse(trigger.is_disappear_burst_active())


if __name__ == "__main__":
    unittest.main()
